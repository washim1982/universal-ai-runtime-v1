"""Route resolution and model policy. Pure: no network access (dry-run uses this module)."""
from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import Iterable

from ..config import Settings, TenantCfg
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


def _pick_provider(s: Settings, cls: str, model: str, ctx: RouteContext, reasons: list[str]) -> str:
    order = s.router.classes.get(cls, [])  # type: ignore[call-overload]
    if not order:
        raise UARError("not_found", f"no providers configured for class {cls}")
    unknown: list[str] = []
    for pid in order:
        known = ctx.catalog.get(pid)
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
    for i, rule in enumerate(s.router.rules):
        if not _rule_applies(rule.when, ctx):
            continue
        if any(cls == d or fnmatchcase(qualified, d) for d in rule.deny):
            raise UARError("policy_denied", f"model route denied by router rule #{i}",
                           details={"rule": i, "model_class": cls})
        if rule.allow and not any(cls == a or fnmatchcase(qualified, a) for a in rule.allow):
            raise UARError("policy_denied", f"model route not in allow list of router rule #{i}",
                           details={"rule": i})
    route.reasons.append("policy allowed")


def _rule_applies(when: dict, ctx: RouteContext) -> bool:
    for k, v in when.items():
        vals = v if isinstance(v, list) else [v]
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
