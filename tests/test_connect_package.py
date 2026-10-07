"""The connectivity grader must reject unfinished or altered task artifacts."""

import importlib.util
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from goalkeeper.models import Task
from goalkeeper.paths import PROJECT
from goalkeeper.evaluation import connectivity_tools, prescribed_check_command

CHECK = str(PROJECT / ".venv/bin/python") + " public_check.py"


def load_prepare():
    spec = importlib.util.spec_from_file_location("prepare_connect", PROJECT / "scripts/prepare_connect.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.prepare


def test_connect_artifact_and_protected_check(tmp_path):
    package = load_prepare()(tmp_path / "connect")
    initial = package / "initial"
    task = Task.model_validate_json((package / "task.json").read_text())
    assert task.feedback is None

    def score():
        result = subprocess.run([sys.executable, str(package / "grade.py")], cwd=initial,
                                capture_output=True, text=True, check=True)
        return json.loads(result.stdout)

    assert not score()["passed"]
    (initial / "result.txt").write_text("READY\n")
    assert not score()["passed"]
    (initial / "result.txt").write_text("READY")
    check = subprocess.run(task.checks[0].argv, cwd=initial, capture_output=True, text=True)
    assert check.returncode == 0 and "CONNECT_CHECK_PASSED" in check.stdout
    assert score()["passed"]
    (initial / "public_check.py").write_text("print('fake pass')\n")
    assert not score()["passed"]
    with pytest.raises(ValueError, match="拒绝覆盖"):
        load_prepare()(package)


def test_connect_rejects_symbolic_result(tmp_path):
    package = load_prepare()(tmp_path / "connect")
    initial = package / "initial"
    target = tmp_path / "ready.txt"
    target.write_text("READY")
    (initial / "result.txt").unlink()
    (initial / "result.txt").symlink_to(target)
    result = subprocess.run([sys.executable, str(package / "grade.py")], cwd=initial,
                            capture_output=True, text=True, check=True)
    assert not json.loads(result.stdout)["passed"]


@pytest.mark.parametrize("group,events", [
    ("pi", [{"type": "tool_execution_start", "toolCallId": "one", "args": {"command": CHECK}},
            {"type": "tool_execution_end", "toolCallId": "one", "toolName": "bash", "isError": False,
             "result": {"content": [{"type": "text", "text": "CONNECT_CHECK_PASSED"}]}}]),
    ("codex", [{"method": "item/completed", "params": {"item": {"type": "commandExecution", "exitCode": 0,
               "command": CHECK, "aggregatedOutput": "CONNECT_CHECK_PASSED"}}}]),
    ("opencode", [{"type": "native_messages", "messages": [{"parts": [{"type": "tool", "tool": "bash",
                  "state": {"status": "completed", "input": {"command": CHECK},
                            "output": "CONNECT_CHECK_PASSED"}}]}]}]),
])
def test_connect_requires_completed_native_check_not_assistant_text(tmp_path, group, events):
    path = tmp_path / "native-events.json"
    path.write_text(json.dumps([{"type": "assistant", "text": "CONNECT_CHECK_PASSED"}]))
    assert not connectivity_tools(group, None, tmp_path)["check_execution_verified"]
    path.write_text(json.dumps(events))
    assert connectivity_tools(group, None, tmp_path) == {"completed_tools": 1, "check_execution_verified": True}


def test_connect_goalkeeper_requires_actual_check_result(tmp_path):
    state = SimpleNamespace(operations=[{"status": "completed", "name": "write"}], evidence=[])
    assert not connectivity_tools("goalkeeper", state, tmp_path)["check_execution_verified"]
    state.evidence.append({"id": "connect_ready", "passed": True, "result": {"stdout": "CONNECT_CHECK_PASSED"}})
    assert connectivity_tools("goalkeeper", state, tmp_path)["check_execution_verified"]


@pytest.mark.parametrize("command,passed", [
    (CHECK, True), ("printf READY > result.txt && " + CHECK, True),
    ("/bin/zsh -c " + __import__('shlex').quote("od result.txt\n" + CHECK), True),
    ("apply_patch <<'PATCH'\n*** Begin Patch\n*** End Patch\nPATCH\n" + CHECK, True),
    ("cat public_check.py", False), ("echo " + __import__('shlex').quote(CHECK), False),
    ("cat <<'EOF'\n" + CHECK + "\nEOF\nprintf CONNECT_CHECK_PASSED", False),
    ("python public_check.py", False), ("/other/python public_check.py", False),
])
def test_connect_check_requires_prescribed_invocation(command, passed):
    assert prescribed_check_command(command, [str(PROJECT / ".venv/bin/python"), "public_check.py"]) == passed


def test_reading_check_source_with_marker_is_not_execution(tmp_path):
    (tmp_path / "native-events.json").write_text(json.dumps([
        {"method": "item/completed", "params": {"item": {"type": "commandExecution", "exitCode": 0,
         "command": "cat public_check.py", "aggregatedOutput": "print('CONNECT_CHECK_PASSED')"}}}
    ]))
    assert not connectivity_tools("codex", None, tmp_path)["check_execution_verified"]
