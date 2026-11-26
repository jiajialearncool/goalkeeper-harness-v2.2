"""Bounded public diagnostics, without request bodies or private reasoning."""
import re

from ..secrets import redact


def error_details(error, events=()):
    result = {'code': error.code, 'message': str(error)[:2000]}
    for event in events:
        data = event.data
        if event.type == 'provider_response_metadata':
            result.update({k: data[k] for k in ('http_status', 'request_id') if k in data})
        elif event.type == 'provider_http_error':
            result.update({k: data[k] for k in ('http_status', 'message') if k in data})
        elif event.type in {'response.failed', 'response.incomplete', 'error'}:
            response = data.get('response') or {}
            failure = response.get('error') or data.get('error') or data
            result['event_type'] = event.type
            if isinstance(failure, dict):
                result.update({k: failure[k] for k in ('type', 'param', 'message')
                               if isinstance(failure.get(k), str)})
            if isinstance(response.get('id'), str):
                result['response_id'] = response['id']
    result = {k: v[:2000] if isinstance(v, str) else v for k, v in result.items()}
    result = redact(result)
    result['category'] = error_category(result)
    return result


def error_category(detail):
    code, status = detail['code'], detail.get('http_status')
    if any(value in code for value in ('auth', 'quota', 'usage')):
        return 'account_or_quota'
    if code in {'server_error', 'server_is_overloaded', 'transport_error', 'interrupted_stream',
                'rate_limit_exceeded', 'http_408', 'http_429', 'http_500', 'http_502', 'http_503',
                'http_504'} or status in {408, 429, 500, 502, 503, 504}:
        return 'temporary_failure'
    if any(value in code for value in ('auth', 'quota', 'usage')) or status in {401, 403}:
        # A scoped policy rejection can be explicit; an ordinary 403 cannot prove it.
        if not explicitly_rejects_search(detail):
            return 'account_or_quota'
    if explicitly_rejects_search(detail):
        return 'search_unsupported'
    if code == 'search_not_performed':
        return 'search_not_performed'
    return 'request_error'


def explicitly_rejects_search(detail):
    message = detail.get('message', '').lower()
    scoped = bool(re.search(r'\bweb_search(?:_preview)?\b', message))
    explicit_code = detail['code'] in {'unsupported_tool', 'tool_not_supported',
                                        'web_search_not_supported', 'web_search_not_allowed'}
    rejection = bool(re.search(r'not supported|unsupported|not allowed|not permitted|disabled|不支持|不允许', message))
    return (scoped and (explicit_code or rejection)) or detail['code'] in {
        'web_search_not_supported', 'web_search_not_allowed'}
