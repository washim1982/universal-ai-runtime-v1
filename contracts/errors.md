# Error codes

Every error on every transport has the same shape (proto `uar.v1.Error`):
`{"code", "message", "request_id", "retryable", "details"}`. HTTP bodies wrap it as `{"error": {...}}`;
gRPC returns the status code below and the full error as JSON in the `uar-error` trailing metadata.
`details` is redacted (keys containing key/secret/token/password/authorization/credential/cookie).

Clients must not retry non-idempotent calls automatically, whatever `retryable` says; the SDKs retry
only GETs and requests carrying an idempotency key.

| Code | HTTP | gRPC | Retryable by default |
|---|---|---|---|
| `invalid_argument` | 400 | INVALID_ARGUMENT | no |
| `invalid_graph` | 400 | INVALID_ARGUMENT | no |
| `unsupported_capability` | 400 | INVALID_ARGUMENT | no |
| `unauthenticated` | 401 | UNAUTHENTICATED | no |
| `permission_denied` | 403 | PERMISSION_DENIED | no |
| `policy_denied` | 403 | PERMISSION_DENIED | no |
| `egress_denied` | 403 | PERMISSION_DENIED | no |
| `not_found` | 404 | NOT_FOUND | no |
| `conflict` | 409 | ALREADY_EXISTS | no |
| `idempotency_mismatch` | 409 | FAILED_PRECONDITION | no |
| `failed_precondition` | 409 | FAILED_PRECONDITION | no |
| `payload_too_large` | 413 | INVALID_ARGUMENT | no |
| `rate_limited` | 429 | RESOURCE_EXHAUSTED | yes |
| `budget_exceeded` | 429 | RESOURCE_EXHAUSTED | no |
| `guardrail_blocked` | 422 | INVALID_ARGUMENT | no |
| `limit_exceeded` | 422 | RESOURCE_EXHAUSTED | no |
| `invalid_model_output` | 502 | INTERNAL | yes |
| `tool_error` | 502 | INTERNAL | no |
| `provider_error` | 502 | UNAVAILABLE | yes |
| `unavailable` | 503 | UNAVAILABLE | yes |
| `audit_unavailable` | 503 | UNAVAILABLE | yes |
| `deadline_exceeded` | 504 | DEADLINE_EXCEEDED | yes |
| `cancelled` | 499 | CANCELLED | no |
| `unimplemented` | 501 | UNIMPLEMENTED | no |
| `internal` | 500 | INTERNAL | no |

`guardrail_blocked`: an inference guardrail (input, tool result or output) blocked the call.
`details.stage` and `details.findings` name the checks that fired (check, type, count); the matched
text is never returned. Retrying the same content fails again. See `docs/guardrails.md`.
