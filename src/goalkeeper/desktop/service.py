import argparse
import asyncio
import json
import os
import secrets
import socket
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import keyring
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from .. import DESKTOP_VERSION, HARNESS_VERSION
from ..paths import PROJECT
from ..secrets import redact
from .manager import Manager, safe_file
from .files import read_file
from .images import attach, add_to_message


def public_value(value, field=None):
    if isinstance(value, dict):
        return {k: public_value(v, k) for k, v in value.items() if k != 'encrypted_content'}
    if isinstance(value, list):
        return [public_value(v, field) for v in value]
    if field == 'preview' and isinstance(value, str) and 'encrypted_content' in value:
        return '[模型内部内容已隐藏；请查看公开回复和资料来源。]'
    return value


def application(manager, token):
    @asynccontextmanager
    async def lifespan(app):
        yield
        await manager.close()

    app = FastAPI(lifespan=lifespan)
    app.state.manager = manager

    @app.middleware('http')
    async def authorize(request, call_next):
        if request.url.path.startswith('/auth/'):
            return await call_next(request)
        if not secrets.compare_digest(request.headers.get('authorization', ''), 'Bearer ' + token):
            return JSONResponse({'error': 'Unauthorized'}, 401)
        return await call_next(request)

    @app.post('/rpc')
    async def rpc(request: Request):
        try:
            data = await request.json()
            action, args = data['action'], data.get('args', {})
            if action == 'bootstrap':
                value = {**manager.index, 'workspaces': [{**w, 'directory': str(manager.recorded_root(w)),
                         'mode': w.get('mode', 'managed')} for w in manager.index['workspaces']], 'auth': manager.auth.status(), 'search': manager.search_capability}
            elif action == 'desktop.bootstrap':
                manager.ensure_default_workspace()
                value = {'version': DESKTOP_VERSION, 'harness': HARNESS_VERSION, 'settings': manager.index['settings'],
                         'workspaces': [{**w, 'directory': str(manager.recorded_root(w)), 'mode': w.get('mode', 'managed')}
                                        for w in manager.index['workspaces']],
                         'conversations': manager.list_conversations(), 'auth': manager.auth.status(),
                         'search': manager.search_capability}
            elif action == 'diagnostics':
                value = {'version': DESKTOP_VERSION, 'harness': HARNESS_VERSION, 'python': sys.version.split()[0], 'data_root': str(manager.root),
                         'active_tasks': len(manager.jobs), 'search': manager.search_capability,
                         'account_connected': manager.auth.status()['connected']}
            elif action == 'models.status':
                value = manager.model_settings.status()
            elif action == 'models.save':
                value = manager.model_settings.save(args['provider'], args['model'], args.get('key'))
            elif action == 'models.select':
                value = manager.model_settings.select(args['provider'], args.get('model'), args.get('effort'))
            elif action == 'models.delete':
                value = manager.model_settings.delete(args['provider'])
            elif action == 'accounts.delete':
                value = manager.model_settings.delete_account(args['provider'])
            elif action == 'models.verify':
                value = await manager.model_settings.verify(args['provider'])
            elif action == 'workspace.create':
                value = manager.create_workspace(args['name'], args.get('directory'))
            elif action == 'workspace.bind':
                value = await manager.bind_workspace(args['workspace_id'], args['directory'])
            elif action == 'workspace.rename':
                value = manager.rename_workspace(args['workspace_id'], args['name'])
            elif action == 'workspace.delete':
                value = await manager.delete_workspace(args['workspace_id'])
            elif action == 'conversation.create':
                value = manager.create_conversation(args.get('workspace_id') or manager.ensure_default_workspace()['id'])
            elif action == 'conversation.list':
                value = manager.list_conversations(args.get('query', ''), args.get('workspace_id'),
                                                   args.get('offset', 0), args.get('limit', 100))
            elif action == 'conversation.detail':
                value = manager.conversation_detail(args['conversation_id'])
            elif action == 'conversation.rename':
                value = manager.rename_conversation(args['conversation_id'], args['title'])
            elif action == 'conversation.delete':
                value = await manager.delete_conversation(args['conversation_id'])
            elif action == 'conversation.deleted':
                value = manager.list_deleted_conversations()
            elif action == 'conversation.restore':
                value = manager.restore_conversation(args['archive_id'])
            elif action == 'file.references':
                value = manager.file_references(args['conversation_id'], args.get('workspace_id'))
            elif action == 'image.attach':
                value = attach(manager, args['workspace_id'], args['conversation_id'], args['data'])
            elif action == 'events.page':
                value = manager.events_page(args['conversation_id'], args.get('after', 0), args.get('limit', 100))
            elif action == 'run.detail':
                value = manager.run_detail(args['conversation_id'], args['run_id'])
            elif action == 'conversation.start':
                args['workspace_id'] = args.get('workspace_id') or manager.ensure_default_workspace()['id']
                paths = manager.validate_references(args['conversation_id'], args.get('references', []), args['workspace_id'])
                text = args['text'] + ('\n当前工作区文件引用：' + '、'.join(paths) if paths else '')
                text = add_to_message(manager, args['conversation_id'], args['workspace_id'], text, args.get('images', []))
                value = manager.start_conversation(args['workspace_id'], args['conversation_id'], text, args['message_id'], display_text=args['text'])
            elif action == 'message.send':
                paths = manager.validate_references(args['conversation_id'], args.get('references', []))
                text = args['text'] + ('\n当前工作区文件引用：' + '、'.join(paths) if paths else '')
                wid = manager.conversation(args['conversation_id'])['workspace_id']
                text = add_to_message(manager, args['conversation_id'], wid, text, args.get('images', []))
                value = manager.send(args['conversation_id'], text, args['message_id'], display_text=args['text'])
            elif action == 'run.stop':
                value = await manager.stop(args['conversation_id'])
            elif action == 'run.continue':
                value = manager.continue_run(args['conversation_id'])
            elif action == 'files.list':
                value = manager.list_files(args['workspace_id'], args.get('conversation_id'), args.get('run_id'))
            elif action == 'file.preview':
                value = await manager.preview_document(args['workspace_id'], args['path'],
                                                       args.get('conversation_id'), args.get('run_id'))
            elif action == 'file.preview_bytes':
                value = await manager.preview_bytes(args['workspace_id'], args['path'],
                                                    args.get('conversation_id'), args.get('run_id'))
            elif action == 'preview.static':
                value = await manager.static_preview(args['workspace_id'], args.get('conversation_id'), args['path'],
                                                     args.get('run_id'))
            elif action == 'file.location':
                root = manager.preview_root(args['workspace_id'], args.get('conversation_id'), args.get('run_id'))
                value = {'path': str(safe_file(root, args['path']))}
            elif action == 'file.export_preview':
                value = manager.export_preview(args['workspace_id'], args['path'],
                                               args.get('conversation_id'), args.get('run_id'))
            elif action == 'import.plan':
                value = manager.import_plan(args['paths'], args.get('workspace_id'))
            elif action == 'import.apply':
                value = manager.import_files(args['workspace_id'], args['paths'])
            elif action == 'events':
                c = manager.conversation(args['conversation_id'])
                value = {'events': [e for e in c['events'] if e['sequence'] > args.get('after', 0)],
                         'conversation': c}
            elif action == 'settings.update':
                value = manager.update_settings(args)
            elif action == 'search.key':
                key = args['key'].strip()
                if key:
                    keyring.set_password('GoalKeeper Desktop Search', manager.account_key, key)
                else:
                    try:
                        keyring.delete_password('GoalKeeper Desktop Search', manager.account_key)
                    except keyring.errors.PasswordDeleteError:
                        pass
                value = {'configured': bool(key)}
            elif action == 'search.status':
                value = {'builtin': manager.search_capability,
                         'configured': bool(keyring.get_password('GoalKeeper Desktop Search', manager.account_key))}
            elif action == 'auth.logout':
                if manager.jobs:
                    raise ValueError('请先停止正在执行的任务。')
                manager.auth.logout()
                value = manager.auth.status()
            else:
                raise ValueError('不支持的操作。')
            return {'result': public_value(value)}
        except (ValueError, KeyError, StopIteration, OSError) as error:
            return JSONResponse({'error': str(redact(str(error)))}, 400)

    @app.get('/auth/start')
    async def auth_start(request: Request):
        # A browser session cookie binds the subsequent PKCE callback.
        profile = request.query_params.get('profile', 'account')
        if profile == 'account':
            broker = manager.auth
        else:
            broker = manager.account_auths.get(profile)
            if broker is None:
                return HTMLResponse('<meta charset="utf-8"><h2>不支持此账号提供商</h2>', 400)
        session = secrets.token_urlsafe(32)
        response = RedirectResponse(broker.begin(
            f'http://127.0.0.1:{request.url.port}/auth/callback', session))
        response.set_cookie('gk_desktop_auth', session, httponly=True, samesite='lax', max_age=600)
        response.set_cookie('gk_desktop_auth_profile', profile, httponly=True, samesite='lax', max_age=600)
        return response

    @app.get('/auth/callback')
    async def auth_callback(request: Request):
        profile = request.cookies.get('gk_desktop_auth_profile', 'account')
        broker = manager.auth if profile == 'account' else manager.account_auths.get(profile)
        if broker is None:
            return HTMLResponse('<meta charset="utf-8"><h2>连接未完成</h2><p>账号提供商无效。</p>', 400)
        try:
            await broker.callback(dict(request.query_params), request.cookies.get('gk_desktop_auth', ''))
            return HTMLResponse('<meta charset="utf-8"><h2>账号已连接</h2><p>请返回 GoalKeeper 桌面应用。</p>')
        except Exception as error:
            broker.last_error = str(redact(str(error)))
            return HTMLResponse('<meta charset="utf-8"><h2>连接未完成</h2><p>请返回应用查看原因。</p>', 400)

    @app.get('/file')
    async def file(request: Request):
        try:
            root = manager.preview_root(request.query_params['workspace'], request.query_params.get('conversation'),
                                        request.query_params.get('run'))
            return Response(read_file(root, request.query_params['path']), media_type='application/pdf')
        except (ValueError, KeyError, StopIteration, OSError):
            return JSONResponse({'error': '文件不可用'}, 404)

    @app.post('/shutdown')
    async def shutdown():
        await manager.close()
        return {'closed': True}

    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-root', required=True)
    parser.add_argument('--port', type=int, default=0)
    args = parser.parse_args()
    root = Path(args.data_root).resolve()
    # Our development host never silently writes outside the authorized worktree.
    if os.environ.get('GK_DEVELOPMENT') == '1' and not root.is_relative_to(PROJECT):
        raise ValueError('开发数据必须位于当前工作树。')
    token = os.environ['GK_DESKTOP_TOKEN']
    sock = socket.socket()
    sock.bind(('127.0.0.1', args.port))
    sock.listen(128)
    manager = Manager(root)
    server = uvicorn.Server(uvicorn.Config(application(manager, token), log_level='error', access_log=False))
    print(json.dumps({'ready': True, 'port': sock.getsockname()[1]}), flush=True)
    asyncio.run(server.serve(sockets=[sock]))


if __name__ == '__main__':
    main()
