"""Authentication field compatibility only: never selects or summarizes history."""
from .models import EFFORT, MODEL

UNSUPPORTED = {"background", "conversation", "max_output_tokens", "max_tool_calls", "metadata",
               "moderation", "multi_agent", "prompt", "prompt_cache_retention", "safety_identifier",
               "temperature", "top_logprobs", "top_p", "truncation", "user", "previous_response_id"}


def subscription_payload(body):
    if not isinstance(body, dict) or not isinstance(body.get("reasoning"), dict):
        raise ValueError("请求与reasoning必须是对象。")
    if body.get("model") != MODEL or body.get("reasoning", {}).get("effort") != EFFORT:
        raise ValueError("model_effort_mismatch")
    if not isinstance(body.get("input"), list) or body.get("previous_response_id"):
        raise ValueError("完整历史必须通过数组提供。")
    if any(not isinstance(item, dict) for item in body["input"]):
        raise ValueError("历史项目必须是对象。")
    if not isinstance(body.get("include", []), list) or any(not isinstance(v, str) for v in body.get("include", [])):
        raise ValueError("include必须是字符串数组。")
    if body.get("service_tier") not in {None, "auto", "default"}:
        raise ValueError("不能启用额外服务档位。")
    result = {k: v for k, v in body.items() if k not in UNSUPPORTED and k != "service_tier"}
    result.update(store=False, stream=True)
    result["input"] = [{**item, "role": "developer"} if item.get("role") == "system" else item
                       for item in body["input"]]
    result["include"] = list(dict.fromkeys([*result.get("include", []), "reasoning.encrypted_content"]))
    return result
