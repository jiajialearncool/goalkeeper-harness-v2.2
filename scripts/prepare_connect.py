"""Prepare the small CONNECT package without model calls or account access."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from goalkeeper.models import Task
from goalkeeper.paths import PROJECT, inside_project

PUBLIC_CHECK = '''from pathlib import Path

path = Path("result.txt")
if path.is_symlink() or not path.is_file() or path.read_bytes() != b"READY":
    raise SystemExit("result.txt must contain exactly READY, without a newline")
print("CONNECT_CHECK_PASSED")
'''


def prepare(package: Path | None = None) -> Path:
    package = inside_project(package or PROJECT / "evaluation/private/connect")
    python = str(PROJECT / ".venv/bin/python")
    task = Task(
        id="CONNECT",
        objective="使用工具把 result.txt 写成精确的 READY，真实运行规定检查，然后正常结束并说明结果。",
        constraints=[
            "只允许修改 result.txt；不得修改或删除 public_check.py。",
            "result.txt 必须是 UTF-8 文本，精确包含 READY，不含换行或其他字符。",
            f"必须通过命令工具运行：{python} public_check.py；检查失败时修复后再检查。",
            "禁止公网访问，不安装依赖，不启动其他任务。",
        ],
        acceptance=["result.txt 精确包含 READY。", "规定检查真实执行且通过，程序正常完成交付。"],
        write_paths=["result.txt"],
        checks=[{"id": "connect_ready", "argv": [python, "public_check.py"], "timeout": 15}],
        protected_paths=["public_check.py"],
        preferences={"language": "中文"},
    )
    expected = hashlib.sha256(PUBLIC_CHECK.encode()).hexdigest()
    grader = '''import hashlib
import json
from pathlib import Path

result = Path("result.txt")
check = Path("public_check.py")
ready = result.is_file() and not result.is_symlink() and result.read_bytes() == b"READY"
protected = (check.is_file() and not check.is_symlink()
             and hashlib.sha256(check.read_bytes()).hexdigest() == EXPECTED)
print(json.dumps({"passed": ready and protected,
                  "checks": [{"id": "exact_ready", "passed": ready},
                             {"id": "protected_check", "passed": protected}]}))
'''.replace("EXPECTED", repr(expected))
    files = {
        "task.json": task.model_dump_json(indent=2) + "\n",
        "initial/result.txt": "",
        "initial/public_check.py": PUBLIC_CHECK,
        "grade.py": grader,
    }
    # Existing packages are not silently overwritten, including any unexpected files.
    if package.exists():
        actual = {str(p.relative_to(package)) for p in package.rglob("*") if p.is_file()}
        if actual != set(files) or any((package / name).is_symlink() or
                                      (package / name).read_text() != content
                                      for name, content in files.items()):
            raise ValueError("CONNECT 目录已有不同内容，拒绝覆盖。")
        return package
    for name, content in files.items():
        target = package / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return package


if __name__ == "__main__":
    package = prepare()
    print(json.dumps({"package": str(package), "real_model_calls": 0}, ensure_ascii=False))
