"""Fresh synthetic failures: no credentials, inference, or historical answer replay."""
import asyncio
import json
import time
from types import SimpleNamespace

import httpx
import pytest

from goalkeeper.streaming import StagedStream
from goalkeeper.transport import MeteredTransport


class Auth:
    async def access_token(self):
        return 'synthetic'

    async def close(self):
        pass


def event(value, ending='\n\n'):
    return ('data: ' + json.dumps(value) + ending).encode()


def complete(model='gpt-6.1-sol', usage=True):
    response = {'model': model, 'output': [{'type': 'function_call', 'name': 'write',
                'call_id': 'whole', 'arguments': '{}'}]}
    if usage:
        response['usage'] = {'input_tokens': 10, 'output_tokens': 7}
    return {'type': 'response.completed', 'response': response}


class Wire(httpx.AsyncByteStream):
    def __init__(self, *, break_stream=False, terminal=None, tail_error=False):
        self.break_stream, self.terminal, self.tail_error = break_stream, terminal, tail_error
        self.closed = False

    async def __aiter__(self):
        yield event({'type': 'response.output_item.done', 'output_index': 0,
                     'item': {'type': 'function_call', 'call_id': 'discard-me', 'name': 'write',
                              'arguments': '{"content":"partial"}'}})
        if self.break_stream:
            raise httpx.RemoteProtocolError('synthetic incomplete chunked read')
        yield event(self.terminal or complete())
        if self.tail_error:
            raise httpx.RemoteProtocolError('synthetic tail break after completion')

    async def aclose(self):
        self.closed = True


def meter(tmp_path, handler, retries=0, **limits):
    return MeteredTransport(tmp_path / 'private', tmp_path / 'usage.json', auth=Auth(),
                            transient_retries=retries, http=httpx.AsyncClient(
                                transport=httpx.MockTransport(handler)), **limits)


def route(m, identifier='one', deadline=None):
    m.port = 1234
    value = m.register(SimpleNamespace(id=identifier, deadline=deadline,
                                      config=SimpleNamespace(engine=identifier)))
    async def payload():
        return {'model': 'gpt-6.1-sol', 'reasoning': {'effort': 'xhigh'}, 'input': []}
    return SimpleNamespace(headers={'authorization': 'Bearer ' + value['LOCAL_ROUTE_TOKEN']},
                           json=payload)


async def body(response):
    if hasattr(response, 'body_iterator'):
        return b''.join([chunk async for chunk in response.body_iterator])
    return response.body


async def test_all_four_groups_stage_partial_output_and_recover_independently(tmp_path):
    seen, wires = {}, []
    def handle(request):
        group = json.loads(request.content)['input'][0]['content']
        seen[group] = seen.get(group, 0) + 1
        wire = Wire(break_stream=seen[group] == 1)
        wires.append(wire)
        return httpx.Response(200, stream=wire)
    m = meter(tmp_path, handle, retries=1, total_limit=100_000)
    jobs = []
    for group in ('goalkeeper', 'codex', 'opencode', 'pi'):
        request = route(m, group, time.time()+10)
        async def payload(group=group):
            return {'model': 'gpt-6.1-sol', 'reasoning': {'effort': 'xhigh'},
                    'input': [{'role': 'user', 'content': group}]}
        request.json = payload
        async def run(group=group, request=request):
            return await body(await m.response(group, request))
        jobs.append(run())
    output = await asyncio.gather(*jobs)
    assert seen == {g: 2 for g in ('goalkeeper', 'codex', 'opencode', 'pi')}
    assert all(raw.count(b'discard-me') == 1 and raw.count(b'response.completed') == 1 for raw in output)
    # Each client sees the second complete response only, never the first attempt.
    assert all(c.get('retry_scheduled') for c in m.records[:4])
    assert len(m.records) == 8 and m.known() == 4*17 and m.reserved() > 0
    assert sum(not c['usage_known'] for c in m.records) == 4
    assert not m.stop.is_set() and all(w.closed for w in wires)
    assert all(c['status'] != 'in_flight' for c in m.records)
    assert len({c['estimate_detail']['request_sha256'] for c in m.records}) == 4
    await m.close()


async def test_retry_is_disabled_by_default_and_partial_tools_never_escape(tmp_path):
    m = meter(tmp_path, lambda request: httpx.Response(200, stream=Wire(break_stream=True)))
    raw = await body(await m.response('one', route(m)))
    assert b'discard-me' not in raw and b'partial' not in raw and b'"type": "error"' in raw
    assert len(m.records) == 1 and m.reason == 'shared_service' and not m.retry_used
    assert m.reserved() > 0 and not m.records[0]['usage_known']
    await m.close()


async def test_second_break_stops_without_third_request(tmp_path):
    m = meter(tmp_path, lambda request: httpx.Response(200, stream=Wire(break_stream=True)), retries=1)
    raw = await body(await m.response('one', route(m)))
    assert len(m.records) == 2 and m.retry_used == {'one': 1}
    assert m.first_reason == 'shared_service' and b'discard-me' not in raw
    assert m.reserved() == sum(c['estimated_request_tokens'] for c in m.records)
    await m.close()


@pytest.mark.parametrize('kind', ['group', 'shared', 'deadline'])
async def test_retry_cannot_bypass_budget_or_original_deadline(tmp_path, kind):
    def handle(request):
        return httpx.Response(200, stream=Wire(break_stream=True))
    limits = {'per_run_limit': 7000} if kind == 'group' else {'total_limit': 7000} if kind == 'shared' else {}
    m = meter(tmp_path, handle, retries=1, **limits)
    deadline = time.time()+.2 if kind == 'deadline' else time.time()+10
    raw = await body(await m.response('one', route(m, deadline=deadline)))
    assert len(m.records) == 1 and b'discard-me' not in raw
    assert m.reserved() > 0
    if kind == 'shared':
        assert m.reason == 'integration_token_threshold'
    await m.close()


@pytest.mark.parametrize('status', [400, 401, 403, 429, 503])
async def test_returned_http_errors_do_not_retry(tmp_path, status):
    m = meter(tmp_path, lambda request: httpx.Response(status, json={'error': {'code': 'rejected'}}), retries=1)
    response = await m.response('one', route(m))
    assert response.status_code == status and len(m.records) == 1 and not m.retry_used
    assert m.records[0]['failure_origin'] == 'upstream_http'
    assert m.reason == ('configuration_error' if status in {400,401,403} else 'shared_service')
    await m.close()


@pytest.mark.parametrize('model,usage,reason', [
    ('other', True, 'returned_model_mismatch'),
    ('gpt-6.1-sol', False, 'response_usage_missing'),
])
async def test_invalid_completed_response_cannot_publish_tools_or_retry(tmp_path, model, usage, reason):
    m = meter(tmp_path, lambda request: httpx.Response(200, stream=Wire(terminal=complete(model, usage))), retries=1)
    raw = await body(await m.response('one', route(m)))
    assert b'discard-me' not in raw and b'function_call' not in raw
    assert m.reason == reason and len(m.records) == 1 and not m.retry_used
    assert m.records[0]['usage_known'] == usage
    await m.close()


async def test_terminal_failure_keeps_returned_usage_without_retry(tmp_path):
    terminal = {'type': 'response.failed', 'response': {'model': 'gpt-6.1-sol',
                'usage': {'input_tokens': 9, 'output_tokens': 2}, 'error': {'code': 'quota_exceeded'}}}
    m = meter(tmp_path, lambda request: httpx.Response(200, stream=Wire(terminal=terminal)), retries=1)
    raw = await body(await m.response('one', route(m)))
    assert b'discard-me' not in raw and m.known() == 11 and m.reserved() == 0
    assert len(m.records) == 1 and m.records[0]['status'] == 'terminal_error' and not m.retry_used
    await m.close()


async def test_complete_response_is_not_invalidated_by_tail_connection_break(tmp_path):
    m = meter(tmp_path, lambda request: httpx.Response(200, stream=Wire(tail_error=True)), retries=1)
    raw = await body(await m.response('one', route(m)))
    assert b'response.completed' in raw and m.known() == 17
    assert not m.stop.is_set() and not m.retry_used and len(m.records) == 1
    await m.close()


async def test_invalid_sse_json_is_protocol_failure_not_core_or_retry(tmp_path):
    m = meter(tmp_path, lambda request: httpx.Response(200, content=b'data: not-json\n\n'), retries=1)
    raw = await body(await m.response('one', route(m)))
    assert m.reason == 'stream_protocol_error' and not m.retry_used
    assert b'not-json' not in raw and len(m.records) == 1
    await m.close()


async def test_stream_size_bound(tmp_path):
    staged = StagedStream(tmp_path, max_bytes=10)
    from goalkeeper.streaming import StreamFailure
    with pytest.raises(StreamFailure, match='stream_size_limit'):
        staged.write('x'*20)
    staged.close()


async def test_staging_heartbeat_has_no_semantic_events_and_cancellation_settles(tmp_path):
    gate = asyncio.Event()
    class Slow(httpx.AsyncByteStream):
        async def __aiter__(self):
            await gate.wait()
            yield event(complete())
    upstream = httpx.Response(200, stream=Slow())
    staged = StagedStream(tmp_path)
    iterator = staged.collect(upstream, heartbeat=.01)
    assert (await anext(iterator)).startswith(b': ')
    await iterator.aclose()
    staged.close()
    await upstream.aclose()


async def test_request_cancellation_never_retries_or_retains_inflight(tmp_path):
    gate = asyncio.Event()
    class Slow(httpx.AsyncByteStream):
        async def __aiter__(self):
            await gate.wait()
            yield event(complete())
    m = meter(tmp_path, lambda request: httpx.Response(200, stream=Slow()), retries=1)
    task = asyncio.create_task(body(await m.response('one', route(m))))
    await asyncio.sleep(.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(m.records) == 1 and not m.retry_used and not m.stop.is_set()
    assert m.records[0]['status'] == 'client_cancelled_usage_unknown'
    assert not any(c['status'] == 'in_flight' for c in m.records)
    await m.close()


async def test_transport_shutdown_cleans_its_stream_worker_and_preserves_unknown(tmp_path):
    gate = asyncio.Event()
    class Slow(httpx.AsyncByteStream):
        async def __aiter__(self):
            await gate.wait()
            yield event(complete())
    m = meter(tmp_path, lambda request: httpx.Response(200, stream=Slow()), retries=1)
    task = asyncio.create_task(body(await m.response('one', route(m))))
    await asyncio.sleep(.01)
    await asyncio.wait_for(m.close(), .5)
    assert task.done() and not m.active_streams and not m.retry_used
    data = json.loads(m.report.read_text())
    assert data['in_flight'] == 0 and data['unknown_calls'] == 1 and m.reserved() > 0


async def test_unconsumed_response_is_accounted_as_unknown_at_shutdown(tmp_path):
    wire = Wire()
    m = meter(tmp_path, lambda request: httpx.Response(200, stream=wire))
    await m.response('one', route(m))
    await m.close()
    assert m.records[0]['status'] == 'shutdown_interrupted_usage_unknown'
    assert m.reserved() > 0
    assert wire.closed and not m.open_responses


async def test_initial_connection_error_has_one_bounded_retry(tmp_path):
    calls = []
    def handle(request):
        calls.append(json.loads(request.content))
        if len(calls) == 1:
            raise httpx.ConnectError('synthetic')
        return httpx.Response(200, content=event(complete()))
    m = meter(tmp_path, handle, retries=1)
    assert b'response.completed' in await body(await m.response('one', route(m)))
    assert len(calls) == 2 and calls[0] == calls[1] and m.known() == 17
    assert not m.records[0]['usage_known'] and m.reserved() > 0 and not m.stop.is_set()
    await m.close()


async def test_retry_allowance_survives_report_reload(tmp_path):
    def handle(request):
        return httpx.Response(200, stream=Wire(break_stream=True))
    m = meter(tmp_path, handle, retries=1)
    await body(await m.response('one', route(m)))
    await m.close()
    restarted = meter(tmp_path, handle, retries=1)
    assert restarted.retry_used == {'one': 1} and restarted.reason == 'shared_service'
    route(restarted)
    assert not restarted.admit('one')
    await restarted.close()


async def test_group_reservation_failure_keeps_exact_budget_reason(tmp_path):
    m = meter(tmp_path, lambda r: httpx.Response(200, stream=Wire(break_stream=True)),
              retries=1, per_run_limit=7000)
    raw = await body(await m.response('one', route(m)))
    assert b'diagnostic_attempt_token_budget' in raw and not m.stop.is_set()
    await m.close()
