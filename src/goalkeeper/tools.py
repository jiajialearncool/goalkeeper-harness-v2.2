from __future__ import annotations

import json
import os
import subprocess
import math
from pathlib import Path
from uuid import uuid4

from .sandbox import Scope, Seatbelt, runtime_roots
from .secrets import redact


def definitions():
    fields = {
        "read": ("读取任务文件指定行，正文超长时返回归档来源。",
                 {"path": {"type": "string"}, "start": {"type": "integer"}, "lines": {"type": "integer"}}),
        "write": ("写入任务范围内文件。", {"path": {"type": "string"}, "content": {"type": "string"}}),
        "edit": ("精确替换唯一文本片段，找不到或多处匹配则拒绝。",
                 {"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}}),
        "edit_many": ("一次修改同一文件多处已知片段。按顺序在内存中替换；任一片段不唯一则整份文件不写入。",
                      {"path": {"type": "string"}, "changes": {"type": "array", "minItems": 1,
                       "items": {"type": "object", "properties": {"old": {"type": "string"},
                                 "new": {"type": "string"}}, "required": ["old", "new"],
                                 "additionalProperties": False}}}),
        "bash": ("在权限沙箱中执行 argv 命令并记录实际结果。",
                 {"argv": {"type": "array", "items": {"type": "string"}}, "timeout": {"type": "number"}}),
        "search_archive": ("按关键词搜索本任务归档，返回最多三条片段。", {"query": {"type": "string"}}),
        "read_archive": ("按来源读取本任务归档指定行。",
                         {"reference": {"type": "string"}, "start": {"type": "integer"}, "lines": {"type": "integer"}}),
        "request_delivery": ("申请交付，程序核对规定检查，失败时不能算完成。",
                             {"summary": {"type": "string"}, "check_ids": {"type": "array", "items": {"type": "string"}},
                              "unresolved": {"type": "array", "items": {"type": "string"}}}),
    }
    required = {"read": ["path"], "write": ["path", "content"], "edit": ["path", "old", "new"],
                "edit_many": ["path", "changes"], "bash": ["argv"], "search_archive": ["query"], "read_archive": ["reference"],
                "request_delivery": ["summary", "check_ids", "unresolved"]}
    return [{"type": "function", "name": name, "description": description,
             "parameters": {"type": "object", "properties": props, "required": required[name],
                            "additionalProperties": False}}
            for name, (description, props) in fields.items()]


def birth(pid):
    result = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "lstart="],
                            capture_output=True, text=True, timeout=5)
    return result.stdout.strip() if result.returncode == 0 else None


class Tools:
    def __init__(self, state, store, tokens):
        self.state, self.store, self.tokens = state, store, tokens
        root = Path(state.workspace)
        self.scope = Scope(root, write_paths=state.task.write_paths, runtimes=runtime_roots(),
                           protected_files=[root / p for p in state.task.protected_paths])
        self.runner = Seatbelt(self.scope, archive=store.archives)
        self.runner.on_spawn = self.spawned
        self.runner.on_exit = self.exited

    def spawned(self, process):
        self.store.save(self.state, "process_spawned", {"pid": process.pid})
        record = {"pid": process.pid, "birth": birth(process.pid)}
        from .storage import save_report
        save_report(self.store.directory / "processes.json", record)

    def exited(self, process):
        (self.store.directory / "processes.json").unlink(missing_ok=True)

    def reconcile(self):
        path = self.store.directory / "processes.json"
        if path.exists():
            record = json.loads(path.read_text())
            if record.get("birth") and birth(record["pid"]) == record["birth"]:
                import signal
                os.killpg(record["pid"], signal.SIGKILL)
                self.store.save(self.state, "orphan_process_cleaned", {"pid": record["pid"]})
            elif birth(record["pid"]):
                raise RuntimeError("进程身份无法核对，不能恢复。")
            path.unlink()

    def output(self, value):
        clean = redact(value)
        ref = self.store.archive(clean)
        text = json.dumps(clean, ensure_ascii=False)
        # Keep command status and archive references readable even for verbose test output.
        if isinstance(clean, dict) and {"exit_code", "stdout", "stderr"}.issubset(clean):
            while self.tokens.count(json.dumps(clean, ensure_ascii=False)) > self.state.config.tool_result_tokens:
                field = max(("stdout", "stderr"), key=lambda k: self.tokens.count(clean[k]))
                size = self.tokens.count(clean[field])
                if size <= 64:
                    break
                clean[field] = self.tokens.preview(clean[field], max(64, size // 2))
                clean[field + "_truncated"] = True
            text = json.dumps(clean, ensure_ascii=False)
        if self.tokens.count(text) > self.state.config.tool_result_tokens:
            clean = {"preview": self.tokens.clip(text, self.state.config.tool_result_tokens - 100),
                     "reference": ref, "truncated": True}
        return clean

    def range(self, raw, args):
        start, lines = args.get("start", 1), args.get("lines", 200)
        if type(start) is not int or type(lines) is not int or start < 1 or not 1 <= lines <= 1000:
            raise ValueError("行范围无效。")
        reference = self.store.archive(raw)
        return {"text": "\n".join(raw.splitlines()[start-1:start-1+lines]),
                "reference": reference, "start": start, "lines": lines}

    async def execute(self, name, args, timeout):
        schema = next((x["parameters"] for x in definitions() if x["name"] == name), None)
        if not schema or not isinstance(args, dict) or set(args) - set(schema["properties"]) or any(
            key not in args for key in schema["required"]
        ):
            raise ValueError("工具参数不符合定义。")
        for key, value in args.items():
            kind = schema["properties"][key]["type"]
            valid = ((kind == "string" and isinstance(value, str))
                     or (kind == "integer" and type(value) is int)
                     or (kind == "number" and type(value) in {int, float} and math.isfinite(value) and value > 0)
                     or (kind == "array" and isinstance(value, list) and
                         (key == "changes" or all(isinstance(v, str) for v in value))))
            if not valid:
                raise ValueError("工具参数类型不正确：" + key)
        if name == "read":
            return self.range(self.scope.path(args["path"]).read_text(), args)
        if name in {"write", "edit", "edit_many"}:
            path = self.scope.path(args["path"], write=True)
            if path in self.scope.protected_files:
                raise ValueError("规定检查文件不可修改。")
            if name in {"edit", "edit_many"}:
                raw = path.read_text()
                changes = args["changes"] if name == "edit_many" else [{"old": args["old"], "new": args["new"]}]
                if not changes or any(not isinstance(c, dict) or set(c) != {"old", "new"}
                                      or not isinstance(c["old"], str) or not isinstance(c["new"], str) for c in changes):
                    raise ValueError("changes 必须是非空的 old/new 字符串对象数组。")
                for index, change in enumerate(changes, 1):
                    if not change["old"] or raw.count(change["old"]) != 1:
                        raise ValueError(f"第{index}处要替换的片段必须唯一；文件未写入。")
                    raw = raw.replace(change["old"], change["new"], 1)
                content = raw
            else:
                content = args["content"]
            if not isinstance(content, str):
                raise ValueError("文件正文必须是字符串。")
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
            try:
                temporary.write_text(content)
                if path.exists():
                    os.chmod(temporary, path.stat().st_mode & 0o777)
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
            return {"path": args["path"], "written": True,
                    **({"changes_applied": len(changes)} if name == "edit_many" else {})}
        if name == "bash":
            result = await self.runner.run(args["argv"], min(args.get("timeout", 120), timeout))
            for field in ("stdout", "stderr"):
                raw = Path(result.pop(field + "_file")).read_text(errors="replace")
                clean = str(redact(raw))
                result[field + "_reference"] = self.store.archive(clean)
                limit = self.state.config.tool_result_tokens // 2
                result[field + "_truncated"] = self.tokens.count(clean) > limit
                result[field] = self.tokens.preview(clean, limit)
            return result
        if name == "read_archive":
            return self.range(self.store.read(args["reference"]), args)
        if name == "search_archive":
            query = args["query"]
            if not isinstance(query, str) or not query.strip():
                raise ValueError("检索词不能为空。")
            matches = []
            for file in sorted(self.store.archives.iterdir()):
                if len(file.name) != 64:
                    continue
                raw = self.store.read(file.name)
                index = raw.casefold().find(query.casefold())
                if index >= 0:
                    matches.append({"reference": file.name, "preview": raw[max(0,index-100):index+500]})
                if len(matches) == 3:
                    break
            return {"matches": matches}
        raise ValueError("交付工具由框架处理。")
