import asyncio
import json
import time

import pytest

from goalkeeper.context import build, split_for_compaction, units
from goalkeeper.cost import totals
from goalkeeper.engine import Engine
from goalkeeper.models import Check, ModelEvent, RunConfig, State, Task
from goalkeeper.provider import ProviderError, ScriptedProvider
from goalkeeper.storage import Store, snapshot
from goalkeeper.tools import definitions


def call(name, arguments, number):
    return {"type": "function_call", "name": name, "arguments": json.dumps(arguments), "call_id": str(number)}


def fixture(tmp_path, tokens, responses, task=None, config=None):
    root = tmp_path / "workspace"
    root.mkdir()
    task = task or Task(id="synthetic", objective="写入READY并交付", constraints=[], acceptance=["精确READY"],
                        write_paths=["result.txt"], checks=[Check(id="marker", argv=["marker"], timeout=1)])
    config = config or RunConfig()
    state = State(id="offline", task=task, config=config, workspace=str(root),
                  started_at=time.time(), deadline=time.time()+config.seconds_budget)
    store = Store(tmp_path / "private")
    store.save(state, "fixture")
    engine = Engine(state, store, ScriptedProvider(responses), tokens)
    commands = []

    async def command(argv, timeout):
        commands.append(argv)
        ok = (root / "result.txt").exists() and (root / "result.txt").read_text() == "READY"
        out = tmp_path / "stdout"
        err = tmp_path / "stderr"
        out.write_text("ok" if ok else "wrong")
        err.write_text("")
        return {"exit_code": 0 if ok else 1, "timed_out": False,
                "stdout": out.read_text(), "stderr": "", "stdout_file": str(out), "stderr_file": str(err),
                "stdout_bytes": len(out.read_text()), "stderr_bytes": 0}

    engine.tools.runner.run = command
    return engine, commands


async def test_complete_delivery_without_plan_or_reviewer(tmp_path, tokens):
    engine, commands = fixture(tmp_path, tokens, [
        [call("write", {"path": "result.txt", "content": "READY"}, 1)],
        [call("request_delivery", {"summary": "写入READY", "check_ids": [], "unresolved": []}, 2)]])
    state = await engine.run()
    assert state.phase == "delivered" and len(state.calls) == 2 and len(commands) == 1
    assert {c["role"] for c in state.calls} == {"main"}
    assert snapshot(engine.store.directory / "delivery/snapshot") == state.delivery["snapshot"]
    assert engine.store.load().phase == "delivered"
    units(state.history)
    assert not any(d["name"] in {"plan", "delegate", "review"} for d in definitions())


async def test_failed_check_is_repaired_not_claimed_success(tmp_path, tokens):
    engine, commands = fixture(tmp_path, tokens, [
        [call("write", {"path": "result.txt", "content": "WRONG"}, 1)],
        [call("request_delivery", {"summary": "done", "check_ids": ["invented"], "unresolved": []}, 2)],
        [call("edit", {"path": "result.txt", "old": "WRONG", "new": "READY"}, 3)],
        [call("request_delivery", {"summary": "已修复", "check_ids": [], "unresolved": []}, 4)]])
    state = await engine.run()
    assert state.phase == "delivered" and len(commands) == 2
    assert [c["passed"] for c in state.evidence] == [False, True]


async def test_exact_check_is_reused_until_project_changes(tmp_path, tokens):
    engine, commands = fixture(tmp_path, tokens, [])
    (engine.root / "result.txt").write_text("READY")
    await engine.check(engine.state.task.checks[0])
    await engine.check(engine.state.task.checks[0])
    assert len(commands) == 1
    (engine.root / "another.txt").write_text("new")
    assert engine.current(engine.state.task.checks[0]) is None
    await engine.check(engine.state.task.checks[0])
    assert len(commands) == 2


async def test_model_run_check_can_be_reused_on_delivery(tmp_path, tokens):
    engine, commands = fixture(tmp_path, tokens, [
        [call("write", {"path": "result.txt", "content": "READY"}, 1)],
        [call("bash", {"argv": ["marker"], "timeout": 1}, 2)],
        [call("request_delivery", {"summary": "done", "check_ids": ["marker"], "unresolved": []}, 3)]])
    assert (await engine.run()).phase == "delivered"
    assert len(commands) == 1


async def test_delivery_with_unresolved_problem_refused(tmp_path, tokens):
    engine, commands = fixture(tmp_path, tokens, [])
    result = await engine.delivery({"summary": "done", "check_ids": [], "unresolved": ["compatibility"]})
    assert not result["passed"] and not engine.state.delivery and not commands


async def test_plain_text_never_becomes_delivery(tmp_path, tokens):
    engine, _ = fixture(tmp_path, tokens, [[{"type": "message", "role": "assistant", "content": []}]]*2)
    state = await engine.run()
    assert state.first_stop_reason == "protocol_incomplete" and not state.delivery


async def test_denied_read_does_not_read_another_workspace(tmp_path, tokens):
    engine, _ = fixture(tmp_path, tokens, [])
    with pytest.raises(Exception):
        await engine.tools.execute("read", {"path": "../private/state.json"}, 1)
    (engine.root / "link").symlink_to(tmp_path / "private")
    with pytest.raises(Exception):
        await engine.tools.execute("read", {"path": "link/state.json"}, 1)


async def test_no_replay_of_uncertain_write(tmp_path, tokens):
    engine, _ = fixture(tmp_path, tokens, [])
    tool = call("write", {"path": "result.txt", "content": "overwrite"}, 1)
    (engine.root / "result.txt").write_text("existing")
    engine.state.history.append(tool)
    engine.state.pending_tools.append(tool)
    engine.state.operations.append({"call_id": "1", "name": "write", "status": "executing"})
    await engine.pending()
    assert (engine.root / "result.txt").read_text() == "existing"
    assert engine.state.operations[0]["status"] == "uncertain"
    units(engine.state.history)


async def test_missing_usage_keeps_reserve_and_no_lower_cost_claim(tmp_path, tokens):
    engine, _ = fixture(tmp_path, tokens, [])
    class Missing:
        async def stream(self, request):
            yield ModelEvent(type="response.completed", data={"response": {"model": request.config.model, "output": []}})
    engine.provider = Missing()
    await engine.call(build(engine.state, tokens, definitions()))
    cost = totals(engine.state.calls)
    assert cost["known_tokens"] == 0 and cost["unknown_calls"] == 1 and cost["unknown_reserve"] > 0
    assert not cost["usage_complete"]


async def test_token_admission_prevents_any_request(tmp_path, tokens):
    engine, _ = fixture(tmp_path, tokens, [], config=RunConfig(token_budget=10))
    assert (await engine.run()).first_stop_reason == "token_budget"
    assert not engine.provider.requests


async def test_expired_deadline_preserved_on_resume(tmp_path, tokens):
    engine, _ = fixture(tmp_path, tokens, [])
    engine.state.phase = "resource_interrupted"
    engine.state.deadline = time.time()-1
    engine.state.calls = [{"status": "failed", "forecast": 100, "usage": {"input_tokens": 10, "output_tokens": 2}}]
    assert (await engine.run(resume=True)).first_stop_reason == "time_budget"
    assert engine.state.recovery_used == 1 and totals(engine.state.calls)["known_tokens"] == 12
    assert not engine.provider.requests


async def test_cancel_is_not_service_error_and_first_reason_survives(tmp_path, tokens):
    engine, _ = fixture(tmp_path, tokens, [])
    engine.stop("token_budget")
    engine.stop("cleanup_cancelled")
    assert engine.state.first_stop_reason == "token_budget"
    class Cancel:
        async def stream(self, request):
            raise asyncio.CancelledError
            yield
    engine.provider = Cancel()
    with pytest.raises(asyncio.CancelledError):
        await engine.call(build(engine.state, tokens, definitions()))
    assert engine.state.calls[-1]["status"] == "cancelled"


async def test_service_failure_records_unknown_and_one_recovery(tmp_path, tokens):
    engine, _ = fixture(tmp_path, tokens, [])
    class Fail:
        async def stream(self, request):
            raise ProviderError("transport_error", resource=True)
            yield
    engine.provider = Fail()
    state = await engine.run()
    assert state.phase == "resource_interrupted" and totals(state.calls)["unknown_calls"] == 1
    await engine.run(resume=True)
    assert state.recovery_used == 1 and totals(state.calls)["unknown_calls"] == 2


def test_tool_pairs_cannot_be_cut_or_have_orphan_results(tokens):
    history = [call("read", {"path": "x"}, 1), call("read", {"path": "y"}, 2),
               {"type": "function_call_output", "call_id": "1", "output": "a"},
               {"type": "function_call_output", "call_id": "2", "output": "b"}]
    assert len(units(history)) == 1
    with pytest.raises(ValueError):
        units(history[:-1])
    with pytest.raises(ValueError):
        units(history[2:])
    chunks, kept = split_for_compaction(history*1, tokens, 1, 1000)
    assert not chunks and kept == history


def test_active_worker_rejects_second_writer(tmp_path):
    store = Store(tmp_path)
    with store.lease():
        with pytest.raises(RuntimeError, match="run_already_active"):
            with store.lease():
                pass


async def test_request_keeps_required_check_time(tmp_path, tokens):
    engine, _ = fixture(tmp_path, tokens, [])
    engine.state.deadline = time.time() + engine.check_reserve() - .1
    assert (await engine.run()).first_stop_reason == "time_budget"
    assert not engine.provider.requests


async def test_environment_change_invalidates_check(tmp_path, tokens, monkeypatch):
    engine, commands = fixture(tmp_path, tokens, [])
    (engine.root / "result.txt").write_text("READY")
    await engine.check(engine.state.task.checks[0])
    monkeypatch.setenv("LANG", "synthetic-changed-locale")
    assert engine.current(engine.state.task.checks[0]) is None
    assert len(commands) == 1


def test_future_feedback_is_not_in_initial_model_input(tmp_path, tokens):
    from goalkeeper.models import Feedback
    engine, _ = fixture(tmp_path, tokens, [])
    engine.state.task.feedback = Feedback(probe=Check(id="base", argv=["base"], timeout=1),
                                         text="FUTURE_REQUIREMENT", checks=[], acceptance=[])
    assert "FUTURE_REQUIREMENT" not in json.dumps(build(engine.state, tokens, definitions()).input)
