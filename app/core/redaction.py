"""Best-effort removal of credentials from text before it is stored or logged.

Pattern-based redaction cannot recognise every secret, so callers should still
avoid storing raw provider error text. This is a second line of defence.
"""

import re
from collections.abc import Iterable

REDACTED = "[REDACTED]"

_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-*]{6,}"),  # OpenAI-style keys, including partly masked ones
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-~+/=]{6,}"),
    re.compile(r"(?i)(api[_-]?key|authorization|token|secret|password)(\s*[\"']?\s*[:=]\s*[\"']?)[^\s\"',}]+"),
]


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    for secret in secrets:
        if secret and len(secret) >= 4:
            text = text.replace(secret, REDACTED)
    for pattern in _PATTERNS:
        if pattern.groups >= 2:
            text = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
        else:
            text = pattern.sub(REDACTED, text)
    return text
