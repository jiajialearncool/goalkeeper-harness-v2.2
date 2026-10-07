from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import time
from pathlib import Path
from uuid import uuid4

from .cost import Tokens, totals
from .engine import Engine
from .models import RunConfig, State, Task
from .paths import DATA, PROJECT, inside_project
from .storage import Store, save_report, snapshot


def create(task, config, source, run_id=None):
    inside_project(source)
    if not source.is_dir():
        raise ValueError("初始项目目录不存在，未调用模型。")
    for protected in task.protected_paths:
        if not (source / protected).is_file():
            raise ValueError("规定检查文件不存在。")
    reserve = sum(c.timeout for c in task.checks) + config.delivery_save_seconds
    if task.feedback:
        reserve += sum(c.timeout for c in task.feedback.checks) + task.feedback.probe.timeout
    if reserve >= config.seconds_budget:
        raise ValueError("时间上限不足以容纳必需检查和保存，未调用模型。")
    run = inside_project(DATA / "runs" / (run_id or uuid4().hex))
    if run.exists():
        raise ValueError("运行目录已经存在，不能覆盖历史。")
    files = snapshot(source)
    root = run / "workspace"
    root.mkdir(parents=True)
    for relative in files:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / relative, path)
    for protected in task.protected_paths:
        if not (root / protected).is_file():
            raise ValueError("规定检查文件不存在。")
    now = time.time()
    state = State(id=run.name, workspace=str(root), task=task, config=config,
                  started_at=now, deadline=now+config.seconds_budget)
    store = Store(run)
    store.save(state, "prepared", {"initial_snapshot": files, "check_reserve_seconds": reserve})
    return state, store


async def execute(args):
    if not args.allow_real_model:
        raise ValueError("真实模型调用尚未授权；需要第二阶段授权与 --allow-real-model。")
    from .auth import AuthBroker
    from .provider import ResponsesProvider
    import tiktoken

    if args.command == "resume":
        store = Store(inside_project(Path(args.run)))
        state = store.load()
    else:
        task = Task.model_validate_json(inside_project(Path(args.task)).read_text())
        config = RunConfig.model_validate_json(inside_project(Path(args.config)).read_text())
        state, store = create(task, config, inside_project(Path(args.source)))
    os.environ["TIKTOKEN_CACHE_DIR"] = str(PROJECT / "work/tokenizer-cache")
    provider = ResponsesProvider(AuthBroker(DATA / "private"))
    try:
        engine = Engine(state, store, provider, Tokens(tiktoken.get_encoding("o200k_base")))
        await engine.run(resume=args.command == "resume")
        print(json.dumps({"run": str(store.directory), "phase": state.phase, "cost": totals(state.calls)}, ensure_ascii=False))
    finally:
        await provider.close()
        await provider.auth.close()


def main():
    parser = argparse.ArgumentParser(description="GoalKeeper 简洁编码循环")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--task", required=True)
    run.add_argument("--source", required=True)
    run.add_argument("--config", default=str(PROJECT / "configs/default.json"))
    run.add_argument("--allow-real-model", action="store_true")
    resume = sub.add_parser("resume")
    resume.add_argument("--run", required=True)
    resume.add_argument("--allow-real-model", action="store_true")
    report = sub.add_parser("report")
    report.add_argument("--run", required=True)
    login = sub.add_parser("login")
    login.add_argument("--port", type=int, default=8892)
    args = parser.parse_args()
    if args.command == "report":
        store = Store(inside_project(Path(args.run)))
        state = store.load()
        value = {"phase": state.phase, "first_stop_reason": state.first_stop_reason,
                 "active_seconds": state.active_seconds, "active_seconds_includes_model_wait": True,
                 "wall_seconds": (state.delivery or {}).get("at", state.ended_at or time.time())-state.started_at,
                 "delivery": state.delivery, "cost": totals(state.calls)}
        save_report(store.directory / "report.json", value)
        print(json.dumps(value, ensure_ascii=False, indent=2))
    elif args.command == "login":
        from .login import serve
        serve(args.port)
    else:
        asyncio.run(execute(args))


if __name__ == "__main__":
    main()
