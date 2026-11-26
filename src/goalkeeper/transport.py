"""Authenticated Responses forwarding and durable admission/cost records only."""
from __future__ import annotations

import asyncio
import json
import secrets
import socket
import time
import tiktoken
from pathlib import Path
from uuid import uuid4

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from goalkeeper.auth import AuthBroker, AuthError, RESOURCE
from .compat import subscription_payload
from .models import EFFORT, MODEL
from .storage import save_report
from goalkeeper.models import utcnow
from goalkeeper.secrets import redact
from .transport_forecast import forecast, valid_usage
from .streaming import StagedStream, StreamFailure


class MeteredTransport:
    def __init__(self, private: Path, report: Path, total_limit=None, per_run_limit=1_000_000,
                 http=None, auth=None, max_requests_per_run=None, transient_retries=0,
                 resilient=False, listen_host="127.0.0.1", advertised_host=None,
                 expected_model=MODEL, expected_effort=EFFORT):
        if type(transient_retries) is not int or transient_retries not in range(4):
            raise ValueError("每个请求的临时连接故障最多允许三次重试。")
        if (per_run_limit is not None and (type(per_run_limit) is not int or per_run_limit <= 0)
                or total_limit is not None and (type(total_limit) is not int or total_limit <= 0)
                or max_requests_per_run is not None and (type(max_requests_per_run) is not int or max_requests_per_run <= 0)):
            raise ValueError("调用额度和请求次数必须是正整数。")
        self.transient_retries = transient_retries
        self.resilient = resilient
        if not isinstance(listen_host, str) or not listen_host.strip():
            raise ValueError("监听地址不能为空。")
        if advertised_host is not None and (not isinstance(advertised_host, str) or not advertised_host.strip()):
            raise ValueError("对外地址不能为空。")
        if not isinstance(expected_model, str) or not expected_model.strip():
            raise ValueError("转发层预期模型不能为空。")
        if not isinstance(expected_effort, str) or not expected_effort.strip():
            raise ValueError("转发层预期推理强度不能为空。")
        self.listen_host = listen_host.strip()
        self.advertised_host = (advertised_host or self.listen_host).strip()
        self.expected_model = expected_model.strip()
        self.expected_effort = expected_effort.strip()
        self.auth = auth or AuthBroker(private)
        self.http = http or httpx.AsyncClient(timeout=httpx.Timeout(360, connect=30), trust_env=True)
        self.report = report
        self.total_limit, self.per_run_limit = total_limit, per_run_limit
        self.max_requests_per_run = max_requests_per_run
        previous = json.loads(report.read_text()) if report.exists() else {}
        self.records = previous.get("calls", [])
        self.retry_used = previous.get("retry_used", {})
        self.run_reasons = previous.get("run_stop_reasons", {})
        self.run_events = {}
        self.deadlines = {}
        self.active_streams = set()
        self.open_responses = {}
        self.call_started = {}
        self.stop = asyncio.Event()
        self.changed = asyncio.Event()
        self.reason = previous.get("stop_reason")
        self.first_reason = previous.get("first_stop_reason", self.reason)
        self.stop_events = previous.get("stop_events", [])
        if self.reason:
            self.stop.set()
        self.recovery_used = report.exists() and bool(json.loads(report.read_text()).get("recovery_used"))
        self.routes = {}
        self.route_keys = {}
        self.nonce = secrets.token_hex(32)
        self.server = self.task = None
        self.app = FastAPI()
        self.app.post("/" + self.nonce + "/{run_id}/v1/responses")(self.response)
        self.app.get("/" + self.nonce + "/{run_id}/v1/models")(self.models)
        self.encoding = tiktoken.get_encoding("o200k_base")

    def register(self, state):
        self.routes[state.id] = state.config.engine
        self.deadlines[state.id] = getattr(state, "deadline", None)
        self.route_keys[state.id] = secrets.token_hex(32)
        return {"DIAGNOSTIC_TRANSPORT_BASE": self.base(state.id), "LOCAL_ROUTE_TOKEN": self.route_keys[state.id]}

    def base(self, run_id):
        return f"http://{self.advertised_host}:{self.port}/{self.nonce}/{run_id}/v1"

    def known(self, run_id=None):
        return sum(c.get("tokens", 0) for c in self.records if run_id is None or c["run_id"] == run_id)

    def persist(self):
        save_report(self.report, {"calls": self.records, "known_tokens": self.known(),
                                "unknown_calls": sum(not c.get("usage_known", False) for c in self.records),
                                "in_flight": sum(c.get("status") == "in_flight" for c in self.records),
                                "stop_reason": self.reason, "first_stop_reason": self.first_reason,
                                "stop_events": self.stop_events, "recovery_used": self.recovery_used,
                                "reserved_or_unknown_estimate": self.reserved(),
                                "total_stop_threshold": self.total_limit,
                                "per_run_stop_threshold": self.per_run_limit,
                                "max_requests_per_run": self.max_requests_per_run,
                                "transient_retries_per_run": None if self.resilient else self.transient_retries,
                                "retry_used": self.retry_used,
                                "run_stop_reasons": self.run_reasons,
                                "failure_scope": "run" if self.resilient else "cohort",
                                "retry_scope": "request" if self.resilient else "run",
                                "transient_retries_per_request": self.transient_retries if self.resilient else None,
                                "buffer_until_completed": True,
                                "estimate_policy": "conservative-wire-proxy-v3; not actual usage or a hard billing cap",
                                "updated_at": utcnow()})
        self.changed.set()

    def pause(self, reason):
        self.stop_events.append({"reason": reason, "timestamp": utcnow(), "known_tokens": self.known()})
        self.first_reason = self.first_reason or reason
        self.reason = self.reason or reason
        self.stop.set()
        self.persist()

    def stopped_reason(self, run_id):
        return self.reason or self.run_reasons.get(run_id)

    def run_event(self, run_id):
        event = self.run_events.setdefault(run_id, asyncio.Event())
        if run_id in self.run_reasons:
            event.set()
        return event

    async def wait_stopped(self, run_id):
        tasks = [asyncio.create_task(event.wait()) for event in (self.stop, self.run_event(run_id))]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def pause_run(self, run_id, reason):
        if not self.resilient or reason in {"core_program_error", "configuration_error", "quota_unavailable",
                                            "returned_model_missing", "returned_model_mismatch"}:
            self.pause(reason)
            return
        self.run_reasons.setdefault(run_id, reason)
        self.run_event(run_id).set()
        self.stop_events.append({"reason": reason, "run_id": run_id, "timestamp": utcnow(),
                                 "known_tokens": self.known()})
        self.persist()

    def recover_once(self):
        if self.recovery_used or self.reason != "shared_service":
            return False
        self.recovery_used = True
        self.reason = None
        self.stop.clear()
        self.persist()
        return True

    def reserved(self, run_id=None):
        return sum(c.get("estimated_request_tokens", 0) for c in self.records
                   if not c.get("usage_known") and (run_id is None or c["run_id"] == run_id))

    def estimate(self, payload, run_id):
        return forecast(payload, self.encoding, self.records, self.routes.get(run_id))["total"]

    async def wait_settled(self, run_id, timeout):
        """Wait for durable accounting, without admitting or retrying requests."""
        async with asyncio.timeout(timeout):
            while any(c["run_id"] == run_id and c.get("status") == "in_flight" for c in self.records):
                self.changed.clear()
                await self.changed.wait()

    def admit(self, run_id):
        if run_id not in self.routes:
            return False
        if self.max_requests_per_run is not None and sum(c["run_id"] == run_id for c in self.records) >= self.max_requests_per_run:
            self.pause_run(run_id, "connectivity_request_limit")
        if self.total_limit is not None and self.known() >= self.total_limit:
            self.pause("integration_token_threshold")
        return not self.stopped_reason(run_id) and (self.per_run_limit is None or self.known(run_id) < self.per_run_limit)

    async def models(self, run_id: str, request: Request):
        if request.headers.get("authorization") != "Bearer " + self.route_keys.get(run_id, "missing") or run_id not in self.routes:
            return JSONResponse({}, 403)
        token = await self.auth.access_token()
        upstream = await self.http.get(RESOURCE + "/models", headers={"Authorization": "Bearer " + token})
        return JSONResponse(upstream.json(), upstream.status_code)

    def problem(self, code, message, status=503):
        return JSONResponse({"error": {"code": code, "message": message,
                                       "stop_reason": message if code == "diagnostic_stopped" else None,
                                       "origin": "local_forwarder"}}, status)

    def remaining(self, run_id):
        deadline = self.deadlines.get(run_id)
        return max(0, deadline - time.time()) if deadline is not None else 360

    async def reserve_call(self, run_id, payload, request_id=None, attempt=0):
        if not self.admit(run_id):
            return None, self.problem("diagnostic_stopped", self.stopped_reason(run_id) or "attempt_token_threshold", 429)
        detail = forecast(payload, self.encoding, self.records, self.routes[run_id])
        estimated, waiting = detail["total"], time.monotonic()
        while self.total_limit is not None and self.known() + self.reserved() + estimated > self.total_limit:
            if self.stop.is_set():
                return None, self.problem("diagnostic_stopped", self.reason, 429)
            if self.remaining(run_id) <= 0:
                return None, self.problem("diagnostic_time_budget", "Original deadline reached", 429)
            if any(c.get("status") == "in_flight" for c in self.records):
                self.changed.clear()
                try:
                    await asyncio.wait_for(self.changed.wait(), min(10, self.remaining(run_id)))
                except TimeoutError:
                    pass
                continue
            self.pause("integration_token_threshold")
            return None, self.problem("diagnostic_token_reservation", "Remaining shared budget cannot cover estimated request", 429)
        if not self.admit(run_id):
            return None, self.problem("diagnostic_stopped", self.reason or "attempt_token_threshold", 429)
        if self.remaining(run_id) <= 0:
            return None, self.problem("diagnostic_time_budget", "Original deadline reached", 429)
        if self.per_run_limit is not None and self.known(run_id) + self.reserved(run_id) + estimated > self.per_run_limit:
            return None, self.problem("diagnostic_attempt_token_budget", "Remaining attempt budget cannot cover estimated request", 429)
        record = {"run_id": run_id, "group": self.routes[run_id], "call": len(self.records) + 1,
                  "model": payload["model"], "effort": payload["reasoning"]["effort"],
                  "started_at": utcnow(), "status": "in_flight", "usage_known": False,
                  "estimated_request_tokens": estimated, "estimate_detail": detail,
                  "admission_wait_seconds": time.monotonic()-waiting,
                  "retry_attempt": attempt if self.resilient else self.retry_used.get(run_id, 0),
                  "request_id": request_id}
        self.records.append(record)
        self.call_started[record["call"]] = time.monotonic()
        self.persist()
        return record, None

    def finish(self, record, started, status=None):
        if status:
            record["status"] = status
        record.update(seconds=time.monotonic()-self.call_started.get(record["call"], started), finished_at=utcnow())
        self.persist()

    async def open_call(self, run_id, payload, request_id=None, attempt=0):
        record, problem = await self.reserve_call(run_id, payload, request_id, attempt)
        if problem is not None:
            return None, None, problem, False
        started = time.monotonic()
        upstream = None
        send_started = False
        try:
            async with asyncio.timeout(self.remaining(run_id)):
                token = await self.auth.access_token()
                request = self.http.build_request("POST", RESOURCE + "/responses",
                    json=payload, headers={"Authorization": "Bearer " + token})
                send_started = True
                upstream = await self.http.send(request, stream=True)
                record["http_status"] = upstream.status_code
                if upstream.status_code == 200:
                    self.open_responses[record["call"]] = (upstream, record)
                    return upstream, record, None, False
                raw = await upstream.aread()
            try:
                problem = redact(json.loads(raw))
                if not isinstance(problem, dict):
                    raise ValueError()
            except ValueError:
                problem = {"error": {"message": f"HTTP {upstream.status_code}"}}
            record.update(error=problem, failure_origin="upstream_http")
            try:
                record["retry_after_seconds"] = min(30, max(0, float(upstream.headers.get("retry-after", "0"))))
            except ValueError:
                record["retry_after_seconds"] = 0
            self.finish(record, started, "http_error")
            # Only transient server/rate failures recover; auth, quota and invalid requests do not.
            return None, record, JSONResponse(problem, upstream.status_code), (
                self.resilient and self.transient_http(problem, upstream.status_code))
        except asyncio.CancelledError:
            if not send_started:
                record.update(usage_known=True, tokens=0, usage={"input_tokens": 0, "output_tokens": 0},
                              failure_origin="authentication_cancelled")
            self.finish(record, started, "client_cancelled_usage_unknown" if send_started else "not_sent")
            raise
        except AuthError as error:
            record.update(error=redact(str(error)), failure_origin="authentication",
                          usage_known=True, tokens=0, usage={"input_tokens": 0, "output_tokens": 0})
            self.finish(record, started, "not_sent")
            self.pause("configuration_error")
            return None, record, self.problem("authentication_unavailable", "Authentication unavailable", 401), False
        except (httpx.TransportError, TimeoutError) as error:
            record.update(error=type(error).__name__, failure_origin="upstream_connection" if send_started else "authentication_connection")
            if not send_started:
                record.update(usage_known=True, tokens=0, usage={"input_tokens": 0, "output_tokens": 0})
            self.finish(record, started, "transport_error")
            return None, record, self.problem("transport_error", type(error).__name__), (
                self.remaining(run_id) > 0 and record.get("http_status") in {None, 200})
        except Exception as error:
            record.update(error=type(error).__name__, failure_origin="local_configuration")
            self.finish(record, started, "configuration_error")
            self.pause("configuration_error")
            raise
        finally:
            if upstream is not None and record.get("status") != "in_flight":
                await self.close_upstream(upstream, record)

    async def close_upstream(self, upstream, record):
        try:
            await asyncio.wait_for(upstream.aclose(), 3)
        except (httpx.TransportError, TimeoutError) as error:
            # A close failure after a complete response does not undo delivery
            # or change the original cause. Keep its diagnostic separately.
            record["close_error"] = type(error).__name__
            self.persist()
        finally:
            self.open_responses.pop(record["call"], None)

    async def retry(self, run_id, record, transient):
        # Never reset a run, replay a tool, erase unknown cost, or clear a cohort stop.
        used = record.get("retry_attempt", 0) if self.resilient else self.retry_used.get(run_id, 0)
        delay = max(min(2 ** used, 8), record.get("retry_after_seconds", 0)) if self.resilient else 1
        if (not transient or not self.transient_retries or self.stopped_reason(run_id)
                or used >= self.transient_retries
                or self.remaining(run_id) <= delay):
            return False
        self.retry_used[run_id] = self.retry_used.get(run_id, 0) + 1
        record["retry_scheduled"] = True
        record["retry_delay_seconds"] = delay
        self.persist()
        await asyncio.sleep(min(delay, self.remaining(run_id)))
        return not self.stopped_reason(run_id) and self.remaining(run_id) > 0

    @classmethod
    def transient_http(cls, problem, status=None):
        if cls.failure_reason(problem, status) == "quota_unavailable":
            return False
        error = problem.get("error", problem) if isinstance(problem, dict) else {}
        code = error.get("code") if isinstance(error, dict) else None
        return status in {408, 429, 500, 502, 503, 504} or code in {
            "server_error", "server_is_overloaded", "rate_limit_exceeded"}

    def pause_failure(self, record):
        if self.stop.is_set():
            return
        status = (record or {}).get("http_status")
        problem = (record or {}).get("error", {})
        self.pause_run(record["run_id"], self.failure_reason(problem, status))

    @staticmethod
    def failure_reason(problem, status=None):
        error = problem.get("error", problem) if isinstance(problem, dict) else {}
        error = error if isinstance(error, dict) else {}
        code = error.get("code", "")
        if isinstance(code, str) and (code in {"insufficient_quota", "quota_exceeded"}
                                     or "usage_limit" in code or "usage_unavailable" in code):
            return "quota_unavailable"
        return "configuration_error" if status in {400, 401, 403, 404} else "shared_service"

    def account_terminal(self, record, payload, terminal):
        response = terminal.get("response", {})
        if not isinstance(response, dict):
            raise StreamFailure("invalid_stream_event")
        usage = response.get("usage")
        record.update(returned_model=response.get("model"), usage=usage, usage_known=valid_usage(usage))
        if valid_usage(usage):
            record["tokens"] = usage["input_tokens"] + usage["output_tokens"]
        if response.get("model") not in {None, payload["model"]}:
            record["status"] = "rejected_model"
            self.pause("returned_model_mismatch")
            return False
        if terminal.get("type") != "response.completed" or response.get("status") not in {None, "completed"}:
            record.update(status="terminal_error", error=redact(terminal), failure_origin="upstream_event")
            if not (self.resilient and self.transient_http(response.get("error") or terminal)):
                self.pause_run(record["run_id"], self.failure_reason(response.get("error") or terminal))
            return False
        record["status"] = "completed"
        if response.get("model") != payload["model"]:
            self.pause("returned_model_missing" if response.get("model") is None else "returned_model_mismatch")
            return False
        if not valid_usage(usage):
            self.pause_run(record["run_id"], "response_usage_missing")
            return False
        if self.total_limit is not None and self.known() >= self.total_limit:
            self.pause("integration_token_threshold")
        return True

    async def response(self, run_id: str, request: Request):
        if run_id not in self.routes or request.headers.get("authorization") != "Bearer " + self.route_keys.get(run_id, "missing"):
            return self.problem("invalid_route_key", "Invalid route", 403)
        if not self.admit(run_id):
            return self.problem("diagnostic_stopped", self.stopped_reason(run_id) or "attempt_token_threshold", 429)
        try:
            payload = subscription_payload(
                await request.json(),
                expected_model=self.expected_model,
                expected_effort=self.expected_effort,
            )
        except ValueError as error:
            self.pause("configuration_error")
            return self.problem("configuration_mismatch", str(error), 400)
        request_id = uuid4().hex

        async def acquire(attempt=0):
            while True:
                upstream, record, problem, transient = await self.open_call(run_id, payload, request_id, attempt)
                if problem is None:
                    return upstream, record, None
                if record is not None and await self.retry(run_id, record, transient):
                    attempt += 1
                    continue
                if record is not None:
                    self.pause_failure(record)
                return None, record, problem

        upstream, record, problem = await acquire()
        if problem is not None:
            return problem

        async def stream():
            nonlocal upstream, record
            while True:
                staged = StagedStream(self.report.parent)
                started = time.monotonic()
                failure = None
                transient = False
                terminal_code = None
                try:
                    async with asyncio.timeout(self.remaining(run_id)):
                        async for heartbeat in staged.collect(upstream):
                            yield heartbeat
                    usable = self.account_terminal(record, payload, staged.terminal)
                    if usable:
                        record["forwarded"] = True
                    self.finish(record, started)
                    if usable:
                        for chunk in staged.chunks():
                            yield chunk
                        return
                    error = staged.terminal.get("response", {}).get("error") or staged.terminal.get("error") or {}
                    terminal_code = error.get("code") if isinstance(error, dict) and record["status"] == "terminal_error" else None
                    transient = self.resilient and record["status"] == "terminal_error" and self.transient_http(error)
                except asyncio.CancelledError:
                    if record.get("status") == "in_flight":
                        self.finish(record, started, "client_cancelled_usage_unknown")
                    raise
                except (httpx.TransportError, StreamFailure, TimeoutError) as error:
                    failure = error
                    record.update(error=getattr(error, "code", type(error).__name__), failure_origin="upstream_stream")
                    self.finish(record, started, "interrupted_usage_unknown")
                    transient = (isinstance(error, httpx.TransportError)
                                 or isinstance(error, StreamFailure) and error.transient)
                except Exception as error:
                    record.update(error=type(error).__name__, failure_origin="local_forwarder")
                    self.finish(record, started, "forwarder_error")
                    self.pause("core_program_error")
                    raise
                finally:
                    staged.close()
                    await self.close_upstream(upstream, record)
                if await self.retry(run_id, record, transient):
                    terminal_code = None
                    upstream, record, problem = await acquire(record.get("retry_attempt", 0) + 1)
                    if problem is None:
                        continue
                    problem_data = json.loads(problem.body).get("error", {})
                    failure_code = problem_data.get("stop_reason") or problem_data.get("code")
                else:
                    reason = ("time_budget" if self.remaining(run_id) <= 0 else
                              "stream_protocol_error" if isinstance(failure, StreamFailure) and not failure.transient else
                              self.failure_reason(record.get("error", {}), record.get("http_status")))
                    if not self.stopped_reason(run_id):
                        self.pause_run(run_id, reason)
                    failure_code = None
                code = terminal_code or self.stopped_reason(run_id) or failure_code or "diagnostic_stopped"
                yield ("data: " + json.dumps({"type": "error", "error": {"code": code,
                       "message": "Forwarding stopped; original task deadline retained"}}) + "\n\n").encode()
                return

        async def forward():
            task = asyncio.current_task()
            self.active_streams.add(task)
            iterator = stream()
            try:
                async for chunk in iterator:
                    yield chunk
            finally:
                try:
                    await iterator.aclose()
                finally:
                    self.active_streams.discard(task)
        return StreamingResponse(forward(), media_type="text/event-stream")

    async def start(self):
        sock = socket.socket()
        sock.bind((self.listen_host, 0))
        sock.listen(128)
        sock.setblocking(False)
        self.port = sock.getsockname()[1]
        self.server = uvicorn.Server(uvicorn.Config(self.app, log_level="error", access_log=False, lifespan="off"))
        self.server.install_signal_handlers = lambda: None
        self.task = asyncio.create_task(self.server.serve(sockets=[sock]))
        while not self.server.started:
            if self.task.done():
                await self.task
            await asyncio.sleep(.01)

    async def close(self):
        try:
            if self.server:
                self.server.should_exit = True
                try:
                    await asyncio.wait_for(self.task, 3)
                except TimeoutError:
                    self.task.cancel()
                    await asyncio.gather(self.task, return_exceptions=True)
            # Only our own response workers are cancelled during final shutdown.
            active = [task for task in self.active_streams if task is not asyncio.current_task()]
            for task in active:
                task.cancel()
            if active:
                await asyncio.gather(*active, return_exceptions=True)
            for upstream, record in list(self.open_responses.values()):
                await self.close_upstream(upstream, record)
            await self.http.aclose()
        finally:
            try:
                await self.auth.close()
            finally:
                for record in self.records:
                    if record.get("status") == "in_flight":
                        record.update(status="shutdown_interrupted_usage_unknown", finished_at=utcnow())
                self.persist()
