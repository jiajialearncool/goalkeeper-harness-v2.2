"""Adapter of host-owned directory bindings and events, never execution strategy."""
from .services import RuntimeServices


def services_from_host(host, conversation):
    def validate(root):
        if hasattr(host, 'files_root'):
            wid = host.conversation(conversation)['workspace_id']
            if host.files_root(wid) != root:
                raise ValueError('工作区绑定已改变，请发送新要求开始执行。')
    cache = None
    if hasattr(host, 'workspace_cache'):
        cache = host.workspace_cache(host.conversation(conversation)['workspace_id'])
    return RuntimeServices(
        event=lambda kind, data, persist=True: host.event(conversation, kind, data, persist=persist),
        applied=lambda mid: host.applied(conversation, mid) if hasattr(host, 'applied') else None,
        validate_workspace=validate, internal_root=cache,
        search_secret=getattr(host, 'search_secret', None),
        search_capability=getattr(host, 'search_capability', 'unknown'))
