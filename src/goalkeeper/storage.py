from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

from .models import State, utcnow
from .secrets import redact


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def save_report(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        with temp.open("w") as file:
            os.chmod(temp, 0o600)
            file.write(canonical(value))
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Store:
    """One atomic checkpoint contains the event sequence, costs and pending tools."""
    def __init__(self, directory: Path, state_type=State):
        self.state_type = state_type
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.directory.chmod(0o700)
        self.archives = directory / "archive"
        self.archives.mkdir(exist_ok=True)
        self.archives.chmod(0o700)
        self.path = directory / "state.json"

    def load(self) -> State:
        return self.state_type.model_validate_json(self.path.read_text())

    def save(self, state: State, kind: str, data=None) -> None:
        if self.path.exists() and self.load().sequence != state.sequence:
            raise RuntimeError("stale_checkpoint")
        if state.phase in {"running", "prepared"}:
            import time
            state.remaining_seconds = max(0, state.deadline - time.time())
        sequence = state.sequence + 1
        event = {"sequence": sequence, "at": utcnow(), "kind": kind, "data": redact(data or {})}
        value = state.model_dump()
        value.update(sequence=sequence, events=[*value["events"], event])
        # Validate the serialized payload, including nested models, before it can
        # replace a good checkpoint. Commit memory only after the durable write.
        self.state_type.model_validate(value)
        save_report(self.path, value)
        state.sequence = sequence
        state.events.append(event)

    def archive(self, value) -> str:
        raw = value if isinstance(value, str) else canonical(value)
        name = hashlib.sha256(raw.encode()).hexdigest()
        target = self.archives / name
        if target.exists():
            if target.is_symlink() or hashlib.sha256(target.read_bytes()).hexdigest() != name:
                raise ValueError("归档内容已损坏。")
        else:
            temporary = target.with_name(name + "." + uuid4().hex + ".tmp")
            try:
                with temporary.open("w") as file:
                    os.chmod(temporary, 0o600)
                    file.write(raw)
                    file.flush()
                    os.fsync(file.fileno())
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        return name

    def read(self, reference: str) -> str:
        if len(reference) != 64 or any(c not in "0123456789abcdef" for c in reference):
            raise ValueError("无效归档引用。")
        raw = (self.archives / reference).read_bytes()
        if hashlib.sha256(raw).hexdigest() != reference:
            raise ValueError("归档校验不一致。")
        return raw.decode("utf-8")

    def lease(self):
        return Lease(self.directory / "run.lock")


class Lease:
    def __init__(self, path):
        self.path = path

    def __enter__(self):
        self.file = self.path.open("a")
        try:
            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.file.close()
            raise RuntimeError("run_already_active") from None
        return self

    def __exit__(self, *args):
        fcntl.flock(self.file, fcntl.LOCK_UN)
        self.file.close()


def snapshot(root: Path) -> dict[str, str]:
    result = {}
    for file in sorted(root.rglob("*")):
        relative = file.relative_to(root)
        if any(p in {".git", ".tmp", ".home", "__pycache__", ".pytest_cache"} for p in relative.parts):
            continue
        if file.is_symlink():
            raise ValueError("项目快照不接受符号链接。")
        if file.is_file():
            result[str(relative)] = hashlib.sha256(file.read_bytes()).hexdigest()
    return result


def fingerprint(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()
