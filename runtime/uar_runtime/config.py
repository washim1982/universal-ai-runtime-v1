"""Runtime configuration: one YAML document validated at startup.

`${VAR}` and `${VAR:-default}` are expanded from the environment before validation.
Secrets are never stored in the file; providers reference them by environment variable name.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ModelClass = Literal["local", "cloud", "enterprise"]
SideEffect = Literal["read", "write", "external"]

PERMISSIONS = {
    "models:list", "tools:list", "tools:execute",
    "inference:local", "inference:cloud", "inference:enterprise",
    "agents:register", "agents:read", "runs:start", "runs:read", "runs:cancel", "runs:resolve",
    "dryrun", "approvals:decide", "admin",
}
DEFAULT_ROLES: dict[str, list[str]] = {
    "viewer": ["models:list", "tools:list", "agents:read", "runs:read"],
    "developer": ["models:list", "tools:list", "tools:execute", "inference:local", "inference:enterprise",
                  "agents:register", "agents:read", "runs:start", "runs:read", "runs:cancel", "dryrun"],
    "operator": ["models:list", "tools:list", "agents:read", "runs:read", "runs:cancel", "runs:resolve", "dryrun"],
    "approver": ["runs:read", "approvals:decide"],
    "admin": sorted(PERMISSIONS),
}


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ServerCfg(Strict):
    host: str = "127.0.0.1"
    http_port: int = 9000
    grpc_port: int = 50051
    grpc_enabled: bool = True
    max_body_bytes: int = 1_048_576
    public_metrics: bool = True
    cors_origins: list[str] = []


class WorkerCfg(Strict):
    embedded: bool = True
    concurrency: int = 4
    lease_s: float = 30.0
    heartbeat_s: float = 10.0
    poll_s: float = 0.5


class EgressCfg(Strict):
    # Model classes the runtime may contact. local-only profile: ["local", "enterprise"].
    allowed_classes: list[ModelClass] = ["local", "cloud", "enterprise"]
    # Hosts/CIDRs remote MCP servers and enterprise endpoints may resolve to, beyond public ranges.
    allow_private: list[str] = ["127.0.0.0/8", "::1/128"]


class ObservabilityCfg(Strict):
    service_name: str = "uar"
    otlp_endpoint: str | None = None   # e.g. http://localhost:4318
    log_level: str = "INFO"
    log_json: bool = True


class ApiKeyCfg(Strict):
    id: str
    sha256: str
    subject: str
    roles: list[str]

    @field_validator("sha256")
    @classmethod
    def _hex(cls, v: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{64}", v):
            raise ValueError("sha256 must be 64 lowercase hex chars")
        return v


class QuotaCfg(Strict):
    requests_per_minute: int = 600
    concurrent_requests: int = 16
    tokens_per_day: int | None = None


class TenantCfg(Strict):
    id: str
    api_keys: list[ApiKeyCfg] = []
    quotas: QuotaCfg = QuotaCfg()
    allow_cloud: bool = False
    allow_cloud_fallback: bool = False


class JwtCfg(Strict):
    issuer: str = "uar-dev"
    audience: str = "uar"
    hs256_secret_env: str | None = None  # dev/test only; OIDC/JWKS arrives in M9
    tenant_claim: str = "uar_tenant"
    roles_claim: str = "uar_roles"


class AuthCfg(Strict):
    tenants: list[TenantCfg]
    roles: dict[str, list[str]] = Field(default_factory=lambda: dict(DEFAULT_ROLES))
    jwt: JwtCfg | None = None

    @model_validator(mode="after")
    def _check(self) -> "AuthCfg":
        for role, perms in self.roles.items():
            bad = set(perms) - PERMISSIONS
            if bad:
                raise ValueError(f"role {role}: unknown permissions {sorted(bad)}")
        ids = [k.id for t in self.tenants for k in t.api_keys]
        if len(ids) != len(set(ids)):
            raise ValueError("api key ids must be unique")
        for t in self.tenants:
            for k in t.api_keys:
                missing = set(k.roles) - set(self.roles)
                if missing:
                    raise ValueError(f"key {k.id}: unknown roles {sorted(missing)}")
        return self


class ProviderCfg(Strict):
    id: str
    type: Literal["ollama", "openai_compat", "openai", "anthropic", "fake"]
    model_class: ModelClass
    base_url: str = ""
    api_key_env: str | None = None
    timeout_s: float = 120.0
    connect_timeout_s: float = 5.0
    max_concurrency: int = 4
    queue_timeout_s: float = 30.0
    capabilities: list[str] = ["chat", "stream", "tools", "json"]
    models: list[str] = []          # static catalog; empty = discover from the provider
    extra_headers: dict[str, str] = {}


class RuleCfg(Strict):
    when: dict[str, Any] = {}       # keys: tenant, role, data_class
    deny: list[str] = []            # class names or model patterns
    allow: list[str] = []           # if present: only these classes/patterns


class FallbackCfg(Strict):
    source: str = Field(alias="from")
    to: str
    when: Literal["provider_unavailable"] = "provider_unavailable"
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class BreakerCfg(Strict):
    failure_threshold: int = 5
    window_s: float = 30.0
    cooldown_s: float = 30.0


class RouterCfg(Strict):
    default_class: ModelClass = "local"
    classes: dict[ModelClass, list[str]]
    aliases: dict[str, str] = {}
    rules: list[RuleCfg] = []
    fallback: list[FallbackCfg] = []
    circuit_breaker: BreakerCfg = BreakerCfg()
    default_max_tokens: int = 1024


class PriceCfg(Strict):
    input_per_mtok: str   # decimal string per million tokens
    output_per_mtok: str


class PricingCfg(Strict):
    version: str
    currency: str = "USD"
    # key: "provider/model" or fnmatch pattern like "ollama/*"
    models: dict[str, PriceCfg] = {}


class ToolOverrideCfg(Strict):
    side_effect: SideEffect = "read"
    timeout_s: float | None = None


class MountCfg(Strict):
    src: str
    dst: str
    readonly: bool = True


class McpServerCfg(Strict):
    id: str                                         # tool namespace
    transport: Literal["stdio", "http"] = "stdio"
    command: str | None = None
    args: list[str] = []
    env: dict[str, str] = {}
    url: str | None = None
    sandbox: Literal["process", "container"] = "process"
    image: str | None = None
    mounts: list[MountCfg] = []                     # container bind mounts
    network: Literal["none", "bridge"] = "none"
    memory: str = "256m"
    cpus: str = "0.5"
    tools: dict[str, ToolOverrideCfg] = {}
    default_side_effect: SideEffect = "read"
    timeout_s: float = 30.0
    max_output_bytes: int = 262_144

    @model_validator(mode="after")
    def _check(self) -> "McpServerCfg":
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,30}", self.id):
            raise ValueError("server id must be a short lowercase identifier")
        if self.transport == "stdio" and not (self.command or self.image):
            raise ValueError(f"{self.id}: stdio transport needs command or image")
        if self.transport == "http" and not self.url:
            raise ValueError(f"{self.id}: http transport needs url")
        if self.sandbox == "container" and not self.image:
            raise ValueError(f"{self.id}: container sandbox needs image")
        return self


class ArgConstraint(Strict):
    prefix: list[str] | None = None     # string must start with one of these
    pattern: str | None = None          # regex fullmatch
    max_length: int | None = None


class ToolPolicyCfg(Strict):
    tool: str                          # fnmatch pattern over namespaced tool names
    effect: Literal["allow", "deny"] = "allow"
    roles: list[str] = []              # empty = any role
    tenants: list[str] = []            # empty = any tenant
    args: dict[str, ArgConstraint] = {}


class LimitsCfg(Strict):
    max_steps: int = 50
    max_loop_iterations: int = 20
    max_tokens: int = 50_000
    max_tool_calls: int = 50
    timeout_s: float = 600
    max_depth: int = 2
    auto_tool_max_steps: int = 6


class Settings(Strict):
    profile: Literal["local-only", "standard", "distributed"] = "standard"
    database_url: str
    server: ServerCfg = ServerCfg()
    worker: WorkerCfg = WorkerCfg()
    egress: EgressCfg = EgressCfg()
    observability: ObservabilityCfg = ObservabilityCfg()
    auth: AuthCfg
    providers: list[ProviderCfg]
    router: RouterCfg
    pricing: PricingCfg = PricingCfg(version="none")
    mcp_servers: list[McpServerCfg] = []
    tool_policies: list[ToolPolicyCfg] = []
    limits: LimitsCfg = LimitsCfg()
    agents_dir: str | None = None  # register *.yaml agents at startup for every tenant

    @model_validator(mode="after")
    def _check(self) -> "Settings":
        pids = {p.id for p in self.providers}
        if len(pids) != len(self.providers):
            raise ValueError("provider ids must be unique")
        for cls, order in self.router.classes.items():
            for pid in order:
                if pid not in pids:
                    raise ValueError(f"router class {cls}: unknown provider {pid}")
                pc = next(p for p in self.providers if p.id == pid)
                if pc.model_class != cls:
                    raise ValueError(f"provider {pid} is {pc.model_class}, listed under {cls}")
        for alias, target in self.router.aliases.items():
            if "/" not in target or target.split("/", 1)[0] not in pids:
                raise ValueError(f"alias {alias} -> {target}: target must be provider/model")
        sids = [s.id for s in self.mcp_servers]
        if len(sids) != len(set(sids)):
            raise ValueError("mcp server ids must be unique")
        if self.profile == "local-only":
            self.egress.allowed_classes = [c for c in self.egress.allowed_classes if c != "cloud"]
        return self

    def provider(self, pid: str) -> ProviderCfg:
        return next(p for p in self.providers if p.id == pid)


_ENV = re.compile(r"\$\{([A-Z0-9_]+)(?::-([^}]*))?\}")


def _expand(text: str) -> str:
    def sub(m: re.Match) -> str:
        val = os.environ.get(m.group(1))
        if val is None:
            if m.group(2) is None:
                raise ValueError(f"environment variable {m.group(1)} is not set")
            return m.group(2)
        return val
    return _ENV.sub(sub, text)


def _expand_values(v: Any) -> Any:
    """Expand ${VAR} in parsed string values only (never in comments or keys)."""
    if isinstance(v, str):
        return _expand(v)
    if isinstance(v, dict):
        return {k: _expand_values(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_expand_values(x) for x in v]
    return v


def load_settings(path: str | os.PathLike | None = None, overrides: dict[str, Any] | None = None) -> Settings:
    path = Path(path or os.environ.get("UAR_CONFIG", "config/uar.yaml"))
    data = _expand_values(yaml.safe_load(path.read_text(encoding="utf-8")))
    if overrides:
        data = deep_merge(data, overrides)
    return Settings.model_validate(data)


def deep_merge(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in b.items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out
