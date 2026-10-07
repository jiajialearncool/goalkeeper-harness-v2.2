"""Fresh nonempty feedback tasks: activation, persistence, resume and cleanup."""
import asyncio
import json

import pytest

from goalkeeper.engine import Engine
from goalkeeper.models import Check, Feedback, Task
from goalkeeper.provider import ScriptedProvider
from test_loop import call, fixture


def task():
    return Task(id="fresh-feedback", objective="Write the initial marker", constraints=[],
                acceptance=["initial marker"], write_paths=["result.txt", "extra.txt"],
                checks=[Check(id="initial", argv=["initial_check"], timeout=1)],
                feedback=Feedback(probe=Check(id="probe", argv=["base_probe"], timeout=1),
                    text="EXTRA_REQUIREMENT: write extra.txt", acceptance=["extra marker"],
                    checks=[Check(id="extra", argv=["extra_check"], timeout=1)]))


def engine_fixture(tmp_path, tokens, responses):
    engine, commands = fixture(tmp_path, tokens, responses, task=task())
    original = engine.tools.runner.run
    async def run(argv, timeout):
        value = await original(argv, timeout)
        if argv == ["extra_check"]:
            ready = (engine.root / "extra.txt").exists() and (engine.root / "extra.txt").read_text() == "EXTRA"
            value.update(exit_code=0 if ready else 1, stdout="extra passed" if ready else "extra missing")
        return value
    engine.tools.runner.run = run
    return engine, commands


async def test_nonempty_feedback_roundtrips_every_checkpoint_and_requires_extra_delivery(tmp_path, tokens, monkeypatch):
    engine, _ = engine_fixture(tmp_path, tokens, [
        [call("write", {"path": "result.txt", "content": "READY"}, 1)],
        [call("request_delivery", {"summary": "premature", "check_ids": [], "unresolved": []}, 2)],
        [call("write", {"path": "extra.txt", "content": "EXTRA"}, 3)],
        [call("request_delivery", {"summary": "complete", "check_ids": [], "unresolved": []}, 4)],
    ])
    original_task = engine.state.task.model_dump()
    save = engine.store.save
    roundtrips = []
    def checked_save(state, kind, data=None):
        save(state, kind, data)
        loaded = engine.store.load()
        assert loaded.task.model_dump() == original_task
        roundtrips.append(kind)
    monkeypatch.setattr(engine.store, "save", checked_save)
    state = await engine.run()
    assert state.phase == "delivered" and state.goal_version == 2 and state.feedback_delivered
    assert len(state.calls) == 4
    assert "feedback_delivered" in roundtrips and "run_closed" in roundtrips
    assert state.delivery["checks"] == ["initial", "extra"]
    assert any(e["id"] == "extra" and not e["passed"] for e in state.evidence)
    first = json.loads(engine.provider.requests[0].input[0]["content"])
    next_request = engine.provider.requests[1]
    changed = next(json.loads(item["content"])["context_update"] for item in next_request.input
                   if item.get("role") == "user" and '"context_update"' in item.get("content", ""))
    assert next_request.input[0] == engine.provider.requests[0].input[0]
    assert "EXTRA_REQUIREMENT" not in json.dumps(first)
    assert "EXTRA_REQUIREMENT" in changed["task"]["objective"]
    assert [c["id"] for c in changed["task"]["checks"]] == ["initial", "extra"]
    with engine.store.lease():
        pass


async def test_feedback_reload_resume_preserves_deadline_budget_and_single_activation(tmp_path, tokens):
    engine, commands = engine_fixture(tmp_path, tokens, [])
    (engine.root / "result.txt").write_text("READY")
    engine.state.phase = "running"
    engine.save("ready_to_activate")
    assert await engine.feedback()
    loaded = engine.store.load()
    deadline, budget, version = loaded.deadline, loaded.config.token_budget, loaded.goal_version
    original_task = loaded.task.model_dump()
    provider = ScriptedProvider([
        [call("write", {"path": "extra.txt", "content": "EXTRA"}, 10)],
        [call("request_delivery", {"summary": "done", "check_ids": [], "unresolved": []}, 11)],
    ])
    resumed = Engine(loaded, engine.store, provider, tokens)
    resumed.tools.runner.run = engine.tools.runner.run
    count = len(commands)
    assert not await resumed.feedback() and len(commands) == count
    state = await resumed.run(resume=True)
    assert state.phase == "delivered" and state.recovery_used == 1
    assert state.deadline == deadline and state.config.token_budget == budget and state.goal_version == version
    assert state.task.model_dump() == original_task
    assert sum(e["kind"] == "feedback_delivered" for e in state.events) == 1


@pytest.mark.parametrize("cancel", [False, True])
async def test_failure_after_feedback_saves_valid_state_and_releases_lock(tmp_path, tokens, cancel):
    engine, _ = engine_fixture(tmp_path, tokens, [])
    (engine.root / "result.txt").write_text("READY")
    class Failure:
        async def stream(self, request):
            raise asyncio.CancelledError if cancel else RuntimeError("fresh synthetic failure")
            yield
    engine.provider = Failure()
    if cancel:
        with pytest.raises(asyncio.CancelledError):
            await engine.run()
    else:
        await engine.run()
    loaded = engine.store.load()
    assert loaded.phase == ("cancelled" if cancel else "program_error")
    assert loaded.feedback_delivered and loaded.goal_version == 2
    assert loaded.events[-1]["kind"] == "run_closed"
    with engine.store.lease():
        pass


def test_invalid_feedback_mutation_cannot_replace_checkpoint(tmp_path, tokens):
    engine, _ = engine_fixture(tmp_path, tokens, [])
    before = engine.store.path.read_bytes()
    sequence, events = engine.state.sequence, list(engine.state.events)
    engine.state.task.checks.extend(engine.state.task.feedback.checks)
    with pytest.raises(ValueError, match="检查ID必须唯一"):
        engine.store.save(engine.state, "invalid_activation")
    assert engine.store.path.read_bytes() == before
    assert engine.state.sequence == sequence and engine.state.events == events
    engine.store.load()


def test_failed_checkpoint_write_does_not_advance_memory(tmp_path, tokens, monkeypatch):
    engine, _ = engine_fixture(tmp_path, tokens, [])
    before = engine.store.path.read_bytes()
    sequence, events = engine.state.sequence, list(engine.state.events)
    def failure(*args):
        raise OSError("fresh synthetic write failure")
    monkeypatch.setattr("goalkeeper.storage.save_report", failure)
    with pytest.raises(OSError):
        engine.store.save(engine.state, "write_failure")
    assert engine.state.sequence == sequence and engine.state.events == events
    assert engine.store.path.read_bytes() == before


async def test_checkpoint_failure_during_exit_always_releases_lock(tmp_path, tokens, monkeypatch):
    engine, _ = engine_fixture(tmp_path, tokens, [])
    before = engine.store.path.read_bytes()
    def failure(*args):
        raise OSError("fresh synthetic checkpoint failure")
    monkeypatch.setattr(engine.store, "save", failure)
    with pytest.raises(OSError):
        await engine.run()
    assert engine.store.path.read_bytes() == before
    assert (engine.store.directory / "run-error.json").is_file()
    with engine.store.lease():
        pass


def test_probe_id_collision_is_rejected():
    definition = task().model_dump()
    definition["feedback"]["probe"]["id"] = "initial"
    with pytest.raises(ValueError, match="检查ID必须唯一"):
        Task.model_validate(definition)


async def test_cached_check_also_requires_exact_argv(tmp_path, tokens):
    engine, _ = engine_fixture(tmp_path, tokens, [])
    (engine.root / "result.txt").write_text("READY")
    check = engine.state.task.checks[0]
    await engine.check(check)
    assert engine.current(check)
    engine.state.evidence[-1]["argv"] = ["different_command"]
    assert engine.current(check) is None
