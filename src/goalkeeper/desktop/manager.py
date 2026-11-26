import asyncio
import hashlib
import copy
import json
import os
import re
import shutil
import signal
import socket
import sys
from pathlib import Path
from uuid import uuid4

import tiktoken

from ..auth import AuthBroker
from ..cost import Tokens, totals
from ..models import utcnow, State
from ..paths import PROJECT, PRIVATE
from ..provider import ResponsesProvider
from ..secrets import redact
from ..storage import Store, save_report
from ..tools import birth
from .providers import ModelSettings, ChatAPIProvider
from ..runtime.provider_catalog import is_account_provider
from ..runtime.session import Runtime, EventStore
from .files import desktop_snapshot, importable, read_file, safe_file




class Manager:
    def __init__(self, data_root, provider=None, auth=None):
        self.root = Path(data_root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.root.chmod(0o700)
        self.path = self.root / 'index.json'
        self.index = json.loads(self.path.read_text()) if self.path.exists() else {
            'version': 1, 'settings': {'minutes': 30}, 'workspaces': [], 'conversations': []}
        self.index['settings'].setdefault('permissionMode', 'workspace_write')
        self.index['settings'].setdefault('fontSize', 16)
        private = Path(os.environ['GK_AUTH_PRIVATE']) if os.environ.get('GK_AUTH_PRIVATE') else (
            PRIVATE if self.root.is_relative_to(PROJECT) else self.root / 'private')
        self.auth = auth or AuthBroker(private)
        self.account_auths = ({} if auth is not None else {
            'chatgpt_account': AuthBroker(private / 'third-party-accounts' / 'chatgpt'),
        })
        self.provider_factory = provider or (lambda: ResponsesProvider(self.auth))
        self.injected_provider = provider is not None
        self.jobs, self.engines, self.preview_processes, self.active_states = {}, {}, {}, {}
        self.deleting_workspaces = set()
        self.search_capability = 'unknown'
        self.account_key = str(self.root)
        self.model_settings = ModelSettings(self)
        os.environ.setdefault('TIKTOKEN_CACHE_DIR', str(PROJECT / 'work/tokenizer-cache'))
        self.tokens = Tokens(tiktoken.get_encoding('o200k_base'))
        self.runtime = Runtime(self, self.tokens)
        # Earlier desktop builds persisted empty placeholders. They have no execution history.
        self.index['conversations'] = [c for c in self.index['conversations']
                                       if c.get('messages') or c.get('events') or c.get('runs')]
        for c in self.index['conversations']:
            if c.get('status') == 'running':
                c['status'] = 'interrupted'
            self.review_legacy_completion(c)
            # Legacy events are immutable. The UI associates missing run IDs
            # with the preceding run_started event without rewriting history.
        self.persist()
        self.reconcile_previews()
        self.reconcile_commands()

    def resolve_provider(self, config):
        if self.injected_provider or config.provider_id == 'account':
            return self.provider_factory()
        if is_account_provider(config.provider_id):
            broker = self.account_auths.get(config.provider_id)
            if not broker or not broker.status().get('connected'):
                raise ValueError('该执行使用的第三方账号已断开，请重新连接后继续。')
            return ResponsesProvider(broker)
        secret = self.model_settings.secret(config.provider_id)
        if not secret:
            raise ValueError('该执行使用的模型密钥已移除，请重新配置后继续。')
        return ChatAPIProvider(config.provider_id, secret)

    def search_secret(self):
        import keyring
        return keyring.get_password('GoalKeeper Desktop Search', self.account_key)

    def review_legacy_completion(self, c):
        return self.runtime.review_legacy_completion(c)

    @staticmethod
    def scope_messages(c):
        return Runtime.scope_messages(c)

    def persist(self):
        save_report(self.path, self.index)

    def ensure_default_workspace(self):
        identifier = self.index['settings'].get('defaultWorkspaceId')
        workspace = next((w for w in self.index['workspaces'] if w['id'] == identifier), None)
        if workspace is None:
            workspace = next((w for w in self.index['workspaces'] if w['name'] == '默认工作区'
                              and w.get('mode', 'managed') == 'managed'), None)
        if workspace is None:
            workspace = self.create_workspace('默认工作区')
        self.files_root(workspace['id'])  # Reuse the directory; never copy another workspace's files.
        if identifier != workspace['id']:
            self.index['settings']['defaultWorkspaceId'] = workspace['id']
            self.persist()
        return workspace

    def update_settings(self, values):
        if not values or set(values) - {'minutes', 'permissionMode', 'fontSize'}:
            raise ValueError('不支持的通用设置。')
        if 'minutes' in values and (type(values['minutes']) not in {int, float}
                                   or not 1 <= values['minutes'] <= 1440):
            raise ValueError('任务时长应在 1 至 1440 分钟之间。')
        if 'fontSize' in values and (type(values['fontSize']) is not int or not 10 <= values['fontSize'] <= 22):
            raise ValueError('字号应为 10 至 22 之间的整数。')
        if 'permissionMode' in values and values['permissionMode'] != 'workspace_write':
            raise ValueError('当前仅支持在工作区内修改。')
        self.index['settings'].update(values)
        self.persist()
        return self.index['settings']

    def conversation(self, identifier):
        return next(c for c in self.index['conversations'] if c['id'] == identifier)

    @staticmethod
    def conversation_summary(c):
        fields = ('id', 'workspace_id', 'title', 'status', 'created_at', 'sequence', 'cost', 'seconds', 'manual_title')
        return {key: c[key] for key in fields if key in c}

    @staticmethod
    def page_args(offset=0, limit=100):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError('分页参数无效。')
        return offset, limit

    def list_conversations(self, query='', wid=None, offset=0, limit=100):
        offset, limit = self.page_args(offset, limit)
        if not isinstance(query, str) or len(query) > 1000:
            raise ValueError('搜索内容无效。')
        if wid:
            self.workspace(wid)
        chats = [c for c in reversed(self.index['conversations']) if (not wid or c['workspace_id'] == wid)
                 and (not query or query.casefold() in c['title'].casefold()
                      or any(query.casefold() in m['text'].casefold() for m in c.get('messages', [])))]
        return {'items': [self.conversation_summary(c) for c in chats[offset:offset + limit]],
                'total': len(chats), 'next_offset': offset + limit if offset + limit < len(chats) else None}

    def conversation_detail(self, cid):
        c = self.conversation(cid)
        return {key: value for key, value in c.items() if key != 'events'}

    def events_page(self, cid, after=0, limit=100):
        self.page_args(after, limit)
        c = self.conversation(cid)
        events = [e for e in c['events'] if e['sequence'] > after][:limit]
        return {'events': events, 'sequence': c['sequence'],
                'next_after': events[-1]['sequence'] if events else after,
                'has_more': bool(events and events[-1]['sequence'] < c['sequence'])}

    def rename_conversation(self, cid, title):
        c = self.conversation(cid)
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= 500:
            raise ValueError('请输入 1 至 500 字的会话名称。')
        before = copy.deepcopy(c)
        c.update(title=title.strip(), manual_title=True)
        try:
            self.persist()
        except OSError:
            c.clear()
            c.update(before)
            raise
        return self.conversation_summary(c)

    async def delete_conversation(self, cid):
        c = self.conversation(cid)
        if cid in self.jobs or c['workspace_id'] in self.deleting_workspaces:
            raise ValueError('会话正在执行或工作区正在删除，请稍后重试。')
        await self.stop_previews(cid)
        await self.stop_previews(cid, 'static')
        archive = self.root / 'trash' / uuid4().hex
        archive.mkdir(parents=True, mode=0o700)
        source = self.root / 'conversations' / cid
        target = archive / 'conversations' / cid
        before = self.index['conversations']
        moved = False
        try:
            save_report(archive / 'manifest.json', {'conversation': c, 'deleted_at': utcnow()})
            if source.exists():
                if source.is_symlink() or not source.resolve().is_relative_to(self.root):
                    raise ValueError('会话目录边界异常，未删除。')
                target.parent.mkdir(parents=True)
                os.replace(source, target)
                moved = True
            self.index['conversations'] = [item for item in before if item['id'] != cid]
            self.persist()
        except Exception:
            self.index['conversations'] = before
            if moved:
                os.replace(target, source)
            shutil.rmtree(archive)
            raise
        return {'deleted': True, 'recoverable': True, 'archive_id': archive.name}

    def list_deleted_conversations(self):
        records = []
        directory = self.root / 'trash'
        if not directory.exists():
            return records
        for archive in directory.iterdir():
            if archive.is_symlink() or not re.fullmatch(r'[0-9a-f]{32}', archive.name):
                continue
            manifest = archive / 'manifest.json'
            if manifest.is_symlink() or not manifest.is_file():
                continue
            data = json.loads(manifest.read_text())
            if data.get('conversation') and not data.get('restored_at') and not any(
                    c['id'] == data['conversation']['id'] for c in self.index['conversations']):
                records.append({'archive_id': archive.name, 'deleted_at': data['deleted_at'],
                                'conversation': self.conversation_summary(data['conversation'])})
        return records

    def restore_conversation(self, archive_id):
        if not isinstance(archive_id, str) or not re.fullmatch(r'[0-9a-f]{32}', archive_id):
            raise ValueError('会话归档标识无效。')
        archive = self.root / 'trash' / archive_id
        manifest = archive / 'manifest.json'
        if archive.is_symlink() or manifest.is_symlink() or not manifest.is_file():
            raise ValueError('会话归档不存在。')
        data = json.loads(manifest.read_text())
        c = data.get('conversation')
        if not c or data.get('restored_at') or any(item['id'] == c['id'] for item in self.index['conversations']):
            raise ValueError('会话无法恢复，或已经恢复。')
        self.workspace(c['workspace_id'])
        source = archive / 'conversations' / c['id']
        target = self.root / 'conversations' / c['id']
        if target.exists() or source.is_symlink():
            raise ValueError('会话目录冲突，未恢复。')
        moved = False
        before = self.index['conversations']
        try:
            if source.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(source, target)
                moved = True
            self.index['conversations'] = [*before, c]
            self.persist()
        except Exception:
            self.index['conversations'] = before
            if moved:
                os.replace(target, source)
            raise
        # The atomically saved live index is the recovery commit; the original archive remains immutable.
        return self.conversation_summary(c)

    def file_references(self, cid, workspace_id=None):
        existing = next((c for c in self.index['conversations'] if c['id'] == cid), None)
        if existing:
            c = existing
        elif workspace_id and re.fullmatch(r'[0-9a-f]{32}', cid):
            self.workspace(workspace_id)
            c = {'workspace_id': workspace_id}
        else:
            raise ValueError('文件引用无效。')
        return [{'id': hashlib.sha256((cid + '\0' + f['path']).encode()).hexdigest(), **f}
                for f in self.list_files(c['workspace_id'])]

    def validate_references(self, cid, refs, workspace_id=None):
        if not isinstance(refs, list) or len(refs) > 100 or any(not isinstance(r, str) for r in refs):
            raise ValueError('文件引用无效。')
        allowed = {f['id']: f['path'] for f in self.file_references(cid, workspace_id)}
        if any(ref not in allowed for ref in refs):
            raise ValueError('文件引用不属于当前会话，或文件已不存在。')
        return [allowed[ref] for ref in refs]

    def run_detail(self, cid, run_id):
        c = self.conversation(cid)
        if run_id not in c['runs']:
            raise ValueError('执行记录不属于当前会话。')
        state = Store(self.root / 'conversations' / cid / 'runs' / run_id, State).load()
        # Request bodies and archives can contain private model content; expose measured metadata only.
        keys = ('role', 'model', 'effort', 'status', 'started_at', 'finished_at', 'usage', 'error',
                'error_detail', 'retry_attempt')
        return {'run_id': run_id, 'phase': state.phase,
                'requests': [{k: call[k] for k in keys if k in call} for call in state.calls],
                'operations': state.operations, 'delivery': state.delivery,
                'cost': totals(state.calls), 'seconds': state.active_seconds}

    def workspace(self, identifier):
        return next(w for w in self.index['workspaces'] if w['id'] == identifier)

    def files_root(self, identifier):
        w = self.workspace(identifier)
        if w.get('mode') != 'directory':
            return self.root / 'workspaces' / identifier / 'files'
        root = self.validate_directory(w['directory'], identifier)
        stat = root.stat()
        if [stat.st_dev, stat.st_ino] != w['directory_identity']:
            raise ValueError('工作区文件夹已被替换，请重新选择并绑定。')
        return root

    def recorded_root(self, w):
        return Path(w['directory']) if w.get('mode') == 'directory' else self.root / 'workspaces' / w['id'] / 'files'

    def workspace_cache(self, wid):
        return self.root / 'workspace-cache' / wid if self.workspace(wid).get('mode') == 'directory' else None

    def validate_directory(self, directory, wid=None):
        if not isinstance(directory, str) or not Path(directory).is_absolute():
            raise ValueError('请从本地文件夹选择器选择工作区。')
        raw = Path(directory)
        root = raw.resolve()
        if raw != root or not root.is_dir():
            raise ValueError('工作区必须是实际存在的文件夹，不能使用符号链接。')
        local = Path((os.environ.get('GK_DESKTOP_LOCAL_TREE') or str(PROJECT))).resolve()
        protected = [PROJECT / name for name in ('src', 'configs', 'scripts', 'evaluation', 'work', '.venv', 'desktop')]
        protected += [local / name for name in ('src', 'configs', 'scripts', 'evaluation', 'work', '.venv', 'desktop', 'docs/isolation')]
        protected += [PRIVATE, Path(os.environ.get('GK_AUTH_PRIVATE', str(self.root / 'private'))),
                      Path.home() / '.ssh', Path.home() / '.aws', Path.home() / '.codex',
                      Path.home() / 'Documents/ChatGPT/harness',
                      Path.home() / '.codex/worktrees/aaa4',
                      Path.home() / 'Documents/Codex/2026-09-30']
        # Our managed working folders are inside .codex, but other chats and program resources are not available.
        protected = [p.resolve() for p in protected if p != Path.home() / '.codex']
        if any(root == p or p.is_relative_to(root) or root.is_relative_to(p) for p in protected):
            raise ValueError('不能将程序资源、账号目录或其他版本的项目作为工作区。')
        if (root.is_relative_to(Path.home() / '.codex') and not root.is_relative_to(local)
                and not root.is_relative_to(self.root / 'workspace-folders')):
            raise ValueError('不能使用其他 Codex 会话或配置目录。')
        allowed_managed = self.root / 'workspaces' / wid / 'files' if wid else None
        if (root.is_relative_to(self.root) and not root.is_relative_to(self.root / 'workspace-folders')
                and root != allowed_managed):
            raise ValueError('不能将应用账号、历史或运行目录作为工作区。')
        for other in self.index['workspaces']:
            target = self.recorded_root(other).resolve()
            if other['id'] != wid and (root == target or root.is_relative_to(target) or target.is_relative_to(root)):
                raise ValueError('工作区文件夹与已有工作区重叠，请使用已有工作区或独立文件夹。')
        return root

    def event(self, cid, kind, data, persist=True):
        c = self.conversation(cid)
        c['sequence'] = c.get('sequence', 0) + 1
        event = {'sequence': c['sequence'], 'at': utcnow(), 'kind': kind, 'data': redact(data)}
        c.setdefault('events', []).append(event)
        if persist:
            self.persist()
        return event

    def applied(self, cid, mid):
        for m in self.conversation(cid)['messages']:
            if m['id'] == mid:
                m['status'] = 'applied'
        self.event(cid, 'message_applied', {'id': mid})

    def create_workspace(self, name, directory=None):
        if not isinstance(name, str) or not name.strip():
            raise ValueError('工作区名称不能为空。')
        if directory is not None:
            if not isinstance(directory, str):
                raise ValueError('请选择有效本地文件夹。')
            for existing in self.index['workspaces']:
                if self.recorded_root(existing) == Path(directory):
                    self.files_root(existing['id'])
                    return existing
            selected = self.validate_directory(directory)
        w = {'id': uuid4().hex, 'name': name.strip(), 'created_at': utcnow()}
        if directory is not None:
            stat = selected.stat()
            w.update(mode='directory', directory=str(selected), directory_identity=[stat.st_dev, stat.st_ino])
        self.index['workspaces'].append(w)
        if directory is None:
            self.files_root(w['id']).mkdir(parents=True)
        self.persist()
        return w

    async def bind_workspace(self, wid, directory):
        w = self.workspace(wid)
        if wid in self.deleting_workspaces or any(c['workspace_id'] == wid and c['id'] in self.jobs
                                                 for c in self.index['conversations']):
            raise ValueError('工作区正在执行或删除，请先停止任务。')
        root = self.validate_directory(directory, wid)
        self.deleting_workspaces.add(wid)
        try:
            for c in self.index['conversations']:
                if c['workspace_id'] == wid:
                    await self.stop_previews(c['id'])
                    await self.stop_previews(c['id'], 'static')
            await self.stop_previews('workspace-' + wid, 'static')
            before = dict(w)
            stat = root.stat()
            w.update(mode='directory', directory=str(root), directory_identity=[stat.st_dev, stat.st_ino])
            try:
                self.persist()
            except OSError:
                w.clear()
                w.update(before)
                raise
        finally:
            self.deleting_workspaces.discard(wid)
        return w

    def rename_workspace(self, wid, name):
        w = self.workspace(wid)
        if wid in self.deleting_workspaces:
            raise ValueError('工作区正在删除，请稍后重试。')
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 120:
            raise ValueError('请输入 1 至 120 字的工作区名称。')
        before = w['name']
        w['name'] = name.strip()
        try:
            self.persist()
        except OSError:
            w['name'] = before
            raise
        return w

    async def delete_workspace(self, wid):
        self.workspace(wid)
        if wid in self.deleting_workspaces:
            raise ValueError('工作区正在删除，请稍后重试。')
        self.deleting_workspaces.add(wid)
        try:
            return await self._delete_workspace(wid)
        finally:
            self.deleting_workspaces.discard(wid)

    async def _delete_workspace(self, wid):
        w = self.workspace(wid)
        chats = [c for c in self.index['conversations'] if c['workspace_id'] == wid]
        if any(c['id'] in self.jobs for c in chats):
            raise ValueError('工作区正在执行任务，请先停止任务再删除。')
        for c in chats:
            await self.stop_previews(c['id'])
            await self.stop_previews(c['id'], 'static')
        await self.stop_previews('workspace-' + wid, 'static')
        archive = self.root / 'trash' / uuid4().hex
        archive.mkdir(parents=True, mode=0o700)
        before_workspaces, before_chats = self.index['workspaces'], self.index['conversations']
        moved = []
        try:
            save_report(archive / 'manifest.json', {'workspace': w, 'conversations': chats, 'deleted_at': utcnow()})
            managed = self.root / 'workspaces' / wid
            sources = [*([managed] if w.get('mode') != 'directory' or not self.recorded_root(w).is_relative_to(managed) else []),
                       self.root / 'workspace-cache' / wid,
                       *(self.root / 'conversations' / c['id'] for c in chats)]
            for source in sources:
                if not source.exists() and not source.is_symlink():
                    continue
                if source.is_symlink() or not source.resolve().is_relative_to(self.root):
                    raise ValueError('工作区目录边界异常，未删除。')
                target = archive / source.relative_to(self.root)
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(source, target)
                moved.append((source, target))
            self.index['workspaces'] = [item for item in before_workspaces if item['id'] != wid]
            self.index['conversations'] = [item for item in before_chats if item['workspace_id'] != wid]
            self.persist()
        except Exception:
            self.index['workspaces'], self.index['conversations'] = before_workspaces, before_chats
            for source, target in reversed(moved):
                os.replace(target, source)
            shutil.rmtree(archive)
            raise
        return {'deleted': True, 'recoverable': True}

    def create_conversation(self, wid, identifier=None):
        self.workspace(wid)
        if wid in self.deleting_workspaces:
            raise ValueError('工作区正在删除，请稍后重试。')
        identifier = identifier or uuid4().hex
        if not isinstance(identifier, str) or not re.fullmatch(r'[0-9a-f]{32}', identifier) or any(
                c['id'] == identifier for c in self.index['conversations']):
            raise ValueError('新会话标识无效或已存在。')
        c = {'id': identifier, 'workspace_id': wid, 'title': '新会话', 'status': 'idle',
             'messages': [], 'events': [], 'runs': [], 'sequence': 0, 'created_at': utcnow()}
        self.index['conversations'].append(c)
        self.persist()
        return c

    def start_conversation(self, wid, identifier, text, mid, display_text=None):
        self.workspace(wid)
        if not isinstance(text, str) or not text.strip() or len(text) > 100_000:
            raise ValueError('请输入有效任务，单条消息不能超过 100000 字符。')
        if not isinstance(mid, str) or not 1 <= len(mid) <= 128:
            raise ValueError('消息标识无效。')
        existing = next((c for c in self.index['conversations'] if c['id'] == identifier), None)
        if existing:
            if existing['workspace_id'] != wid:
                raise ValueError('会话不属于所选工作区。')
            return self.send(identifier, text, mid, display_text=display_text)
        if any(c['workspace_id'] == wid and c['id'] in self.jobs for c in self.index['conversations']):
            raise ValueError('同一工作区另一个会话正在执行，请先停止它。')
        c = self.create_conversation(wid, identifier)
        try:
            return self.send(c['id'], text, mid, display_text=display_text)
        except Exception:
            if not c['messages'] and not c['events'] and not c['runs']:
                self.index['conversations'].remove(c)
                self.persist()
            raise

    def import_plan(self, sources, wid=None):
        files, skipped, already = [], [], []
        root = self.files_root(wid) if wid else None
        for raw in sources:
            source = Path(raw)
            if source.is_symlink() or not importable(source.name):
                skipped.append(str(source))
                continue
            if not source.exists():
                raise ValueError('选择的文件已不存在。')
            if root and source.resolve().is_relative_to(root):
                safe_file(root, str(source.relative_to(root)))
                already.append(str(source))
                continue
            candidates = source.rglob('*') if source.is_dir() else [source]
            for f in candidates:
                relative = f.relative_to(source.parent)
                if f.is_symlink() or any(not importable(p) for p in relative.parts):
                    skipped.append(str(relative))
                elif f.is_file():
                    files.append({'source': str(f), 'relative': str(relative), 'size': f.stat().st_size})
                if len(files) > 10_000:
                    raise ValueError('单次导入超过 10000 个文件，请选择更小的目录。')
        if sum(f['size'] for f in files) > 250_000_000:
            raise ValueError('单次导入超过 250MB，请选择所需文件。')
        return {'files': files, 'skipped': skipped, 'bytes': sum(f['size'] for f in files), 'already_in_workspace': already}

    def import_files(self, wid, sources):
        if wid in self.deleting_workspaces:
            raise ValueError('工作区正在删除，请稍后重试。')
        if any(c['workspace_id'] == wid and c['id'] in self.jobs for c in self.index['conversations']):
            raise ValueError('工作区正在执行，请停止后导入文件。')
        root = self.files_root(wid)
        plan = self.import_plan(sources, wid)
        mapping, imported = {}, []
        for f in plan['files']:
            rel = Path(f['relative'])
            first = rel.parts[0]
            if first not in mapping:
                candidate, suffix = first, 2
                while (root / candidate).exists() or candidate in mapping.values():
                    stem, ext = os.path.splitext(first)
                    candidate = f'{stem}-导入{suffix}{ext}'
                    suffix += 1
                mapping[first] = candidate
            dest_rel = str(Path(mapping[first], *rel.parts[1:]))
            dest = safe_file(root, dest_rel)
            source = Path(f['source'])
            if source.is_symlink() or any(p.is_symlink() for p in source.parents):
                raise ValueError('导入来源含符号链接，文件未授权。')
            dest.parent.mkdir(parents=True, exist_ok=True)
            with source.open('rb') as src, dest.open('xb') as out:
                shutil.copyfileobj(src, out)
            imported.append(dest_rel)
        # Return controlled relative paths for the composer, including files already in this workspace.
        # This is input material; importing it is not a request to open the output preview panel.
        existing = [Path(p).resolve() for p in plan['already_in_workspace']]
        referenced = imported + [f['path'] for f in self.list_files(wid)
                                 if any((root / f['path']) == p or (root / f['path']).is_relative_to(p)
                                        for p in existing)]
        return {'imported': imported, 'skipped': plan['skipped'], 'already_in_workspace': plan['already_in_workspace'],
                'referenced': list(dict.fromkeys(referenced))}

    def preview_root(self, wid, cid=None, run_id=None):
        if not run_id:
            return self.files_root(wid)
        self.workspace(wid)
        if not cid:
            raise ValueError('请选择当前会话的交付版本。')
        conversation = self.conversation(cid)
        if conversation['workspace_id'] != wid or run_id not in conversation['runs']:
            raise ValueError('交付版本与当前会话或工作区不一致。')
        store = Store(self.root / 'conversations' / cid / 'runs' / run_id, State)
        state = store.load()
        if not state.delivery:
            raise ValueError('该次执行尚未保存交付版本。')
        root = store.directory / 'delivery/snapshot'
        if desktop_snapshot(root) != state.delivery['snapshot']:
            raise ValueError('交付版本文件已变化，不能作为原交付内容打开。')
        return root

    def list_files(self, wid, cid=None, run_id=None):
        root = self.preview_root(wid, cid, run_id)
        return [{'path': name, 'size': (root / name).stat().st_size} for name in desktop_snapshot(root)]

    def export_preview(self, wid, name, cid=None, run_id=None):
        root = self.preview_root(wid, cid, run_id)
        data = read_file(root, name)
        directory = self.root / 'exports' / uuid4().hex
        directory.mkdir(parents=True, mode=0o700)
        target = directory / Path(name).name
        target.write_bytes(data)
        target.chmod(0o400)
        return {'path': str(target)}

    async def preview_bytes(self, wid, name, cid=None, run_id=None):
        from .document_preview import preview_bytes, OfficePreview
        if not hasattr(self, '_office_preview'):
            self._office_preview = OfficePreview(self.root / 'previews/office')
        return await preview_bytes(self.preview_root(wid, cid, run_id), name, self._office_preview)

    async def preview_document(self, wid, name, cid=None, run_id=None):
        root = self.preview_root(wid, cid, run_id)
        if name.lower().endswith('.png'):
            import base64
            from .images import png_bytes
            encoded = base64.b64encode(read_file(root, name, 8 * 1024 * 1024)).decode()
            png_bytes(encoded)
            return {'type': 'image', 'url': 'data:image/png;base64,' + encoded}
        file = safe_file(root, name)
        if not file.is_file() or file.stat().st_size > 20_000_000:
            raise ValueError('文件不存在或超过单次预览大小。')
        from ..sandbox import runtime_roots
        from .permissions import DesktopScope, PreviewSeatbelt
        scope = DesktopScope(root, writable=False, runtimes=runtime_roots(), extra_read=[PROJECT / 'src'])
        directory = self.root / 'previews' / uuid4().hex
        directory.mkdir(parents=True)
        runner = PreviewSeatbelt(scope, internal_root=directory / 'sandbox')
        original = runner.environment
        runner.environment = lambda: {**original(), 'PYTHONPATH': str(PROJECT / 'src')}
        runner.archive = directory
        try:
            result = await runner.run([sys.executable, '-m', 'goalkeeper.runtime.documents', str(file)], 30)
            raw = Path(result['stdout_file']).read_text()
            if len(raw) > 2_000_000:
                raise ValueError('内容超过预览大小，请通过任务按需读取。')
            return json.loads(raw) if raw else {'error': '预览失败。'}
        finally:
            await runner.stop()

    def latest_store(self, cid):
        c = self.conversation(cid)
        return EventStore(self.root / 'conversations' / cid / 'runs' / c['runs'][-1], self, cid)

    def send(self, cid, text, mid, display_text=None):
        return self.runtime.submit_message(cid, text, mid, display_text)

    def new_run(self, *args, **kwargs):
        return self.runtime.start(*args, **kwargs)

    def launch(self, *args, **kwargs):
        return self.runtime.launch(*args, **kwargs)

    async def execute(self, *args, **kwargs):
        return await self.runtime.execute(*args, **kwargs)

    async def stop(self, cid):
        return await self.runtime.cancel(cid)

    def continue_run(self, cid):
        return self.runtime.continue_task(cid)

    async def start_preview(self, engine, args, emit=True, mode='task'):
        await self.stop_previews(engine.conversation, mode)
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
        sock.close()
        engine.tools.scope.ports = [port]
        root = safe_file(engine.root, args.get('path', '')) if args.get('path') else engine.root
        if not root.is_dir():
            raise ValueError('预览目录不存在。')
        argv = args.get('argv') or [sys.executable, '-m', 'goalkeeper.runtime.preview_server', str(port), str(root)]
        if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
            raise ValueError('预览命令必须是字符串数组。')
        argv = [a.replace('{port}', str(port)) for a in argv]
        runner = engine.tools.runner
        log = (engine.store.directory / 'preview.log').open('ab')
        try:
            process = await asyncio.create_subprocess_exec('/usr/bin/sandbox-exec', '-p', runner.profile(), *argv,
                                                          cwd=root, env=runner.environment(), stdout=log,
                                                          stderr=log, start_new_session=True)
        finally:
            log.close()
        record = {'pid': process.pid, 'birth': birth(process.pid), 'conversation': engine.conversation,
                  'port': port}
        self.preview_processes[(engine.conversation, mode)] = process
        save_report(engine.store.directory / 'preview-process.json', record)
        for _ in range(50):
            if process.returncode is not None:
                raise ValueError('网页进程退出，请读取 preview.log 检查原因。')
            try:
                _, writer = await asyncio.open_connection('127.0.0.1', port)
                writer.close()
                await writer.wait_closed()
                url = f'http://127.0.0.1:{port}/'
                if emit:
                    self.event(engine.conversation, 'web_preview', {'url': url, 'source': 'task'})
                return {'url': url, 'port': port, 'notice': '进程在任务结束时停止，静态交付文件仍可预览。'}
            except OSError:
                await asyncio.sleep(.1)
        raise ValueError('网页未在获准端口启动。')

    async def stop_previews(self, cid, mode='task'):
        if process := self.preview_processes.pop((cid, mode), None):
            if process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                    await asyncio.wait_for(process.wait(), 3)
                except (ProcessLookupError, TimeoutError):
                    if process.returncode is None:
                        os.killpg(process.pid, signal.SIGKILL)
                        await process.wait()
        c = next((c for c in self.index['conversations'] if c['id'] == cid), None)
        if mode == 'task' and c and c['runs']:
            (self.latest_store(cid).directory / 'preview-process.json').unlink(missing_ok=True)
        if mode == 'static':
            (self.root / 'previews' / cid / 'preview-process.json').unlink(missing_ok=True)

    def reconcile_previews(self):
        for f in [*self.root.glob('conversations/*/runs/*/preview-process.json'),
                  *self.root.glob('previews/*/preview-process.json')]:
            record = json.loads(f.read_text())
            if record.get('birth') and birth(record['pid']) == record['birth']:
                try:
                    os.killpg(record['pid'], signal.SIGKILL)
                except ProcessLookupError:
                    pass
            f.unlink()

    def reconcile_commands(self):
        from .processes import recover
        for record in self.root.glob('conversations/*/runs/*/desktop-processes.json'):
            state = Store(record.parent, State).load()
            cid = record.parent.parent.parent.name
            c = self.conversation(cid)
            cached = self.root / 'workspace-cache' / c['workspace_id']
            recover(record, Path(state.workspace), cached)
        for f in self.root.glob('conversations/*/runs/*/processes.json'):
            record = json.loads(f.read_text())
            if record.get('birth') and birth(record['pid']) == record['birth']:
                try:
                    os.killpg(record['pid'], signal.SIGKILL)
                except ProcessLookupError:
                    pass
            f.unlink()

    async def static_preview(self, wid, cid, name, run_id=None):
        from types import SimpleNamespace
        from ..sandbox import runtime_roots
        from .permissions import DesktopScope, PreviewSeatbelt
        root = self.preview_root(wid, cid, run_id)
        target = safe_file(root, name)
        if not target.is_file() or target.suffix.lower() not in {'.html', '.htm'}:
            raise ValueError('请选择工作区中的 HTML 文件。')
        if cid and self.conversation(cid)['workspace_id'] != wid:
            raise ValueError('会话与工作区不一致。')
        preview_key = cid or 'workspace-' + wid
        scope = DesktopScope(root, writable=False, runtimes=runtime_roots(), extra_read=[PROJECT / 'src'])
        directory = self.root / 'previews' / preview_key
        directory.mkdir(parents=True, exist_ok=True)
        runner = PreviewSeatbelt(scope, internal_root=directory / 'sandbox')
        original = runner.environment
        runner.environment = lambda: {**original(), 'PYTHONPATH': str(PROJECT / 'src')}
        engine = SimpleNamespace(root=root, conversation=preview_key, tools=SimpleNamespace(scope=scope, runner=runner),
                                 store=SimpleNamespace(directory=directory))
        value = await self.start_preview(engine, {}, emit=False, mode='static')
        from urllib.parse import quote
        value['url'] += quote(str(target.relative_to(root)))
        if cid:
            self.event(cid, 'web_preview', {'url': value['url'], 'run_id': run_id, 'path': name, 'source': 'file'})
        return value


    async def close(self):
        for cid in list(self.jobs):
            await self.stop(cid)
        for cid, mode in list(self.preview_processes):
            await self.stop_previews(cid, mode)
        await self.auth.close()
        for broker in self.account_auths.values():
            await broker.close()

