from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any


REDACTED = "[REDACTED]"

_CREDENTIAL_PATTERNS = (
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{8,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{8,}\b"),
    re.compile(
        r"(?i)\b(?:authorization\s*[:=]\s*)?"
        r"(?:bearer|token)\s+[A-Za-z0-9._~+/=-]{8,}"
    ),
    re.compile(
        r"(?i)\b(?:access_token|api[_-]?key|client_secret|password)"
        r"\s*=\s*[^&\s,;]+"
    ),
)


class SensitiveDataError(ValueError):
    """Raised before credential-like data can cross a persistence boundary."""


def redact_text(value: object, *, secrets: Sequence[str | None] = ()) -> str:
    redacted = str(value)
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, REDACTED)
    for pattern in _CREDENTIAL_PATTERNS:
        redacted = pattern.sub(REDACTED, redacted)
    return redacted


def contains_sensitive_text(
    value: object, *, secrets: Sequence[str | None] = ()
) -> bool:
    original = str(value)
    return redact_text(original, secrets=secrets) != original


def ensure_no_sensitive_data(
    value: Any,
    *,
    secrets: Sequence[str | None] = (),
    context: str = "data",
) -> None:
    if isinstance(value, str):
        if contains_sensitive_text(value, secrets=secrets):
            raise SensitiveDataError(
                f"Credential-like content is not allowed in {context}"
            )
        return
    if isinstance(value, bytes):
        ensure_no_sensitive_bytes(value, secrets=secrets, context=context)
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            ensure_no_sensitive_data(
                str(key),
                secrets=secrets,
                context=context,
            )
            ensure_no_sensitive_data(item, secrets=secrets, context=context)
        return
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        for item in value:
            ensure_no_sensitive_data(item, secrets=secrets, context=context)


def ensure_no_sensitive_bytes(
    value: bytes,
    *,
    secrets: Sequence[str | None] = (),
    context: str = "binary data",
) -> None:
    for secret in secrets:
        if secret and secret.encode("utf-8") in value:
            raise SensitiveDataError(
                f"Credential-like content is not allowed in {context}"
            )
    decoded = value.decode("utf-8", errors="ignore")
    if contains_sensitive_text(decoded):
        raise SensitiveDataError(
            f"Credential-like content is not allowed in {context}"
        )
