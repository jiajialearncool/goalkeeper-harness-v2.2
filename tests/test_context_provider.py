import json

import httpx
import pytest

from goalkeeper.context import build, units
from goalkeeper.models import ModelEvent, ModelRequest, RunConfig
from goalkeeper.provider import ProviderError, ResponsesProvider
from goalkeeper.storage import canonical
from test_loop import call, fixture


def summary():
    return {
        "goal": "keep task",
        "constraints": [],
        "completed": [],
        "decisions": ["keep interface"],
        "unresolved": ["pending implementation"],
        "next_steps": ["write code"],
        "source_refs": [],
    }


async def test_compaction_saves_sources_and_preserves_tail(tmp_path, tokens):
    config = RunConfig(
        compact_trigger=3000,
        context_ceiling=10000,
        keep_recent_tokens=1000,
        summary_max_tokens=200,
        summary_input_tokens=9000,
    )
    engine, _ = fixture(tmp_path, tokens, [], config=config)
    engine.state.history = [
        {"role": "user", "content": "old context " * 1500},
        {"role": "assistant", "content": "decision " * 300},
        {"role": "user", "content": "current work " * 400},
    ]

    class Summaries:
        requests = []

        async def stream(self, request):
            self.requests.append(request)
            yield ModelEvent(
                type="response.completed",
                data={
                    "response": {
                        "model": request.config.model,
                        "usage": {"input_tokens": 100, "output_tokens": 50},
                        "output": [
                            {
                                "type": "message",
                                "content": [{"type": "output_text", "text": canonical(summary())}],
                            }
                        ],
                    }
                },
            )

    engine.provider = Summaries()
    old = engine.state.history.copy()
    await engine.compact()
    assert engine.state.summary and engine.state.summary["source_refs"]
    assert len(engine.state.history) < len(old)
    for source in engine.state.summary["source_refs"]:
        assert engine.store.read(source)
    request = build(engine.state, tokens, [])
    assert engine.state.task.objective in request.input[0]["content"]
    units(engine.state.history)


async def test_invalid_summary_does_not_delete_original_and_only_one_correction(tmp_path, tokens):
    config = RunConfig(
        compact_trigger=3000,
        context_ceiling=10000,
        keep_recent_tokens=1000,
        summary_max_tokens=200,
        summary_input_tokens=9000,
    )
    engine, _ = fixture(tmp_path, tokens, [], config=config)
    engine.state.history = [
        {"role": "user", "content": "old " * 2000},
        {"role": "user", "content": "new " * 1000},
    ]
    old = engine.state.history.copy()

    class Bad:
        count = 0

        async def stream(self, request):
            self.count += 1
            yield ModelEvent(
                type="response.completed",
                data={
                    "response": {
                        "model": request.config.model,
                        "usage": {"input_tokens": 10, "output_tokens": 10},
                        "output": [{"type": "message", "content": [{"type": "output_text", "text": "{}"}]}],
                    }
                },
            )

    engine.provider = Bad()
    await engine.compact()
    assert engine.state.history == old and engine.provider.count == 2
    await engine.compact()
    assert engine.provider.count == 2


async def test_partial_summary_resume_skips_already_saved_chunks(tmp_path, tokens):
    from goalkeeper.storage import fingerprint

    engine, _ = fixture(tmp_path, tokens, [])
    history = [{"role": "user", "content": "old"}, {"role": "user", "content": "recent"}]
    engine.state.history = history
    engine.state.config = RunConfig(
        compact_trigger=1000,
        context_ceiling=10000,
        keep_recent_tokens=500,
        summary_max_tokens=200,
        summary_input_tokens=8000,
    )
    # Force enough request size to trigger while restoring an explicit completed first chunk.
    engine.state.task.objective = "task " * 1000
    saved = summary()
    source = engine.store.archive([{"role": "user", "content": "saved chunk"}])
    saved["source_refs"] = [source]
    engine.state.compaction = {
        "identity": fingerprint({"history": history, "version": 1}),
        "chunks": [
            [{"role": "user", "content": "saved chunk"}],
            [{"role": "user", "content": "remaining chunk"}],
        ],
        "kept": [history[-1]],
        "next": 1,
        "summary": saved,
        "correction_used": False,
        "sources": [source],
    }

    class Remaining:
        requests = []

        async def stream(self, request):
            self.requests.append(request)
            yield ModelEvent(
                type="response.completed",
                data={
                    "response": {
                        "model": request.config.model,
                        "usage": {"input_tokens": 10, "output_tokens": 10},
                        "output": [
                            {
                                "type": "message",
                                "content": [{"type": "output_text", "text": json.dumps(summary())}],
                            }
                        ],
                    }
                },
            )

    engine.provider = Remaining()
    await engine.compact()
    assert len(engine.provider.requests) == 1
    assert "remaining chunk" in engine.provider.requests[0].input[0]["content"]


class Auth:
    async def access_token(self):
        return "synthetic-offline"


def stream(events):
    return "".join("data: " + json.dumps(e) + "\n\n" for e in events).encode()


async def test_provider_requires_terminal_and_retains_encrypted_reasoning():
    item = {"type": "reasoning", "id": "r", "encrypted_content": "opaque bytes"}
    events = [
        {"type": "response.output_item.done", "output_index": 0, "item": item},
        {
            "type": "response.completed",
            "response": {
                "model": "gpt-6.1-sol",
                "status": "completed",
                "output": [],
                "usage": {"input_tokens": 10, "output_tokens": 2},
            },
        },
    ]
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, content=stream(events))

    provider = ResponsesProvider(Auth(), httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    request = ModelRequest(config=RunConfig(), instructions="test", input=[item])
    result = [e async for e in provider.stream(request)]
    assert result[-1].data["response"]["output"] == [item]
    assert seen[0]["input"] == [item] and seen[0]["reasoning"] == {"effort": "xhigh"}
    assert seen[0]["store"] is False and seen[0]["stream"] is True
    await provider.close()


async def test_incomplete_stream_tools_never_execute(tmp_path, tokens):
    item = call("write", {"path": "result.txt", "content": "READY"}, 1)
    provider = ResponsesProvider(
        Auth(),
        httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    content=stream([{"type": "response.output_item.done", "output_index": 0, "item": item}]),
                )
            )
        ),
    )
    engine, _ = fixture(tmp_path, tokens, [])
    engine.provider = provider
    state = await engine.run()
    assert state.phase == "resource_interrupted" and not (engine.root / "result.txt").exists()
    assert not state.pending_tools
    await provider.close()


@pytest.mark.parametrize(
    "code", ["http_400", "http_401", "http_403", "subscription_sharing_usage_limit_exceeded"]
)
async def test_configuration_and_quota_errors_are_not_automatic_recovery(tmp_path, tokens, code):
    engine, _ = fixture(tmp_path, tokens, [])

    class Failed:
        async def stream(self, request):
            raise ProviderError(code, resource=True)
            yield

    engine.provider = Failed()
    assert (await engine.run()).phase == "upstream_error"
    assert engine.state.recovery_used == 0


def test_cached_and_reasoning_subsets_not_double_counted():
    from goalkeeper.cost import totals

    record = {
        "forecast": 100,
        "usage": {
            "input_tokens": 10,
            "output_tokens": 5,
            "input_tokens_details": {"cached_tokens": 8},
            "output_tokens_details": {"reasoning_tokens": 4},
        },
    }
    assert totals([record])["known_tokens"] == 15


@pytest.mark.parametrize('ending', ['', '\n'])
async def test_failed_terminal_at_eof_retains_its_resource_reason(ending):
    terminal = {'type': 'response.failed', 'response': {'error': {'code': 'quota_exceeded'}}}
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(
        200, content=('data: ' + json.dumps(terminal) + ending).encode())))
    provider = ResponsesProvider(Auth(), client)
    request = ModelRequest(config=RunConfig(), instructions='offline', input=[])
    with pytest.raises(ProviderError) as failure:
        _ = [item async for item in provider.stream(request)]
    assert failure.value.code == 'quota_exceeded' and failure.value.resource
    await provider.close()


async def test_invalid_provider_sse_is_protocol_error():
    provider = ResponsesProvider(Auth(), httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, content=b'data: [bad-json]\n\n'))))
    request = ModelRequest(config=RunConfig(), instructions='offline', input=[])
    with pytest.raises(ProviderError) as failure:
        _ = [item async for item in provider.stream(request)]
    assert failure.value.code == 'invalid_stream_event' and not failure.value.resource
    await provider.close()


async def test_local_shared_stop_preserves_reason_instead_of_becoming_quota_error():
    provider = ResponsesProvider(Auth(), httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(429, json={'error': {'code': 'diagnostic_stopped',
                                                   'stop_reason': 'shared_service'}}))))
    request = ModelRequest(config=RunConfig(), instructions='offline', input=[])
    with pytest.raises(ProviderError) as failure:
        _ = [item async for item in provider.stream(request)]
    assert failure.value.code == 'shared_service'
    await provider.close()
