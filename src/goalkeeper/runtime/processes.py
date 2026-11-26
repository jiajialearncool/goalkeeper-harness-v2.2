"""Track inherited sandbox identities even after a child changes process groups.

SPI and filter values: WebKit's Source/WTF/wtf/spi/darwin/SandboxSPI.h.
Random Mach names are identity probes; no service is registered under them.
"""
import ctypes
import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path
from uuid import uuid4

from ..sandbox import Seatbelt
from ..storage import save_report
from .files import workspace_secret_rules


class Identity:
    def __init__(self, record):
        self.record = Path(record)
        self.lib = ctypes.CDLL('/usr/lib/libSystem.B.dylib')
        self.check = self.lib.sandbox_check
        self.check.restype = ctypes.c_int
        # Darwin ARM64 variadic arguments must NOT be declared as fixed parameters.
        self.check.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
        self.no_report = ctypes.c_int.in_dll(self.lib, 'SANDBOX_CHECK_NO_REPORT').value

    def query(self, pid, operation, kind, value):
        return self.check(pid, operation.encode(), kind | self.no_report, ctypes.c_char_p(str(value).encode()))

    def matches(self, pid, marker, probe):
        return (pid != os.getpid() and self.query(pid, 'mach-lookup', 2, marker) == 0
                and self.query(pid, 'file-read-data', 1, probe) == 0
                and self.query(pid, 'file-write-data', 1, self.record) == 1)

    def candidates(self, markers, probe):
        result = subprocess.run(['/bin/ps', '-axo', 'pid=,uid=,stat='], capture_output=True, text=True,
                                timeout=5, check=True)
        found = []
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) != 3 or fields[1] != str(os.getuid()) or fields[2].startswith('Z'):
                continue
            pid = int(fields[0])
            if any(self.matches(pid, marker, probe) for marker in markers):
                found.append(pid)
        return found

    def clean(self, markers, probe):
        deadline = time.monotonic() + 3
        while markers:
            pids = self.candidates(markers, probe)
            if not pids:
                return
            for pid in pids:
                if any(self.matches(pid, marker, probe) for marker in markers):
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            if time.monotonic() >= deadline:
                raise RuntimeError('任务子进程未清理完毕，已保留诊断记录。')
            time.sleep(.01)


def recover(record, allowed_root, allowed_cache=None):
    record = Path(record)
    data = json.loads(record.read_text())
    root = Path(data['workspace'])
    probe = Path(data['probe'])
    expected_home = Path(allowed_cache) / '.home' if allowed_cache is not None and data.get('cache_root') else root / '.home'
    if (root.is_symlink() or not root.resolve().is_relative_to(Path(allowed_root).resolve())
            or probe.parent != expected_home or probe.is_symlink() or expected_home.is_symlink()
            or (data.get('cache_root') and Path(data['cache_root']) != Path(allowed_cache or root))
            or not all(re.fullmatch(r'local\.goalkeeper\.task\.[a-f0-9]{32}', m) for m in data['markers'])):
        raise RuntimeError('任务进程诊断记录不在允许范围内，不能自动清理。')
    Identity(record).clean(data['markers'], probe)
    probe.unlink(missing_ok=True)
    record.unlink()


class TaskSeatbelt(Seatbelt):
    def __init__(self, scope, archive, directory, internal_root=None):
        super().__init__(scope, archive, internal_root)
        self.record = Path(directory) / 'desktop-processes.json'
        self.marker = 'local.goalkeeper.task.' + uuid4().hex
        self.markers = [self.marker]
        self.probe = self.home / ('.process-identity-' + uuid4().hex)
        self.scope.protected_files.append(self.probe)
        self.identity = Identity(self.record)
        self.prepared = False

    def prepare(self):
        if not self.prepared:
            self.probe.write_text('GoalKeeper task process identity\n')
            self.prepared = True
            self.persist()

    def persist(self):
        save_report(self.record, {'workspace': str(self.root), 'probe': str(self.probe), 'markers': self.markers,
                                 **({'cache_root': str(self.internal_root)} if self.internal_root != self.root else {})})

    def profile(self):
        self.prepare()
        return (super().profile() + '\n' + workspace_secret_rules(self.root)
                + f'\n(allow mach-lookup (global-name {json.dumps(self.marker)}))\n')

    async def run(self, argv, timeout=120):
        self.prepare()
        marker = 'local.goalkeeper.task.' + uuid4().hex
        self.markers.append(marker)
        self.persist()
        original = self.profile
        self.profile = lambda: original() + f'\n(allow mach-lookup (global-name {json.dumps(marker)}))\n'
        try:
            return await super().run(argv, timeout)
        finally:
            self.profile = original
            self.identity.clean([marker], self.probe)
            self.markers.remove(marker)
            self.persist()

    async def stop(self):
        await super().stop()
        if self.record.exists():
            self.identity.clean(self.markers, self.probe)
            self.record.unlink()
        self.probe.unlink(missing_ok=True)
