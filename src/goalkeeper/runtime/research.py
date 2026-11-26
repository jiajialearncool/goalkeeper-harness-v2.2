"""Shared source handling, bounded compaction and read-only scheduling."""
import asyncio
import json
import time

from ..context import facts, split_for_compaction, units, validate_summary
from ..sandbox import PermissionDenied
from ..storage import canonical, fingerprint
from ..models import ModelRequest
from .provider_catalog import is_account_provider

READ_ONLY = {'web_search', 'web_search_many', 'web_read', 'search_archive', 'read_archive', 'list_files', 'query_recommendation_case'}
MATERIAL_TOOLS = {'web_search', 'web_search_many', 'web_read', 'read', 'read_document', 'bash', 'check_command', 'query_recommendation_case'}


class RuntimeTokens:
    """Native search billing includes server tool turns, not just the submitted query."""
    def __init__(self, tokens):
        self.tokens = tokens

    def __getattr__(self, name):
        return getattr(self.tokens, name)

    def context(self, value, records=(), role='main'):
        if role == 'search' and isinstance(value, ModelRequest) and any(
                tool.get('type') == 'web_search' for tool in value.tools):
            result = self.tokens.context(value, (), role)
            result['policy'] = 'desktop-native-search-visible-v1'
            return result
        return self.tokens.context(value, records, role)


def remember_material(state, value):
    if not isinstance(value, dict):
        return
    for key in ('reference', 'full_reference', 'stdout_reference', 'stderr_reference'):
        ref = value.get(key)
        if isinstance(ref, str) and len(ref) == 64 and all(x in '0123456789abcdef' for x in ref):
            if ref not in state.material_refs:
                state.material_refs.append(ref)
    if isinstance(value.get('result'), dict):
        remember_material(state, value['result'])


def hydrate_materials(state):
    names = {}
    for item in state.history:
        if item.get('type') == 'function_call':
            names[item['call_id']] = item['name']
        if item.get('type') == 'function_call_output' and names.get(item.get('call_id')) in MATERIAL_TOOLS:
            try:
                remember_material(state, json.loads(item['output']))
            except (ValueError, KeyError):
                pass


async def compact(engine):
    """Keep all provenance in metadata, not in a repeatedly growing model summary."""
    from ..engine import SummaryLimit
    state, tokens = engine.state, engine.tokens
    if state.compaction and state.compaction.get('failed_identity'):
        engine.disable_compaction('summary_invalid')
    if state.compaction_disabled or not state.config.summary_enabled:
        return
    if tokens.context(engine.build_request(), state.calls)['estimated_tokens'] < state.config.compact_trigger:
        return
    identity = fingerprint({'history': state.history, 'version': state.goal_version})
    if not state.compaction or state.compaction.get('identity') != identity:
        limit = min(state.config.summary_input_tokens, state.config.context_ceiling) - state.config.summary_max_tokens - 256
        try:
            chunks, kept = split_for_compaction(state.history, tokens,
                8000 if state.config.task_profile == 'research_report' else state.config.keep_recent_tokens,
                limit, measure=lambda chunk: tokens.count(engine.summary_request(chunk, state.summary)[0].model_dump()),
                recent_measure=lambda unit: tokens.context(unit, state.calls)['estimated_tokens'])
        except ValueError:
            engine.disable_compaction('summary_input_limit')
            return
        if not chunks:
            return
        state.compaction = {'identity': identity, 'chunks': chunks, 'kept': kept, 'next': 0,
                            'summary': state.summary, 'correction_used': False, 'sources': []}
        engine.save('compaction_prepared', {'chunks': len(chunks)})
    progress = state.compaction
    while progress['next'] < len(progress['chunks']):
        chunk = progress['chunks'][progress['next']]
        request, source, permitted = engine.summary_request(chunk, progress['summary'], progress['sources'])
        assert engine.store.archive(chunk) == source
        if is_account_provider(state.config.provider_id) and state.config.adaptive_effort:
            request.config = request.config.model_copy(update={'effort': 'low'})
        request.instructions += '\n正文尽量控制在800 token；只保留关键事实、约束和下一步，不复制长篇资料。来源引用最多6个。'
        while True:
            try:
                response = await engine.call(request, 'summary')
            except (SummaryLimit, TimeoutError) as error:
                engine.disable_compaction(str(error) if isinstance(error, SummaryLimit) else "summary_time_budget")
                return
            text = ''.join(p.get('text', '') for item in response.get('output', [])
                           for p in item.get('content', []) if p.get('type') == 'output_text')
            try:
                value = json.loads(text)
                validate_summary(value, tokens, state.config.summary_max_tokens)
                if any(ref not in permitted for ref in value['source_refs']):
                    raise ValueError('来源引用不在已归档资料中。')
                refs = [*state.summary_sources, *(progress['summary'] or {}).get('source_refs', []),
                        *progress['sources'], *value['source_refs'], source]
                for ref in refs:
                    engine.store.read(ref)
                state.summary_sources = list(dict.fromkeys(refs))
                # Never append metadata to validated prose. The complete original remains archived.
                value['source_refs'] = list(dict.fromkeys([*value['source_refs'], source]))[:6]
                break
            except (ValueError, TypeError, KeyError, OSError) as error:
                if progress['correction_used']:
                    engine.disable_compaction('summary_invalid')
                    return
                progress['correction_used'] = True
                engine.save('summary_correction_used', {'reason': str(error)})
                request.input.append({'role': 'user', 'content': '摘要校验失败：'+str(error)+
                                      '。返回短JSON，正文不超过800 token，来源最多6个。'})
        progress['summary'] = value
        progress['sources'].append(source)
        progress['next'] += 1
        engine.save('summary_chunk_saved', {'next': progress['next']})
    state.summary, state.history = progress['summary'], progress['kept']
    state.context_base = facts(state)
    state.compaction = None
    units(state.history)
    engine.save('compaction_completed', {'provenance_count': len(state.summary_sources)})


async def parallel_reads(engine, batch):
    """Journal before dispatch, preserve result ordering, cancel all siblings on interruption."""
    from ..engine import Stop
    semaphore = asyncio.Semaphore(3)

    async def one(item):
        async with semaphore:
            operation = next((o for o in engine.state.operations if o['call_id'] == item['call_id']), None)
            if operation and operation['status'] in {'executing', 'completed', 'uncertain'}:
                operation['status'] = 'uncertain'
                result = {'error': '操作中断，结果不明；先核对实际状态，不直接重放。'}
            else:
                operation = {'call_id': item['call_id'], 'name': item['name'], 'status': 'executing'}
                engine.state.operations.append(operation)
                engine.save('operation_started', operation)
                try:
                    args = json.loads(item['arguments'])
                    if not isinstance(args, dict):
                        raise ValueError('工具参数必须是对象。')
                    if engine.remaining() <= 0:
                        raise Stop('time_budget')
                    result = await engine.tools.execute(item['name'], args, engine.remaining())
                    operation['status'] = 'completed'
                except (ValueError, OSError, PermissionDenied) as error:
                    operation['status'] = 'failed'
                    result = {'error': str(error)}
            result = engine.tools.output(result)
            engine.save('operation_result_saved', {'call_id': item['call_id'], 'result': result})
            return result

    jobs = [asyncio.create_task(one(item)) for item in batch]
    try:
        results = await asyncio.gather(*jobs)
    except BaseException:
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        raise
    for item, result in zip(batch, results):
        engine.state.history.append({'type': 'function_call_output', 'call_id': item['call_id'], 'output': canonical(result)})
        assert engine.state.pending_tools[0]['call_id'] == item['call_id']
        engine.state.pending_tools.pop(0)
    engine.save('read_batch_merged', {'count': len(batch)})


def cache_key(name, args, version):
    if name == 'web_search':
        value = ' '.join(str(args.get('query', '')).casefold().split())
    elif name == 'web_search_many':
        value = canonical(args)
    else:
        value = str(args.get('url', '')).split('#')[0]
    return name, value, version


async def cached_read(tools, name, args, timeout):
    if name == 'web_read' and tools.state.report and not tools.state.report['refresh']:
        from .report_flow import source_key, day
        for value in reversed(list(tools.state.report['materials'].values())):
            if value.get('url') and source_key(value['url']) == source_key(args['url']) and value['day'] == day():
                tools.engine.save('source_cache_hit', {'tool': name, 'scope': 'report_source'})
                return {**value, 'notice': '使用本报告同日已取得的原始资料；没有再次联网请求。'}
    key = cache_key(name, args, tools.state.goal_version)
    saved = tools.read_cache.get(key)
    if saved and time.monotonic()-saved[0] < 300:
        tools.engine.save('source_cache_hit', {'tool': name})
        return saved[1]
    if key in tools.read_pending:
        return await asyncio.shield(tools.read_pending[key])

    async def fetch():
        value = await tools.execute_uncached(name, args, timeout)
        if isinstance(value, dict) and not value.get('error') and value.get('available') is not False:
            tools.read_cache[key] = (time.monotonic(), value)
        return value
    job = asyncio.create_task(fetch())
    tools.read_pending[key] = job
    try:
        return await job
    finally:
        tools.read_pending.pop(key, None)
