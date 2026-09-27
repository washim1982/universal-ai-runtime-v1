"""Structured errors shared by every transport.

Each error carries a stable `code`, a display-safe `message`, and `retryable`.
Details are redacted before they leave the process.
"""
from __future__ import annotations

from typing import Any

import grpc

# code -> (HTTP status, gRPC status, retryable default)
CODES: dict[str, tuple[int, grpc.StatusCode, bool]] = {
    "invalid_argument": (400, grpc.StatusCode.INVALID_ARGUMENT, False),
    "invalid_graph": (400, grpc.StatusCode.INVALID_ARGUMENT, False),
    "unsupported_capability": (400, grpc.StatusCode.INVALID_ARGUMENT, False),
    "unauthenticated": (401, grpc.StatusCode.UNAUTHENTICATED, False),
    "permission_denied": (403, grpc.StatusCode.PERMISSION_DENIED, False),
    "policy_denied": (403, grpc.StatusCode.PERMISSION_DENIED, False),
    "egress_denied": (403, grpc.StatusCode.PERMISSION_DENIED, False),
    "not_found": (404, grpc.StatusCode.NOT_FOUND, False),
    "conflict": (409, grpc.StatusCode.ALREADY_EXISTS, False),
    "idempotency_mismatch": (409, grpc.StatusCode.FAILED_PRECONDITION, False),
    "failed_precondition": (409, grpc.StatusCode.FAILED_PRECONDITION, False),
    "payload_too_large": (413, grpc.StatusCode.INVALID_ARGUMENT, False),
    "rate_limited": (429, grpc.StatusCode.RESOURCE_EXHAUSTED, True),
    "budget_exceeded": (429, grpc.StatusCode.RESOURCE_EXHAUSTED, False),
    "limit_exceeded": (422, grpc.StatusCode.RESOURCE_EXHAUSTED, False),
    "invalid_model_output": (502, grpc.StatusCode.INTERNAL, True),
    "tool_error": (502, grpc.StatusCode.INTERNAL, False),
    "provider_error": (502, grpc.StatusCode.UNAVAILABLE, True),
    "unavailable": (503, grpc.StatusCode.UNAVAILABLE, True),
    "audit_unavailable": (503, grpc.StatusCode.UNAVAILABLE, True),
    "deadline_exceeded": (504, grpc.StatusCode.DEADLINE_EXCEEDED, True),
    "cancelled": (499, grpc.StatusCode.CANCELLED, False),
    "unimplemented": (501, grpc.StatusCode.UNIMPLEMENTED, False),
    "internal": (500, grpc.StatusCode.INTERNAL, False),
}

_SENSITIVE = ("key", "secret", "token", "password", "authorization", "credential", "cookie")


def redact(value: Any, depth: int = 0) -> Any:
    """Remove values under sensitive-looking keys; bound depth and size."""
    if depth > 6:
        return "…"
    if isinstance(value, dict):
        return {k: ("[redacted]" if any(s in str(k).lower() for s in _SENSITIVE) else redact(v, depth + 1))
                for k, v in list(value.items())[:50]}
    if isinstance(value, list):
        return [redact(v, depth + 1) for v in value[:50]]
    if isinstance(value, str) and len(value) > 500:
        return value[:500] + "…"
    return value


class UARError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool | None = None,
                 details: dict[str, Any] | None = None):
        if code not in CODES:
            code = "internal"
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = CODES[code][2] if retryable is None else retryable
        self.details = details or {}

    @property
    def http_status(self) -> int:
        return CODES[self.code][0]

    @property
    def grpc_status(self) -> grpc.StatusCode:
        return CODES[self.code][1]

    def to_dict(self, request_id: str = "") -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "request_id": request_id,
                "retryable": self.retryable, "details": redact(self.details)}

    def __repr__(self) -> str:
        return f"UARError({self.code!r}, {self.message!r})"


def invalid(message: str, **details: Any) -> UARError:
    return UARError("invalid_argument", message, details=details)


def denied(message: str, code: str = "permission_denied", **details: Any) -> UARError:
    return UARError(code, message, details=details)


def not_found(what: str) -> UARError:
    return UARError("not_found", f"{what} not found")
