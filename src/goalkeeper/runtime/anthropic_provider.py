"""Anthropic Messages stream translated into the existing harness transaction contract."""
import json
from uuid import uuid4
import httpx
from ..models import ModelEvent
from ..provider import ProviderError
from .providers import messages_for


def anthropic_payload(request):
    converted = messages_for(request)
    system, messages = [], []
    for item in converted:
        if item['role'] == 'system':
            system.append(item['content'])
            continue
        if item['role'] == 'tool':
            role, content = 'user', [{'type': 'tool_result', 'tool_use_id': item['tool_call_id'], 'content': item['content']}]
        else:
            role, content = item['role'], []
            if item.get('content'):
                content.append({'type': 'text', 'text': item['content']})
            for call in item.get('tool_calls', []):
                content.append({'type': 'tool_use', 'id': call['id'], 'name': call['function']['name'],
                                'input': json.loads(call['function']['arguments'])})
        if messages and messages[-1]['role'] == role:
            messages[-1]['content'].extend(content)
        else:
            messages.append({'role': role, 'content': content})
    if any(t.get('type') != 'function' for t in request.tools):
        raise ProviderError('unsupported_tool', '此 API 接入不提供账号内置搜索。')
    payload = {'model': request.config.model, 'system': '\n\n'.join(system), 'messages': messages,
               'max_tokens': 8192, 'stream': True}
    if request.tools:
        payload['tools'] = [{'name': t['name'], 'description': t['description'], 'input_schema': t['parameters']}
                            for t in request.tools]
    return payload


async def stream_anthropic(api, request):
    payload = anthropic_payload(request)
    message, blocks, stopped, finish, usage = {}, {}, False, None, None
    try:
        async with api.http.stream('POST', 'https://api.anthropic.com/v1/messages', json=payload,
                                  headers={'x-api-key': api.key, 'anthropic-version': '2023-06-01'}) as response:
            yield ModelEvent(type='provider_response_metadata', data={'http_status': response.status_code})
            if response.status_code != 200:
                detail = f'Anthropic 请求失败：HTTP {response.status_code}。'
                yield ModelEvent(type='provider_http_error', data={'http_status': response.status_code, 'message': detail})
                raise ProviderError(f'http_{response.status_code}', detail, response.status_code == 429 or response.status_code >= 500)
            async for line in response.aiter_lines():
                if not line.startswith('data:'):
                    continue
                try:
                    event = json.loads(line[5:].strip())
                    kind = event['type']
                    if kind == 'error':
                        raise ProviderError('provider_stream_error', 'Anthropic 返回流式错误，工具未执行。')
                    if kind == 'message_start':
                        message = event['message']
                        u = message.get('usage')
                        if u is not None:
                            usage = {'input_tokens': u['input_tokens'] + u.get('cache_creation_input_tokens', 0) + u.get('cache_read_input_tokens', 0),
                                     'output_tokens': u['output_tokens'],
                                     'input_tokens_details': {'cached_tokens': u.get('cache_read_input_tokens', 0)}}
                    elif kind == 'content_block_start':
                        block = event['content_block']
                        blocks[event['index']] = {**block, 'arguments': '', 'closed': False}
                    elif kind == 'content_block_delta':
                        block, delta = blocks[event['index']], event['delta']
                        if delta['type'] == 'text_delta':
                            block['text'] = block.get('text', '') + delta['text']
                            yield ModelEvent(type='response.output_text.delta', data={'delta': delta['text']})
                        elif delta['type'] == 'input_json_delta':
                            block['arguments'] += delta['partial_json']
                    elif kind == 'content_block_stop':
                        blocks[event['index']]['closed'] = True
                    elif kind == 'message_delta':
                        finish = event['delta'].get('stop_reason') or finish
                        if usage is not None and 'usage' in event:
                            usage['output_tokens'] = event['usage']['output_tokens']
                    elif kind == 'message_stop':
                        stopped = True
                except (ValueError, KeyError, TypeError) as error:
                    raise ProviderError('invalid_stream_event', 'Anthropic 流式响应格式错误。') from error
        if not stopped or not message or not finish or any(not b['closed'] for b in blocks.values()):
            raise ProviderError('interrupted_stream', '模型响应未完整结束，工具未执行。', True)
        if finish not in {'end_turn', 'tool_use'}:
            raise ProviderError('response.incomplete', '模型响应被截断或拒绝，工具未执行。')
        output, prefix = [], 'api_' + uuid4().hex + '_'
        for index, block in sorted(blocks.items()):
            if block['type'] == 'text':
                output.append({'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': block.get('text', '')}]})
            elif block['type'] == 'tool_use':
                if not block.get('id') or not block.get('name'):
                    raise ProviderError('invalid_output', '工具调用缺少标识或名称。')
                args = block['arguments'] or json.dumps(block.get('input', {}))
                try:
                    if not isinstance(json.loads(args), dict):
                        raise ValueError()
                except ValueError as error:
                    raise ProviderError('invalid_output', '工具参数格式错误，工具未执行。') from error
                output.append({'type': 'function_call', 'call_id': prefix + str(index), 'name': block['name'], 'arguments': args})
        terminal = {'id': message.get('id'), 'status': 'completed', 'model': message.get('model'),
                    'provider_model': message.get('model'), 'output': output}
        if usage is not None:
            terminal['usage'] = usage
        yield ModelEvent(type='response.completed', data={'response': terminal})
    except httpx.HTTPError as error:
        raise ProviderError('transport_error', '模型服务网络连接异常。', True) from error
