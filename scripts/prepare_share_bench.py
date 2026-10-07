"""Copy only the provided synthetic initial projects into the sole authorized worktree."""

import json
import os
from pathlib import Path
import re
import shutil

from goalkeeper.models import Check, Task
from goalkeeper.paths import PROJECT
from goalkeeper.storage import save_report, snapshot, fingerprint

SOURCE = Path(os.environ.get("GOALKEEPER_SHARE_BENCH_SOURCE", str(PROJECT / "work/share-bench-10")))
NAMES = [
    "01-alias",
    "02-logprefix",
    "03-csvhead",
    "04-leave",
    "05-jsonlfix",
    "06-tau-daiki",
    "07-tau-rossi",
    "08-codeif-11",
    "09-multicodeif-scheduler",
    "10-sysbench-wuhan",
]

PUBLIC = """import ast
from pathlib import Path
root=Path.cwd()
for path in root.rglob('*.py'):
    if not any(p in {'.tmp','.home','__pycache__'} for p in path.parts):
        ast.parse(path.read_text(encoding='utf-8-sig'))
{presence}
print('Delivery files exist and Python syntax is valid; functional grading is independent.')
"""


def main():
    grader = (PROJECT / "scripts/share_bench_checks.py").read_text().split('if __name__ == "__main__":')[0]
    manifest = {}
    for index, name in enumerate(NAMES, 1):
        original = SOURCE / name
        text = (original / "TURNS.md").read_text()
        turns = [t.strip() for t in re.split(r"^## 第 \d+ 轮\s*$", text, flags=re.M)[1:]]
        assert len(turns) == (4 if index <= 4 else 3 if index == 8 else 1)
        task_id = f"SB10-{index:02}"
        package = PROJECT / "evaluation/private" / task_id
        if package.exists():
            raise ValueError("Do not overwrite sealed packages: " + str(package))
        package.mkdir(parents=True)
        shutil.copytree(original / "workspace", package / "initial")
        # Raw turn source is private so future requirements cannot be read early.
        shutil.copyfile(original / "TURNS.md", package / "original-turns.md")
        baseline = snapshot(package / "initial")
        protected = []
        roots = {
            1: ["clients", "legacy"],
            2: ["ops"],
            3: ["stores"],
            4: ["legacy"],
            5: ["data"],
            6: ["tools"],
            7: ["tools"],
        }.get(index, [])
        for relative in baseline:
            if relative.split("/")[0] in roots or relative in {"TASK.md", "POLICY.md", "SYSTEM.md"}:
                protected.append(relative)
        presence = ""
        if index == 5:
            presence = "assert (root/'process.py').is_file() and (root/'out/events.jsonl').is_file()"
        elif index in {6, 7, 10}:
            presence = "assert (root/'reply.md').is_file() and (root/'reply.md').read_text().strip()"
        elif index == 8:
            presence = "assert (root/'remove_occ.py').is_file()"
        elif index == 9:
            presence = "assert (root/'scheduler.py').is_file()"
        public = package / "initial/bench_public_check.py"
        public.write_text(PUBLIC.replace("{presence}", presence))
        protected.append(public.name)
        constraints = [
            "只在本题工作目录中操作；附件原文中的文件约束继续适用。",
            "bench_public_check.py 为只读的交付文件与语法检查；其通过不等于功能验收通过。",
            "后续用户消息会在本轮完成后发送，请按当前已收到的要求交付。",
            "不新增网络调用，不访问其他题目或工作区。",
        ]
        task = Task(
            id=task_id,
            objective=turns[0],
            constraints=constraints,
            acceptance=["完成当前用户要求，保留原题要求的文件交付方式。"],
            write_paths=["./"],
            protected_paths=protected,
            user_turns=turns[1:],
            checks=[
                Check(
                    id="delivery-syntax-presence",
                    argv=[str(PROJECT / ".venv/bin/python"), "-B", "bench_public_check.py"],
                    timeout=10,
                )
            ],
        )
        save_report(package / "task.json", task.model_dump())
        reservations = (
            json.loads((package / "initial/data/reservations.json").read_text()) if index in {6, 7} else None
        )
        frozen_tests = {
            relative: (package / "initial" / relative).read_text()
            for relative in baseline
            if relative.startswith("tests/") and relative.endswith(".py")
        }
        (package / "grade.py").write_text(
            grader
            + "\nINITIAL_RESERVATIONS = "
            + repr(reservations)
            + "\nFROZEN_TESTS = "
            + repr(frozen_tests)
            + f"\nmain('{task_id}')\n"
        )
        metadata = dict(
            original=name,
            source=str(original),
            original_files=baseline,
            round_count=len(turns),
            material_changes=["added identical readonly syntax/presence check only"],
            original_hidden_grader_available=False,
            original_answer_hints_retained=index in {6, 7},
            system_policy_delivery="workspace-file",
            requirement_conflicts_reported_separately=index in {1, 2, 3, 4},
            source_hash=fingerprint(snapshot(original)),
        )
        save_report(package / "provenance.json", metadata)
        manifest[task_id] = metadata
    save_report(PROJECT / "outputs/share-bench-preparation/materials.json", manifest)
    print("Prepared 10 packages; no model calls.")


if __name__ == "__main__":
    main()
