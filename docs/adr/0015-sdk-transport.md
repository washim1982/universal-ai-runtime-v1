# ADR 0015: All SDKs use the HTTP/JSON + SSE surface

Status: **Accepted** 2026-09-27 (M8). Refines ADR 12.

**Context.** The plan suggested gRPC-based clients for Java, .NET, Go and Rust. The runtime serves the
same service over gRPC, HTTP/JSON + SSE and WebSocket, and the SDK conformance suite (shared fixtures
and `uar-mock`) is HTTP-based.

**Decision.** All six SDKs use HTTP/JSON + SSE. Types come from the contract: Go decodes into the
generated protobuf types (`sdks/go/uarv1`); the others model the same messages and keep unknown
fields so responses round-trip. gRPC remains available: Go stubs are committed, Java/.NET/Rust can
generate stubs from `proto/` with their standard tooling (the Java and .NET plugin SDKs already do).

**Consequences.** One fixture set and one mock verify every SDK, and every SDK passes the same
fixtures against the live runtime. Streaming uses SSE everywhere (HTTP/1.1 friendly, proxy friendly).
Clients that need gRPC-specific features (bidirectional streams in future) can use the stubs.
