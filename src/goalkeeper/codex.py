"""Codex is a comparator only. Experimental runtime never imports this adapter."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import re
import hashlib
import urllib.request
from pathlib import Path

from .models import EFFORT, MODEL
from .secrets import redact

CODEX = Path(__file__).resolve().parents[2] / "work/pinned-codex-0.159.0/codex"
PINNED_VERSION = "0.159.0"


class ProtocolError(RuntimeError):
    def __init__(self, message, rpc_error=None):
        super().__init__(message)
        self.rpc_error = rpc_error


class ConfigurationError(ProtocolError):
    pass


def python_link_read_rules(read_roots: list[Path], executable: Path, base_prefix: Path) -> dict[str, str]:
    """Allow uv's intermediate version link, without opening the user's home.

    Codex resolves permission paths, so allowing the alias itself does not allow
    readlink(alias). Its installation parent must be readable. Deny sibling
    installations that are not already among the explicitly allowed runtimes.
    """
    base = base_prefix.resolve()
    allowed = [p.resolve() for p in read_roots]
    if base not in allowed or not re.fullmatch(r"cpython-\d[^/]*", base.name):
        return {}
    parent = base.parent
    if parent.name != "python" or parent.parent.name != "uv":
        return {}
    pointer = executable.absolute()
    for _ in range(16):
        if any(p.is_symlink() and p.parent == parent and p.resolve() == base for p in pointer.parents):
            rules = {str(parent): "read"}
            for child in sorted(parent.iterdir()):
                resolved = child.resolve()
                if not any(resolved.is_relative_to(p) or p.is_relative_to(resolved) for p in allowed):
                    rules[str(resolved)] = "deny"
            return rules
        if not pointer.is_symlink():
            break
        target = Path(os.readlink(pointer))
        pointer = target if target.is_absolute() else pointer.parent / target
    return {}


def task_permissions(workspace: Path, read_roots: list[Path], write_paths=None, protected_paths=()) -> tuple[str, dict[str, str]]:
    """Build the actual adapter policy and task environment; no inference or login."""
    from .paths import inside_project
    from .sandbox import Scope, Seatbelt

    root = inside_project(workspace)
    tools = inside_project(root.parent / "codex-tool-bin")
    if (root.parent / "codex-tool-bin").is_symlink():
        raise ConfigurationError("Codex 工具目录不能是符号链接。")
    tools.mkdir(exist_ok=True)
    expected = {"apply_patch", "applypatch"}
    if any(p.name not in expected for p in tools.iterdir()):
        raise ConfigurationError("Codex 工具目录含有非预期文件。")
    for name in sorted(expected):
        link = tools / name
        if link.is_symlink():
            if os.readlink(link) != str(CODEX):
                raise ConfigurationError("Codex 工具链接未指向固定安装。")
        elif link.exists():
            raise ConfigurationError("Codex 工具链接被其他文件替换。")
        else:
            link.symlink_to(CODEX)
    rules = {":root": "deny", ":minimal": "read"}
    rules.update({str(p.resolve()): "read" for p in read_roots})
    rules.update(python_link_read_rules(read_roots, Path(sys.executable), Path(sys.base_prefix)))
    rules.update({str(CODEX.parent.resolve()): "read", str(tools): "read",
                  str(root): "write" if write_paths is None else "read"})
    if write_paths is not None:
        scope = Scope(root, write_paths=write_paths)
        for path in write_paths:
            rules[str(scope.path(path, write=True))] = "write"
        for path in (root / ".tmp", root / ".home"):
            rules[str(path)] = "write"
    for path in protected_paths:
        rules[str(Scope(root).path(path))] = "read"
    rules[str(root / ".git")] = "read"
    profile = (
        "{filesystem={"
        + ",".join(json.dumps(k) + "=" + json.dumps(v) for k, v in rules.items())
        + "},network={enabled=false}}"
    )
    environment = Seatbelt(Scope(root, runtimes=read_roots)).environment()
    environment["PATH"] = ":".join([str(tools), str(CODEX.parent), environment["PATH"]])
    # A synthetic task is not the parent harness repository. Do not follow its
    # worktree .git pointer into shared, deliberately inaccessible metadata.
    environment["GIT_CEILING_DIRECTORIES"] = str(root.parent)
    return profile, environment


def normalized_configuration(config: dict, workspace: Path) -> str:
    value = dict(redact(config))
    # thread/start registers task directories globally. Unrelated project entries
    # are not effective settings for this run; its own trust policy is fixed below.
    value["projects"] = {
        str(workspace.resolve()): value.get("projects", {}).get(str(workspace.resolve()), {})
    }
    return (
        json.dumps(value, sort_keys=True, ensure_ascii=False)
        .replace(str(workspace.parent / "native-agent-role.toml"), "<ROLE_CONFIGURATION>")
        .replace(str(workspace.parent / "codex-tool-bin"), "<TASK_TOOLS>")
        .replace(str(workspace.resolve()), "<WORKSPACE>")
    )


class CodexClient:
    def __init__(self, workspace: Path | None = None, read_roots: list[Path] | None = None,
                 write_paths=None, protected_paths=()):
        self.workspace = workspace
        self.read_roots = read_roots or []
        self.write_paths, self.protected_paths = write_paths, protected_paths
        self.profile_id = "goalkeeper-evaluation"
        self.process = None
        self.reader = None
        self.stderr_task = None
        self.diagnostics = b""
        self.counter = 0
        self.pending = {}
        self.events = asyncio.Queue()
        self.usage_by_thread: dict[str, dict] = {}
        self.thread_id = None
        self.on_spawn = None
        self.on_exit = None
        self.configuration_fingerprint = None
        self.transport_overrides = {}
        self.provider_settings = {}
        self.progress_events_omitted = 0

    def use_subscription_transport(self, private_home: Path, access_token: str):
        """Authentication only; never supply task context or execution strategy."""
        from .paths import PROJECT

        if not private_home.resolve().is_relative_to(PROJECT.resolve()):
            raise ConfigurationError("Codex 配置与历史目录必须留在当前项目内。")
        self.transport_overrides = {"CODEX_HOME": str(private_home), "ACCESS_TOKEN": access_token}
        self.provider_settings = {
            "model_provider": '"openai_chatgpt_plan"',
            "model_providers.openai_chatgpt_plan.name": '"ChatGPT plan"',
            "model_providers.openai_chatgpt_plan.base_url": '"https://api.openai.com/v1"',
            "model_providers.openai_chatgpt_plan.env_key": '"ACCESS_TOKEN"',
            "model_providers.openai_chatgpt_plan.wire_api": '"responses"',
            "model_providers.openai_chatgpt_plan.requires_openai_auth": "false",
            "model_providers.openai_chatgpt_plan.supports_websockets": "false",
        }

    async def start(self):
        for name in ["codex", "codex-code-mode-host", "rg"]:
            if not (CODEX.parent / name).is_file():
                raise ConfigurationError("固定 Codex 安装缺少执行组件：" + name)
        version = subprocess.run([str(CODEX), "--version"], capture_output=True, text=True, timeout=10)
        if version.stdout.strip() != f"codex-cli {PINNED_VERSION}":
            raise ConfigurationError("Codex 版本不符合冻结要求。")
        overrides = []
        for key, value in self.provider_settings.items():
            overrides.extend(["-c", key + "=" + value])
        if self.workspace:
            profile, sandbox_env = task_permissions(self.workspace, self.read_roots, self.write_paths, self.protected_paths)
            settings = {
                "default_permissions": json.dumps(self.profile_id),
                "permissions." + self.profile_id: profile,
                "approval_policy": '"never"',
                "web_search": '"disabled"',
                "features.multi_agent": "true",
                "features.goals": "true",
                "model": json.dumps(MODEL),
                "model_reasoning_effort": json.dumps(EFFORT),
                "features.memory_tool": "false",
                "features.apps": "false",
                "features.computer_use": "false",
                "memories.use_memories": "false",
                "memories.generate_memories": "false",
                "project_doc_max_bytes": "0",
                "agents.max_threads": "3",
                "agents.max_depth": "1",
                "shell_environment_policy.inherit": '"none"',
                "allow_login_shell": "false",
                "projects": "{" + json.dumps(str(self.workspace.resolve())) + '={trust_level="trusted"}}',
            }
            # Enumerate connector names without exposing credentials or editing the user's configuration.
            inspector = CodexClient()
            inspector.transport_overrides = dict(self.transport_overrides)
            inspector.provider_settings = dict(self.provider_settings)
            try:
                await inspector.start()
                cfg = (await inspector.request("config/read", {"includeLayers": False})).get("config", {})
                for name in cfg.get("mcp_servers", {}):
                    settings["mcp_servers." + name + ".enabled"] = "false"
                for name in cfg.get("plugins", {}):
                    settings["plugins." + name + ".enabled"] = "false"
                role_file = self.workspace.parent / "native-agent-role.toml"
                role_file.write_text(
                    f"model = {json.dumps(MODEL)}\nmodel_reasoning_effort = {json.dumps(EFFORT)}\n"
                )
                role_file.chmod(0o600)
                roles = {"default", "explorer", "worker"} | {
                    k for k, v in (cfg.get("agents") or {}).items() if isinstance(v, dict)
                }
                for name in roles:
                    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", name):
                        raise ProtocolError("自定义子 Agent 角色名无法安全固定配置。")
                    settings["agents." + name + ".config_file"] = json.dumps(str(role_file))
                    settings["agents." + name + ".description"] = json.dumps(
                        "Native Codex role with the frozen evaluation model and reasoning effort.")
            finally:
                await inspector.close()
            settings["shell_environment_policy.set"] = (
                "{" + ",".join(json.dumps(k) + "=" + json.dumps(v) for k, v in sandbox_env.items()) + "}"
            )
            for key, value in settings.items():
                overrides.extend(["-c", key + "=" + value])
        transport_env = dict(os.environ)
        transport_env.update(self.transport_overrides)
        # Rust clients read proxy environment variables, unlike Python's macOS
        # system-proxy discovery. Respect the user's existing transport settings.
        for scheme, address in urllib.request.getproxies().items():
            if scheme in {"http", "https", "all"}:
                transport_env.setdefault(scheme.upper() + "_PROXY", address)
        self.process = await asyncio.create_subprocess_exec(
            str(CODEX),
            *overrides,
            "app-server",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            limit=16_000_000,
            env=transport_env,
        )
        if self.on_spawn:
            self.on_spawn(self.process)
        self.stderr_task = asyncio.create_task(self._drain_stderr())
        self.reader = asyncio.create_task(self._read())
        result = await self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "GoalKeeper Harness" if self.provider_settings else "goalkeeper_evaluation",
                    "version": "0.1.0",
                },
                "capabilities": {"experimentalApi": True},
            },
        )
        await self.notify("initialized", {})
        if self.workspace:
            effective = (
                await self.request("config/read", {"includeLayers": False, "cwd": str(self.workspace)})
            )["config"]
            normalized = normalized_configuration(effective, self.workspace)
            self.configuration_fingerprint = hashlib.sha256(normalized.encode()).hexdigest()
        return result

    async def _drain_stderr(self):
        while chunk := await self.process.stderr.read(8192):
            self.diagnostics = (self.diagnostics + chunk)[-16000:]
            await asyncio.sleep(0)

    async def _send(self, body):
        self.process.stdin.write((json.dumps(body, ensure_ascii=False) + "\n").encode())
        await self.process.stdin.drain()

    async def notify(self, method, params):
        await self._send({"method": method, "params": params})

    async def request(self, method, params, timeout=30):
        self.counter += 1
        number = self.counter
        future = asyncio.get_running_loop().create_future()
        self.pending[number] = future
        try:
            await self._send({"id": number, "method": method, "params": params})
            return await asyncio.wait_for(future, timeout)
        finally:
            self.pending.pop(number, None)

    async def _read(self):
        try:
            while line := await self.process.stdout.readline():
                message = json.loads(line)
                if "id" in message and "method" not in message:
                    future = self.pending.get(message["id"])
                    if future and not future.done():
                        if "error" in message:
                            future.set_exception(ProtocolError(str(redact(message["error"])), redact(message["error"])))
                        else:
                            future.set_result(message.get("result"))
                elif "id" in message:
                    # A formal run never stalls waiting for a human or grants additional permissions.
                    method = message["method"]
                    decision = {"decision": "decline"}
                    if method == "item/tool/requestUserInput":
                        decision = {"answers": {}}
                    if method == "item/permissions/requestApproval":
                        decision = {"permissions": {}, "scope": "turn"}
                    await self._send({"id": message["id"], "result": decision})
                    await self.events.put({"method": "approval_rejected", "params": {"request": method}})
                else:
                    params = message.get("params", {})
                    if message.get("method", "").endswith("/delta"):
                        self.progress_events_omitted += 1
                        await asyncio.sleep(0)
                        continue
                    if message.get("method") == "thread/tokenUsage/updated":
                        self.usage_by_thread[params["threadId"]] = params["tokenUsage"]["total"]
                    await self.events.put(redact(message))
                await asyncio.sleep(0)
        except (ValueError, OSError) as error:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(ProtocolError(type(error).__name__))
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(
                        ProtocolError(
                            "app-server 已退出："
                            + str(redact(self.diagnostics.decode(errors="replace")[-3000:]))
                        )
                    )
            await self.events.put({"method": "app_server/exited", "params": {}})

    @property
    def observed_tokens(self):
        # Native child thread totals are counted once per thread; cached/reasoning subsets aren't added again.
        return sum(v.get("inputTokens", 0) + v.get("outputTokens", 0) for v in self.usage_by_thread.values())

    async def create_thread(self, workspace: Path):
        result = await self.request(
            "thread/start",
            {
                "cwd": str(workspace),
                "model": MODEL,
                "approvalPolicy": "never",
                "permissions": self.profile_id,
                "ephemeral": False,
                "config": {
                    "model_reasoning_effort": EFFORT,
                    "agents.max_threads": 3,
                    "agents.max_depth": 1,
                    "features.multi_agent": True,
                    "features.goals": True,
                    "features.memory_tool": False,
                },
            },
        )
        self.thread_id = result["thread"]["id"]
        return self.thread_id

    async def set_goal(self, objective: str | None = None, budget: int | None = None, status="active"):
        if objective is not None and (not objective.strip() or len(objective) > 4000):
            raise ProtocolError("原生 Goal 的目标必须为 1–4,000 字符，禁止截断后评测。")
        return await self.request(
            "thread/goal/set",
            {"threadId": self.thread_id, "objective": objective, "tokenBudget": budget, "status": status},
        )

    async def turn(self, text: str, workspace: Path, read_roots: list[Path]):
        return await self.request(
            "turn/start",
            {
                "threadId": self.thread_id,
                "model": MODEL,
                "effort": EFFORT,
                "input": [{"type": "text", "text": text}],
                "approvalPolicy": "never",
                "permissions": self.profile_id,
            },
        )

    async def steer(self, text: str, turn_id: str):
        return await self.request(
            "turn/steer",
            {
                "threadId": self.thread_id,
                "expectedTurnId": turn_id,
                "input": [{"type": "text", "text": text}],
            },
        )

    async def steer_or_start(self, text: str, turn_id: str):
        """A probe can finish after the active native turn has already ended."""
        try:
            await self.steer(text, turn_id)
            return turn_id
        except ProtocolError as error:
            if error.rpc_error is None:
                raise  # Lost acknowledgement is uncertain; never replay it.
            result = await self.request("thread/read", {"threadId": self.thread_id, "includeTurns": True})
            active = [turn for turn in result["thread"]["turns"] if turn.get("status") == "inProgress"]
            if active:
                if active[-1]["id"] == turn_id:
                    raise  # A real rejection on a still-active turn is not a lifecycle race.
                await self.steer(text, active[-1]["id"])
                return active[-1]["id"]
            started = await self.turn(text, self.workspace, self.read_roots)
            return started["turn"]["id"]

    async def interrupt(self, turn_id: str):
        return await self.request("turn/interrupt", {"threadId": self.thread_id, "turnId": turn_id})

    async def resume(self, thread_id: str, workspace: Path):
        result = await self.request(
            "thread/resume",
            {
                "threadId": thread_id,
                "cwd": str(workspace),
                "approvalPolicy": "never",
                "model": MODEL,
                "permissions": self.profile_id,
            },
        )
        self.thread_id = thread_id
        return result

    async def close(self):
        if self.process and self.process.returncode is None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except TimeoutError:
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await self.process.wait()
            if self.on_exit:
                self.on_exit(self.process)
        if self.stderr_task:
            try:
                await asyncio.wait_for(asyncio.gather(self.stderr_task, return_exceptions=True), 5)
            except TimeoutError:
                self.stderr_task.cancel()
        if self.reader:
            self.reader.cancel()
            await asyncio.gather(self.reader, return_exceptions=True)


async def inspect_protocol(schema_dir: Path, own_email: str | None = None) -> dict:
    """Local read-only handshake. Does not start a thread, Goal, turn, or model inference."""
    schema = schema_dir / "v2/TurnStartParams.json"
    supports_read_scope = schema.exists() and "permissions" in json.loads(schema.read_text()).get(
        "properties", {}
    )
    client = CodexClient()
    try:
        initialized = await client.start()
        account = await client.request("account/read", {})
        return {
            "version": PINNED_VERSION,
            "handshake": True,
            "user_agent": initialized.get("userAgent"),
            "signed_in": bool(account.get("account")),
            "same_chatgpt_account": bool(
                own_email
                and (account.get("account") or {}).get("email", "").casefold() == own_email.casefold()
            )
            if own_email
            else None,
            "restricted_read_schema": supports_read_scope,
            "formal_permissions_verified": False,
            "note": "必须实际拒绝评分包、其他工作区和凭据访问，不能仅凭 workspace-write 宣称读取已隔离。",
        }
    finally:
        await client.close()
