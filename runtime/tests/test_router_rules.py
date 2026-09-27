"""M9 gates: router rules v2 - data residency, provider regions, price caps, cost/latency preference."""
from __future__ import annotations

from pathlib import Path

import pytest

from conftest import settings_dict
from uar_runtime.config import Settings
from uar_runtime.errors import UARError
from uar_runtime.governance import Principal
from uar_runtime.router.resolve import RouteContext, resolve


def router_settings(**mut) -> Settings:
    d = settings_dict("postgresql://x/y", [{"id": "acme"}, {"id": "globex"}, {"id": "cloudco", "allow_cloud": True}],
                      Path.cwd(), Path.cwd())
    d["providers"][0]["region"] = "eu-west"      # fake
    d["providers"][1]["region"] = "us-east"      # fake_b
    d["pricing"]["models"] = {"fake/*": {"input_per_mtok": "2", "output_per_mtok": "2"},
                              "fake_b/*": {"input_per_mtok": "1", "output_per_mtok": "1"}}
    for k, v in mut.items():
        k1, k2 = k.split("__")
        d[k1][k2] = v
    return Settings.model_validate(d)


def ctx(s: Settings, tenant: str, **kw) -> RouteContext:
    p = Principal(tenant, "u", ("developer",), "", frozenset({"inference:local", "inference:cloud"}))
    t = next(x for x in s.auth.tenants if x.id == tenant)
    return RouteContext(p, t, **kw)


def test_router_data_residency():
    d_tenants = [{"id": "acme", "data_residency": ["us-east"]}, {"id": "globex"}, {"id": "cloudco"}]
    s = router_settings(auth__tenants=d_tenants)
    r = resolve(s, "local:echo", ctx(s, "acme"))
    assert r.provider == "fake_b" and any("outside the tenant's data residency" in x for x in r.reasons)
    with pytest.raises(UARError, match="data residency"):
        resolve(s, "local:fake/echo", ctx(s, "acme"))
    assert resolve(s, "local:echo", ctx(s, "globex")).provider == "fake"
    with pytest.raises(ValueError, match="no provider is in its data residency"):
        router_settings(auth__tenants=[{"id": "acme", "data_residency": ["ap-south"]}])


def test_router_region_cost_and_latency_rules():
    s = router_settings(router__rules=[
        {"when": {"tenant": "globex"}, "regions": ["us-east"]},
        {"when": {"tenant": "cloudco", "model_class": "local"}, "prefer": "cost"},
        {"when": {"tenant": "acme"}, "prefer": "latency"},
    ])
    assert resolve(s, "local:echo", ctx(s, "globex")).provider == "fake_b"
    r = resolve(s, "local:echo", ctx(s, "cloudco"))
    assert r.provider == "fake_b" and "prefer cost: fake_b, fake" in r.reasons
    assert resolve(s, "local:echo", ctx(s, "acme", latency={"fake": 0.9, "fake_b": 0.1})).provider == "fake_b"
    assert resolve(s, "local:echo", ctx(s, "acme", latency={"fake": 0.1, "fake_b": 0.9})).provider == "fake"

    s = router_settings(router__rules=[{"when": {"tenant": "acme"}, "max_input_per_mtok": "1.5"}])
    r = resolve(s, "local:echo", ctx(s, "acme"))
    assert r.provider == "fake_b" and any("above the cap" in x for x in r.reasons)
    s = router_settings(router__rules=[{"when": {"tenant": "acme"}, "max_input_per_mtok": "0.5"}])
    with pytest.raises(UARError, match="residency, region and price"):
        resolve(s, "local:echo", ctx(s, "acme"))
    with pytest.raises(ValueError, match="unknown rule condition"):
        router_settings(router__rules=[{"when": {"weekday": "monday"}, "deny": ["cloud"]}])
