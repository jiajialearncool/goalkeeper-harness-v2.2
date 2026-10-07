import asyncio
import json
import time
from types import SimpleNamespace

import httpx
import pytest

from goalkeeper.compat import subscription_payload
from goalkeeper.evaluation import evaluate, parallel
from goalkeeper.native import configuration
from goalkeeper.sandbox import Scope, Seatbelt
from goalkeeper.transport import MeteredTransport


async def test_four_jobs_overlap_and_one_failure_does_not_kill_peers(tmp_path):
    marks = []

    async def job(group):
        started = time.monotonic()
        marks.append((group, "start", started))
        path = tmp_path / group
        path.mkdir()
        (path / "state.json").write_text(json.dumps({"group": group, "tokens": 5}))
        await asyncio.sleep(0.05)
        marks.append((group, "end", time.monotonic()))
        if group == "pi":
            raise RuntimeError("ordinary task failure")
        return group

    results = await parallel([lambda g=g: job(g) for g in ("goalkeeper", "codex", "opencode", "pi")])
    assert max(t for g, k, t in marks if k == "start") < min(t for g, k, t in marks if k == "end")
    assert results[:3] == ["goalkeeper", "codex", "opencode"] and isinstance(results[3], RuntimeError)
    for group in ("goalkeeper", "codex", "opencode", "pi"):
        assert json.loads((tmp_path / group / "state.json").read_text())["group"] == group


async def test_core_failure_cancels_only_pending_jobs_and_preserves_finished_results():
    failure = asyncio.Event()
    completed = asyncio.Event()
    cancelled = asyncio.Event()
    async def success():
        completed.set()
        return "finished_success"
    async def core_error():
        await completed.wait()
        failure.set()
        return "core_program_error"
    async def pending():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    results = await asyncio.wait_for(parallel([success, core_error, pending], failure), .2)
    assert results[:2] == ["finished_success", "core_program_error"]
    assert isinstance(results[2], asyncio.CancelledError) and cancelled.is_set()


async def test_real_evaluation_requires_separate_authorization_before_creating_auth():
    with pytest.raises(ValueError, match="禁止真实评测"):
        await evaluate(__import__("pathlib").Path("not-used"), authorized=False, shared_limit=100)


@pytest.mark.parametrize("model,effort", [("other", "xhigh"), ("gpt-6.1-sol", "high")])
def test_transport_rejects_model_substitution(model, effort):
    with pytest.raises(ValueError):
        subscription_payload({"model": model, "reasoning": {"effort": effort}, "input": []})


def test_compatibility_preserves_native_context_and_tools():
    body = {
        "model": "gpt-6.1-sol",
        "reasoning": {"effort": "xhigh"},
        "input": [
            {"type": "reasoning", "encrypted_content": "keep"},
            {"type": "function_call", "call_id": "a", "name": "read", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "a", "output": "keep result"},
        ],
        "tools": [{"name": "native_tool"}],
        "max_output_tokens": 1,
    }
    result = subscription_payload(body)
    assert result["input"] == body["input"] and result["tools"] == body["tools"]
    assert "max_output_tokens" not in result


def test_native_configuration_separates_directories_and_fixes_all_models(tmp_path):
    for group in ("pi", "opencode"):
        root = tmp_path / group
        root.mkdir()
        runner = Seatbelt(Scope(root))
        argv, env = configuration(group, "http://127.0.0.1:1234/run/v1", "synthetic", runner)
        assert env["HOME"] == str(root / ".home")
        if group == "pi":
            assert "--thinking" in argv and "xhigh" in argv
            data = json.loads((root / ".home/pi/models.json").read_text())
            assert data["providers"]["subscription"]["models"][0]["id"] == "gpt-6.1-sol"
        else:
            data = json.loads(env["OPENCODE_CONFIG_CONTENT"])
            assert data["model"] == data["small_model"] == "subscription/gpt-6.1-sol"
            assert (
                data["provider"]["subscription"]["models"]["gpt-6.1-sol"]["options"]["reasoningEffort"]
                == "xhigh"
            )


class Auth:
    async def access_token(self):
        return "synthetic"

    async def close(self):
        pass


async def test_meter_threshold_unknown_usage_and_route_isolation(tmp_path):
    meter = MeteredTransport(
        tmp_path / "auth",
        tmp_path / "cost.json",
        total_limit=10,
        auth=Auth(),
        http=httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))),
    )
    meter.port = 1234
    route = meter.register(SimpleNamespace(id="one", config=SimpleNamespace(engine="pi")))
    other = meter.register(SimpleNamespace(id="two", config=SimpleNamespace(engine="codex")))
    assert route["LOCAL_ROUTE_TOKEN"] != other["LOCAL_ROUTE_TOKEN"]
    meter.records.append({"run_id": "one", "tokens": 11, "usage_known": True, "status": "completed"})
    assert not meter.admit("one") and meter.first_reason == "integration_token_threshold"
    meter.pause("cleanup_cancel")
    assert meter.first_reason == "integration_token_threshold"
    meter.records.append(
        {"run_id": "two", "usage_known": False, "estimated_request_tokens": 25, "status": "failed"}
    )
    meter.persist()
    data = json.loads(meter.report.read_text())
    assert (
        data["known_tokens"] == 11
        and data["reserved_or_unknown_estimate"] == 25
        and data["unknown_calls"] == 1
    )
    await meter.close()


async def test_candidate_cannot_start_without_matching_connectivity(tmp_path):
    from goalkeeper.evaluation import GROUPS
    with pytest.raises(ValueError, match="缺少四组真实接入"):
        await evaluate(tmp_path, authorized=True, shared_limit=100, readiness=None)
    proof = tmp_path / "proof.json"
    proof.write_text(json.dumps({"kind": "four_native_connectivity", "passed_groups": list(GROUPS),
                                 "all_passed": True,
                                 "execution_identity": {"source_hash": "different"}}))
    with pytest.raises(ValueError, match="发生变化"):
        await evaluate(tmp_path, authorized=True, shared_limit=100, readiness=proof)


async def test_candidate_rejects_all_groups_list_when_final_proof_is_false(tmp_path):
    from goalkeeper.evaluation import GROUPS, execution_identity
    proof = tmp_path / "proof.json"
    proof.write_text(json.dumps({"kind": "four_native_connectivity", "passed_groups": list(GROUPS),
                                 "all_passed": False, "execution_identity": execution_identity()}))
    with pytest.raises(ValueError, match="未全部通过"):
        await evaluate(tmp_path, authorized=True, shared_limit=100, readiness=proof)


def test_actual_model_must_be_returned_and_match():
    from goalkeeper.evaluation import verified_models
    call = {"model": "gpt-6.1-sol", "effort": "xhigh", "status": "completed",
            "returned_model": "gpt-6.1-sol"}
    assert verified_models([call])
    assert not verified_models([])
    assert not verified_models([{**call, "returned_model": None}])
    assert not verified_models([{**call, "effort": "high"}])


def test_transport_retry_policy_is_part_of_frozen_identity():
    from goalkeeper.evaluation import execution_identity
    disabled, enabled = execution_identity(0), execution_identity(1)
    assert disabled['source_hash'] == enabled['source_hash']
    assert disabled != enabled
    assert disabled['transport_policy']['transient_retries_per_request'] == 0
    assert enabled['transport_policy']['transient_retries_per_request'] == 1


async def test_candidate_rejects_connect_with_different_retry_policy(tmp_path):
    from goalkeeper.evaluation import GROUPS, execution_identity
    proof = tmp_path/'proof.json'
    proof.write_text(json.dumps({'kind': 'four_native_connectivity', 'all_passed': True,
                                 'passed_groups': list(GROUPS), 'execution_identity': execution_identity(0)}))
    with pytest.raises(ValueError, match='发生变化'):
        await evaluate(tmp_path, authorized=True, shared_limit=100, readiness=proof, transient_retries=1)
