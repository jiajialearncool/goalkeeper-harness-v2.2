import json
from uuid import uuid4
import httpx
from ..models import ModelEvent
from ..provider import ProviderError, ResponsesProvider
from .provider_catalog import CATALOG, response_model_matches

def messages_for(request):
    """Convert the harness's tool transactions to Chat Completions messages."""
    messages = [{'role': 'system', 'content': request.instructions}]
    for item in request.input:
        kind = item.get('type')
        if kind == 'reasoning':
            continue  # Provider-specific encrypted reasoning cannot cross providers.
        if kind == 'function_call':
            call = {'id': item['call_id'], 'type': 'function',
                    'function': {'name': item['name'], 'arguments': item['arguments']}}
            if messages[-1]['role'] != 'assistant':
                messages.append({'role': 'assistant', 'content': None})
            messages[-1].setdefault('tool_calls', []).append(call)
        elif kind == 'function_call_output':
            messages.append({'role': 'tool', 'tool_call_id': item['call_id'], 'content': item['output']})
        elif item.get('role') in {'user', 'assistant', 'system', 'developer'}:
            content = item.get('content', '')
            if isinstance(content, list):
                content = '\n'.join(part.get('text', '') for part in content if isinstance(part, dict))
            messages.append({'role': 'system' if item['role'] == 'developer' else item['role'], 'content': content})
    return messages


class ChatAPIProvider:
    """Normalize complete tool responses; incomplete streams never become tool operations."""
    def __init__(self, provider, key, http=None):
        self.provider, self.key = provider, key
        self.http = http or httpx.AsyncClient(timeout=httpx.Timeout(360, connect=30), follow_redirects=False)

    async def close(self):
        await self.http.aclose()

    async def stream(self, request):
        if CATALOG[self.provider].get('protocol') == 'responses':
            class APIKeyAuth:
                async def access_token(inner):
                    return self.key

            # OpenAI's current Codex model range uses the Responses transaction
            # format for tool calls. Reuse the shared transport parser so API-key
            # and account runs retain the same history and completion semantics.
            direct = ResponsesProvider(APIKeyAuth(), self.http)
            async for event in direct.stream(request):
                yield event
            return
        if CATALOG[self.provider].get('protocol') == 'anthropic':
            from .anthropic_provider import stream_anthropic
            async for event in stream_anthropic(self, request):
                yield event
            return
        payload = {'model': request.config.model, 'messages': messages_for(request), 'stream': True,
                   'stream_options': {'include_usage': True}, 'max_tokens': CATALOG[self.provider].get('max_tokens', 8192)}
        if self.provider in {'deepseek', 'kimi', 'moonshotai', 'zai'}:
            payload['thinking'] = {'type': 'disabled'}
        if self.provider == 'google':
            payload['reasoning_effort'] = 'none'
        if request.tools:
            if any(tool.get('type') != 'function' for tool in request.tools):
                raise ProviderError('unsupported_tool', '此 API 接入不提供账号内置搜索。')
            payload['tools'] = [{'type': 'function', 'function': {k: t[k] for k in ('name', 'description', 'parameters')}}
                                for t in request.tools]
        text, calls, usage, finish, done, response_id, returned_model = '', {}, None, None, False, None, None
        try:
            async with self.http.stream('POST', CATALOG[self.provider]['base_url'] + '/chat/completions',
                                        json=payload, headers={'Authorization': 'Bearer ' + self.key}) as response:
                yield ModelEvent(type='provider_response_metadata', data={'http_status': response.status_code})
                if response.status_code != 200:
                    # Do not record arbitrary remote bodies: they can echo the API key or conversation.
                    message = f"{CATALOG[self.provider]['name']} 请求失败：HTTP {response.status_code}。"
                    code = f'http_{response.status_code}'
                    yield ModelEvent(type='provider_http_error', data={'http_status': response.status_code, 'message': message})
                    raise ProviderError(code, message, response.status_code == 429 or response.status_code >= 500)
                async for line in response.aiter_lines():
                    if not line.startswith('data:'):
                        continue
                    raw = line[5:].strip()
                    if raw == '[DONE]':
                        done = True
                        break
                    try:
                        chunk = json.loads(raw)
                        if not isinstance(chunk, dict) or chunk.get('error'):
                            raise ValueError()
                        response_id = chunk.get('id') or response_id
                        returned_model = chunk.get('model') or returned_model
                        if chunk.get('usage'):
                            u = chunk['usage']
                            usage = {'input_tokens': u['prompt_tokens'], 'output_tokens': u['completion_tokens']}
                            if isinstance(u.get('prompt_tokens_details'), dict):
                                usage['input_tokens_details'] = u['prompt_tokens_details']
                        for choice in chunk.get('choices', []):
                            if choice.get('index', 0) != 0:
                                continue
                            finish = choice.get('finish_reason') or finish
                            delta = choice.get('delta') or {}
                            if delta.get('content'):
                                text += delta['content']
                                yield ModelEvent(type='response.output_text.delta', data={'delta': delta['content']})
                            for part in delta.get('tool_calls') or []:
                                index = part['index']
                                target = calls.setdefault(index, {'id': '', 'name': '', 'arguments': ''})
                                target['id'] += part.get('id') or ''
                                target['name'] += (part.get('function') or {}).get('name') or ''
                                target['arguments'] += (part.get('function') or {}).get('arguments') or ''
                    except (ValueError, KeyError, TypeError) as error:
                        raise ProviderError('invalid_stream_event', '模型流式响应格式错误。') from error
            if not done or not finish:
                raise ProviderError('interrupted_stream', '模型响应未完整结束，工具未执行。', True)
            if finish not in {'stop', 'tool_calls'}:
                raise ProviderError('response.incomplete', '模型响应被截断或拒绝，工具未执行。')
            output = []
            if text:
                output.append({'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': text}]})
            prefix = 'api_' + uuid4().hex + '_'
            for index, call in sorted(calls.items()):
                if not call['id'] or not call['name']:
                    raise ProviderError('invalid_output', '工具调用缺少标识或名称。')
                output.append({'type': 'function_call', 'call_id': prefix + str(index),
                               'name': call['name'], 'arguments': call['arguments']})
            # OpenAI aliases can return a dated snapshot of that exact model family.
            # Keep all other names intact so Engine's existing mismatch check rejects them,
            # while still retaining their actual usage and response evidence.
            expected = request.config.model
            matches = response_model_matches(self.provider, expected, returned_model)
            terminal = {'id': response_id, 'status': 'completed', 'model': expected if matches else returned_model,
                        'provider_model': returned_model, 'output': output}
            if usage is not None:
                terminal['usage'] = usage
            yield ModelEvent(type='response.completed', data={'response': terminal})
        except httpx.HTTPError as error:
            raise ProviderError('transport_error', '模型服务网络连接异常。', True) from error
