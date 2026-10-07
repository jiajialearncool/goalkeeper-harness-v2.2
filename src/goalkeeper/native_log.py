"""Append-only control evidence; progress snapshots never rewrite the run history."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from .secrets import redact
from .storage import canonical, save_report


class NativeLog:
    def __init__(self, directory):
        self.directory = directory
        self.path = directory / "native-events.ndjson"
        self.progress_events = 0
        self.events = 0
        self.bytes_written = 0
        self.lock = asyncio.Lock()

    async def append(self, value):
        kind = value.get("type", "")
        method = value.get("method", "")
        if kind in {"message_update", "tool_execution_update"} or method.endswith("/delta"):
            self.progress_events += 1
            # A buffered pipe may already contain thousands of lines. Awaiting
            # readline alone need not yield to the other three execution jobs.
            await asyncio.sleep(0)
            return
        async with self.lock:
            writer = asyncio.create_task(asyncio.to_thread(self._append, value))
            try:
                await asyncio.shield(writer)
            except asyncio.CancelledError:
                # Do not release the lock while the disk writer is still using
                # this journal. Cancellation never creates a torn evidence line.
                await writer
                raise

    def _append(self, value):
        raw = (canonical(redact(value)) + "\n").encode()
        self.directory.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "ab") as file:
            file.write(raw)
            file.flush()
            os.fsync(file.fileno())
        self.events += 1
        self.bytes_written += len(raw)

    async def close(self):
        async with self.lock:
            await asyncio.to_thread(save_report, self.directory / "native-log-summary.json", {
                "format": "append_only_ndjson", "control_events": self.events,
                "progress_events_omitted": self.progress_events, "bytes_written": self.bytes_written,
                "execution_context_changed": False,
            })


def native_events(directory):
    """Read current journals incrementally; old completed JSON evidence stays readable."""
    path = Path(directory) / "native-events.ndjson"
    if path.exists():
        with path.open() as file:
            for line in file:
                if line.strip():
                    yield json.loads(line)
    else:
        old = Path(directory) / "native-events.json"
        if old.exists():
            yield from json.loads(old.read_text())
