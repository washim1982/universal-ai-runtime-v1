"""Model router: adapters, catalog, circuit breakers, concurrency, fallback, usage and cost."""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import AsyncIterator

from ..config import Settings, TenantCfg
from ..errors import UARError
from ..governance import Audit, Budget, Concurrency, Principal
from ..observability import (MODEL_COST, MODEL_SECONDS, MODEL_TOKENS, MODEL_TTFT, POLICY_DENIALS, get_tracer)
from ..store import Store
from .adapters import build_adapter
from .adapters.base import Adapter, ChatRequest, ChatResult, ProviderUnavailable, estimate_messages, estimate_tokens
from .resolve import Route, RouteContext, fallbacks, price_for, resolve

log = logging.getLogger("uar.router")


@dataclass
class UsageInfo:
    input_tokens: int
    output_tokens: int
    estimated: bool
    cost: Decimal | None
    currency: str
    price_version: str

    def to_dict(self) -> dict:
        d = {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens, "estimated": self.estimated,
             "price_version": self.price_version}
        if self.cost is not None:
            d["cost"] = {"amount": f"{self.cost:.8f}".rstrip("0").rstrip(".") or "0", "currency": self.currency}
        return d


class Breaker:
    def __init__(self, threshold: int, window_s: float, cooldown_s: float):
        self.threshold, self.window_s, self.cooldown_s = threshold, window_s, cooldown_s
        self.failures: list[float] = []
        self.open_until = 0.0
        self.half_open_trial = False

    def is_open(self) -> bool:
        """Closed: allow. Open: reject until cooldown ends. Half-open: allow one trial at a time."""
        if self.open_until == 0.0:
            return False
        if time.monotonic() < self.open_until or self.half_open_trial:
            return True
        self.half_open_trial = True
        return False

    def success(self) -> None:
        self.failures.clear()
        self.open_until = 0.0
        self.half_open_trial = False

    def failure(self) -> None:
        now = time.monotonic()
        self.failures = [t for t in self.failures if now - t < self.window_s] + [now]
        if len(self.failures) >= self.threshold or self.half_open_trial:
            self.open_until = now + self.cooldown_s
            self.half_open_trial = False
            log.warning("circuit opened", extra={"fields": {"failures": len(self.failures)}})


class ModelRouter:
    def __init__(self, settings: Settings, store: Store, audit: Audit, budget: Budget):
        self.s = settings
        self.store = store
        self.audit = audit
        self.budget = budget
        self.adapters: dict[str, Adapter] = {p.id: build_adapter(p) for p in settings.providers}
        b = settings.router.circuit_breaker
        self.breakers = {p.id: Breaker(b.failure_threshold, b.window_s, b.cooldown_s) for p in settings.providers}
        self.catalog: dict[str, set[str] | None] = {p.id: (self._norm(p.models) if p.models else None)
                                                    for p in settings.providers}
        self.concurrency = Concurrency()
        self.latency: dict[str, float] = {}   # provider -> EWMA seconds (router rules: prefer latency)
        self.redactor = None                  # set by the service: redaction of prompts to some classes

    def _observe(self, provider: str, seconds: float) -> None:
        old = self.latency.get(provider)
        self.latency[provider] = seconds if old is None else 0.8 * old + 0.2 * seconds

    def _egress(self, r: Route, req: ChatRequest) -> ChatRequest:
        """The request as sent to this route: redacted when its model class is configured for it."""
        if self.redactor is None or not self.redactor.egress(r.model_class):
            return req
        msgs = [{**m, "content": self.redactor.value(m["content"])} if "content" in m else m for m in req.messages]
        return dataclasses.replace(req, messages=msgs)

    @staticmethod
    def _norm(models: list[str]) -> set[str]:
        out = set(models)
        out |= {m[: -len(":latest")] for m in models if m.endswith(":latest")}
        return out

    async def refresh_catalog(self, timeout: float = 5.0) -> dict[str, str]:
        """Discover models from providers with no static catalog. Returns provider -> status."""
        status: dict[str, str] = {}

        async def one(pid: str, a: Adapter) -> None:
            if self.s.provider(pid).models:
                status[pid] = "static"
                return
            if self.s.provider(pid).model_class not in self.s.egress.allowed_classes:
                status[pid] = "egress-disabled"
                return
            try:
                models = await asyncio.wait_for(a.list_models(), timeout)
                self.catalog[pid] = self._norm(models) if models else None
                status[pid] = f"{len(models)} models"
            except Exception as e:
                status[pid] = f"unreachable ({type(e).__name__})"
        await asyncio.gather(*(one(pid, a) for pid, a in self.adapters.items()))
        return status

    def context(self, p: Principal, tenant: TenantCfg, data_class: str = "", caps: tuple[str, ...] = ()) -> RouteContext:
        return RouteContext(p, tenant, data_class, caps, dict(self.catalog),
                            {pid for pid, b in self.breakers.items() if time.monotonic() < b.open_until},
                            dict(self.latency))

    def route(self, requested: str, ctx: RouteContext) -> Route:
        try:
            return resolve(self.s, requested, ctx)
        except UARError as e:
            if e.code in ("policy_denied", "permission_denied", "egress_denied"):
                POLICY_DENIALS.labels("model").inc()
            raise

    # ------------------------------------------------------------ pricing

    def price(self, provider: str, model: str, tin: int, tout: int) -> Decimal | None:
        return price_for(self.s, provider, model, tin, tout)

    def usage(self, route: Route, req: ChatRequest, res: ChatResult) -> UsageInfo:
        estimated = res.input_tokens is None or res.output_tokens is None
        tin = res.input_tokens if res.input_tokens is not None else estimate_messages(req.messages)
        tout = res.output_tokens if res.output_tokens is not None else estimate_tokens(res.content)
        return UsageInfo(tin, tout, estimated, self.price(route.provider, route.model, tin, tout),
                         self.s.pricing.currency, self.s.pricing.version)

    async def _record(self, p: Principal, route: Route, u: UsageInfo, request_id: str, run_id: str | None) -> None:
        MODEL_TOKENS.labels(route.provider, route.model, "input").inc(u.input_tokens)
        MODEL_TOKENS.labels(route.provider, route.model, "output").inc(u.output_tokens)
        if u.cost is not None:
            MODEL_COST.labels(route.provider, route.model, u.currency).inc(float(u.cost))
        try:
            await self.store.execute(
                "INSERT INTO usage_ledger (tenant, subject, request_id, run_id, provider, model, input_tokens, "
                "output_tokens, cost, currency, estimated, price_version) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                p.tenant, p.subject, request_id, run_id, route.provider, route.model, u.input_tokens, u.output_tokens,
                u.cost, u.currency if u.cost is not None else None, u.estimated, u.price_version)
        except Exception as e:  # ledger is accounting, not a safety control
            log.error("usage ledger write failed: %s", type(e).__name__)

    # ------------------------------------------------------------ calls

    def _reservation(self, req: ChatRequest) -> int:
        return estimate_messages(req.messages) + (req.max_tokens or self.s.router.default_max_tokens)

    async def _pre(self, p: Principal, tenant: TenantCfg, route: Route, req: ChatRequest, request_id: str,
                   run_id: str | None) -> int:
        if route.model_class != "local":
            # Data leaves the host: record intent first (fail closed).
            await self.audit.record(p, "model.invoke", route.qualified, "intent", request_id=request_id,
                                    run_id=run_id, details={"provider": route.provider, "class": route.model_class,
                                                            "region": self.s.provider(route.provider).region or "",
                                                            "redacted": bool(self.redactor and
                                                                             self.redactor.egress(route.model_class))})
        return await self.budget.reserve(tenant, self._reservation(req))

    async def chat(self, p: Principal, tenant: TenantCfg, route: Route, req: ChatRequest, ctx: RouteContext,
                   request_id: str, run_id: str | None = None) -> tuple[ChatResult, Route, UsageInfo]:
        candidates = [route]
        last: UARError | None = None
        i = 0
        while i < len(candidates):
            r = candidates[i]
            i += 1
            req.model = r.model
            if req.namespaced_extensions:
                req.extensions = dict(req.namespaced_extensions.get(r.provider) or {})
            reserved = await self._pre(p, tenant, r, req, request_id, run_id)
            t0 = time.monotonic()
            try:
                with get_tracer().start_as_current_span("model.chat") as span:
                    span.set_attributes({"gen_ai.system": r.provider, "gen_ai.request.model": r.model,
                                         "uar.model_class": r.model_class})
                    sent = self._egress(r, req)
                    res = await self._call(r, lambda a: a.chat(sent))
                    u = self.usage(r, req, res)
                    span.set_attributes({"gen_ai.usage.input_tokens": u.input_tokens,
                                         "gen_ai.usage.output_tokens": u.output_tokens,
                                         "gen_ai.response.finish_reasons": [res.finish_reason]})
            except ProviderUnavailable as e:
                await self.budget.settle(tenant, reserved, 0)
                MODEL_SECONDS.labels(r.provider, r.model, "unavailable").observe(time.monotonic() - t0)
                last = e
                if i == len(candidates) and not r.fallback_used:
                    candidates += fallbacks(self.s, r, self.context(p, tenant, ctx.data_class, ctx.capabilities))
                continue
            except UARError:
                await self.budget.settle(tenant, reserved, 0)
                MODEL_SECONDS.labels(r.provider, r.model, "error").observe(time.monotonic() - t0)
                raise
            MODEL_SECONDS.labels(r.provider, r.model, "ok").observe(time.monotonic() - t0)
            self._observe(r.provider, time.monotonic() - t0)
            await self.budget.settle(tenant, reserved, u.input_tokens + u.output_tokens)
            await self._record(p, r, u, request_id, run_id)
            if r.model_class != "local":
                await self.audit.record(p, "model.invoke", r.qualified, "succeeded", request_id=request_id,
                                        run_id=run_id, required=False)
            return res, r, u
        assert last is not None
        raise last

    async def _call(self, r: Route, fn):
        pc = self.s.provider(r.provider)
        breaker = self.breakers[r.provider]
        if breaker.is_open():
            raise ProviderUnavailable(r.provider, "circuit open")
        sem = self.concurrency.get(r.provider, pc.max_concurrency)
        try:
            await asyncio.wait_for(sem.acquire(), pc.queue_timeout_s)
        except asyncio.TimeoutError:
            raise UARError("unavailable", f"{r.provider}: at capacity, try again later") from None
        try:
            res = await fn(self.adapters[r.provider])
        except ProviderUnavailable:
            breaker.failure()
            raise
        except UARError as e:
            if e.code in ("deadline_exceeded",) or (e.code == "provider_error" and e.retryable):
                breaker.failure()
            raise
        finally:
            sem.release()
        breaker.success()
        return res

    async def stream(self, p: Principal, tenant: TenantCfg, route: Route, req: ChatRequest, ctx: RouteContext,
                     request_id: str, run_id: str | None = None) -> tuple[Route, AsyncIterator[str | tuple]]:
        """Open a stream. Fallback only happens before the first token; a partially emitted
        generation is never restarted. The iterator yields text deltas, then ("final", ChatResult, UsageInfo)."""
        candidates = [route]
        i = 0
        while i < len(candidates):
            r = candidates[i]
            i += 1
            req.model = r.model
            if req.namespaced_extensions:
                req.extensions = dict(req.namespaced_extensions.get(r.provider) or {})
            reserved = await self._pre(p, tenant, r, req, request_id, run_id)
            pc = self.s.provider(r.provider)
            breaker = self.breakers[r.provider]
            if breaker.is_open():
                await self.budget.settle(tenant, reserved, 0)
                if i == len(candidates) and not r.fallback_used:
                    candidates += fallbacks(self.s, r, self.context(p, tenant, ctx.data_class, ctx.capabilities))
                if i == len(candidates):
                    raise ProviderUnavailable(r.provider, "circuit open")
                continue
            sem = self.concurrency.get(r.provider, pc.max_concurrency)
            try:
                await asyncio.wait_for(sem.acquire(), pc.queue_timeout_s)
            except asyncio.TimeoutError:
                await self.budget.settle(tenant, reserved, 0)
                raise UARError("unavailable", f"{r.provider}: at capacity, try again later") from None
            t0 = time.monotonic()
            gen = self.adapters[r.provider].stream(self._egress(r, req)).__aiter__()
            try:
                first = await gen.__anext__()
            except ProviderUnavailable:
                sem.release()
                breaker.failure()
                await self.budget.settle(tenant, reserved, 0)
                if i == len(candidates) and not r.fallback_used:
                    candidates += fallbacks(self.s, r, self.context(p, tenant, ctx.data_class, ctx.capabilities))
                if i == len(candidates):
                    raise
                continue
            except BaseException:
                sem.release()
                await self.budget.settle(tenant, reserved, 0)
                raise
            MODEL_TTFT.labels(r.provider).observe(time.monotonic() - t0)
            self._observe(r.provider, time.monotonic() - t0)
            return r, self._drain(p, tenant, r, req, gen, first, sem, breaker, reserved, t0, request_id, run_id)
        raise UARError("unavailable", "no provider available")

    async def _drain(self, p, tenant, r: Route, req, gen, first, sem, breaker, reserved, t0, request_id, run_id):
        done = False
        try:
            item = first
            while True:
                if isinstance(item, ChatResult):
                    u = self.usage(r, req, item)
                    breaker.success()
                    MODEL_SECONDS.labels(r.provider, r.model, "ok").observe(time.monotonic() - t0)
                    await self.budget.settle(tenant, reserved, u.input_tokens + u.output_tokens)
                    reserved = 0
                    await self._record(p, r, u, request_id, run_id)
                    done = True
                    yield ("final", item, u)
                    return
                yield item
                item = await gen.__anext__()
        finally:
            sem.release()
            if not done:
                # Client went away or the provider failed mid-stream: close the provider request
                # (stops generation) and settle with an estimate of what was produced.
                await gen.aclose()
                MODEL_SECONDS.labels(r.provider, r.model, "aborted").observe(time.monotonic() - t0)
                if reserved:
                    await self.budget.settle(tenant, reserved, 0)

    async def aclose(self) -> None:
        for a in self.adapters.values():
            await a.aclose()
