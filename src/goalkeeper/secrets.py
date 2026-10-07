import re
from typing import Any

SECRET_KEYS = re.compile(
    r"(?i)(access_token|refresh_token|id_token|code_verifier|authorization|password|api_key|session_secret)"
)
PATTERNS = (
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"(?i)Bearer\s+\S+"),
    re.compile(
        r"(?i)\b(?:access_token|refresh_token|id_token|code_verifier|api[_-]?key|password)\s*[:=]\s*[^\s,;]+"
    ),
)


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: "[REDACTED]" if SECRET_KEYS.search(k) else redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        for pattern in PATTERNS:
            value = pattern.sub("[REDACTED]", value)
        return value
    return value


def contains_secret(value: str) -> bool:
    return redact(value) != value or bool(
        re.search(r"(?i)(api[_-]?key|password|token)\s*[:=]\s*\S{8,}", value)
    )
