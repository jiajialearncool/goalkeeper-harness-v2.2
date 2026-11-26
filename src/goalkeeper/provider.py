from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Protocol

import httpx

from .auth import AuthBroker, RESOURCE
from .models import ModelEvent, ModelRequest
from .secrets import redact


class ProviderError(RuntimeError):
    def __init__(self, code: str, message: str = "", resource: bool = False):
        self.code, self.resource = code, resource
        super().__init__(redact(message or code))


def resource_error(code: str, error_type: str = "") -> bool:
    return (
        code in {"insufficient_quota", "quota_exceeded", "server_is_overloaded", "shared_service"}
        or error_type == "service_unavailable_error"
        or "usage_limit" in code
        or "usage_unavailable" in code
    )


class ModelProvider(Protocol):
    def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]: ...


class ResponsesProvider:
    """Independent HTTP client. No Codex process, SDK, app-server, or credential imports."""

    def __init__(self, auth: AuthBroker, http: httpx.AsyncClient | None = None):
        self.auth = auth
        self.http = http or httpx.AsyncClient(timeout=httpx.Timeout(360, connect=30), trust_env=True)

    async def models(self) -> list[dict]:
        token = await self.auth.access_token()
        response = await self.http.get(f"{RESOURCE}/models", headers={"Authorization": f"Bearer {token}"})
        if response.status_code != 200:
            raise ProviderError("model_catalog_failed", f"模型列表请求失败（HTTP {response.status_code}）。")
        data = response.json()
        return data.get("models", data.get("data", []))

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        request.config.require_current_model()
        try:
            token = await self.auth.access_token()
        except httpx.HTTPError as error:
            raise ProviderError("auth_transport_error", type(error).__name__, True) from error
        payload = {
            "model": request.config.model,
            "reasoning": {"effort": request.config.effort},
            "instructions": request.instructions,
            "input": request.input,
            "store": False,
            "stream": True,
            "include": ["reasoning.encrypted_content"],
        }
        if request.tools:
            payload["tools"] = request.tools
            payload["parallel_tool_calls"] = True
        if request.cache_key:
            payload["prompt_cache_key"] = request.cache_key
        completed = False
        finished_items: dict[int, dict] = {}
        invalid_items = False

        def event(data: str) -> ModelEvent:
            nonlocal completed, invalid_items
            try:
                parsed = json.loads(data)
                if not isinstance(parsed, dict):
                    raise ValueError()
            except ValueError as error:
                raise ProviderError("invalid_stream_event", "流式响应事件格式错误。") from error
            kind = parsed.get("type", "unknown")
            if not isinstance(kind, str):
                raise ProviderError("invalid_stream_event", "流式事件类型必须是字符串。")
            if kind == "response.output_item.done":
                index, item = parsed.get("output_index"), parsed.get("item")
                if isinstance(index, int) and index >= 0 and isinstance(item, dict):
                    finished_items[index] = item
                else:
                    invalid_items = True
            if kind == "response.completed":
                completed = True
                # Some subscription streams provide items only in their item-done events.
                # Never execute them before the response itself has completed.
                terminal = parsed.get("response", {})
                if not isinstance(terminal, dict):
                    raise ProviderError("invalid_terminal_response", "模型终止响应格式错误。")
                if not terminal.get("output") and invalid_items:
                    raise ProviderError("invalid_output_item", "流式响应项目缺少标识，不能执行工具。")
                if not terminal.get("output") and finished_items:
                    parsed["raw_terminal_response"] = terminal
                    parsed["response"] = {
                        **terminal,
                        "output": [finished_items[index] for index in sorted(finished_items)],
                    }
            return ModelEvent(type=kind, data=parsed)

        def check_failure(item):
            parsed, kind = item.data, item.type
            if kind in {"response.failed", "response.incomplete", "error"}:
                terminal = parsed.get("response", {})
                error = (terminal.get("error") if isinstance(terminal, dict) else None) or parsed.get("error") or parsed
                error = error if isinstance(error, dict) else {}
                code = error.get("code") or kind
                code = code if isinstance(code, str) else kind
                raise ProviderError(code, error.get("message", kind), resource_error(code, error.get("type", "")))

        try:
            async with self.http.stream(
                "POST", f"{RESOURCE}/responses", json=payload, headers={"Authorization": f"Bearer {token}"}
            ) as response:
                metadata = {"http_status": response.status_code}
                request_id = response.headers.get("x-request-id")
                if request_id and len(request_id) <= 128 and all(
                    c.isascii() and (c.isalnum() or c in "_-") for c in request_id
                ):
                    metadata["request_id"] = request_id
                yield ModelEvent(type="provider_response_metadata", data=metadata)
                if response.status_code != 200:
                    body = await response.aread()
                    try:
                        failure = json.loads(body).get("error", {})
                    except (ValueError, AttributeError):
                        failure = {}
                    failure = failure if isinstance(failure, dict) else {}
                    code = failure.get("code") or f"http_{response.status_code}"
                    code = code if isinstance(code, str) else f"http_{response.status_code}"
                    if code == "diagnostic_stopped" and failure.get("stop_reason"):
                        code = failure["stop_reason"]
                    yield ModelEvent(
                        type="provider_http_error",
                        data={
                            "http_status": response.status_code,
                            "code": code,
                            "message": redact(failure.get("message") or "模型请求失败。"),
                        },
                    )
                    raise ProviderError(
                        code,
                        failure.get("message", f"模型请求失败（HTTP {response.status_code}）。"),
                        response.status_code == 429
                        or response.status_code >= 500
                        or resource_error(code, failure.get("type", "")),
                    )
                block: list[str] = []
                async for line in response.aiter_lines():
                    if line.startswith("data:"):
                        block.append(line[5:].lstrip())
                    elif line == "" and block:
                        data = "\n".join(block)
                        block.clear()
                        if data == "[DONE]":
                            continue
                        item = event(data)
                        yield item
                        check_failure(item)
                        if completed:
                            break
                if block:
                    data = "\n".join(block)
                    if data != "[DONE]":
                        item = event(data)
                        yield item
                        check_failure(item)
            if not completed:
                raise ProviderError(
                    "interrupted_stream", "模型流未收到 response.completed，未执行其中的工具请求。", True
                )
        except httpx.HTTPError as error:
            raise ProviderError("transport_error", type(error).__name__, True) from error

    async def close(self) -> None:
        await self.http.aclose()


class ScriptedProvider:
    """Explicit offline fixture, never counted as real model evidence."""

    def __init__(self, responses: list[list[dict]]):
        self.responses = list(responses)
        self.requests: list[ModelRequest] = []

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        self.requests.append(request)
        if not self.responses:
            raise ProviderError("fixture_exhausted")
        items = self.responses.pop(0)
        yield ModelEvent(
            type="response.completed",
            data={
                "response": {
                    "id": f"offline-{len(self.requests)}",
                    "model": request.config.model,
                    "output": items,
                    "usage": {"input_tokens": 100, "output_tokens": 25},
                    "status": "completed",
                }
            },
        )
