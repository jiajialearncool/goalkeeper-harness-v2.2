"""Fresh synthetic protocol events and streams; no model, login or old replay."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from goalkeeper.codex import CodexClient
from goalkeeper.models import Task
from goalkeeper.native import Native
from goalkeeper.transport import MeteredTransport


def notification(method, thread="root", **params):
    return {"method": method, "params": {"threadId": thread, **params}}


def goal(status, thread="root"):
    return notification("thread/goal/updated", thread, goal={"status": status})


def turn(status="completed", thread="root", identifier="turn-1", started=False):
    return notification("turn/started" if started else "turn/completed", thread,
                        turn={"id": identifier, "status": status})


class FakeCodex:
    def __init__(self, *args):
        self.events = asyncio.Queue()
        self.thread_id = "root"
        self.process = SimpleNamespace(returncode=None)
        self.provider_settings = {}
        self.closed = False
        self.observed_tokens = 0

    def use_subscription_transport(self, *args):
        pass

    async def start(self):
        pass

    async def create_thread(self, *args):
        pass

    async def turn(self, *args):
        return {"turn": {"id": "turn-1"}}

    async def set_goal(self, *args):
        pass

    async def close(self):
        self.closed = True


def native(tmp_path, monkeypatch):
    fake = FakeCodex()
    monkeypatch.setattr("goalkeeper.native.CodexClient", lambda *args, **kwargs: fake)
    directory = tmp_path / "run"
    workspace = directory / "workspace"
    workspace.mkdir(parents=True)
    task = Task(id="offline", objective="synthetic task", constraints=[], acceptance=["synthetic"],
                write_paths=["result.txt"], checks=[{"id": "ready", "argv": ["/bin/true"]}])
    adapter = Native("codex", directory, task,
                     workspace, "http://127.0.0.1:1234/v1", "synthetic", seconds=1)
    return adapter, fake


async def test_goal_complete_waits_for_response_usage_and_turn_end(tmp_path, monkeypatch):
    adapter, fake = native(tmp_path, monkeypatch)
    task = asyncio.create_task(adapter.codex())
    await fake.events.put(goal("complete"))
    await asyncio.sleep(.01)
    assert not fake.closed and not task.done()
    fake.observed_tokens = 17  # The final response arrives after its Goal tool notification.
    await fake.events.put(notification("thread/tokenUsage/updated"))
    await fake.events.put(turn())
    assert await asyncio.wait_for(task, .2)
    assert fake.closed and fake.observed_tokens == 17


async def test_turn_end_before_goal_complete_and_continuation(tmp_path, monkeypatch):
    adapter, fake = native(tmp_path, monkeypatch)
    for event in [turn(), turn(started=True, identifier="turn-2"), goal("complete")]:
        await fake.events.put(event)
    task = asyncio.create_task(adapter.codex())
    await asyncio.sleep(.01)
    assert not task.done() and not fake.closed
    await fake.events.put(turn(identifier="turn-2"))
    assert await asyncio.wait_for(task, .2)


async def test_child_events_cannot_finish_root_or_fail_its_turn(tmp_path, monkeypatch):
    adapter, fake = native(tmp_path, monkeypatch)
    for event in [goal("complete", "child"), turn(started=True, thread="child"),
                  turn(status="failed", thread="child"), turn()]:
        await fake.events.put(event)
    task = asyncio.create_task(adapter.codex())
    await asyncio.sleep(.01)
    assert not task.done()
    await fake.events.put(goal("complete"))
    assert await asyncio.wait_for(task, .2)


@pytest.mark.parametrize("event,reason", [
    (goal("blocked"), "goal_blocked"), (goal("paused"), "goal_paused"),
    (goal("budgetLimited"), "goal_budgetLimited"), (goal("usageLimited"), "goal_usageLimited"),
    ({"method": "app_server/exited", "params": {}}, "app_server_exited"),
    (turn(status="failed"), "turn_failed"), (turn(status="interrupted"), "turn_failed"),
])
async def test_terminal_failures_stop_without_waiting_for_deadline(tmp_path, monkeypatch, event, reason):
    adapter, fake = native(tmp_path, monkeypatch)
    await fake.events.put(goal("complete"))
    await fake.events.put(event)
    with pytest.raises(RuntimeError, match=reason):
        await asyncio.wait_for(adapter.codex(), .2)
    assert fake.closed


async def test_app_server_eof_wakes_event_consumer_and_pending_rpc():
    client = CodexClient()
    stdout = asyncio.StreamReader()
    stdout.feed_eof()
    client.process = SimpleNamespace(stdout=stdout)
    future = asyncio.get_running_loop().create_future()
    client.pending[1] = future
    await client._read()
    assert (await client.events.get())["method"] == "app_server/exited"
    with pytest.raises(RuntimeError, match="app-server"):
        await future


class Auth:
    async def access_token(self):
        return "synthetic"

    async def close(self):
        pass


@pytest.mark.parametrize("ending", ["\n\n", "\n", ""])
async def test_forwarder_accounts_final_sse_at_eof(tmp_path, ending):
    terminal = {"type": "response.completed", "response": {"model": "gpt-6.1-sol",
                "usage": {"input_tokens": 10, "output_tokens": 7}}}
    raw = "data: " + json.dumps(terminal) + ending
    meter = MeteredTransport(tmp_path / "private", tmp_path / "usage.json", auth=Auth(),
              http=httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=raw))))
    meter.port = 1234
    route = meter.register(SimpleNamespace(id="root", config=SimpleNamespace(engine="codex")))
    async def payload():
        return {"model": "gpt-6.1-sol", "reasoning": {"effort": "xhigh"}, "input": []}
    response = await meter.response("root", SimpleNamespace(
        headers={"authorization": "Bearer " + route["LOCAL_ROUTE_TOKEN"]}, json=payload))
    _ = [chunk async for chunk in response.body_iterator]
    await meter.wait_settled("root", .1)
    saved = json.loads(meter.report.read_text())
    assert saved["known_tokens"] == 17 and saved["unknown_calls"] == 0 and saved["in_flight"] == 0
    assert meter.records[0]["returned_model"] == "gpt-6.1-sol"
    await meter.close()


async def test_accounting_settle_waits_and_times_out_without_zeroing_unknown(tmp_path):
    meter = MeteredTransport(tmp_path / "private", tmp_path / "usage.json", auth=Auth(),
                            http=httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200))))
    meter.records.append({"run_id": "root", "status": "in_flight", "usage_known": False,
                          "estimated_request_tokens": 19})
    with pytest.raises(TimeoutError):
        await meter.wait_settled("root", .01)
    assert meter.reserved("root") == 19
    task = asyncio.create_task(meter.wait_settled("root", .2))
    await asyncio.sleep(.01)
    assert not task.done()
    meter.records[0].update(status="completed", usage_known=True, tokens=17)
    meter.persist()
    await task
    assert meter.known("root") == 17 and meter.reserved("root") == 0
    await meter.close()


@pytest.mark.parametrize("terminal,reason", [
    ({"type": "response.completed", "response": {"model": "gpt-6.1-sol"}}, "response_usage_missing"),
    ({"type": "response.output_text.delta", "delta": "unfinished"}, "shared_service"),
])
async def test_missing_terminal_or_usage_keeps_reservation_and_stops(tmp_path, terminal, reason):
    raw = "data: " + json.dumps(terminal) + "\n\n"
    meter = MeteredTransport(tmp_path / "private", tmp_path / "usage.json", auth=Auth(),
              http=httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=raw))))
    meter.port = 1234
    route = meter.register(SimpleNamespace(id="root", config=SimpleNamespace(engine="codex")))
    async def payload():
        return {"model": "gpt-6.1-sol", "reasoning": {"effort": "xhigh"}, "input": []}
    response = await meter.response("root", SimpleNamespace(
        headers={"authorization": "Bearer " + route["LOCAL_ROUTE_TOKEN"]}, json=payload))
    _ = [chunk async for chunk in response.body_iterator]
    assert meter.reason == reason and not meter.admit("root")
    assert not meter.records[0]["usage_known"] and meter.reserved("root") > 0
    await meter.close()


async def test_native_cleanup_diagnostic_failure_cannot_erase_delivery(tmp_path, monkeypatch):
    adapter, _ = native(tmp_path, monkeypatch)
    async def finish():
        return True
    adapter.codex = finish
    stopped = []
    class Runner:
        async def stop(self):
            stopped.append(True)
    adapter.runner = Runner()
    adapter.stderr = asyncio.get_running_loop().create_future()
    adapter.stderr.set_exception(RuntimeError('synthetic diagnostic failure'))
    assert await adapter.run()
    saved = json.loads((adapter.directory / 'native-result.json').read_text())
    assert stopped and saved['normal_end'] and saved['stop_reason'] is None
    assert not saved['cleanup_complete']
    assert saved['cleanup_errors'] == [{'step': 'diagnostic_read', 'error': 'RuntimeError'}]


async def test_native_eof_has_explicit_protocol_reason_and_saves_result(tmp_path, monkeypatch):
    adapter, _ = native(tmp_path, monkeypatch)
    async def eof():
        return False
    adapter.codex = eof
    async def stop():
        pass
    adapter.runner = SimpleNamespace(stop=stop)
    assert not await adapter.run()
    saved = json.loads((adapter.directory / 'native-result.json').read_text())
    assert saved['stop_reason'] == 'native_protocol_incomplete' and saved['cleanup_complete']


async def test_cancelled_native_diagnostic_still_saves_original_stop_reason(tmp_path, monkeypatch):
    adapter, _ = native(tmp_path, monkeypatch)
    async def failed():
        raise RuntimeError('synthetic original failure')
    adapter.codex = failed
    adapter.stderr = asyncio.get_running_loop().create_future()
    adapter.stderr.cancel()
    stopped = []
    async def stop():
        stopped.append(True)
    adapter.runner = SimpleNamespace(stop=stop)
    assert not await adapter.run()
    result = json.loads((adapter.directory/'native-result.json').read_text())
    assert stopped and result['stop_reason'] == 'RuntimeError:synthetic original failure'
    assert result['cleanup_errors'] == [{'step': 'diagnostic_read', 'error': 'CancelledError'}]
