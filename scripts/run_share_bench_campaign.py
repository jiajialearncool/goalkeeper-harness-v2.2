"""Authorized ten-question evaluation, each question using four parallel product loops."""

import argparse
import datetime
import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from goalkeeper.evaluation import execution_identity
from goalkeeper.paths import PROJECT, inside_project
from goalkeeper.readiness import package_identity, require_offline_readiness
from goalkeeper.storage import save_report

TASKS = tuple(f"SB10-{i:02}" for i in range(1,11))


def run_stage(out, stage, readiness=None, retries=3):
    command = [
        sys.executable,
        "-B",
        str(PROJECT / "scripts/run_real.py"),
        "--allow-real-model",
        "--unlimited-tokens",
        "--transient-retries",
        str(retries),
        "--mode",
        "connect" if stage == "CONNECT" else "candidate",
    ]
    if stage != "CONNECT":
        command += ["--candidate", stage, "--readiness", str(readiness)]
    save_report(
        out / "current-stage.json",
        dict(stage=stage, started_at=datetime.datetime.now(datetime.timezone.utc).isoformat()),
    )
    print("START " + stage, flush=True)
    with (out / (stage + ".log")).open("w") as stream:
        result = subprocess.run(
            command,
            cwd=PROJECT,
            env=dict(
                os.environ,
                PYTHONDONTWRITEBYTECODE="1",
                TIKTOKEN_CACHE_DIR=str(PROJECT / "work/tokenizer-cache"),
            ),
            stdout=stream,
            stderr=subprocess.STDOUT,
        )
    lines = (out / (stage + ".log")).read_text().splitlines()
    candidates = [
        inside_project(Path(line))
        for line in lines
        if line.startswith(str(PROJECT / "runtime/evaluations") + "/")
    ]
    if not candidates or not (candidates[-1] / "results.json").is_file():
        raise RuntimeError(stage + "没有完整结果；保留日志并停止，不自动重跑")
    directory = candidates[-1]
    data = json.loads((directory / "results.json").read_text())
    if result.returncode not in (0, 1):
        raise RuntimeError(stage + "异常退出；保留结果并停止")
    if any(row.get("core_program_error") or row.get("cohort_stop_reason") in
           {"core_program_error", "authentication_failure", "returned_model_mismatch", "quota_exceeded"}
           for row in data["results"]):
        raise RuntimeError(stage + "发生全组基础设施停止，保留结果并停止后续题目")
    print("FINISH " + stage + " " + str(directory), flush=True)
    return dict(
        directory=str(directory),
        exit_code=result.returncode,
        all_passed=all(row.get("passed") is True for row in data["results"]),
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--allow-real-model", action="store_true")
    parser.add_argument("--tasks", nargs="+", choices=TASKS, default=list(TASKS))
    parser.add_argument("--transient-retries", type=int, choices=range(4), default=3)
    args = parser.parse_args()
    if len(set(args.tasks)) != len(args.tasks):
        raise SystemExit("同一次运行不得重复题目")
    if not args.allow_real_model:
        raise SystemExit("需要用户明确授权后运行")
    identity = execution_identity(args.transient_retries)
    proof = require_offline_readiness(identity)
    out = (
        PROJECT
        / "outputs"
        / ("share-bench-ten-real-" + datetime.date.today().isoformat() + "-" + uuid4().hex[:10])
    )
    out.mkdir()
    save_report(
        out / "authorization-and-identity.json",
        dict(
            scope="User explicitly authorized evaluating the supplied ten tasks on all four groups; original turn text and answer hints retained, local disclosed grading rather than unavailable original hidden grading; no whole-task automatic reruns",
            selected_tasks=args.tasks,
            execution_identity=identity,
            packages=package_identity(),
            limits=dict(
                seconds_per_group_per_question=1800, token_limit=None, summary_limit=None, request_limit=None
            ),
            transport_policy=identity["transport_policy"],
            offline_readiness=proof,
        ),
    )
    save_report(out / "offline-readiness.json", proof)
    save_report(PROJECT / "outputs/share-bench-current-output.json", dict(directory=str(out)))
    cohorts = {}
    save_report(out / "cohorts.json", cohorts)
    try:
        cohorts["CONNECT"] = run_stage(out, "CONNECT", retries=args.transient_retries)
        save_report(out / "cohorts.json", cohorts)
        connectivity = Path(cohorts["CONNECT"]["directory"]) / "connectivity.json"
        if not cohorts["CONNECT"]["all_passed"] or not json.loads(connectivity.read_text()).get("all_passed"):
            raise RuntimeError("CONNECT未全部通过，停止后续题目")
        for task_id in args.tasks:
            if execution_identity(args.transient_retries) != identity:
                raise RuntimeError("封存后源码改变，停止")
            cohorts[task_id] = run_stage(out, task_id, connectivity, retries=args.transient_retries)
            save_report(out / "cohorts.json", cohorts)
        save_report(out / "current-stage.json", dict(stage="completed", automatic_reruns=0))
    except Exception as error:
        save_report(
            out / "current-stage.json",
            dict(stage="stopped", error=type(error).__name__ + ":" + str(error), automatic_reruns=0),
        )
        raise
    print(str(out), flush=True)


if __name__ == "__main__":
    main()
