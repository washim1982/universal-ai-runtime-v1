"""Route resolution and model policy. Pure: no network access (dry-run uses this module).

Router rules (evaluated in order, every matching rule applies):
  when:  tenant | role | data_class | model_class
  deny / allow:        model classes or qualified-name patterns
  regions:             the provider's region must be listed
  max_input_per_mtok:  providers with a higher (or no) input price are not used
  prefer:              order (configured) | cost | latency (observed) - provider choice within a class
Tenants with `data_residency` only use providers whose region is listed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from fnmatch import fnmatchcase
from typing import Iterable

from ..config import PriceCfg, RuleCfg, Settings, TenantCfg
from ..errors import UARError
from ..governance import Principal
from .names import parse


@dataclass
class Route:
    requested: str
    model_class: str
    provider: str
    model: str
    reasons: list[str] = field(default_factory=list)
    fallback_used: bool = False

    @property
    def qualified(self) -> str:
        return f"{self.model_class}:{self.provider}/{self.model}"

    def to_dict(self) -> dict:
        return {"requested": self.requested, "model_class": self.model_class, "provider": self.provider,
                "model": self.model, "reasons": list(self.reasons), "fallback_used": self.fallback_used}


@dataclass
class RouteContext:
    principal: Principal
    tenant: TenantCfg
    data_class: str = ""
    capabilities: tuple[str, ...] = ()
    # provider id -> known model ids (None = catalog unknown, e.g. not yet discovered)
    catalog: dict[str, set[str] | None] = field(default_factory=dict)
    unavailable: set[str] = field(default_factory=set)  # providers with an open circuit
    latency: dict[str, float] = field(default_factory=dict)  # provider -> observed seconds (EWMA)


def resolve(s: Settings, requested: str, ctx: RouteContext, *, check_policy: bool = True) -> Route:
    reasons: list[str] = []
    providers = {p.id: p for p in s.providers}
    target = s.router.aliases.get(requested)
    if target:
        pid, model = target.split("/", 1)
        cls = providers[pid].model_class
        reasons.append(f"alias {requested} -> {target}")
    else:
        try:
            mn = parse(requested)
        except ValueError as e:
            raise UARError("invalid_argument", str(e)) from e
        if mn.provider and mn.provider in providers:
            pid, model = mn.provider, mn.name
            cls = providers[pid].model_class
            if mn.model_class and mn.model_class != cls:
                raise UARError("invalid_argument", f"provider {pid} is {cls}, not {mn.model_class}")
            reasons.append(f"explicit provider {pid}")
        else:
            model = f"{mn.provider}/{mn.name}" if mn.provider else mn.name
            cls = mn.model_class or s.router.default_class
            if not mn.model_class:
                reasons.append(f"no class prefix; default class {cls}")
            pid = _pick_provider(s, cls, model, ctx, reasons)
    route = Route(requested, cls, pid, model, reasons)
    if check_policy:
        check_route_policy(s, route, ctx)
    check_capabilities(s, route, ctx.capabilities)
    return route


def _price_cfg(s: Settings, provider: str, model: str) -> PriceCfg | None:
    key = f"{provider}/{model}"
    for pat, pc in s.pricing.models.items():
        if pat == key or fnmatchcase(key, pat):
            return pc
    return None


def _rules(s: Settings, ctx: RouteContext, cls: str) -> list[tuple[int, RuleCfg]]:
    return [(i, r) for i, r in enumerate(s.router.rules) if _rule_applies(r.when, ctx, cls)]


def placement_issue(s: Settings, pid: str, model: str, ctx: RouteContext, rules: list[tuple[int, RuleCfg]]) -> str:
    """Why this provider may not serve the request (residency, region, price rules), or ''."""
    region = s.provider(pid).region
    if ctx.tenant.data_residency and region not in ctx.tenant.data_residency:
        return f"region {region or 'unset'} is outside the tenant's data residency"
    for i, r in rules:
        if r.regions and region not in r.regions:
            return f"region {region or 'unset'} not allowed by router rule #{i}"
        if r.max_input_per_mtok is not None:
            pc = _price_cfg(s, pid, model)
            if pc is None:
                return f"no price configured (router rule #{i} caps the input price)"
            if Decimal(pc.input_per_mtok) > Decimal(r.max_input_per_mtok):
                return f"input price {pc.input_per_mtok}/Mtok above the cap of router rule #{i}"
    return ""


def _pick_provider(s: Settings, cls: str, model: str, ctx: RouteContext, reasons: list[str]) -> str:
    order = list(s.router.classes.get(cls, []))  # type: ignore[call-overload]
    if not order:
        raise UARError("not_found", f"no providers configured for class {cls}")
    rules = _rules(s, ctx, cls)
    prefer = next((r.prefer for _, r in rules if r.prefer), "order")
    if prefer == "cost":
        def cost(pid: str) -> Decimal:
            pc = _price_cfg(s, pid, model)
            return Decimal(pc.input_per_mtok) + Decimal(pc.output_per_mtok) if pc else Decimal("Infinity")
        order.sort(key=cost)
        reasons.append("prefer cost: " + ", ".join(order))
    elif prefer == "latency":
        order.sort(key=lambda pid: ctx.latency.get(pid, float("inf")))
        reasons.append("prefer latency: " + ", ".join(order))
    unknown: list[str] = []
    placed = False
    for pid in order:
        known = ctx.catalog.get(pid)
        issue = placement_issue(s, pid, model, ctx, rules)
        if issue:
            reasons.append(f"skip {pid}: {issue}")
            continue
        placed = True
        if pid in ctx.unavailable:
            reasons.append(f"skip {pid}: circuit open")
            continue
        if known is None:
            unknown.append(pid)
        elif model in known:
            reasons.append(f"{pid} serves {model}")
            return pid
    if unknown:
        reasons.append(f"catalog unknown; trying {unknown[0]}")
        return unknown[0]
    if not placed:
        raise UARError("policy_denied", f"no {cls} provider satisfies the residency, region and price rules",
                       details={"model_class": cls})
    if any(pid in ctx.unavailable for pid in order):
        raise UARError("unavailable", f"no available {cls} provider serves {model}")
    raise UARError("not_found", f"model {model} is not served by any {cls} provider")


def check_route_policy(s: Settings, route: Route, ctx: RouteContext) -> None:
    cls, p = route.model_class, ctx.principal
    if cls not in s.egress.allowed_classes:
        raise UARError("egress_denied", f"{cls} models are disabled in profile {s.profile}",
                       details={"model_class": cls})
    if not p.has(f"inference:{cls}"):
        raise UARError("permission_denied", f"missing permission inference:{cls}")
    if cls == "cloud" and not ctx.tenant.allow_cloud:
        raise UARError("policy_denied", "tenant is not allowed to use cloud models")
    qualified = route.qualified
    rules = _rules(s, ctx, cls)
    issue = placement_issue(s, route.provider, route.model, ctx, rules)
    if issue:
        raise UARError("policy_denied", f"{route.provider}: {issue}", details={"provider": route.provider})
    for i, rule in rules:
        if any(cls == d or fnmatchcase(qualified, d) for d in rule.deny):
            raise UARError("policy_denied", f"model route denied by router rule #{i}",
                           details={"rule": i, "model_class": cls})
        if rule.allow and not any(cls == a or fnmatchcase(qualified, a) for a in rule.allow):
            raise UARError("policy_denied", f"model route not in allow list of router rule #{i}",
                           details={"rule": i})
    route.reasons.append("policy allowed")


def _rule_applies(when: dict, ctx: RouteContext, cls: str = "") -> bool:
    for k, v in when.items():
        vals = v if isinstance(v, list) else [v]
        if k == "model_class":
            if cls not in vals:
                return False
            continue
        if k == "tenant" and ctx.tenant.id not in vals:
            return False
        if k == "role" and not set(vals) & set(ctx.principal.roles):
            return False
        if k == "data_class" and ctx.data_class not in vals:
            return False
        if k not in ("tenant", "role", "data_class"):
            return False
    return True


def check_capabilities(s: Settings, route: Route, needed: Iterable[str]) -> None:
    have = set(s.provider(route.provider).capabilities)
    missing = sorted(set(needed) - have)
    if missing:
        raise UARError("unsupported_capability", f"{route.provider} does not support {', '.join(missing)}",
                       details={"missing": missing})


def fallbacks(s: Settings, route: Route, ctx: RouteContext) -> list[Route]:
    """Policy-compliant fallback routes for a provider failure, in configured order."""
    out: list[Route] = []
    for fb in s.router.fallback:
        if fb.source not in (route.requested, route.qualified):
            continue
        try:
            alt = resolve(s, fb.to, ctx)
        except UARError:
            continue
        if alt.model_class == "cloud" and route.model_class != "cloud" and not ctx.tenant.allow_cloud_fallback:
            continue
        alt.fallback_used = True
        alt.reasons.insert(0, f"fallback from {route.provider} ({fb.when})")
        out.append(alt)
    return out


def price_for(s: Settings, provider: str, model: str, tin: int, tout: int):
    """Cost for token counts from the versioned price catalog, or None when no price is configured."""
    from decimal import Decimal
    key = f"{provider}/{model}"
    for pat, pc in s.pricing.models.items():
        if pat == key or fnmatchcase(key, pat):
            return (Decimal(pc.input_per_mtok) * tin + Decimal(pc.output_per_mtok) * tout) / Decimal(1_000_000)
    return None
