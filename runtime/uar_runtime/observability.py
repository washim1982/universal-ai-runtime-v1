"""Tracing (OpenTelemetry), metrics (Prometheus) and structured logs.

Trace context is persisted with each run (W3C traceparent) so worker spans join the
trace of the request that started the run: one run = one connected trace.
"""
from __future__ import annotations

import json
import logging
import sys
import time
from typing import Any

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExporter
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from prometheus_client import CollectorRegistry, Counter, Histogram

from .errors import redact

_propagator = TraceContextTextMapPropagator()
tracer = trace.get_tracer("uar")

REGISTRY = CollectorRegistry()
REQUESTS = Counter("uar_requests_total", "API requests", ["transport", "operation", "code"], registry=REGISTRY)
REQUEST_SECONDS = Histogram("uar_request_duration_seconds", "API request latency", ["transport", "operation"],
                            registry=REGISTRY, buckets=(.005, .01, .025, .05, .1, .25, .5, 1, 2.5, 5, 10, 30, 60, 120))
MODEL_TOKENS = Counter("uar_model_tokens_total", "Model tokens", ["provider", "model", "direction"], registry=REGISTRY)
MODEL_COST = Counter("uar_model_cost_total", "Model cost (known prices only)", ["provider", "model", "currency"],
                     registry=REGISTRY)
MODEL_SECONDS = Histogram("uar_model_call_duration_seconds", "Provider call latency", ["provider", "model", "outcome"],
                          registry=REGISTRY, buckets=(.05, .1, .25, .5, 1, 2, 5, 10, 20, 40, 80, 160))
MODEL_TTFT = Histogram("uar_model_time_to_first_token_seconds", "Time to first streamed token", ["provider"],
                       registry=REGISTRY, buckets=(.05, .1, .25, .5, 1, 2, 5, 10, 20))
TOOL_CALLS = Counter("uar_tool_calls_total", "Tool calls", ["tool", "outcome"], registry=REGISTRY)
TOOL_SECONDS = Histogram("uar_tool_duration_seconds", "Tool latency", ["tool"], registry=REGISTRY)
RUNS = Counter("uar_runs_total", "Runs reaching a terminal or attention state", ["status"], registry=REGISTRY)
RUN_QUEUE_SECONDS = Histogram("uar_run_queue_delay_seconds", "Delay between run creation and first claim",
                              registry=REGISTRY, buckets=(.01, .05, .1, .25, .5, 1, 2, 5, 10, 30))
NODE_SECONDS = Histogram("uar_node_duration_seconds", "Agent node duration", ["node_type", "outcome"],
                         registry=REGISTRY, buckets=(.001, .01, .05, .1, .5, 1, 2, 5, 10, 30, 60))
POLICY_DENIALS = Counter("uar_policy_denials_total", "Policy denials", ["kind"], registry=REGISTRY)


def setup_tracing(service_name: str, otlp_endpoint: str | None, exporter: SpanExporter | None = None) -> TracerProvider:
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    if exporter is not None:
        provider.add_span_processor(SimpleSpanProcessor(exporter))
    elif otlp_endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint.rstrip("/") + "/v1/traces")))
    trace.set_tracer_provider(provider)
    global tracer
    tracer = provider.get_tracer("uar")
    return provider


def get_tracer() -> trace.Tracer:
    return tracer


def current_traceparent() -> str | None:
    carrier: dict[str, str] = {}
    _propagator.inject(carrier)
    return carrier.get("traceparent")


def context_from_traceparent(tp: str | None) -> otel_context.Context | None:
    if not tp:
        return None
    return _propagator.extract({"traceparent": tp})


def current_trace_id() -> str:
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else ""


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
                               "level": record.levelname, "logger": record.name, "msg": record.getMessage()}
        tid = current_trace_id()
        if tid:
            out["trace_id"] = tid
        extra = getattr(record, "fields", None)
        if extra:
            out.update(redact(extra))
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


def setup_logging(level: str = "INFO", as_json: bool = True) -> None:
    h = logging.StreamHandler(sys.stderr)
    h.setFormatter(JsonFormatter() if as_json else logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [h]
    root.setLevel(level)
    for noisy in ("httpx", "httpcore", "mcp", "psycopg.pool", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
