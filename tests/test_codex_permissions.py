"""Pinned Codex sandbox regressions: local commands only, no model or login."""

import json
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

import pytest

from goalkeeper.codex import CODEX, ConfigurationError, python_link_read_rules, task_permissions
from goalkeeper.paths import PROJECT
from goalkeeper.sandbox import runtime_roots


@pytest.fixture
def local_root():
    parent = PROJECT / "work/offline-codex-tests"
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=parent) as directory:
        yield Path(directory)


def test_uv_link_permission_is_narrow_and_excludes_other_installations(local_root):
    installations = local_root / "uv/python"
    base = installations / "cpython-3.12.14-macos-aarch64-none"
    base.mkdir(parents=True)
    alias = installations / "cpython-3.12-macos-aarch64-none"
    alias.symlink_to(base, target_is_directory=True)
    sibling = installations / "cpython-3.11.12-macos-aarch64-none"
    sibling.mkdir()
    binary = local_root / "python"
    binary.symlink_to(alias / "bin/python3.12")
    rules = python_link_read_rules([base], binary, base)
    assert rules == {str(installations): "read", str(sibling): "deny"}
    assert python_link_read_rules([], binary, base) == {}
    unrelated = local_root / "unrelated"
    unrelated.mkdir()
    assert python_link_read_rules([unrelated], binary, unrelated) == {}


def test_helper_links_are_fixed_and_cannot_be_replaced(local_root):
    workspace = local_root / "task"
    workspace.mkdir()
    profile, env = task_permissions(workspace, runtime_roots())
    rules = tomllib.loads("profile=" + profile)["profile"]
    assert rules["network"] == {"enabled": False}
    assert rules["filesystem"][":root"] == "deny"
    assert rules["filesystem"][str(local_root / "codex-tool-bin")] == "read"
    assert str(local_root) not in rules["filesystem"]
    assert env["PATH"].split(":")[:2] == [str(local_root / "codex-tool-bin"), str(CODEX.parent)]
    link = local_root / "codex-tool-bin/apply_patch"
    link.unlink()
    link.symlink_to("/bin/sh")
    with pytest.raises(ConfigurationError, match="固定安装"):
        task_permissions(workspace, runtime_roots())


@pytest.mark.skipif(sys.platform != "darwin", reason="Pinned Codex Seatbelt regression requires macOS")
def test_native_codex_python_tools_and_boundaries_without_inference(local_root):
    workspace = local_root / "task"
    workspace.mkdir()
    readonly = local_root / "readonly"
    readonly.mkdir()
    (readonly / "probe.txt").write_text("synthetic-readonly")
    private = local_root / "private.txt"
    private.write_text("synthetic-private")
    (workspace / "escape").symlink_to(private)
    (workspace / ".git").mkdir()
    (workspace / ".git/config").write_text("synthetic-git")
    codex_home = local_root / "codex-home"
    codex_home.mkdir()
    interpreter = PROJECT / ".venv/bin/python"
    probe = workspace / "probe.py"
    probe.write_text(
        "import errno,json,shutil,socket,subprocess\nfrom pathlib import Path\n"
        f"assert shutil.which('apply_patch')=={str(local_root / 'codex-tool-bin/apply_patch')!r}\n"
        f"assert shutil.which('rg')=={str(CODEX.parent / 'rg')!r}\n"
        "p=subprocess.run(['git','rev-parse','--show-toplevel'],capture_output=True,text=True)\n"
        "assert p.returncode!=0 and 'not a git repository' in p.stderr,p.stderr\n"
        "def denied(action):\n"
        "    try: action()\n"
        "    except OSError as e: return e.errno in (errno.EPERM,errno.EACCES)\n"
        "    return False\n"
        "patch='*** Begin Patch\\n*** Add File: result.txt\\n+READY\\n*** End Patch\\n'\n"
        "p=subprocess.run(['apply_patch',patch],capture_output=True,text=True)\n"
        "assert p.returncode==0,(p.stdout,p.stderr)\n"
        "p=subprocess.run(['rg','--no-config','^READY$','result.txt'],capture_output=True,text=True)\n"
        "assert p.returncode==0 and p.stdout.strip()=='READY',(p.stdout,p.stderr)\n"
        "assert Path('result.txt').read_text()=='READY\\n'\n"
        f"assert Path({str(readonly / 'probe.txt')!r}).read_text()=='synthetic-readonly'\n"
        "sock=socket.socket()\n"
        "try:\n"
        "    checks={\n"
        f"        'readonly_write_denied':denied(lambda:Path({str(readonly / 'probe.txt')!r}).write_text('bad')),\n"
        f"        'private_read_denied':denied(lambda:Path({str(private)!r}).read_text()),\n"
        "        'symlink_escape_denied':denied(lambda:Path('escape').read_text()),\n"
        "        'git_write_denied':denied(lambda:Path('.git/config').write_text('bad')),\n"
        f"        'helper_write_denied':denied(lambda:Path({str(local_root / 'codex-tool-bin/new-tool')!r}).write_text('bad')),\n"
        "        'network_denied':denied(lambda:sock.connect(('127.0.0.1',9))),\n"
        "    }\n"
        "finally: sock.close()\n"
        "assert all(checks.values()),checks\n"
        "print(json.dumps({'python_passed':True,'apply_patch_passed':True,'rg_passed':True,**checks}))\n"
    )
    profile, environment = task_permissions(workspace, [*runtime_roots(), readonly])
    # Isolate CLI metadata as well. No app-server, auth, thread or turn command is used.
    environment["CODEX_HOME"] = str(codex_home)
    command = [
        str(CODEX), "-c", "permissions.goalkeeper-evaluation=" + profile,
        "-c", 'approval_policy="never"',
        "sandbox", "-P", "goalkeeper-evaluation", "-C", str(workspace),
        "--", str(interpreter), str(probe),
    ]
    result = subprocess.run(command, cwd=workspace, env=environment, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    checks = json.loads(result.stdout.strip().splitlines()[-1])
    assert all(checks.values())
    assert (readonly / "probe.txt").read_text() == "synthetic-readonly"
    assert (workspace / ".git/config").read_text() == "synthetic-git"
    assert private.read_text() == "synthetic-private"
    assert not (local_root / "codex-tool-bin/new-tool").exists()
