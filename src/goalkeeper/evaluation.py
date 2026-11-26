from __future__ import annotations

import asyncio
import json
import shlex
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx

from .cli import create
from .cost import Tokens
from .runtime.factory import create_engine
from .models import RunConfig, Task
from .lifecycle import finish_cleanup
from .native import Native, objective
from .native_log import native_events
from .paths import PROJECT, PRIVATE, inside_project
from .provider import ResponsesProvider
from .sandbox import Scope, Seatbelt, runtime_roots
from .storage import fingerprint, save_report, snapshot
from .transport import MeteredTransport

GROUPS = ("goalkeeper", "codex", "opencode", "pi")


async def join(tasks):
    return await asyncio.gather(*tasks, return_exceptions=True)


async def score(workspace, grader, directory):
    before = snapshot(workspace)
    runner = Seatbelt(
        Scope(workspace, writable=False, runtimes=runtime_roots(), extra_read=[grader]),
        archive=directory / "grading-output",
    )
    result = await runner.run([str(PROJECT / ".venv/bin/python"), str(grader)], 120)
    try:
        value = json.loads(Path(result["stdout_file"]).read_text())
        if not isinstance(value, dict):
            raise ValueError("invalid_grader_output")
    except ValueError:
        value = {"passed": False, "error": "invalid_grader_output"}
    value["passed"] = value.get("passed") is True and result["exit_code"] == 0 and not result["timed_out"]
    value["snapshot_unchanged"] = snapshot(workspace) == before
    value["passed"] = value["passed"] and value["snapshot_unchanged"]
    save_report(directory / "independent-score.json", value)
    return value


async def execute_until_stopped(operation, transport, run_id, deadline, grace_seconds):
    """Let accepted work settle; bound native retry/backoff after admission closes."""
    worker = asyncio.create_task(operation)
    async def watch():
        if hasattr(transport, "wait_stopped"):
            await transport.wait_stopped(run_id)
        else:
            await transport.stop.wait()
        await transport.wait_settled(run_id, max(.01, deadline-time.time()))
        await asyncio.sleep(min(grace_seconds, max(0, deadline-time.time())))
    watcher = asyncio.create_task(watch())
    try:
        await asyncio.wait([worker, watcher], return_when=asyncio.FIRST_COMPLETED)
        if not worker.done():
            stopped = transport.stopped_reason(run_id) if hasattr(transport, "stopped_reason") else transport.first_reason
            reason = "cohort_" + (stopped or "stopped")
            worker.cancel(reason)
            await finish_cleanup(join([worker]))
            return False
        return await worker
    except asyncio.CancelledError:
        worker.cancel("outer_cancel")
        await finish_cleanup(join([worker]))
        raise
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)


async def parallel(jobs, core_failure=None):
    # Jobs are started before any one is awaited. A normal individual failure is not a cohort kill.
    tasks = [asyncio.create_task(job()) for job in jobs]
    if core_failure is None:
        core_failure = asyncio.Event()
    results = asyncio.gather(*tasks, return_exceptions=True)
    watcher = asyncio.create_task(core_failure.wait())
    try:
        await asyncio.wait([results, watcher], return_when=asyncio.FIRST_COMPLETED)
        if watcher.done() and core_failure.is_set():
            for task in tasks:
                if not task.done():
                    task.cancel()
        return await results
    except asyncio.CancelledError:
        for task in tasks:
            if not task.done():
                task.cancel("outer_cancel")
        await finish_cleanup(join(tasks))
        raise
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)


def execution_identity(transient_retries=0):
    sources = {
        str(path.relative_to(PROJECT)): path.read_bytes().hex()
        for base in ("src", "configs", "scripts")
        for path in (PROJECT / base).rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }
    locks = {
        name: fingerprint((PROJECT / name).read_text())
        for name in ("pyproject.toml", "uv.lock", "work/native-tools/package.json", "work/native-tools/package-lock.json")
    }
    return {
        "source_hash": fingerprint(sources),
        "locks": locks,
        "model": "gpt-6.1-sol",
        "effort": "xhigh",
        "groups": list(GROUPS),
        "transport_policy": {"buffer_until_completed": True,
                             "transient_retries_per_request": transient_retries,
                             "failure_scope": "run",
                             "retry_scope": "request"},
    }


def freeze(directory, package, transient_retries=0):
    manifest = {**execution_identity(transient_retries), "package_hash": fingerprint(snapshot(package)), "at": time.time()}
    save_report(directory / "freeze.json", manifest)
    return manifest


def verified_models(calls):
    completed = [call for call in calls if call.get("status") == "completed"]
    recovered = {call.get("request_id") for call in completed if call.get("forwarded") is True}
    return bool(completed) and all(
        call.get("model") == "gpt-6.1-sol" and call.get("effort") == "xhigh"
        and call.get("returned_model") in {None, "gpt-6.1-sol"}
        and (call.get("returned_model") == "gpt-6.1-sol" if call.get("status") == "completed" else
             call.get("retry_scheduled") is True and call.get("request_id") is not None
             and call["request_id"] in recovered)
        for call in calls)


def prescribed_check_command(command, argv):
    """Recognize a literal invocation, not a mention, echo or here-document body.

    Unsupported shell expansions are not evidence. CONNECT uses a fixed, literal
    argv; native command records and their successful output are checked together.
    """
    try:
        parts = shlex.split(command)
        if parts and Path(parts[0]).name in {"sh", "bash", "zsh"}:
            index = next((i for i, p in enumerate(parts) if p in {"-c", "-lc"}), None)
            if index is None:
                return False
            command = parts[index + 1]
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<\n")
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        lines, line = [], []
        for token in lexer:
            if token and set(token) == {"\n"}:
                lines.append(line)
                line = []
            else:
                line.append(token)
        lines.append(line)
        delimiters = []
        for line in lines:
            if delimiters:
                if line == [delimiters[0]]:
                    delimiters.pop(0)
                continue
            for i, token in enumerate(line[:-1]):
                if token == "<<":
                    delimiters.append(line[i + 1])
            segment = []
            for token in [*line, ";"]:
                if token in {";", "&&", "||"}:
                    if segment == list(argv):
                        return True
                    segment = []
                else:
                    segment.append(token)
        return False
    except (ValueError, IndexError, TypeError):
        return False


def connectivity_tools(group, state, directory):
    """Require completed native tool evidence, not an assistant's claim of success."""
    marker = "CONNECT_CHECK_PASSED"
    argv = [str(PROJECT / ".venv/bin/python"), "public_check.py"]
    if group == "goalkeeper":
        tools = [o for o in state.operations if o.get("status") == "completed"
                 and o.get("name") in {"read", "write", "edit", "bash"}]
        checked = any(e.get("id") == "connect_ready" and e.get("passed")
                      and marker in e.get("result", {}).get("stdout", "")
                      for e in state.evidence)
        return {"completed_tools": len(tools), "check_execution_verified": checked}
    events = native_events(directory)
    completed, checked, starts = 0, False, {}
    for event in events:
        if group == "pi":
            if event.get("type") == "tool_execution_start":
                starts[event.get("toolCallId")] = event.get("args", {})
            if event.get("type") == "tool_execution_end" and not event.get("isError", True):
                completed += 1
                command = starts.get(event.get("toolCallId"), {}).get("command", "")
                output = event.get("result", {}).get("content", [])
                checked |= event.get("toolName") == "bash" and prescribed_check_command(command, argv) and any(
                    part.get("type") == "text" and marker in part.get("text", "") for part in output)
        elif group == "codex" and event.get("method") == "item/completed":
            item = event.get("params", {}).get("item", {})
            if item.get("type") == "commandExecution" and item.get("exitCode") == 0:
                completed += 1
                checked |= prescribed_check_command(item.get("command", ""), argv) and marker in item.get("aggregatedOutput", "")
            elif item.get("type") == "fileChange" and item.get("status") == "completed":
                completed += 1
        elif group == "opencode" and event.get("type") == "native_messages":
            for message in event.get("messages", []):
                for part in message.get("parts", []):
                    tool = part.get("state", {})
                    if part.get("type") == "tool" and tool.get("status") == "completed":
                        completed += 1
                        checked |= part.get("tool") == "bash" and prescribed_check_command(tool.get("input", {}).get("command", ""), argv) and marker in tool.get("output", "")
    return {"completed_tools": completed, "check_execution_verified": checked}


async def evaluate(package, authorized=False, shared_limit=None, readiness=None, connectivity=False,
                   transient_retries=0, offline_readiness=None, unlimited_tokens=False):
    if (authorized is not True or type(unlimited_tokens) is not bool
            or not (unlimited_tokens and shared_limit is None or
                    not unlimited_tokens and type(shared_limit) is int and shared_limit > 0)):
        raise ValueError("先确认题目、协议和新增额度；当前阶段禁止真实评测。")
    if not connectivity:
        if readiness is None or not inside_project(readiness).is_file():
            raise ValueError("缺少四组真实接入通过记录，禁止启动候选题。")
        proof = json.loads(readiness.read_text())
        if (proof.get("kind") != "four_native_connectivity" or not proof.get("all_passed")
                or proof.get("passed_groups") != list(GROUPS)):
            raise ValueError("四组接入未全部通过。")
        if proof.get("execution_identity") != execution_identity(transient_retries):
            raise ValueError("接入通过后源码、配置或产品锁文件发生变化，禁止沿用旧接入记录。")
    from .readiness import require_offline_readiness
    require_offline_readiness(execution_identity(transient_retries), offline_readiness, package)
    import tiktoken

    package = inside_project(package)
    task = Task.model_validate_json((package / "task.json").read_text())
    config = RunConfig.model_validate_json((PROJECT / "configs/default.json").read_text())
    if unlimited_tokens and any(value is not None for value in
            (config.token_budget, config.summary_budget_fraction, config.summary_time_fraction)):
        raise ValueError("不限 token 模式要求整题和摘要额度全部关闭，未调用模型。")
    config = config.model_copy(update={"effort": "xhigh", "task_profile": "general", "adaptive_effort": False, "transient_retries": transient_retries})
    config.require_evaluation_model()
    # Validate all four task preparations before opening an authenticated route.
    if max(len(objective(task, i) + ("\n用户追加要求：" + task.feedback.text if task.feedback else ""))
           for i in range(len(task.user_turns) + 1)) > 4000:
        raise ValueError("共同目标超过Codex原生Goal限制，未调用模型。")
    directory = PROJECT / "runtime/evaluations" / uuid4().hex
    directory.mkdir(parents=True)
    transport = MeteredTransport(
        PRIVATE,
        directory / "transport-usage.json",
        total_limit=shared_limit,
        per_run_limit=config.token_budget,
        transient_retries=transient_retries,
        resilient=True,
    )
    encoding = tiktoken.get_encoding("o200k_base")
    states = {}
    for group in GROUPS:
        state, store = create(
            task.model_copy(deep=True), config, package / "initial", run_id=directory.name + "-" + group
        )
        states[group] = (state, store)
    frozen = freeze(directory, package, transient_retries)
    try:
        await transport.start()
    except BaseException:
        try:
            await finish_cleanup(transport.close())
        finally:
            save_report(directory / "startup-error.json", {"stage": "local_transport_start", "model_calls": 0})
        raise
    jobs = []
    core_failure = asyncio.Event()
    for group in GROUPS:
        state, store = states[group]
        base = transport.register(SimpleNamespace(id=state.id, deadline=state.deadline,
                                                  config=SimpleNamespace(engine=group)))
        route_token = base["LOCAL_ROUTE_TOKEN"]
        base = base["DIAGNOSTIC_TRANSPORT_BASE"]

        async def job(group=group, state=state, store=store, base=base, route_token=route_token):
            cleanup_complete = True
            grace = config.delivery_save_seconds + sum(c.timeout for c in task.active_checks(True))
            if group == "goalkeeper":
                # This client's only change is authenticated transport destination, not its independent loop.
                class LocalAuth:
                    async def access_token(self):
                        return route_token

                # Avoid global endpoint mutation: a dedicated request-rewriting HTTP transport preserves wire history.
                original = httpx.AsyncClient(timeout=config.seconds_budget, trust_env=False)

                class LocalClient:
                    def stream(self, method, url, **kwargs):
                        return original.stream(method, base + "/responses", **kwargs)

                    async def aclose(self):
                        await original.aclose()

                provider = ResponsesProvider(LocalAuth(), http=LocalClient())
                engine = create_engine(state, store, provider, Tokens(encoding), transport_manages_retries=True)
                try:
                    await execute_until_stopped(engine.run(), transport, state.id, state.deadline, grace)
                finally:
                    await provider.close()
                normal = state.phase == "delivered"
                if state.phase == "program_error":
                    transport.pause("core_program_error")
                    result = {"group": group, "passed": False, "normal_delivery": False,
                              "stop_reason": state.first_stop_reason, "core_program_error": True,
                              "feedback_delivered": state.feedback_delivered}
                    save_report(store.directory / "result.json", result)
                    core_failure.set()
                    return result
            else:
                native = Native(
                    group,
                    store.directory,
                    task,
                    Path(state.workspace),
                    base,
                    route_token,
                    seconds=max(.01, state.deadline - time.time()),
                    tokens=config.token_budget,
                )
                normal = await execute_until_stopped(native.run(), transport, state.id, state.deadline, grace)
                native_result = json.loads((store.directory / "native-result.json").read_text())
                normal = native_result.get("normal_end", normal) is True
                cleanup_complete = native_result.get("cleanup_complete", False)
                state.feedback_delivered = native.feedback_delivered
                state.turn_index = native.turn_index
                state.phase = "delivered" if normal else "stopped"
                state.ended_at = time.time()
                store.save(state, "native_finished")
            execution_ended = time.time()
            accounting_settled = True
            try:
                await transport.wait_settled(state.id, max(.01, state.deadline - time.time()))
            except TimeoutError:
                accounting_settled = False
            accounting_ended = time.time()
            source = (
                store.directory / "delivery/snapshot"
                if group == "goalkeeper" and normal
                else Path(state.workspace)
            )
            verdict = await score(source, package / "grade.py", store.directory)
            grading_ended = time.time()
            prepared = snapshot(package / "initial")
            current_files = snapshot(Path(state.workspace))
            protected_unchanged = all(current_files.get(p) == prepared.get(p)
                                      for p in task.protected_paths)
            calls = [c for c in transport.records if c["run_id"] == state.id]
            cost = {
                "known_tokens": transport.known(state.id),
                "usage_complete": bool(calls) and all(c.get("usage_known") for c in calls),
                "unknown_calls": sum(not c.get("usage_known") for c in calls),
            }
            result = {
                "group": group,
                "artifact_passed": verdict["passed"],
                "normal_delivery": normal,
                "cleanup_complete": cleanup_complete,
                "stop_reason": state.first_stop_reason if group == "goalkeeper" else
                               json.loads((store.directory / "native-result.json").read_text()).get("stop_reason"),
                "feedback_delivered": state.feedback_delivered,
                "turns_delivered": state.turn_index + 1,
                "turns_required": len(task.user_turns) + 1,
                "model_verified": verified_models(calls),
                "transport_stop_reason": transport.stopped_reason(state.id) if hasattr(transport, "stopped_reason") else getattr(transport, "reason", None),
                "accounting_settled": accounting_settled,
                "protected_checks_unchanged": protected_unchanged,
                "passed": normal and verdict["passed"] and verified_models(calls)
                          and accounting_settled and cleanup_complete and protected_unchanged
                          and (state.feedback_delivered or task.feedback is None)
                          and state.turn_index == len(task.user_turns),
                "wall_seconds": execution_ended - state.started_at,
                "accounting_wait_seconds": accounting_ended - execution_ended,
                "grading_seconds": grading_ended - accounting_ended,
                "cost": cost,
            }
            result["efficiency_comparable"] = result["passed"] and cost["usage_complete"]
            if connectivity:
                evidence = connectivity_tools(group, state, store.directory)
                result["tool_evidence"] = evidence
                result["passed"] = result["passed"] and evidence["completed_tools"] > 0 and evidence["check_execution_verified"]
            save_report(store.directory / "result.json", result)
            if state.phase == "program_error":
                core_failure.set()
            return result

        async def guarded(job=job, group=group, state=state, store=store):
            try:
                return await job()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                from .secrets import redact
                transport.pause("core_program_error")
                result = {"group": group, "passed": False, "normal_delivery": False,
                          "stop_reason": "core_program_error", "error": redact(type(error).__name__ + ":" + str(error)),
                          "feedback_delivered": state.feedback_delivered,
                          "cost": {"known_tokens": transport.known(state.id)}}
                try:
                    save_report(store.directory / "result.json", result)
                finally:
                    core_failure.set()
                return result

        jobs.append(guarded)
    cancelled = False
    try:
        results = await parallel(jobs, core_failure)
    except asyncio.CancelledError:
        cancelled = True
        results = [json.loads((store.directory / "result.json").read_text())
                   if (store.directory / "result.json").exists() else asyncio.CancelledError()
                   for state, store in states.values()]
    finally:
        try:
            await finish_cleanup(transport.close())
        except asyncio.CancelledError:
            cancelled = True
    serializable = []
    identity_matches = all(frozen.get(k) == v for k, v in execution_identity(transient_retries).items())
    package_matches = frozen["package_hash"] == fingerprint(snapshot(package))
    for group, result in zip(GROUPS, results):
        if isinstance(result, BaseException):
            state, store = states[group]
            calls = [c for c in transport.records if c["run_id"] == state.id]
            native_path = store.directory / "native-result.json"
            if native_path.exists():
                native_data = json.loads(native_path.read_text())
                state.feedback_delivered = native_data.get("feedback_delivered", False)
            else:
                native_data = {}
            result = {"group": group, "passed": False, "normal_delivery": False,
                      "stop_reason": "cohort_core_program_error" if core_failure.is_set() else "outer_cancel",
                      "feedback_delivered": state.feedback_delivered,
                      "cost": {"known_tokens": transport.known(state.id), "usage_complete": bool(calls) and all(c.get("usage_known") for c in calls),
                               "unknown_calls": sum(not c.get("usage_known") for c in calls)}}
            result["normal_delivery"] = native_data.get("normal_end", state.phase == "delivered") is True
            result["cleanup_complete"] = native_data.get("cleanup_complete", state.ended_at is not None)
            save_report(store.directory / "result.json", result)
        state, store = states[group]
        calls = [c for c in transport.records if c["run_id"] == state.id]
        # Use final durable accounting for every row, including jobs that ended
        # before a late response or during cancellation/cleanup.
        result["cost"] = {"known_tokens": transport.known(state.id),
                          "usage_complete": bool(calls) and all(c.get("usage_known") for c in calls),
                          "unknown_calls": sum(not c.get("usage_known") for c in calls),
                          "unknown_reserve": transport.reserved(state.id), "requests": len(calls)}
        result["cohort_stop_reason"] = transport.first_reason
        result["transient_retries_used"] = transport.retry_used.get(state.id, 0)
        if not identity_matches or not package_matches:
            result["passed"] = False
            result["validation_failure"] = "execution_or_package_changed_during_run"
        result["efficiency_comparable"] = result["passed"] and result["cost"]["usage_complete"]
        save_report(store.directory / "result.json", result)
        serializable.append(result)
    save_report(directory / "results.json", {"results": serializable,
                "core_program_error": core_failure.is_set(), "cancelled": cancelled, "execution_identity_matches": identity_matches,
                "package_unchanged": package_matches, "scope": "one_task_diagnostic_not_general_superiority"})
    if cancelled:
        raise asyncio.CancelledError()
    return directory
