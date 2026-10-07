"""Independent contracts, error counterexamples and common-feedback lifecycle for four tasks."""

import importlib.util
import json
import shutil

import pytest

from goalkeeper.evaluation import score
from goalkeeper.models import RunConfig, Task
from goalkeeper.paths import PROJECT
from goalkeeper.sandbox import Seatbelt
from goalkeeper.storage import save_report, snapshot
from test_loop import call, fixture

IDS = ("B1-v2", "B3-v2", "B6-v2", "B11-v2")
MUTATIONS = {
    "B1-v1": [
        (
            "unknown_as_failure",
            "reason='unknown_profile';confirmed=False",
            "reason='profile_unavailable';confirmed=True",
        ),
        ("image_as_category", "'image':('spu',365)", "'image':('category',365)"),
        ("wrong_feedback_expiry", "r['at']-f['at']>=days*86400", "r['at']-f['at']>days*86400"),
        ("ad_as_organic", "items[0]['source']=='ad'", "False"),
    ],
    "B3-v1": [
        ("ignore_cohort_harm", "harmed.append(c)", "None"),
        ("count_all_user_events", "if query.get('unit','request')=='user':", "if False:"),
        ("zero_denominator_as_zero", "if not d:return None", "if not d:return [0,1]"),
        ("no_conflicting_duplicate", "if n['id'] in events and events[n['id']]!=n:", "if False:"),
    ],
    "B6-v1": [
        ("ignore_live_reference", "if any(k in refs for refs in s['consumers'].values()):", "if False:"),
        ("lose_restore_data", "s['data'][k]=s['backup'].pop(k)", "s['backup'].pop(k)"),
        ("finish_before_verify", "not f['verified']", "False"),
        ("no_revision", "envelope['expected_revision']!=s['revision']", "False"),
    ],
    "B11-v1": [
        ("reward_duplicates", "if key in seen:continue", "if False:continue"),
        ("ignore_reward_withdrawal", "if delta:s['ledger'].append", "if delta>0:s['ledger'].append"),
        ("skip_version_check", "r['version']!=a['expected_version']", "False"),
        ("round_reward_up", "rewards[r['id']]=amount//100", "rewards[r['id']]=(amount+99)//100"),
    ],
}


def preparation():
    spec = importlib.util.spec_from_file_location(
        "prepare_business_test", PROJECT / "scripts/prepare_business.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_business_reference_contracts_and_critical_mutations(tmp_path):
    prep = preparation()
    rows = []
    for task_id in IDS:
        package = PROJECT / "evaluation/private" / task_id
        before = snapshot(package)
        generated = tmp_path / "generated" / task_id
        assert prep.prepare(task_id, generated) == generated and snapshot(generated) == before
        with pytest.raises(ValueError, match="已存在"):
            prep.prepare(task_id, generated)
        for variant, old, new in [
            ("reference", None, None),
            ("initial", None, None),
            *MUTATIONS[task_id.replace("-v2", "-v1")],
        ]:
            root = tmp_path / task_id / variant / "workspace"
            shutil.copytree(package / ("initial" if variant == "initial" else "reference"), root)
            if old is not None:
                file = root / prep.SPECS[task_id]["package"] / "core.py"
                source = file.read_text()
                assert old in source, (task_id, variant)
                file.write_text(source.replace(old, new))
            verdict = await score(root, package / "grade.py", tmp_path / task_id / variant / "grading")
            assert verdict["passed"] is (variant == "reference"), (task_id, variant, verdict)
            assert verdict["snapshot_unchanged"]
            if variant == "reference":
                for p in (package / "initial").glob("public*.py"):
                    shutil.copyfile(p, root / p.name)
                from goalkeeper.sandbox import Scope, runtime_roots

                runner = Seatbelt(Scope(root, runtimes=runtime_roots()))
                task = Task.model_validate_json((package / "task.json").read_text())
                for check in task.checks + [task.feedback.probe] + task.feedback.checks:
                    result = await runner.run(check.argv, check.timeout)
                    assert result["exit_code"] == 0 and not result["timed_out"], (task_id, check, result)
            rows.append(
                {
                    "task": task_id,
                    "variant": variant,
                    "passed": verdict["passed"],
                    "checks": verdict.get("checks", []),
                }
            )
        assert snapshot(package) == before
    save_report(
        tmp_path / "business-counterexamples.json", {"model_calls": 0, "all_correct": True, "rows": rows}
    )


async def test_business_feedback_delivery_and_quality(tmp_path, tokens):
    from goalkeeper.native import objective
    from goalkeeper.provider import ScriptedProvider

    for task_id in IDS:
        package = PROJECT / "evaluation/private" / task_id
        task = Task.model_validate_json((package / "task.json").read_text())
        assert len(objective(task) + task.feedback.text) < 4000
        (tmp_path / task_id).mkdir()
        engine, _ = fixture(
            tmp_path / task_id,
            tokens,
            [],
            task=task,
            config=RunConfig(token_budget=None, summary_budget_fraction=None, summary_time_fraction=None),
        )
        shutil.copytree(package / "initial", engine.root, dirs_exist_ok=True)
        engine.tools.runner = Seatbelt(engine.tools.scope, tmp_path / task_id / "commands")
        files = [
            p
            for p in (package / "reference").rglob("*")
            if p.is_file() and str(p.relative_to(package / "reference")) not in task.protected_paths
        ]
        writes = [
            call(
                "write", {"path": str(p.relative_to(package / "reference")), "content": p.read_text()}, str(i)
            )
            for i, p in enumerate(files)
        ]
        engine.provider = ScriptedProvider(
            [
                writes,
                [
                    call(
                        "request_delivery",
                        {"summary": "offline authored business reference", "check_ids": [], "unresolved": []},
                        "delivery",
                    )
                ],
            ]
        )
        state = await engine.run()
        assert state.phase == "delivered" and state.feedback_delivered and state.goal_version == 2, (
            task_id,
            state.phase,
            state.stops,
        )
        verdict = await score(
            engine.store.directory / "delivery/snapshot", package / "grade.py", tmp_path / task_id / "grading"
        )
        assert verdict["passed"], (task_id, verdict)
        assert state.config.token_budget is None and state.config.seconds_budget == 1800
        assert state.config.summary_budget_fraction is state.config.summary_time_fraction is None
        assert json.loads((package / "contract.json").read_text())["human_duration_calibrated"] is False


async def test_business_artifacts_under_actual_task_permissions(tmp_path):
    import tomllib
    from goalkeeper.codex import task_permissions
    from goalkeeper.sandbox import Scope, runtime_roots

    commands = {"B1-v2": "diagnose", "B3-v2": "analyze", "B6-v2": "plan", "B11-v2": "apply"}
    for task_id in IDS:
        package = PROJECT / "evaluation/private" / task_id
        root = tmp_path / task_id / "workspace"
        shutil.copytree(package / "reference", root)
        task = Task.model_validate_json((package / "task.json").read_text())
        name = preparation().SPECS[task_id]["package"]
        # Exercise actual task-scoped permissions, including all protected paths.
        runner = Seatbelt(
            Scope(
                root,
                write_paths=task.write_paths,
                runtimes=runtime_roots(),
                protected_files=[root / p for p in task.protected_paths],
            ),
            archive=tmp_path / task_id / "commands",
        )
        destination = root / "artifacts/analysis.json"
        before = destination.read_bytes()
        destination.unlink()
        command = [
            str(PROJECT / ".venv/bin/python"),
            "-B",
            "-m",
            name + ".cli",
            commands[task_id],
            "data/sample.json",
            "--save",
            "artifacts/analysis.json",
        ]
        result = await runner.run(command, 30)
        assert result["exit_code"] == 0 and not result["timed_out"], (task_id, result)
        assert json.loads(destination.read_text()) == json.loads(before)
        assert json.loads(result["stdout"]) == json.loads(before)
        before = destination.read_bytes()
        failed = await runner.run(
            [*command[:5], "missing-input.json", "--save", "artifacts/analysis.json"], 30
        )
        assert failed["exit_code"] == 2 and not failed["stdout"] and destination.read_bytes() == before
        denied = await runner.run([*command[:-1], "unauthorized.json"], 30)
        assert denied["exit_code"] == 2 and not (root / "unauthorized.json").exists()
        code = f"from {name} import util\nfrom unittest.mock import patch\nwith patch('os.replace',side_effect=OSError('forced')):\n    try:util.save({{'new':1}},'artifacts/analysis.json')\n    except OSError:pass\n    else:raise AssertionError('replace failure must propagate')\n"
        names = set((root / "artifacts").iterdir())
        forced = await runner.run([str(PROJECT / ".venv/bin/python"), "-B", "-c", code], 30)
        assert forced["exit_code"] == 0 and destination.read_bytes() == before
        assert set((root / "artifacts").iterdir()) == names
        profile, _ = task_permissions(root, runtime_roots(), task.write_paths, task.protected_paths)
        permissions = tomllib.loads("policy=" + profile)["policy"]["filesystem"]
        assert permissions[str(root)] == "read"
        assert permissions[str(root / "artifacts")] == "write"
        assert permissions[str(root / "data/sample.json")] == "read"
        assert permissions[str(root / "tests/test_legacy.py")] == "read"
