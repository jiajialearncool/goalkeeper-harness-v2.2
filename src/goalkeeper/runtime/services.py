"""Host resources and event callbacks. No host is allowed to replace Engine policy."""
import asyncio
import json
import os
import re
import signal
import socket
import sys
from urllib.parse import urlsplit
from urllib.request import getproxies

from ..storage import save_report
from ..tools import birth
from .files import safe_file


DEPENDENCY_INSTALL_MAX_SECONDS = 180
SYSTEM_INSTALL_IDLE_SECONDS = 90
SYSTEM_INSTALL_RESERVE_SECONDS = 120
SYSTEM_INSTALL_MAX_SECONDS = 720


class RuntimeServices:
    def __init__(self, *, event=None, applied=None, validate_workspace=None, internal_root=None,
                 search_secret=None, search_capability='unknown'):
        self._event = event
        self._applied = applied
        self._validate_workspace = validate_workspace
        self.internal_root = internal_root
        self._search_secret = search_secret
        self.search_capability = search_capability
        self.previews = []

    def emit(self, kind, data, persist=True):
        if self._event:
            self._event(kind, data, persist=persist)

    def applied(self, message_id):
        if self._applied:
            self._applied(message_id)

    def validate_workspace(self, root):
        if self._validate_workspace:
            self._validate_workspace(root)

    def search_secret(self):
        return self._search_secret() if self._search_secret else None

    async def start_preview(self, engine, args):
        await self.stop_previews()
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
        sock.close()
        root = safe_file(engine.root, args['path']) if args.get('path') else engine.root
        if not root.is_dir():
            raise ValueError('预览目录不存在。')
        argv = args.get('argv') or [sys.executable, '-m', 'goalkeeper.runtime.preview_server', str(port), str(root)]
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
            raise ValueError('预览命令必须是字符串数组。')
        runner = engine.tools.runner
        runner.scope.ports = [port]
        argv = [a.replace('{port}', str(port)) for a in argv]
        with (engine.store.directory / 'preview.log').open('ab') as log:
            process = await asyncio.create_subprocess_exec('/usr/bin/sandbox-exec', '-p', runner.profile(), *argv,
                cwd=root, env=runner.environment(), stdout=log, stderr=log, start_new_session=True)
        record = engine.store.directory / 'preview-process.json'
        save_report(record, {'pid': process.pid, 'birth': birth(process.pid), 'port': port})
        self.previews.append((process, record))
        for _ in range(50):
            if process.returncode is not None:
                raise ValueError('网页进程退出，请读取 preview.log 检查原因。')
            try:
                _, writer = await asyncio.open_connection('127.0.0.1', port)
                writer.close()
                await writer.wait_closed()
                url = f'http://127.0.0.1:{port}/'
                engine.emit('web_preview', {'url': url, 'source': 'task'})
                return {'url': url, 'port': port, 'notice': '进程在任务结束时停止，静态交付文件仍可预览。'}
            except OSError:
                await asyncio.sleep(.1)
        raise ValueError('网页未在获准端口启动。')

    async def stop_previews(self):
        while self.previews:
            process, record = self.previews.pop()
            if process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                    await asyncio.wait_for(process.wait(), 3)
                except (ProcessLookupError, TimeoutError):
                    if process.returncode is None:
                        os.killpg(process.pid, signal.SIGKILL)
                        await process.wait()
            record.unlink(missing_ok=True)

    async def install_dependencies(self, engine, args, timeout):
        packages = args['packages']
        if (not isinstance(packages, list) or not packages or any(not isinstance(p, str)
                or not re.fullmatch(r'(?:@\w[\w.-]*/)?\w[\w.-]*(?:[=@<>!~^]+[\w.*+,-]+)?', p) for p in packages)):
            raise ValueError('仅允许包名及版本，不允许地址、命令选项或脚本。')
        runner = engine.tools.runner
        if args['kind'] == 'python':
            argv = [sys.executable, '-m', 'pip', 'install', '--target', str(runner.home / 'site-packages'),
                    '--disable-pip-version-check', '--use-deprecated=legacy-certs', '--', *packages]
        elif args['kind'] == 'node':
            argv = ['npm', 'install', '--ignore-scripts', '--no-audit', '--no-fund', '--', *packages]
        elif args['kind'] == 'system':
            installer = getattr(runner, 'install_system_packages', None)
            if installer is None:
                raise ValueError('当前执行环境不支持安装系统软件包。')
            return await installer(
                packages,
                timeout=timeout,
                idle_seconds=SYSTEM_INSTALL_IDLE_SECONDS,
                reserve_seconds=SYSTEM_INSTALL_RESERVE_SECONDS,
                max_seconds=SYSTEM_INSTALL_MAX_SECONDS,
            )
        else:
            raise ValueError('安装类型仅支持 python/node/system。')
        profile = runner.profile() + '\n(allow network-outbound (remote tcp "*:443"))\n'
        profile += '(allow network-outbound (literal "/private/var/run/mDNSResponder"))\n'
        profile += '(allow mach-lookup (global-name "com.apple.SystemConfiguration.DNSConfiguration") '
        profile += '(global-name "com.apple.SystemConfiguration.configd") (global-name "com.apple.networkd"))\n'
        profile += '(allow file-read* (literal "/private/etc/resolv.conf") (literal "/private/etc/hosts"))\n'
        proxy_env = {}
        proxy = getproxies().get('https')
        if proxy:
            parsed = urlsplit(proxy)
            if parsed.scheme in {'http', 'https'} and parsed.hostname in {'127.0.0.1', 'localhost', '::1'}:
                endpoint = f'localhost:{parsed.port or (443 if parsed.scheme == "https" else 80)}'
                profile += f'(allow network-outbound (remote tcp {json.dumps(endpoint)}))\n'
                proxy_env['HTTPS_PROXY'] = proxy
        original_profile, original_environment = runner.profile, runner.environment
        runner.profile = lambda: profile
        runner.environment = lambda: {**original_environment(), **proxy_env}
        try:
            return await engine.tools.execute(
                'bash', {'argv': argv, 'timeout': min(DEPENDENCY_INSTALL_MAX_SECONDS, timeout)}, timeout
            )
        finally:
            runner.profile, runner.environment = original_profile, original_environment
