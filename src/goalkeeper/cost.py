from __future__ import annotations

import math
from copy import deepcopy

from .storage import canonical
from .models import MODEL, EFFORT, ModelRequest


def valid_usage(value):
    return isinstance(value, dict) and all(type(value.get(k)) is int and value[k] >= 0
                                          for k in ("input_tokens", "output_tokens"))


def totals(records):
    return {"known_tokens": sum(c["usage"]["input_tokens"] + c["usage"]["output_tokens"]
                                for c in records if valid_usage(c.get("usage"))),
            "unknown_calls": sum(not valid_usage(c.get("usage")) for c in records),
            "unknown_reserve": sum(c["forecast"] for c in records if not valid_usage(c.get("usage"))),
            "usage_complete": all(valid_usage(c.get("usage")) for c in records)}


class Tokens:
    def __init__(self, encoding):
        self.encoding = encoding

    def count(self, value):
        text = value if isinstance(value, str) else canonical(value)
        return len(self.encoding.encode(text, disallowed_special=()))

    def clip(self, value, limit):
        encoded = self.encoding.encode(value, disallowed_special=())
        return self.encoding.decode(encoded[:limit])

    def preview(self, value, limit):
        encoded = self.encoding.encode(value, disallowed_special=())
        if len(encoded) <= limit:
            return value
        marker = "\n…[输出已截断，完整内容见归档]…\n"
        available = max(0, limit - self.count(marker))
        head = available // 3
        tail = available - head
        return self.encoding.decode(encoded[:head]) + marker + self.encoding.decode(encoded[-tail:] if tail else [])

    def context(self, value, records=(), role="main"):
        """Estimate model input from this run's usage; never rewrite sent reasoning bytes.

        Visible JSON has a conservative floor. Opaque cost is unknown until a matching
        completed request observes it; budget reservation remains the wire forecast.
        """
        model, effort = MODEL, EFFORT
        if isinstance(value, ModelRequest):
            model, effort = value.config.model, value.config.effort
            value = value.model_dump(exclude={"config", "cache_key"})
        wire = self.count(value)
        visible_value = deepcopy(value)
        def strip(item):
            if isinstance(item, dict):
                item.pop("encrypted_content", None)
                for child in item.values():
                    strip(child)
            elif isinstance(item, list):
                for child in item:
                    strip(child)
        strip(visible_value)
        visible = self.count(visible_value)
        opaque = max(0, wire - visible)
        def usable_features(record):
            info = record.get("input_features")
            return (isinstance(info, dict) and info.get("policy") == "context-v1"
                    and type(info.get("visible_tokens")) is int and info["visible_tokens"] > 0
                    and type(info.get("opaque_proxy_tokens")) is int and info["opaque_proxy_tokens"] >= 0)
        peers = [c for c in records if c.get("role") == role and c.get("model") == model
                 and c.get("effort") == effort and c.get("status") == "completed"
                 and valid_usage(c.get("usage")) and usable_features(c)][-8:]
        plain = [c for c in peers if not c["input_features"]["opaque_proxy_tokens"]]
        visible_factor = max([1.0, *(c["usage"]["input_tokens"] / max(1, c["input_features"]["visible_tokens"])
                                    for c in plain)])
        encrypted = [c for c in peers if c["input_features"]["opaque_proxy_tokens"]]
        opaque_factor = max([.1, *(max(0, c["usage"]["input_tokens"] -
                                          c["input_features"]["visible_tokens"] * visible_factor) /
                                   c["input_features"]["opaque_proxy_tokens"] for c in encrypted)]) if encrypted else 1.0
        estimated = math.ceil((visible * visible_factor + opaque * opaque_factor) * 1.1)
        return {"policy": "context-v1", "wire_proxy_tokens": wire, "visible_tokens": visible,
                "opaque_proxy_tokens": opaque, "visible_factor": visible_factor, "opaque_factor": opaque_factor,
                "samples": len(peers), "opaque_samples": len(encrypted), "estimated_tokens": estimated,
                "is_actual_usage": False, "hard_billing_bound": False}

    def forecast(self, request, records):
        count = self.count(request.model_dump())
        same_role = [c for c in records if c.get("input_estimate") and valid_usage(c.get("usage"))]
        multiplier = max([1, *(c["usage"]["input_tokens"] / c["input_estimate"] for c in same_role)])
        output = max([request.config.output_forecast_tokens,
                      *(c["usage"]["output_tokens"] for c in same_role)])
        return count, math.ceil(count * multiplier) + output
