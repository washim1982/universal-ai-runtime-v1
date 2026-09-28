"""Inference guardrails: security checks on what goes into a model and what comes out.

Stages
  input         the caller's messages (user and assistant turns) before the model is called
  tool_results  tool output fed back to the model (indirect prompt injection lives here)
  output        the model's answer, including streamed answers

Checks (each with an action: off | flag | redact | block)
  prompt_injection  jailbreaks and instruction overrides: weighted heuristics, hidden-text tricks
                    (invisible Unicode tag characters, zero-width runs, role-marker spoofing) and,
                    optionally, a classifier model
  pii               personal data: email, phone, us_ssn, iban, ipv4
  pci               payment card data: card numbers (Luhn-checked), CVV/CVC codes, magnetic-stripe track data
  secrets           credentials: cloud/API keys, private keys, JWTs, UAR keys, passwords, connection strings
  denied_terms      organisation-specific words and patterns
  max_chars         input size limit

`block` rejects the call with `guardrail_blocked` (the matched text is never echoed back); `redact`
replaces matches with [redacted:<type>] and continues; `flag` continues and records the finding.
Every finding is written to the audit trail (types and counts, never the content) and counted in
the metric uar_guardrail_findings_total.

Detection is pattern- and heuristic-based: it stops common attacks and leaks, not every possible
one. Combine it with least-privilege tool policies and approvals for anything that matters.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Callable

from .config import GuardrailPolicyCfg, GuardrailsCfg, StageCfg
from .errors import UARError
from .redaction import BUILTIN as PII_PATTERNS, _card

log = logging.getLogger("uar.guardrails")

# ------------------------------------------------------------------ detectors

PII_TYPES = ("email", "phone", "us_ssn", "iban", "ipv4")

PCI: dict[str, tuple[str, Callable[[re.Match], bool] | None]] = {
    "card_number": (PII_PATTERNS["card_number"][0], _card),
    "cvv": (r"(?i)\b(?:cvv2?|cvc2?|cid|security\s+code|card\s+verification(?:\s+(?:value|code))?)\b\W{0,6}\d{3,4}\b", None),
    "track_data": (r"%B\d{12,19}\^[^^\n]{2,26}\^\d{4}|;\d{12,19}=\d{4}\d*\?", None),
}

SECRETS: dict[str, str] = {
    "private_key": r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED |PGP )?PRIVATE KEY(?: BLOCK)?-----",
    "aws_access_key": r"\b(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}\b",
    "github_token": r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36}\b|\bgithub_pat_[A-Za-z0-9_]{60,}\b",
    "slack_token": r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b",
    "anthropic_key": r"\bsk-ant-[A-Za-z0-9_-]{20,}\b",
    "openai_key": r"\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}\b",
    "google_api_key": r"\bAIza[0-9A-Za-z_-]{35}\b",
    "uar_key": r"\buars?_[A-Za-z0-9]{0,32}_?[A-Za-z0-9_-]{24,}\b",
    "jwt": r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b",
    "connection_string": r"\b[a-z][a-z0-9+]{2,15}://[^\s:/@]{1,64}:[^\s@/]{3,128}@[^\s/]+",
    "password_assignment": r"(?i)\b(?:password|passwd|pwd|passphrase)\b\s*[:=]\s*[\"']?[^\s\"']{6,}",
    "generic_api_key": r"(?i)\b(?:api[_-]?key|secret[_-]?key|access[_-]?token|client[_-]?secret)\b\s*[:=]\s*[\"']?[A-Za-z0-9_\-./+]{16,}",
}

# Prompt injection: (weight, pattern). Scores combine as 1 - prod(1 - w) over distinct matches.
_INJECTION: list[tuple[str, float, str]] = [
    ("override_instructions", 0.9, r"\b(?:ignore|disregard|forget|override|bypass|skip)\b[\w\s,]{0,40}?\b(?:previous|prior|above|earlier|preceding|all|any|your|the|system|original)\b[\w\s]{0,20}?\b(?:instructions?|prompts?|rules?|directives?|guidelines?|messages?|context|constraints?)"),
    ("new_instructions", 0.55, r"\b(?:new|updated|real|actual|revised)\s+(?:instructions?|rules?|system\s+prompt)\s*[:\-]"),
    ("reveal_system_prompt", 0.8, r"\b(?:reveal|print|show|repeat|output|display|leak|tell\s+me|what\s+(?:is|are))\b[\w\s]{0,30}?\b(?:system\s+prompt|hidden\s+(?:prompt|instructions?)|initial\s+(?:prompt|instructions?)|your\s+(?:instructions|rules|guidelines|prompt))"),
    ("jailbreak_persona", 0.85, r"\b(?:DAN|do\s+anything\s+now|developer\s+mode|jailbr(?:ea|o)k(?:en)?|god\s+mode|unfiltered\s+mode|evil\s+confidant|AIM\s+mode)\b"),
    ("no_restrictions", 0.7, r"\b(?:without|no|free\s+of|ignore\s+(?:all|your))\s+(?:any\s+)?(?:restrictions?|filters?|limitations?|censorship|safety|guidelines|ethical\s+(?:guidelines|constraints))\b"),
    ("pretend_role", 0.5, r"\b(?:pretend|act|behave|roleplay|role-play)\s+(?:to\s+be|as|like)\s+(?:an?\s+)?(?:unrestricted|uncensored|unfiltered|different|evil|rogue|jailbroken|hacker)"),
    ("role_marker_spoof", 0.75, r"(?im)(?:<\|?(?:im_start|im_end|system|endoftext)\|?>|\[/?INST\]|<</?SYS>>|</?system>|^\s*#{1,3}\s*system\s*:|^\s*system\s*:\s*you\s+are)"),
    ("exfiltration", 0.6, r"\b(?:send|post|upload|forward|exfiltrate|transmit)\b[\w\s]{0,40}?\b(?:to|at)\b\s+(?:https?://|\S+@\S+\.\w+)"),
    ("tool_hijack", 0.55, r"\b(?:call|invoke|use|run|execute)\s+(?:the\s+)?(?:tool|function)\b[\w\s]{0,30}?\b(?:without|instead|immediately|silently)\b"),
    ("obey_embedded", 0.6, r"\b(?:AI|assistant|model|LLM|chatbot)s?\b[\w\s,]{0,20}?\b(?:must|should|shall|will)\b[\w\s]{0,15}?\b(?:now|immediately|instead)\b"),
]
_BASE64_BLOB = re.compile(r"[A-Za-z0-9+/]{200,}={0,2}")
_TAG_CHARS = re.compile("[\U000E0000-\U000E007F]")
_ZERO_WIDTH = re.compile("[\u200b\u200c\u200d\u2060\ufeff]{3,}")
_BIDI = re.compile("[\u202a-\u202e\u2066-\u2069]")


def _norm(text: str) -> str:
    """Undo common obfuscation before matching: compatibility forms and invisible characters."""
    t = unicodedata.normalize("NFKC", text)
    return re.sub("[\u200b\u200c\u200d\u2060\ufeff\u00ad]", "", t)


def injection_score(text: str) -> tuple[float, list[str]]:
    """0..1 likelihood that the text tries to override the model's instructions, plus the signals."""
    signals: list[tuple[str, float]] = []
    if _TAG_CHARS.search(text):
        signals.append(("hidden_unicode_tags", 0.8))   # invisible "ASCII smuggling"
    if _ZERO_WIDTH.search(text):
        signals.append(("zero_width_run", 0.35))
    if _BIDI.search(text):
        signals.append(("bidi_override", 0.3))
    t = _norm(text)
    for name, weight, rx in _COMPILED_INJECTION:
        if rx.search(t):
            signals.append((name, weight))
    if _BASE64_BLOB.search(t):
        signals.append(("encoded_blob", 0.25))
    p = 1.0
    for _, w in signals:
        p *= 1 - w
    return round(1 - p, 3), [n for n, _ in signals]


_COMPILED_INJECTION = [(n, w, re.compile(rx, re.IGNORECASE)) for n, w, rx in _INJECTION]


@dataclass
class Match:
    type: str
    start: int
    end: int


def _find(text: str, patterns: dict[str, Any], types: list[str]) -> list[Match]:
    out: list[Match] = []
    for name, spec in patterns.items():
        if types and name not in types:
            continue
        rx, check = (spec, None) if isinstance(spec, str) else spec
        for m in _RX_CACHE.setdefault(rx, re.compile(rx)).finditer(text):
            if check is None or check(m):
                out.append(Match(name, m.start(), m.end()))
    return out


_RX_CACHE: dict[str, re.Pattern] = {}
_PII = {k: PII_PATTERNS[k] for k in PII_TYPES}


def _redact(text: str, matches: list[Match]) -> str:
    """Replace matched spans (overlaps merged, earliest type wins) with [redacted:<type>]."""
    spans: list[Match] = []
    for m in sorted(matches, key=lambda m: (m.start, -m.end)):
        if spans and m.start < spans[-1].end:
            spans[-1].end = max(spans[-1].end, m.end)
            continue
        spans.append(Match(m.type, m.start, m.end))
    for m in reversed(spans):
        text = text[:m.start] + f"[redacted:{m.type}]" + text[m.end:]
    return text


# ------------------------------------------------------------------ evaluation

_RANK = {"off": 0, "flag": 1, "redact": 2, "block": 3}


@dataclass
class Result:
    text: str
    findings: list[dict] = field(default_factory=list)
    action: str = "allow"          # allow | flag | redact | block

    @property
    def blocked(self) -> bool:
        return self.action == "block"


_DETECTORS = (("pii", _PII), ("pci", PCI), ("secrets", SECRETS))


def spans(stage_cfg: StageCfg, text: str, terms_rx: re.Pattern | None = None,
          actions: tuple[str, ...] = ("flag", "redact", "block")) -> dict[str, list[Match]]:
    """Matches of the pattern checks (pii, pci, secrets, denied_terms) whose action is in `actions`."""
    out: dict[str, list[Match]] = {}
    for check, patterns in _DETECTORS:
        cfg = getattr(stage_cfg, check)
        if cfg and cfg.action in actions:
            out[check] = _find(text, patterns, cfg.types)
    dt = stage_cfg.denied_terms
    if dt and dt.action in actions and terms_rx is not None:
        out["denied_terms"] = [Match("denied_term", m.start(), m.end()) for m in terms_rx.finditer(text)]
    return out


def evaluate(stage_cfg: StageCfg, stage: str, text: str, terms_rx: re.Pattern | None = None,
             model_score: float | None = None) -> Result:
    """Run every configured check of one stage over one text."""
    res = Result(text)

    def hit(check: str, action: str, typ: str, count: int, score: float | None = None) -> None:
        f = {"stage": stage, "check": check, "type": typ, "action": action, "count": count}
        if score is not None:
            f["score"] = score
        res.findings.append(f)
        if _RANK[action] > _RANK.get(res.action, 0):
            res.action = action

    if stage_cfg.max_chars and len(text) > stage_cfg.max_chars:
        hit("max_chars", "block", "too_long", 1)

    inj = stage_cfg.prompt_injection
    if inj and inj.action != "off":
        score, signals = injection_score(text)
        if model_score is not None:
            score = max(score, model_score)
            signals = signals + ["classifier"]
        if score >= inj.threshold:
            hit("prompt_injection", inj.action, ",".join(signals) or "classifier", 1, score)

    # Pattern checks run on the text as written. If normalising it (compatibility forms, invisible
    # characters) reveals more, the normalised text is what gets checked and, if needed, redacted.
    base = text
    found = spans(stage_cfg, text, terms_rx)
    norm = _norm(text)
    if norm != text:
        found_n = spans(stage_cfg, norm, terms_rx)
        if sum(map(len, found_n.values())) > sum(map(len, found.values())):
            base, found = norm, found_n
    # A card number is not also a phone number: drop PII matches that overlap PCI or secret matches.
    stronger = found.get("pci", []) + found.get("secrets", [])
    if "pii" in found and stronger:
        found["pii"] = [m for m in found["pii"] if not any(m.start < s.end and s.start < m.end for s in stronger)]
    to_redact: list[Match] = []
    for check, matches in found.items():
        action = getattr(stage_cfg, check).action
        for typ in sorted({m.type for m in matches}):
            hit(check, action, typ, sum(1 for m in matches if m.type == typ))
        if action == "redact":
            to_redact += matches
    if to_redact and res.action != "block":
        res.text = _redact(base, to_redact)
    return res


def terms_regex(stage_cfg: StageCfg) -> re.Pattern | None:
    dt = stage_cfg.denied_terms
    if not dt or (not dt.terms and not dt.patterns):
        return None
    parts = [r"\b" + re.escape(t) + r"\b" for t in dt.terms] + list(dt.patterns)
    return re.compile("|".join(f"(?:{p})" for p in parts), re.IGNORECASE)


class Guardrails:
    """Policy resolution (per tenant, exempt roles) and the stage checks used by the model router."""

    def __init__(self, cfg: GuardrailsCfg):
        self.cfg = cfg
        self._terms: dict[tuple[int, str], re.Pattern | None] = {}

    def policy(self, tenant: str, roles: tuple[str, ...]) -> GuardrailPolicyCfg | None:
        if not self.cfg.enabled:
            return None
        pol = self.cfg.tenants.get(tenant, self.cfg.policy)
        if set(pol.exempt_roles) & set(roles):
            return None
        return pol

    def stage(self, pol: GuardrailPolicyCfg, stage: str) -> StageCfg:
        return getattr(pol, stage)

    def _terms_rx(self, pol: GuardrailPolicyCfg, stage: str) -> re.Pattern | None:
        key = (id(pol), stage)
        if key not in self._terms:
            self._terms[key] = terms_regex(self.stage(pol, stage))
        return self._terms[key]

    def check(self, pol: GuardrailPolicyCfg, stage: str, text: str, model_score: float | None = None) -> Result:
        return evaluate(self.stage(pol, stage), stage, text, self._terms_rx(pol, stage), model_score)

    def check_messages(self, pol: GuardrailPolicyCfg, messages: list[dict]) -> tuple[list[dict], list[dict], bool]:
        """Input and tool-result stages over chat messages. System messages (the application's own
        instructions) are trusted and not checked. Returns (messages to send, findings, blocked)."""
        out, findings, blocked = [], [], False
        for m in messages:
            role, content = m.get("role"), m.get("content")
            if role == "system" or not isinstance(content, str) or not content:
                out.append(m)
                continue
            stage = "tool_results" if role == "tool" else "input"
            r = self.check(pol, stage, content)
            findings += r.findings
            blocked |= r.blocked
            out.append({**m, "content": r.text} if r.text != content else m)
        return out, findings, blocked

    def needs_output_buffer(self, pol: GuardrailPolicyCfg) -> bool:
        """Output checks that act on the text (redact/block) need a hold-back buffer when streaming."""
        o = pol.output
        acts = [c.action for c in (o.pii, o.pci, o.secrets, o.denied_terms, o.prompt_injection) if c]
        return any(a in ("redact", "block") for a in acts)


def blocked_error(findings: list[dict], stage: str) -> UARError:
    blocking = [f for f in findings if f["action"] == "block"]
    what = ", ".join(sorted({f["check"] for f in blocking}))
    return UARError("guardrail_blocked", f"request blocked by guardrails ({stage}: {what})",
                    details={"stage": stage, "findings": blocking})


class StreamGuard:
    """Output checks on a stream. The last `window` characters are held back, and a release never cuts
    through a match, so a secret or card number split across chunks is still caught and redacted.
    Blocking checks stop the stream with guardrail_blocked."""

    def __init__(self, guard: Guardrails, pol: GuardrailPolicyCfg, window: int = 96):
        self.guard, self.pol, self.window = guard, pol, window
        self.buf = ""
        self.findings: list[dict] = []

    def feed(self, delta: str) -> str:
        self.buf += delta
        if len(self.buf) <= self.window:
            return ""
        return self._release(len(self.buf) - self.window)

    def flush(self) -> str:
        return self._release(len(self.buf))

    def _release(self, cut: int) -> str:
        stage = self.guard.stage(self.pol, "output")
        for matches in spans(stage, self.buf, self.guard._terms_rx(self.pol, "output")).values():
            for m in matches:
                if m.start < cut < m.end:
                    cut = m.start
        if cut <= 0:
            return ""
        chunk, self.buf = self.buf[:cut], self.buf[cut:]
        r = self.guard.check(self.pol, "output", chunk)
        self._merge(r.findings)
        if r.blocked:
            raise blocked_error(r.findings, "output")
        return r.text

    def _merge(self, findings: list[dict]) -> None:
        for f in findings:
            same = next((g for g in self.findings if (g["check"], g["type"]) == (f["check"], f["type"])), None)
            if same:
                same["count"] += f["count"]
            else:
                self.findings.append(dict(f))
