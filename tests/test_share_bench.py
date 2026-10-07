"""New sequential-turn support and local task scoring, with no real inference."""

import asyncio
import json
import shutil
import subprocess
import time
from types import SimpleNamespace

import httpx
import pytest

from goalkeeper.codex import CODEX, CodexClient, task_permissions
from goalkeeper.context import build
from goalkeeper.evaluation import score
from goalkeeper.models import Task
from goalkeeper.native import Native, objective
from goalkeeper.native_log import native_events
from goalkeeper.paths import PROJECT
from goalkeeper.sandbox import runtime_roots
from goalkeeper.storage import snapshot
from goalkeeper.transport import MeteredTransport
from test_loop import call, fixture
from test_pinned_offline import SlowResponse
from test_transport_reliability import Auth


def task(turns=None):
    return Task(
        id="sequential",
        objective="ROUND_ONE",
        constraints=[],
        acceptance=["current turn"],
        write_paths=["./"],
        checks=[{"id": "syntax", "argv": ["/bin/true"], "timeout": 1}],
        user_turns=turns or ["SECRET_TWO", "SECRET_THREE", "SECRET_FOUR"],
    )


async def test_sequential_turns_do_not_leak_or_reset_history_deadline(tmp_path, tokens):
    responses = [[call("write", {"path": "result.txt", "content": "READY"}, 1)]]
    responses += [
        [call("request_delivery", {"summary": f"round {i}", "check_ids": [], "unresolved": []}, i + 1)]
        for i in range(1, 5)
    ]
    engine, _ = fixture(tmp_path, tokens, responses, task=task())
    initial = build(engine.state, tokens, []).model_dump()
    assert "SECRET_TWO" not in json.dumps(initial)
    deadline = engine.state.deadline
    state = await engine.run()
    assert state.phase == "delivered" and state.turn_index == 3 and state.goal_version == 4
    assert state.deadline == deadline and len(state.calls) == 5
    assert engine.store.load().turn_index == 3
    requests = engine.provider.requests
    assert all(req.input[0] == requests[0].input[0] for req in requests)
    assert "SECRET_TWO" not in json.dumps(requests[1].model_dump())
    assert "SECRET_TWO" in json.dumps(requests[2].model_dump())
    assert "SECRET_THREE" not in json.dumps(requests[2].model_dump())
    assert sum(e["kind"] == "user_turn_delivered" for e in state.events) == 3
    assert state.delivery["goal_version"] == 4


async def test_pi_sequential_turns_preserve_session_and_reject_old_cycle(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    adapter = Native("pi", tmp_path, task(), root, "http://127.0.0.1:1234/v1", "synthetic")
    reader = asyncio.StreamReader()
    stderr = asyncio.StreamReader()
    stderr.feed_eof()
    sent = []

    class Input:
        def write(self, raw):
            command = json.loads(raw)
            sent.append(command)
            messages = [{"role": "user", "content": c["message"]} for c in sent]
            reader.feed_data(
                (json.dumps({"type": "response", "id": command.get("id"), "success": True}) + "\n").encode()
            )
            reader.feed_data((json.dumps({"type": "agent_end", "messages": messages}) + "\n").encode())

        async def drain(self):
            pass

    async def spawn(*args):
        adapter.process = SimpleNamespace(stdout=reader, stderr=stderr, stdin=Input(), returncode=0)

    adapter.spawn = spawn
    monkeypatch.setattr("goalkeeper.native.configuration", lambda *args: ([], {}))
    assert await adapter.pi()
    assert adapter.turn_index == 3 and len(sent) == 4
    assert [c["message"] for c in sent[1:]] == task().user_turns
    await adapter.log.close()
    await adapter.stderr


async def test_pinned_codex_consumes_four_sequential_turns_without_inference(tmp_path, monkeypatch):
    directory = tmp_path / "run"
    root = directory / "workspace"
    root.mkdir(parents=True)
    sent = []

    def upstream(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, stream=SlowResponse())

    meter = MeteredTransport(
        tmp_path / "auth",
        tmp_path / "usage.json",
        auth=Auth(),
        http=httpx.AsyncClient(transport=httpx.MockTransport(upstream)),
    )
    await meter.start()
    route = meter.register(
        SimpleNamespace(id="codex", deadline=time.time() + 30, config=SimpleNamespace(engine="codex"))
    )

    class Consumer(CodexClient):
        def use_subscription_transport(self, *args):
            super().use_subscription_transport(*args)
            self.transport_overrides.update(
                HTTP_PROXY="", HTTPS_PROXY="", ALL_PROXY="", NO_PROXY="127.0.0.1,localhost"
            )

        async def set_goal(self, *args):
            await self.events.put(
                {
                    "method": "thread/goal/updated",
                    "params": {"threadId": self.thread_id, "goal": {"status": "complete"}},
                }
            )

    monkeypatch.setattr("goalkeeper.native.CodexClient", Consumer)
    adapter = Native(
        "codex",
        directory,
        task(),
        root,
        route["DIAGNOSTIC_TRANSPORT_BASE"],
        route["LOCAL_ROUTE_TOKEN"],
        seconds=30,
        tokens=None,
    )
    try:
        assert await adapter.run(), (directory / "native-result.json").read_text()
        assert len(sent) == 4 and adapter.turn_index == 3
        assert "SECRET_TWO" not in json.dumps(sent[0])
        assert "SECRET_TWO" in json.dumps(sent[1]) and "SECRET_THREE" not in json.dumps(sent[1])
        assert json.loads((directory / "native-result.json").read_text())["turns_delivered"] == 4
    finally:
        await meter.close()


def test_workspace_root_scope_allows_atomic_edits_and_denies_other_trees(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "protected.txt").write_text("original")
    profile, env = task_permissions(root, runtime_roots(), ["./"], ["protected.txt"])
    home = tmp_path / "codex-home"
    home.mkdir()
    env["CODEX_HOME"] = str(home)
    code = """from pathlib import Path
import os
Path('scratch.tmp').write_text('solution')
os.replace('scratch.tmp','answer.py')
for filename in ['protected.txt','../outside.txt']:
    try: Path(filename).write_text('bad')
    except PermissionError: pass
    else: raise AssertionError(filename)
print('OK')
"""
    result = subprocess.run(
        [
            str(CODEX),
            "-c",
            "permissions.goalkeeper-evaluation=" + profile,
            "-c",
            'approval_policy="never"',
            "sandbox",
            "-P",
            "goalkeeper-evaluation",
            "-C",
            str(root),
            "--",
            str(PROJECT / ".venv/bin/python"),
            "-B",
            "-c",
            code,
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (root / "answer.py").read_text() == "solution" and (
        root / "protected.txt"
    ).read_text() == "original"


@pytest.mark.parametrize("index", range(1, 11))
async def test_share_bench_initial_package_and_grader_runs_readonly(tmp_path, index):
    package = PROJECT / "evaluation/private" / f"SB10-{index:02}"
    root = tmp_path / "workspace"
    shutil.copytree(package / "initial", root)
    initial = snapshot(root)
    verdict = await score(root, package / "grade.py", tmp_path / "grading")
    assert "invalid_grader_output" not in str(verdict)
    assert verdict["snapshot_unchanged"] and snapshot(root) == initial
    assert verdict["passed"] is False
    definition = Task.model_validate_json((package / "task.json").read_text())
    assert len(definition.user_turns) == (3 if index <= 4 else 2 if index == 8 else 0)
    assert all(objective(definition, i) for i in range(len(definition.user_turns) + 1))


async def test_remove_occ_grader_accepts_reference_rejects_wrong_all(tmp_path):
    package = PROJECT / "evaluation/private/SB10-08"
    root = tmp_path / "workspace"
    shutil.copytree(package / "initial", root)
    source = """def remove_occ(string, char, all=False):
    if not isinstance(char,str) or len(char)!=1:
        raise ValueError('Second parameter must be a single character.')
    if all: return string.replace(char,'')
    positions=[i for i,c in enumerate(string) if c==char]
    remove=set(positions[:1]+positions[-1:])
    return ''.join(c for i,c in enumerate(string) if i not in remove)
"""
    (root / "remove_occ.py").write_text(source)
    assert (await score(root, package / "grade.py", tmp_path / "reference"))["passed"]
    (root / "remove_occ.py").write_text(
        source.replace("if all: return string.replace(char,'')", "if all: return string")
    )
    assert not (await score(root, package / "grade.py", tmp_path / "mutation"))["passed"]


@pytest.mark.parametrize("group", ["pi", "opencode"])
async def test_actual_native_products_consume_four_sequential_local_turns(tmp_path, group):
    from test_transport_reliability import event

    directory = tmp_path / "run"
    root = directory / "workspace"
    root.mkdir(parents=True)
    sent = []

    def upstream(request):
        sent.append(json.loads(request.content))
        number = len(sent)
        item = {
            "id": f"msg_{number}",
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": "Local fixture complete.", "annotations": []}],
        }
        base = {
            "id": f"resp_{number}",
            "object": "response",
            "created_at": 1,
            "model": "gpt-6.1-sol",
            "status": "in_progress",
            "output": [],
        }
        frames = [
            {"type": "response.created", "response": base},
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": dict(item, status="in_progress", content=[]),
            },
            {
                "type": "response.content_part.added",
                "item_id": item["id"],
                "output_index": 0,
                "content_index": 0,
                "part": {"type": "output_text", "text": "", "annotations": []},
            },
            {
                "type": "response.output_text.delta",
                "item_id": item["id"],
                "output_index": 0,
                "content_index": 0,
                "delta": "Local fixture complete.",
            },
            {
                "type": "response.output_text.done",
                "item_id": item["id"],
                "output_index": 0,
                "content_index": 0,
                "text": "Local fixture complete.",
            },
            {
                "type": "response.content_part.done",
                "item_id": item["id"],
                "output_index": 0,
                "content_index": 0,
                "part": item["content"][0],
            },
            {"type": "response.output_item.done", "output_index": 0, "item": item},
            {
                "type": "response.completed",
                "response": dict(
                    base,
                    status="completed",
                    output=[item],
                    usage={
                        "input_tokens": 10,
                        "output_tokens": 7,
                        "total_tokens": 17,
                        "input_tokens_details": {"cached_tokens": 0},
                        "output_tokens_details": {"reasoning_tokens": 0},
                    },
                ),
            },
        ]
        raw = b"".join(event(dict(frame, sequence_number=i)) for i, frame in enumerate(frames))
        return httpx.Response(200, content=raw, headers={"content-type": "text/event-stream"})

    meter = MeteredTransport(
        tmp_path / "auth",
        tmp_path / "usage.json",
        auth=Auth(),
        http=httpx.AsyncClient(transport=httpx.MockTransport(upstream)),
    )
    await meter.start()
    route = meter.register(
        SimpleNamespace(id=group, deadline=time.time() + 30, config=SimpleNamespace(engine=group))
    )
    adapter = Native(
        group,
        directory,
        task(),
        root,
        route["DIAGNOSTIC_TRANSPORT_BASE"],
        route["LOCAL_ROUTE_TOKEN"],
        seconds=30,
        tokens=None,
    )
    try:
        assert await adapter.run(), (directory / "native-result.json").read_text()
        assert len(sent) == 4 and adapter.turn_index == 3
        assert "SECRET_TWO" not in json.dumps(sent[0])
        assert "SECRET_TWO" in json.dumps(sent[1]) and "SECRET_THREE" not in json.dumps(sent[1])
        assert sum(e.get("type") == "user_turn_delivered" for e in native_events(directory)) == 3
    finally:
        await meter.close()
