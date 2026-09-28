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
    "dryrun", "approvals:read", "approvals:decide", "plugins:manage", "audit:read", "usage:read",
    "keys:manage", "apps:manage", "logs:read", "guardrails:check", "admin",
}
DEFAULT_ROLES: dict[str, list[str]] = {
    "viewer": ["models:list", "tools:list", "agents:read", "runs:read"],
    "developer": ["models:list", "tools:list", "tools:execute", "inference:local", "inference:enterprise",
                  "agents:register", "agents:read", "runs:start", "runs:read", "runs:cancel", "dryrun",
                  "guardrails:check"],
    "operator": ["models:list", "tools:list", "agents:read", "runs:read", "runs:cancel", "runs:resolve", "dryrun",
                 "approvals:read"],
    "approver": ["runs:read", "approvals:read", "approvals:decide"],
    "auditor": ["runs:read", "approvals:read", "audit:read", "usage:read"],
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
    public_url: str | None = None   # how clients reach this runtime (token URL shown to apps); default from host/port


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
    # Regions this tenant's data may be processed in. Empty = no residency constraint; otherwise only
    # providers whose `region` is listed are used (a provider without a region never qualifies).
    data_residency: list[str] = []


class JwtCfg(Strict):
    issuer: str = "uar-dev"
    audience: str = "uar"
    hs256_secret_env: str | None = None  # dev/test only; production identity uses `auth.oidc`
    tenant_claim: str = "uar_tenant"
    roles_claim: str = "uar_roles"


class OidcCfg(Strict):
    """An OpenID Connect issuer whose access tokens are accepted (signature checked against its JWKS)."""
    issuer: str
    audience: str
    jwks_url: str
    algorithms: list[Literal["RS256", "RS384", "RS512", "PS256", "ES256", "ES384", "EdDSA"]] = ["RS256", "ES256"]
    # The tenant is fixed per issuer, or read from a claim and mapped (unmapped values are refused).
    tenant: str | None = None
    tenant_claim: str | None = None
    tenant_map: dict[str, str] = {}
    groups_claim: str = "groups"
    group_roles: dict[str, list[str]] = {}     # IdP group -> UAR roles
    subject_roles: dict[str, list[str]] = {}   # service accounts: token subject (client id) -> roles
    leeway_s: int = 30
    jwks_refresh_s: float = 600.0

    @model_validator(mode="after")
    def _check(self) -> "OidcCfg":
        if bool(self.tenant) == bool(self.tenant_claim):
            raise ValueError(f"oidc {self.issuer}: set exactly one of tenant or tenant_claim")
        if self.tenant_claim and not self.tenant_map:
            raise ValueError(f"oidc {self.issuer}: tenant_claim needs tenant_map (claim value -> tenant)")
        if not self.jwks_url.startswith(("https://", "http://127.0.0.1", "http://localhost")):
            raise ValueError(f"oidc {self.issuer}: jwks_url must use https")
        return self


class AuthCfg(Strict):
    tenants: list[TenantCfg]
    roles: dict[str, list[str]] = Field(default_factory=lambda: dict(DEFAULT_ROLES))
    jwt: JwtCfg | None = None
    oidc: list[OidcCfg] = []

    @model_validator(mode="after")
    def _check(self) -> "AuthCfg":
        for role, perms in self.roles.items():
            bad = set(perms) - PERMISSIONS
            if bad:
                raise ValueError(f"role {role}: unknown permissions {sorted(bad)}")
        tenant_ids = {t.id for t in self.tenants}
        issuers = [o.issuer for o in self.oidc]
        if len(issuers) != len(set(issuers)):
            raise ValueError("oidc issuers must be unique")
        for o in self.oidc:
            for tid in ([o.tenant] if o.tenant else list(o.tenant_map.values())):
                if tid not in tenant_ids:
                    raise ValueError(f"oidc {o.issuer}: unknown tenant {tid}")
            for src, roles in list(o.group_roles.items()) + list(o.subject_roles.items()):
                missing = set(roles) - set(self.roles)
                if missing:
                    raise ValueError(f"oidc {o.issuer} mapping {src}: unknown roles {sorted(missing)}")
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
    type: Literal["ollama", "openai_compat", "openai", "anthropic", "azure_openai", "vertex", "fake", "plugin"]
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
    api_version: str | None = None  # azure_openai classic API (deployments + api-version)
    region: str | None = None       # where requests are processed (data residency, router rules)


class RuleCfg(Strict):
    when: dict[str, Any] = {}       # keys: tenant, role, data_class, model_class
    deny: list[str] = []            # class names or model patterns
    allow: list[str] = []           # if present: only these classes/patterns
    regions: list[str] = []         # if present: the provider's region must be one of these
    max_input_per_mtok: str | None = None   # providers priced above this (or unpriced) are not used
    prefer: Literal["order", "cost", "latency"] | None = None  # provider choice within a class

    @field_validator("when")
    @classmethod
    def _keys(cls, v: dict[str, Any]) -> dict[str, Any]:
        bad = set(v) - {"tenant", "role", "data_class", "model_class"}
        if bad:
            raise ValueError(f"unknown rule condition(s) {sorted(bad)}")
        return v


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
    # MCP protocol negotiation: auto probes the 2026-07-28 revision and falls back to the initialize
    # handshake; legacy always uses the handshake (2025-11-25 and earlier).
    protocol: Literal["auto", "legacy"] = "auto"

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
    # require_approval: allowed inside agent runs once a human approves this exact call (tool + args hash)
    effect: Literal["allow", "deny", "require_approval"] = "allow"
    approver_roles: list[str] = []     # require_approval: who may decide (empty = any approvals:decide)
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


GuardAction = Literal["off", "flag", "redact", "block"]


class GuardCheckCfg(Strict):
    action: GuardAction = "flag"
    types: list[str] = []            # detector names to use; empty = all of this check


class InjectionCheckCfg(Strict):
    action: Literal["off", "flag", "block"] = "block"
    threshold: float = 0.7           # heuristic score (0..1) at which the check fires
    # Optional classifier model (e.g. local:ollama/granite4) asked for a second opinion on text the
    # heuristics find suspicious (score >= classifier_min_score). Its confidence can raise the score.
    model: str | None = None
    classifier_min_score: float = 0.3

    @field_validator("threshold")
    @classmethod
    def _range(cls, v: float) -> float:
        if not 0 < v <= 1:
            raise ValueError("threshold must be in (0, 1]")
        return v


class DeniedTermsCfg(Strict):
    action: GuardAction = "block"
    terms: list[str] = []            # whole words or phrases, case-insensitive
    patterns: list[str] = []         # regular expressions

    @field_validator("patterns")
    @classmethod
    def _compile(cls, v: list[str]) -> list[str]:
        for pat in v:
            re.compile(pat)
        return v


class StageCfg(Strict):
    prompt_injection: InjectionCheckCfg | None = None
    pii: GuardCheckCfg | None = None
    pci: GuardCheckCfg | None = None
    secrets: GuardCheckCfg | None = None
    denied_terms: DeniedTermsCfg | None = None
    max_chars: int | None = None

    @model_validator(mode="after")
    def _types(self) -> "StageCfg":
        known = {"pii": {"email", "phone", "us_ssn", "iban", "ipv4"},
                 "pci": {"card_number", "cvv", "track_data"},
                 "secrets": {"private_key", "aws_access_key", "github_token", "slack_token", "anthropic_key",
                             "openai_key", "google_api_key", "uar_key", "jwt", "connection_string",
                             "password_assignment", "generic_api_key"}}
        for check, names in known.items():
            cfg = getattr(self, check)
            if cfg and set(cfg.types) - names:
                raise ValueError(f"guardrails {check}: unknown types {sorted(set(cfg.types) - names)}")
        return self


class GuardrailPolicyCfg(Strict):
    input: StageCfg = StageCfg(prompt_injection=InjectionCheckCfg(), pii=GuardCheckCfg(action="flag"),
                               pci=GuardCheckCfg(action="block"), secrets=GuardCheckCfg(action="block"),
                               max_chars=100_000)
    tool_results: StageCfg = StageCfg(prompt_injection=InjectionCheckCfg(action="flag"),
                                      secrets=GuardCheckCfg(action="redact"))
    output: StageCfg = StageCfg(pii=GuardCheckCfg(action="flag"), pci=GuardCheckCfg(action="redact"),
                                secrets=GuardCheckCfg(action="redact"))
    exempt_roles: list[str] = []     # principals with one of these roles skip the checks (e.g. red teams)


class GuardrailsCfg(Strict):
    """Inference guardrails (see runtime/uar_runtime/guardrails.py and docs/guardrails.md)."""
    enabled: bool = False
    policy: GuardrailPolicyCfg = GuardrailPolicyCfg()
    tenants: dict[str, GuardrailPolicyCfg] = {}   # a tenant's policy replaces the default one


class StsCfg(Strict):
    """Built-in security token service: registered applications exchange their client credentials
    for short-lived signed access tokens (OAuth 2.0 client_credentials grant)."""
    enabled: bool = True
    issuer: str = "urn:uar:sts"
    audience: str = "uar"
    default_token_ttl_s: int = 900
    max_token_ttl_s: int = 3600
    # Environment variable holding a secret that encrypts the signing keys stored in the database.
    key_encryption_env: str | None = None
    # true: API keys are refused everywhere; callers need an STS (or OIDC) access token.
    require_tokens: bool = False
    # A rotated-in signing key is used for signing only after this delay, so every replica already
    # knows its public key when the first token signed with it arrives.
    key_activation_delay_s: float = 15.0
    max_failed_attempts: int = 10     # per client id per minute, then the client is throttled

    @model_validator(mode="after")
    def _check(self) -> "StsCfg":
        if not 60 <= self.default_token_ttl_s <= self.max_token_ttl_s <= 86_400:
            raise ValueError("sts: need 60 <= default_token_ttl_s <= max_token_ttl_s <= 86400")
        return self


class AdminCfg(Strict):
    # Tenants whose administrators may read process-wide data (service logs, runtime components).
    # Empty = any tenant's admins: suitable only when one organisation runs the runtime.
    platform_tenants: list[str] = []
    key_refresh_s: float = 5.0        # how quickly keys created/revoked on another replica take effect
    log_buffer_records: int = 5000    # in-memory service log records served by ListLogs


class ApprovalsCfg(Strict):
    default_ttl_s: float = 86_400       # pending approvals expire (fail closed) after this
    max_ttl_s: float = 7 * 86_400
    allow_self_approval: bool = False   # the principal that started the run may not decide by default


class RedactionPatternCfg(Strict):
    name: str
    regex: str
    replace: str = ""                   # default: [redacted:<name>]

    @field_validator("regex")
    @classmethod
    def _compiles(cls, v: str) -> str:
        re.compile(v)
        return v


class RedactionCfg(Strict):
    # Built-in detectors: email, card_number (Luhn-checked), us_ssn, iban, ipv4, phone
    builtin: list[Literal["email", "card_number", "us_ssn", "iban", "ipv4", "phone"]] = []
    patterns: list[RedactionPatternCfg] = []
    audit: bool = True                  # audit details
    events: bool = True                 # run event log (tool summaries, errors, completed output copy)
    egress_classes: list[ModelClass] = []   # prompts sent to these model classes are redacted first


class RetentionCfg(Strict):
    enabled: bool = False
    interval_s: float = 3600
    runs_days: int | None = None        # terminal runs (+ events, intents, approvals)
    usage_days: int | None = None
    audit_days: int | None = None       # prunes the oldest audit rows; the chain keeps an anchor
    idempotency_days: int | None = 7


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
    approvals: ApprovalsCfg = ApprovalsCfg()
    admin: AdminCfg = AdminCfg()
    sts: StsCfg = StsCfg()
    guardrails: GuardrailsCfg = GuardrailsCfg()
    redaction: RedactionCfg = RedactionCfg()
    retention: RetentionCfg = RetentionCfg()
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
        for t in self.auth.tenants:
            if t.data_residency and not any(p.region in t.data_residency for p in self.providers):
                raise ValueError(f"tenant {t.id}: no provider is in its data residency {t.data_residency}")
        for pol in self.tool_policies:
            missing = set(pol.approver_roles) - set(self.auth.roles)
            if missing:
                raise ValueError(f"tool policy {pol.tool}: unknown approver roles {sorted(missing)}")
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
