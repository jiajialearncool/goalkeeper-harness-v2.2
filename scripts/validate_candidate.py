"""Reference and critical counterexamples, never model attempts."""

import argparse
import asyncio
import json
import shutil
from pathlib import Path

from goalkeeper.evaluation import score
from goalkeeper.paths import PROJECT, inside_project
from goalkeeper.storage import save_report


async def validate(package, output):
    package, output = inside_project(package), inside_project(output)
    output.mkdir(parents=True, exist_ok=False)
    rows = []
    versions = {
        "reference": None,
        "mutates_input": ("service.py", "out=deepcopy(state)", "out=state"),
        "no_idempotency": ("service.py", "if batch_id in out['batches']:", "if False:"),
        "blank_erases_old": ("service.py", "r[k] or old[k]", "r[k]"),
        "wrong_revision": ("service.py", "revision!=out['tickets'][ident]['revision']", "False"),
        "allows_bool_revision": ("service.py", "type(revision) is not int", "not isinstance(revision,int)"),
        "direct_save": (
            "persistence.py",
            "os.replace(name,path)",
            "path.write_text('{}'); raise OSError('lost original')",
        ),
        "wrong_report": ("report.py", "'total':len(state['tickets'])", "'total':0"),
        "inline_cli_payload": ("cli.py", "with open(args.input,encoding='utf-8') as file: rows=json.load(file)", "rows=json.loads(args.input)"),
        "replay_reads_file": ("cli.py", "if args.batch_id not in state['batches']:\n                with open", "if True:\n                with open"),
        "replay_rewrites_database": ("cli.py", "if args.batch_id not in state['batches']: persistence.save", "if True: persistence.save"),
        "premature_replay_validation": ("service.py", "out=deepcopy(state)", "\n    if not isinstance(rows if 'rows' in locals() else changes,list): raise ValueError('early payload')\n    out=deepcopy(state)"),
        "initial_unimplemented": "initial",
    }
    for name, mutation in versions.items():
        root = output / "workspaces" / name
        if root.exists():
            raise ValueError("验证目录已存在，不能覆盖。")
        shutil.copytree(package / ("initial" if mutation == "initial" else "reference"), root)
        if isinstance(mutation, tuple):
            filename, old, new = mutation
            path = root / "tickets" / filename
            text = path.read_text()
            if old not in text:
                raise ValueError("mutation_not_applied")
            path.write_text(text.replace(old, new))
        result = await score(root, package / "grade.py", output / name)
        expected = name == "reference"
        rows.append(
            {
                "variant": name,
                "passed": result["passed"],
                "expected_pass": expected,
                "correct": result["passed"] == expected,
                "checks": result.get("checks", []),
            }
        )
    save_report(
        output / "report.json",
        {
            "kind": "offline_reference_and_counterexamples",
            "rows": rows,
            "all_correct": all(r["correct"] for r in rows),
            "real_model_calls": 0,
        },
    )
    print(
        json.dumps(
            {"all_correct": all(r["correct"] for r in rows), "variants": len(rows)}, ensure_ascii=False
        )
    )
    if not all(r["correct"] for r in rows):
        raise ValueError("reference_or_counterexample_failed")
    return rows


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--package', type=Path, default=PROJECT/'evaluation/private/C1-v2')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    await validate(args.package, args.output)


if __name__ == "__main__":
    asyncio.run(main())
