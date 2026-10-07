"""Offline regressions for batching, input calibration and stable requirement updates."""
import copy
import json

import httpx
import pytest

from goalkeeper.context import build, units
from goalkeeper.models import ModelEvent, ModelRequest, RunConfig
from goalkeeper.provider import ResponsesProvider
from goalkeeper.sandbox import PermissionDenied
from goalkeeper.storage import snapshot
from goalkeeper.tools import definitions
from test_feedback_checkpoint import engine_fixture
from test_loop import call, fixture


async def test_edit_many_all_or_nothing_and_preserves_file_mode(tmp_path, tokens):
    engine, _ = fixture(tmp_path, tokens, [])
    file = engine.root / "result.txt"
    file.write_text("one two three")
    file.chmod(0o600)
    before = snapshot(engine.root)
    inode = file.stat().st_ino
    with pytest.raises(ValueError, match="第2处"):
        await engine.tools.execute("edit_many", {"path": "result.txt", "changes": [
            {"old": "one", "new": "ONE"}, {"old": "missing", "new": "TWO"}]}, 1)
    assert snapshot(engine.root) == before and file.stat().st_ino == inode
    result = await engine.tools.execute("edit_many", {"path": "result.txt", "changes": [
        {"old": "one", "new": "ONE"}, {"old": "two", "new": "TWO"}]}, 1)
    assert file.read_text() == "ONE TWO three" and result["changes_applied"] == 2
    assert file.stat().st_mode & 0o777 == 0o600
    assert not [p for p in engine.root.glob("*.tmp") if p.is_file()]


@pytest.mark.parametrize("changes", [[], ["wrong"], [{"old": "one"}],
    [{"old": "one", "new": 1}], [{"old": "", "new": "a"}],
    [{"old": "one", "new": "a", "other": True}]])
async def test_edit_many_rejects_malformed_changes_without_writing(tmp_path, tokens, changes):
    engine, _ = fixture(tmp_path, tokens, [])
    file = engine.root / "result.txt"
    file.write_text("one one")
    before = snapshot(engine.root)
    with pytest.raises(ValueError):
        await engine.tools.execute("edit_many", {"path": "result.txt", "changes": changes}, 1)
    assert snapshot(engine.root) == before


async def test_edit_many_respects_protection_and_scope(tmp_path, tokens):
    engine, _ = fixture(tmp_path, tokens, [])
    changes = [{"old": "x", "new": "y"}]
    engine.tools.scope.protected_files.append(engine.root / "result.txt")
    for path in ["result.txt", "../private/state.json", "other.txt"]:
        with pytest.raises((ValueError, PermissionDenied)):
            await engine.tools.execute("edit_many", {"path": path, "changes": changes}, 1)


async def test_eight_known_edits_batch_to_one_round_with_same_verified_delivery(tmp_path, tokens):
    changes = [{"old": "bad" + str(i) + " ", "new": ""} for i in range(7)] + [
        {"old": "bad7", "new": "READY"}]
    runs = []
    for name, batched in [("single", False), ("batch", True)]:
        directory = tmp_path / name
        directory.mkdir()
        responses = [[call("edit_many", {"path": "result.txt", "changes": changes}, 1)]] if batched else [
            [call("edit", {"path": "result.txt", **change}, i + 1)] for i, change in enumerate(changes)]
        responses.append([call("request_delivery", {"summary": "done", "check_ids": [], "unresolved": []}, 10)])
        engine, commands = fixture(directory, tokens, responses)
        (engine.root / "result.txt").write_text(" ".join("bad" + str(i) for i in range(8)))
        state = await engine.run()
        assert state.phase == "delivered" and len(commands) == 1
        units(state.history)
        runs.append((len(state.calls), state.delivery["snapshot"]))
    assert runs[0][0] == 9 and runs[1][0] == 2 and runs[0][1] == runs[1][1]


def opaque_request():
    return ModelRequest(instructions="stable rules", config=RunConfig(), input=[
        {"type": "reasoning", "id": "rs_test", "encrypted_content": "Opaque9+-" * 6000, "summary": []},
        {"role": "user", "content": "visible evidence " * 800}])


def observation(request, tokens, actual, **extra):
    return {"role": "main", "status": "completed", "model": request.config.model,
            "effort": request.config.effort, "input_features": tokens.context(request),
            "usage": {"input_tokens": actual, "output_tokens": 20}, **extra}


def test_context_calibration_retains_opaque_bytes_and_wire_budget_forecast(tokens):
    request = opaque_request()
    original = copy.deepcopy(request.model_dump())
    initial = tokens.context(request)
    observed = observation(request, tokens, initial["visible_tokens"] + 100)
    calibrated = tokens.context(request, [observed])
    assert calibrated["estimated_tokens"] >= observed["usage"]["input_tokens"]
    assert calibrated["estimated_tokens"] < initial["estimated_tokens"] / 2
    assert calibrated["opaque_samples"] == 1 and calibrated["opaque_factor"] >= .1
    assert request.model_dump() == original
    estimate, reserve = tokens.forecast(request, [observed])
    assert estimate == tokens.count(original) and reserve > estimate
    assert not calibrated["is_actual_usage"] and not calibrated["hard_billing_bound"]


@pytest.mark.parametrize("extra", [{"status": "failed"}, {"role": "summary"},
    {"model": "other"}, {"effort": "low"}, {"usage": {}}, {"input_features": {}},
    {"input_features": {"policy": "context-v1"}}, {"input_features": "invalid"}])
def test_context_calibration_ignores_ineligible_observations(tokens, extra):
    request = opaque_request()
    obs = observation(request, tokens, 1, **extra)
    info = tokens.context(request, [obs])
    assert info["samples"] == 0 and info["opaque_factor"] == 1


def test_context_calibration_raises_estimate_when_observed_cost_grows(tokens):
    request = opaque_request()
    info = tokens.context(request)
    expensive = info["visible_tokens"] + info["opaque_proxy_tokens"] // 2
    estimate = tokens.context(request, [observation(request, tokens, expensive)])
    assert estimate["opaque_factor"] >= .49 and estimate["estimated_tokens"] > expensive


async def test_edit_many_commit_failure_keeps_original_and_removes_temporary(tmp_path, tokens, monkeypatch):
    from pathlib import Path
    engine, _ = fixture(tmp_path, tokens, [])
    file = engine.root / "result.txt"
    file.write_text("one two")
    def fail(self, target):
        raise OSError("synthetic commit failure")
    monkeypatch.setattr(Path, "replace", fail)
    with pytest.raises(OSError, match="commit failure"):
        await engine.tools.execute("edit_many", {"path": "result.txt", "changes": [
            {"old": "one", "new": "ONE"}, {"old": "two", "new": "TWO"}]}, 1)
    assert file.read_text() == "one two" and not list(engine.root.glob("result.txt.*.tmp"))


async def test_calibrated_opaque_history_does_not_trigger_premature_summary(tmp_path, tokens):
    config = RunConfig(compact_trigger=15000, context_ceiling=60000,
                       keep_recent_tokens=1000, summary_max_tokens=200, summary_input_tokens=40000)
    engine, _ = fixture(tmp_path, tokens, [], config=config)
    engine.state.history = opaque_request().input
    request = build(engine.state, tokens, definitions())
    initial = tokens.context(request)
    engine.state.calls = [observation(request, tokens, initial["visible_tokens"] + 100)]
    before = copy.deepcopy(engine.state.history)
    assert tokens.context(request, engine.state.calls)["estimated_tokens"] < config.compact_trigger
    assert tokens.count(request.model_dump()) > config.compact_trigger
    await engine.compact()
    assert engine.state.history == before and not engine.provider.requests


async def test_feedback_appends_context_without_rewriting_prefix_and_survives_reload(tmp_path, tokens):
    engine, _ = engine_fixture(tmp_path, tokens, [])
    first = build(engine.state, tokens, definitions())
    assert "EXTRA_REQUIREMENT" not in json.dumps(first.model_dump())
    (engine.root / "result.txt").write_text("READY")
    assert await engine.feedback()
    changed = build(engine.state, tokens, definitions())
    assert changed.input[:len(first.input)] == first.input
    update = json.loads(changed.input[-1]["content"])["context_update"]
    assert update["goal_version"] == 2 and update["feedback_delivered"]
    assert [c["id"] for c in update["task"]["checks"]] == ["initial", "extra"]
    restored = engine.store.load()
    assert build(restored, tokens, definitions()) == changed
    assert restored.task == engine.state.task and restored.summary is None


async def test_command_preview_preserves_status_failure_tail_and_archive(tmp_path, tokens):
    engine, _ = fixture(tmp_path, tokens, [])
    raw = "start\n" + "passing output " * 20000 + "\nFAILED crucial_test: specific error"
    async def run(argv, timeout):
        stdout, stderr = tmp_path / "verbose", tmp_path / "empty"
        stdout.write_text(raw)
        stderr.write_text("")
        return {"exit_code": 1, "timed_out": False, "stdout_file": str(stdout), "stderr_file": str(stderr)}
    engine.tools.runner.run = run
    result = engine.tools.output(await engine.tools.execute("bash", {"argv": ["test"]}, 1))
    assert result["exit_code"] == 1 and not result["timed_out"] and result["stdout_truncated"]
    assert "start" in result["stdout"] and "FAILED crucial_test: specific error" in result["stdout"]
    assert engine.store.read(result["stdout_reference"]) == raw
    assert tokens.count(json.dumps(result, ensure_ascii=False)) <= engine.state.config.tool_result_tokens


async def test_provider_emits_stable_role_cache_key_and_batch_flag_without_inference():
    seen = []
    async def handler(request):
        seen.append(json.loads(request.content))
        terminal = {"type": "response.completed", "response": {"model": "gpt-6.1-sol", "status": "completed",
                    "usage": {"input_tokens": 2, "output_tokens": 1}, "output": []}}
        return httpx.Response(200, text="data: " + json.dumps(terminal) + "\n\n")
    class Auth:
        async def access_token(self):
            return "synthetic"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        provider = ResponsesProvider(Auth(), http)
        request = ModelRequest(config=RunConfig(), instructions="same", input=[],
                               tools=definitions(), cache_key="offline:main")
        async for event in provider.stream(request):
            assert isinstance(event, ModelEvent)
    assert seen[0]["parallel_tool_calls"] is True and seen[0]["prompt_cache_key"] == "offline:main"
    assert seen[0]["reasoning"]["effort"] == "xhigh" and not seen[0]["store"]
    assert seen[0]["include"] == ["reasoning.encrypted_content"]
