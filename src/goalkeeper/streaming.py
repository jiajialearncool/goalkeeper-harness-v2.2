"""Bounded SSE staging. No incomplete model output reaches execution clients."""
from __future__ import annotations

import asyncio
import json
import tempfile


class StreamFailure(RuntimeError):
    def __init__(self, code, *, transient=False):
        self.code, self.transient = code, transient
        super().__init__(code)


class StagedStream:
    def __init__(self, directory, max_bytes=64 * 1024 * 1024):
        self.file = tempfile.SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b", dir=directory)
        self.max_bytes, self.bytes = max_bytes, 0
        self.terminal = None

    def write(self, line):
        raw = (line + "\n").encode()
        self.bytes += len(raw)
        if self.bytes > self.max_bytes:
            raise StreamFailure("stream_size_limit")
        self.file.write(raw)

    def observe(self, data):
        if data == "[DONE]":
            return
        try:
            event = json.loads(data)
            if not isinstance(event, dict):
                raise ValueError()
        except ValueError as error:
            raise StreamFailure("invalid_stream_event") from error
        if event.get("type") in {"response.completed", "response.failed", "response.incomplete", "error"}:
            self.terminal = event

    async def collect(self, upstream, heartbeat=10):
        """Yield only SSE comments while collecting; clients cannot execute them."""
        lines, block = upstream.aiter_lines().__aiter__(), []
        pending = None
        try:
            while self.terminal is None:
                pending = asyncio.create_task(anext(lines))
                while not pending.done():
                    done, _ = await asyncio.wait([pending], timeout=heartbeat)
                    if not done:
                        yield b": awaiting complete response\n\n"
                try:
                    line = pending.result()
                except StopAsyncIteration:
                    if block:
                        self.observe("\n".join(block))
                        self.write("")
                    break
                self.write(line)
                if line.startswith("data:"):
                    block.append(line[5:].lstrip())
                elif not line and block:
                    self.observe("\n".join(block))
                    block.clear()
            if self.terminal is None:
                raise StreamFailure("interrupted_stream", transient=True)
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            await lines.aclose()

    def chunks(self):
        self.file.seek(0)
        while raw := self.file.read(64 * 1024):
            yield raw

    def close(self):
        self.file.close()
