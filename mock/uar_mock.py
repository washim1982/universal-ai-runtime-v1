"""uar-mock: serves the shared conformance fixtures so SDKs can be tested without a runtime.

    python mock/uar_mock.py --port 9100

A request matches a fixture when method, path and JSON body are equal and the fixture's auth
expectation holds ("dev": an X-API-Key or bearer header is present; "none": no credentials).
Unmatched requests get 404 {"error": {"code": "no_fixture", ...}}.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

FIXTURES = Path(__file__).resolve().parents[1] / "contracts" / "fixtures"


def load() -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(FIXTURES.glob("*.json"))]


def create_app(fixtures: list[dict] | None = None) -> Starlette:
    fx = fixtures if fixtures is not None else load()

    async def handle(request: Request) -> Response:
        raw = await request.body()
        body = json.loads(raw) if raw else None
        authed = bool(request.headers.get("x-api-key") or request.headers.get("authorization"))
        for f in fx:
            req = f["request"]
            if req["method"] != request.method or req["path"] != request.url.path:
                continue
            if req.get("body") != body:
                continue
            if (f["auth"] == "none") == authed:
                continue
            resp = f["response"]
            if resp.get("events") is not None:
                async def gen(events=resp["events"]):
                    for ev in events:
                        kind = next(k for k in ev if k not in ("run_id", "seq", "ts", "trace_id", "request_id"))
                        payload = {"type": kind, **ev}
                        yield f"id: {ev['seq']}\nevent: {kind}\ndata: {json.dumps(payload)}\n\n".encode()
                return StreamingResponse(gen(), media_type="text/event-stream")
            return JSONResponse(resp["body"], status_code=resp["status"], headers={"X-Request-ID": "req_mock"})
        return JSONResponse({"error": {"code": "no_fixture", "message": f"no fixture matches {request.method} "
                                       f"{request.url.path}", "request_id": "req_mock", "retryable": False,
                                       "details": {}}}, status_code=404)

    return Starlette(routes=[Route("/{path:path}", handle, methods=["GET", "POST"])])


def main() -> None:
    import uvicorn
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9100)
    a = ap.parse_args()
    uvicorn.run(create_app(), host=a.host, port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
