"""Comparator preparation: native loops remain inside each installed product."""
from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import time
import math
from pathlib import Path

import httpx

from .codex import CodexClient
from .models import EFFORT, MODEL
from .lifecycle import finish_cleanup
from .native_log import NativeLog
from .paths import PROJECT
from .sandbox import Scope, Seatbelt, runtime_roots
from .storage import save_report, snapshot

TOOLS_ROOT = PROJECT / "work/native-tools"


class NativeProgramError(RuntimeError):
    pass


def feedback_in_messages(messages, text):
    for message in messages:
        if message.get("role") != "user":
            continue
        content = message.get("content", [])
        rendered = content if isinstance(content, str) else "".join(p.get("text", "") for p in content if isinstance(p, dict))
        if "用户追加要求：" + text in rendered:
            return True
    return False


def configuration(group, base_url, token, runner):
    env = runner.environment()
    env.update(NO_COLOR="1", TERM="dumb", XDG_CONFIG_HOME=str(runner.home / "config"),
               XDG_CACHE_HOME=str(runner.home / "cache"), XDG_DATA_HOME=str(runner.home / "data"),
               PI_OFFLINE="1")
    if group == "pi":
        directory = runner.home / "pi"
        directory.mkdir(exist_ok=True)
        save_report(directory / "models.json", {"providers": {"subscription": {
            "api": "openai-responses", "baseUrl": base_url, "apiKey": token,
            "models": [{"id": MODEL, "name": MODEL, "reasoning": True, "input": ["text"],
                        "contextWindow": 1050000, "maxTokens": 128000,
                        "thinkingLevelMap": {"xhigh": "xhigh"},
                        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}}]}}})
        env["PI_CODING_AGENT_DIR"] = str(directory)
        return [str(TOOLS_ROOT / "node_modules/.bin/pi"), "--mode", "rpc", "--provider", "subscription",
                "--model", MODEL, "--thinking", EFFORT, "--session-dir", str(directory / "sessions"),
                "--no-extensions", "--no-skills", "--no-prompt-templates", "--no-themes"], env
    if group != "opencode":
        raise ValueError("unknown_native_product")
    cfg = {"model": "subscription/"+MODEL, "small_model": "subscription/"+MODEL,
           "share": "disabled", "enabled_providers": ["subscription"],
           "provider": {"subscription": {"npm": "@ai-sdk/openai", "name": "ChatGPT subscription",
             "options": {"baseURL": base_url, "apiKey": token}, "models": {MODEL: {
             "name": MODEL, "reasoning": True, "limit": {"context": 1050000, "output": 128000},
             "options": {"reasoningEffort": EFFORT, "store": False},
             "variants": {EFFORT: {"reasoningEffort": EFFORT}}}}}},
           "permission": {"*": "allow", "external_directory": "deny", "webfetch": "deny", "websearch": "deny"}}
    config_dir = runner.home / "config/opencode"
    config_dir.mkdir(parents=True, exist_ok=True)
    save_report(config_dir / "package.json", {"dependencies": {"@opencode-ai/plugin": "1.18.34"}})
    link = config_dir / "node_modules"
    if not link.exists():
        link.symlink_to(TOOLS_ROOT / "node_modules", target_is_directory=True)
    env["OPENCODE_CONFIG_CONTENT"] = json.dumps(cfg)
    for flag in ("MODELS_FETCH", "AUTOUPDATE", "DEFAULT_PLUGINS", "LSP_DOWNLOAD", "PROJECT_CONFIG"):
        env["OPENCODE_DISABLE_"+flag] = "true"
    env["OPENCODE_EXPERIMENTAL_DISABLE_FILEWATCHER"] = "true"
    return [], env


def objective(task, turn_index=0):
    return json.dumps({"目标": task.active_definition(False, turn_index)["objective"], "硬约束": task.constraints,
                       "验收要求": task.acceptance, "允许修改": task.write_paths,
                       "执行环境": "工作目录是本题项目。使用PATH中的python，或" + str(PROJECT / ".venv/bin/python")
                       + "。解释器已就绪，无需安装依赖或在本题目录寻找、新建.venv。"}, ensure_ascii=False)


class Native:
    def __init__(self, group, directory, task, source, base_url, route_token, seconds=1800, tokens=1_000_000):
        self.group, self.directory, self.task = group, directory, task
        self.root, self.base_url, self.route_token = source, base_url, route_token
        self.started, self.deadline = time.time(), time.time()+seconds
        self.token_budget = tokens
        self.feedback_delivered = False
        self.turn_index = 0
        self.feedback_queued = False
        self.process = None
        self.pid = None
        self.probe_hash = None
        self.terminal_diagnostic = None
        self.log = NativeLog(directory)
        ports = [int(httpx.URL(base_url).port)]
        self.runner = Seatbelt(Scope(source, write_paths=task.write_paths, runtimes=runtime_roots(),
            extra_read=[TOOLS_ROOT, Path("/private/var/db/timezone")], ports=ports,
            protected_files=[source / path for path in task.protected_paths]), archive=directory / "archive")

    async def event(self, value):
        await self.log.append(value)

    async def next_turn(self, send):
        if self.turn_index >= len(self.task.user_turns):
            return False
        await self.event({"type": "user_turn_completed", "turn": self.turn_index + 1,
                          "snapshot": snapshot(self.root)})
        text = self.task.user_turns[self.turn_index]
        accepted = await send(text)
        if accepted is False:
            raise RuntimeError("next_user_turn_not_acknowledged")
        self.turn_index += 1
        await self.event({"type": "user_turn_delivered", "turn": self.turn_index + 1})
        return True

    async def drain_diagnostic(self, stream):
        preview = b""
        while chunk := await stream.read(8192):
            preview = (preview + chunk)[-16000:]
            await asyncio.sleep(0)
        return preview

    async def feedback(self, send):
        if not self.task.feedback or self.feedback_delivered or self.feedback_queued:
            return
        digest = snapshot(self.root)
        if digest == self.probe_hash:
            return
        self.probe_hash = digest
        probe = self.task.feedback.probe
        result = await self.runner.run(probe.argv, min(probe.timeout, max(.01,self.deadline-time.time())))
        if (result["exit_code"] == 0 and not result["timed_out"] and snapshot(self.root) == digest):
            accepted = await send("用户追加要求：" + self.task.feedback.text)
            self.feedback_queued = True
            if accepted is not False:
                self.feedback_delivered = True
                await self.event({"type": "common_feedback_delivered", "goal_version": 2})
            return True
        return False

    async def spawn(self, argv, env):
        self.process = await asyncio.create_subprocess_exec("/usr/bin/sandbox-exec", "-p", self.runner.profile(),
            *argv, cwd=self.root, env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, start_new_session=True, limit=16_000_000)
        from .tools import birth
        save_report(self.directory / "native-process.json", {"pid": self.process.pid, "birth": birth(self.process.pid)})

    async def pi(self):
        argv, env = configuration("pi", self.base_url, self.route_token, self.runner)
        await self.spawn(argv, env)
        feedback_id = "common-feedback"
        turn_receipts = set()
        async def send(kind, text, identifier=None):
            command = {"type": kind, "message": text}
            if identifier:
                command.update(id=identifier, streamingBehavior="steer")
            self.process.stdin.write((json.dumps(command)+"\n").encode())
            await self.process.stdin.drain()
        await send("prompt", objective(self.task))
        self.stderr = asyncio.create_task(self.drain_diagnostic(self.process.stderr))
        while line := await self.process.stdout.readline():
            event = json.loads(line)
            await self.event(event)
            if event.get("type") == "response" and event.get("success") and str(event.get("id", "")).startswith("user-turn-"):
                turn_receipts.add(event["id"])
            if event.get("type") == "response" and event.get("id") == feedback_id and event.get("success"):
                self.feedback_delivered = True
                await self.event({"type": "common_feedback_delivered", "goal_version": 2})
            if event.get("type") == "tool_execution_end":
                async def submit_feedback(text):
                    # Prompt with streamingBehavior is queued while active and
                    # starts a native turn while idle, in the same session.
                    await send("prompt", text, feedback_id)
                    return False  # The RPC receipt is authoritative, not stdin.drain.
                await self.feedback(submit_feedback)
            if event.get("type") == "agent_end":
                # The same native instance has settled; no GoalKeeper planning or finish request is injected.
                messages = event.get("messages", [])
                if any(message.get("role") == "assistant" and message.get("stopReason") in {"error", "aborted"}
                       for message in messages):
                    raise RuntimeError("pi_native_end_with_error")
                if self.turn_index and "user-turn-" + str(self.turn_index + 1) not in turn_receipts:
                    continue
                if self.feedback_queued and not feedback_in_messages(messages, self.task.feedback.text):
                    continue  # An older cycle ended while the probe was running.
                if await self.next_turn(lambda text: send("prompt", text, "user-turn-" + str(self.turn_index + 2))):
                    continue
                return True
            if event.get("type") == "response" and event.get("success") is False:
                raise RuntimeError("pi_rpc_error")
        return False

    async def opencode(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.runner.scope.ports.append(port)
        _, env = configuration("opencode", self.base_url, self.route_token, self.runner)
        import secrets
        password = secrets.token_hex(24)
        env["OPENCODE_SERVER_PASSWORD"] = password
        await self.spawn([str(TOOLS_ROOT / "node_modules/.bin/opencode"), "serve", "--hostname", "127.0.0.1",
                          "--port", str(port)], env)
        self.stdout_task = asyncio.create_task(self.drain_diagnostic(self.process.stdout))
        self.stderr = asyncio.create_task(self.drain_diagnostic(self.process.stderr))
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", auth=("opencode", password),
                                     trust_env=False, timeout=15) as client:
            for _ in range(40):
                try:
                    if (await client.get("/global/health")).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if self.process.returncode is not None:
                    raise RuntimeError("opencode_start_failed")
                await asyncio.sleep(.25)
            else:
                raise RuntimeError("opencode_start_timeout")
            response = await client.post("/session", json={"title": self.task.id})
            response.raise_for_status()
            session = response.json()["id"]
            before_feedback_ids = set()
            async def prompt(text):
                if text.startswith("用户追加要求：") or self.task.user_turns:
                    previous = await client.get(f"/session/{session}/message")
                    previous.raise_for_status()
                    before_feedback_ids.update(m.get("info", {}).get("id") for m in previous.json())
                response = await client.post(f"/session/{session}/prompt_async", json={
                    "model": {"providerID": "subscription", "modelID": MODEL}, "variant": EFFORT,
                    "agent": "build", "parts": [{"type": "text", "text": text}]})
                response.raise_for_status()
            await prompt(objective(self.task))
            while self.process.returncode is None:
                await self.feedback(prompt)
                status = await client.get("/session/status")
                status.raise_for_status()
                if status.json().get(session, {}).get("type", "idle") == "idle":
                    messages = await client.get(f"/session/{session}/message")
                    messages.raise_for_status()
                    data = messages.json()
                    await self.event({"type": "native_messages", "messages": data})
                    assistants = [m for m in data if m.get("info", {}).get("role") == "assistant"]
                    if assistants:
                        last = assistants[-1]["info"]
                        if last.get("error"):
                            raise RuntimeError("opencode_native_error")
                        if last.get("finish") == "stop" and last.get("id") not in before_feedback_ids:
                            if await self.next_turn(prompt):
                                continue
                            return True
                        if last.get("finish") in {"length", "content-filter", "error"}:
                            raise RuntimeError("opencode_native_incomplete")
                await asyncio.sleep(.5)
        return False

    async def codex(self):
        client = CodexClient(self.root, runtime_roots(), write_paths=self.task.write_paths,
                             protected_paths=self.task.protected_paths)
        home = self.directory / "codex-home"
        home.mkdir(parents=True, exist_ok=True)
        client.use_subscription_transport(home, self.route_token)
        client.provider_settings["model_providers.openai_chatgpt_plan.base_url"] = json.dumps(self.base_url)
        client.provider_settings["model_providers.openai_chatgpt_plan.stream_idle_timeout_ms"] = str(
            max(1000, math.ceil((self.deadline-time.time()+5)*1000)))
        try:
            await client.start()
            await client.create_thread(self.root)
            goal_context = "本任务的原生Goal由客户端通过API创建。请继续该目标，用get_goal查看状态；不要重复调用create_goal。\n"
            turn = await client.turn(goal_context + objective(self.task), self.root, runtime_roots())
            active = turn["turn"]["id"]
            pending_turns = {active}
            goal_complete = False
            await client.set_goal(objective(self.task), self.token_budget)
            async def steer(text):
                nonlocal active
                await client.set_goal(objective(self.task)+"\n"+text,
                    None if self.token_budget is None else max(1,self.token_budget-client.observed_tokens))
                active = await client.steer_or_start(text, active)
                pending_turns.add(active)
            while True:
                event = await client.events.get()
                await self.event(event)
                method, params = event.get("method"), event.get("params", {})
                if method == "app_server/exited":
                    raise RuntimeError("codex_app_server_exited_before_completion")
                # Child threads have their own turns and Goals. They cannot finish
                # the root task or replace the turn receiving common feedback.
                if params.get("threadId") != client.thread_id:
                    continue
                if method == "turn/started":
                    active = params["turn"]["id"]
                    pending_turns.add(active)
                if method == "item/completed":
                    await self.feedback(steer)
                if method == "turn/completed":
                    if params["turn"].get("status") != "completed":
                        self.terminal_diagnostic = {"method": method, "turn": params["turn"]}
                        raise RuntimeError("codex_native_turn_failed")
                    pending_turns.discard(params["turn"]["id"])
                if method == "thread/goal/updated":
                    status = params.get("goal", {}).get("status")
                    goal_complete = status == "complete"
                    if status in {"blocked", "paused", "budgetLimited", "usageLimited"}:
                        self.terminal_diagnostic = {"method": method, "goal": params.get("goal", {})}
                        raise RuntimeError("codex_native_goal_" + status)
                # A Goal update can be a tool call in an unfinished response.
                # Wait for that turn's terminal event before closing app-server.
                if goal_complete and not pending_turns:
                    async def advance(text):
                        nonlocal active, goal_complete
                        await client.set_goal(objective(self.task, self.turn_index + 1), self.token_budget)
                        goal_complete = False
                        started = await client.turn(text, self.root, runtime_roots())
                        active = started["turn"]["id"]
                        pending_turns.add(active)
                    if await self.next_turn(advance):
                        continue
                    return True
        finally:
            try:
                await finish_cleanup(client.close())
            finally:
                self.log.progress_events += getattr(client, "progress_events_omitted", 0)
        return False

    async def run(self):
        normal = False
        reason = None
        try:
            async with asyncio.timeout(max(.01,self.deadline-time.time())):
                normal = await getattr(self, self.group)()
                if not normal:
                    reason = "native_protocol_incomplete"
        except TimeoutError:
            reason = "time_budget"
        except asyncio.CancelledError as error:
            reason = error.args[0] if error.args and str(error.args[0]).startswith("cohort_") else "outer_cancel"
            raise
        except (ValueError, KeyError, TypeError, AttributeError, OSError) as error:
            # Model/API terminal failures use explicit RuntimeError or HTTPError.
            # Invalid local protocol frames or failed evidence writes are core defects.
            reason = "core_program_error:" + type(error).__name__
            raise NativeProgramError(type(error).__name__) from error
        except Exception as error:
            reason = type(error).__name__ + ":" + str(error)
        finally:
            await finish_cleanup(self.finalize(normal, reason))
        return normal

    async def finalize(self, normal, reason):
        cleanup_errors = await self.cleanup()
        try:
            await self.log.close()
        except Exception as error:
            cleanup_errors.append({"step": "event_log_close", "error": type(error).__name__})
        (self.directory / "native-process.json").unlink(missing_ok=True)
        save_report(self.directory / "native-result.json", {"normal_end": normal, "stop_reason": reason,
            "terminal_diagnostic": self.terminal_diagnostic,
            "cleanup_complete": not cleanup_errors, "cleanup_errors": cleanup_errors,
            "feedback_delivered": self.feedback_delivered, "turns_delivered": self.turn_index + 1, "wall_seconds": time.time()-self.started,
            "model": MODEL, "effort": EFFORT, "delivery_snapshot": snapshot(self.root)})

    async def cleanup(self):
        errors = []
        try:
            if self.process and self.process.returncode is None:
                try:
                    os.killpg(self.process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(self.process.wait(), 5)
                except TimeoutError:
                    Seatbelt.kill(self.process)
                    await asyncio.wait_for(self.process.wait(), 5)
        except Exception as error:
            errors.append({"step": "process_stop", "error": type(error).__name__})
        try:
            if hasattr(self, "stderr"):
                data = await asyncio.wait_for(self.stderr, 5)
                await self.event({"type": "process_diagnostic", "text": str(data)[-16000:]})
        except (Exception, asyncio.CancelledError) as error:
            errors.append({"step": "diagnostic_read", "error": type(error).__name__})
        finally:
            if hasattr(self, "stdout_task"):
                try:
                    await asyncio.wait_for(self.stdout_task, 5)
                except (Exception, asyncio.CancelledError) as error:
                    errors.append({"step": "stdout_read", "error": type(error).__name__})
            try:
                await asyncio.wait_for(self.runner.stop(), 5)
            except Exception as error:
                errors.append({"step": "tool_stop", "error": type(error).__name__})
        return errors
