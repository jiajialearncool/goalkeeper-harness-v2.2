"""Admission forecasts, not billed usage. Opaque wire data is conservatively retained."""
from __future__ import annotations

import hashlib
import json
import math


def valid_usage(usage):
    return (isinstance(usage, dict) and all(type(usage.get(k)) is int and usage[k] >= 0
            for k in ("input_tokens", "output_tokens")))


def features(payload, encoding):
    wire = json.dumps(payload, ensure_ascii=False)
    opaque_chars = 0
    def visit(value):
        nonlocal opaque_chars
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "encrypted_content" and isinstance(item, str):
                    opaque_chars += len(item)
                else:
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
    visit(payload)
    return {"wire_proxy_tokens": len(encoding.encode(wire, disallowed_special=())),
            "opaque_characters": opaque_chars,
            "request_sha256": hashlib.sha256(wire.encode()).hexdigest()}


def forecast(payload, encoding, records, group):
    info = features(payload, encoding)
    peers = [c for c in records if c.get("group") == group and c.get("model") == payload["model"]
             and c.get("effort") == payload["reasoning"]["effort"] and valid_usage(c.get("usage"))]
    paired = [c for c in peers if c.get("estimate_detail", {}).get("wire_proxy_tokens", 0) > 0]
    # Never reduce unknown reservations or claim ciphertext bytes are billed text.
    # Same-group observed underestimates only increase subsequent safety forecasts.
    ratio = max([1.0, *(c["usage"]["input_tokens"] / c["estimate_detail"]["wire_proxy_tokens"] for c in paired)])
    output = max([4000, *(c["usage"]["output_tokens"] for c in peers)])
    total = math.ceil(info["wire_proxy_tokens"] * ratio) + output
    return {**info, "input_multiplier": ratio, "output_forecast": output,
            "paired_samples": len(paired), "output_samples": len(peers), "total": total,
            "method": "conservative-wire-proxy-v3", "is_actual_usage": False,
            "hard_billing_bound": False, "opaque_cost_known": info["opaque_characters"] == 0}


def historical_audit(records):
    rows, groups = [], {}
    for c in records:
        if not c.get("usage_known") or not valid_usage(c.get("usage")):
            continue
        actual = c["usage"]["input_tokens"] + c["usage"]["output_tokens"]
        estimate = c.get("estimated_request_tokens")
        group = groups.setdefault(c["group"], {"known_tokens": 0, "completed_samples": 0,
                                              "paired_samples": 0, "underestimated_samples": 0})
        group["known_tokens"] += actual
        group["completed_samples"] += 1
        if estimate:
            group["paired_samples"] += 1
            group["underestimated_samples"] += actual > estimate
            rows.append({"run_id": c["run_id"], "group": c["group"], "call": c["call"],
                         "forecast": estimate, "actual": actual, "actual_over_forecast": actual/estimate})
    return {"groups": groups, "paired_calls": rows,
            "unknown_calls": sum(not c.get("usage_known") for c in records),
            "unknown_reserve": sum(c.get("estimated_request_tokens", 0) for c in records if not c.get("usage_known")),
            "limitation": "Historical records lack full per-request features. Do not derive a reduction multiplier, reprice missing usage, or treat forecasts as a hard billing bound."}
