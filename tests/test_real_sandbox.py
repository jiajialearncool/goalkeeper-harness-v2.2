"""Real local processes under Seatbelt, but no inference, login or old trajectory replay."""

from pathlib import Path

from goalkeeper.sandbox import Scope, Seatbelt, runtime_roots
from test_loop import call, fixture


async def test_sandbox_blocks_other_directory_and_network_and_preserves_full_output(tmp_path):
    root = tmp_path / "task"
    root.mkdir()
    private = tmp_path / "private"
    private.mkdir()
    (private / "secret.txt").write_text("synthetic-private")
    runner = Seatbelt(
        Scope(root, write_paths=["result.txt"], runtimes=runtime_roots()), archive=private / "output"
    )
    python = str(__import__("sys").executable)
    allowed = await runner.run(
        [python, "-c", "from pathlib import Path;Path('result.txt').write_text('ok');print('x'*150000)"], 10
    )
    assert allowed["exit_code"] == 0 and allowed["stdout_bytes"] > 150000
    assert len(Path(allowed["stdout_file"]).read_bytes()) == allowed["stdout_bytes"]
    denied = await runner.run(
        [python, "-c", f"from pathlib import Path;print(Path({str(private / 'secret.txt')!r}).read_text())"],
        10,
    )
    assert denied["exit_code"] != 0 and "synthetic-private" not in denied["stdout"]
    denied = await runner.run(
        [python, "-c", "from pathlib import Path;Path('outside.txt').write_text('no')"], 10
    )
    assert denied["exit_code"] != 0 and not (root / "outside.txt").exists()
    network = await runner.run([python, "-c", "import socket;socket.socket().connect(('1.1.1.1',443))"], 3)
    assert network["exit_code"] != 0


async def test_real_sandbox_timeout_cleans_owned_child_group(tmp_path):
    root = tmp_path / "task"
    root.mkdir()
    runner = Seatbelt(Scope(root, runtimes=runtime_roots()), archive=tmp_path / "output")
    result = await runner.run(
        [
            str(__import__("sys").executable),
            "-c",
            "import subprocess,time,sys;subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);time.sleep(30)",
        ],
        0.15,
    )
    assert result["timed_out"] and not runner.processes


async def test_entire_loop_under_real_sandbox_with_mock_model(tmp_path, tokens):
    engine, _ = fixture(
        tmp_path,
        tokens,
        [
            [call("write", {"path": "result.txt", "content": "READY"}, 1)],
            [call("request_delivery", {"summary": "done", "check_ids": [], "unresolved": []}, 2)],
        ],
    )
    check = engine.state.task.checks[0]
    check.argv = [
        str(__import__("sys").executable),
        "-c",
        "from pathlib import Path;assert Path('result.txt').read_text()=='READY'",
    ]
    check.timeout = 5
    runner = Seatbelt(engine.tools.scope, archive=engine.store.archives)
    runner.on_spawn = engine.tools.spawned
    runner.on_exit = engine.tools.exited
    engine.tools.runner = runner
    assert (await engine.run()).phase == "delivered"
    assert engine.store.load().delivery["snapshot"]["result.txt"]
    assert not (engine.store.directory / "processes.json").exists()


async def test_actual_c1_feedback_delivery_roundtrips_under_real_sandbox(tmp_path, tokens):
    """Developer reference fixtures only; never a model benchmark or old replay."""
    import json
    from goalkeeper.cli import create
    from goalkeeper.engine import Engine
    from goalkeeper.models import RunConfig, Task
    from goalkeeper.paths import PROJECT
    from goalkeeper.provider import ScriptedProvider
    from goalkeeper.evaluation import score
    from goalkeeper.storage import snapshot
    from uuid import uuid4

    package = PROJECT / "evaluation/private/C1"
    task = Task.model_validate_json((package / "task.json").read_text())
    # Historical fixtures are immutable; interpreter paths belong to this checkout.
    task = task.model_copy(deep=True)
    for check in [*task.checks, task.feedback.probe, *task.feedback.checks]:
        check.argv[0] = str(PROJECT / '.venv/bin/python')
    original = task.model_dump()
    state, store = create(task, RunConfig(), package / "initial", run_id="offline-c1-" + uuid4().hex)
    writes = [call("write", {"path": "tickets/" + p.name, "content": p.read_text()}, i)
              for i, p in enumerate(sorted((package / "reference/tickets").glob("*.py")), start=1)]
    provider = ScriptedProvider([writes, [call("request_delivery", {"summary": "offline fixture", "check_ids": [], "unresolved": []}, 99)]])
    engine = Engine(state, store, provider, tokens)
    await engine.run()
    saved = store.load()
    assert saved.phase == "delivered" and saved.feedback_delivered and saved.goal_version == 2
    assert saved.task.model_dump() == original
    assert saved.delivery["checks"] == ["public_initial", "public_change"]
    assert all(c["passed"] for c in saved.evidence if c["id"] in saved.delivery["checks"])
    assert snapshot(store.directory / "delivery/snapshot") == saved.delivery["snapshot"]
    result = await score(store.directory / "delivery/snapshot", package / "grade.py", store.directory / "offline-score")
    assert result["passed"]
    report = json.loads((store.directory / "offline-score/independent-score.json").read_text())
    assert len(report["checks"]) == 8
    assert not (store.directory / "processes.json").exists()


async def test_c1_staged_retry_delivers_without_executing_partial_tools(tokens):
    """Current developer reference + fresh broken wire; no real model or old answer."""
    import json
    import httpx
    from types import SimpleNamespace
    from uuid import uuid4
    from goalkeeper.cli import create
    from goalkeeper.engine import Engine
    from goalkeeper.evaluation import score
    from goalkeeper.models import RunConfig, Task
    from goalkeeper.paths import PROJECT
    from goalkeeper.provider import ResponsesProvider
    from goalkeeper.transport import MeteredTransport
    from test_transport_reliability import Auth, event

    package = PROJECT / 'evaluation/private/C1'
    task = Task.model_validate_json((package / 'task.json').read_text())
    # Keep the sealed historical fixture intact; run its checks with this checkout's interpreter.
    task = task.model_copy(deep=True)
    for check in [*task.checks, task.feedback.probe, *task.feedback.checks]:
        check.argv[0] = str(PROJECT / '.venv/bin/python')
    state, store = create(task, RunConfig(), package / 'initial', run_id='offline-staged-c1-'+uuid4().hex)
    writes = [call('write', {'path': 'tickets/'+p.name, 'content': p.read_text()}, index)
              for index, p in enumerate(sorted((package / 'reference/tickets').glob('*.py')), start=1)]
    upstream_calls = []
    class Broken(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield event({'type': 'response.output_item.done', 'output_index': 0,
                         'item': call('write', {'path': 'tickets/partial.py', 'content': 'must not execute'}, 100)})
            raise httpx.RemoteProtocolError('synthetic mid-response close')
    def handle(request):
        upstream_calls.append(json.loads(request.content))
        if len(upstream_calls) == 1:
            return httpx.Response(200, stream=Broken())
        output = writes if len(upstream_calls) == 2 else [call('request_delivery',
                    {'summary': 'offline delivery', 'check_ids': [], 'unresolved': []}, 101)]
        return httpx.Response(200, content=event({'type': 'response.completed', 'response': {
            'model': 'gpt-6.1-sol', 'output': output, 'usage': {'input_tokens': 100, 'output_tokens': 25}}}))
    meter = MeteredTransport(store.directory / 'synthetic-auth', store.directory / 'synthetic-transport.json',
                            auth=Auth(), http=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
                            transient_retries=1, total_limit=4_000_000)
    meter.port = 1234
    route = meter.register(SimpleNamespace(id=state.id, deadline=state.deadline,
                                          config=SimpleNamespace(engine='goalkeeper')))
    local = httpx.AsyncClient(transport=httpx.ASGITransport(app=meter.app))
    class RouteAuth:
        async def access_token(self):
            return route['LOCAL_ROUTE_TOKEN']
    class LocalClient:
        def stream(self, method, url, **kwargs):
            return local.stream(method, route['DIAGNOSTIC_TRANSPORT_BASE']+'/responses', **kwargs)
        async def aclose(self):
            await local.aclose()
    provider = ResponsesProvider(RouteAuth(), LocalClient())
    engine = Engine(state, store, provider, tokens)
    try:
        await engine.run()
        saved = store.load()
        assert saved.phase == 'delivered' and saved.feedback_delivered and saved.goal_version == 2
        assert not (Path(saved.workspace)/'tickets/partial.py').exists()
        assert len(saved.operations) == len(writes)+1  # Exactly one execution per complete tool.
        assert len(upstream_calls) == 3 and upstream_calls[0] == upstream_calls[1]
        assert meter.records[0]['status'] == 'interrupted_usage_unknown'
        assert meter.records[0]['retry_scheduled'] and meter.reserved() > 0
        assert meter.known() == 250 and not meter.stop.is_set()
        verdict = await score(store.directory/'delivery/snapshot', package/'grade.py', store.directory/'offline-score')
        assert verdict['passed'] and len(verdict['checks']) == 8
    finally:
        await provider.close()
        await meter.close()
