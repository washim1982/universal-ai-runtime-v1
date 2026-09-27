"""Content redaction policies.

A Redactor replaces sensitive substrings (built-in detectors plus configured regular expressions) in
strings and, recursively, in JSON-shaped values. Where it applies is configured per sink:
audit details, the run event log, and prompts sent to selected model classes.

Detectors are pattern based: they reduce what reaches logs and third parties, but they are not a
guarantee that no personal data remains. The compliance map says so.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

from .config import RedactionCfg


def _luhn(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d > 4 else d * 2
        total, alt = total + d, not alt
    return total % 10 == 0


def _card(m: re.Match) -> bool:
    digits = re.sub(r"[ -]", "", m.group(0))
    return 13 <= len(digits) <= 19 and _luhn(digits)


BUILTIN: dict[str, tuple[str, Callable[[re.Match], bool] | None]] = {
    "email": (r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}", None),
    "card_number": (r"\b\d(?:[ -]?\d){12,18}\b", _card),
    "us_ssn": (r"\b(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b", None),
    "iban": (r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,4})?\b", None),
    "ipv4": (r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b", None),
    "phone": (r"(?<![\w+])\+?\d{1,3}[ .-]?\(?\d{2,4}\)?[ .-]?\d{3,4}[ .-]?\d{3,4}(?!\w)", None),
}


@dataclass
class _Rule:
    name: str
    rx: re.Pattern
    replace: str
    check: Callable[[re.Match], bool] | None = None


class Redactor:
    def __init__(self, cfg: RedactionCfg):
        self.cfg = cfg
        self.rules = [_Rule(n, re.compile(BUILTIN[n][0]), f"[redacted:{n}]", BUILTIN[n][1]) for n in cfg.builtin]
        self.rules += [_Rule(p.name, re.compile(p.regex), p.replace or f"[redacted:{p.name}]") for p in cfg.patterns]

    @property
    def active(self) -> bool:
        return bool(self.rules)

    def text(self, s: str) -> str:
        for r in self.rules:
            if r.check is None:
                s = r.rx.sub(r.replace, s)
            else:
                s = r.rx.sub(lambda m, r=r: r.replace if r.check(m) else m.group(0), s)
        return s

    def value(self, v: Any, depth: int = 0) -> Any:
        if not self.rules or depth > 12:
            return v
        if isinstance(v, str):
            return self.text(v)
        if isinstance(v, dict):
            return {k: self.value(x, depth + 1) for k, x in v.items()}
        if isinstance(v, list):
            return [self.value(x, depth + 1) for x in v]
        return v

    def for_audit(self, v: Any) -> Any:
        return self.value(v) if self.cfg.audit else v

    def for_events(self, v: Any) -> Any:
        return self.value(v) if self.cfg.events else v

    def egress(self, model_class: str) -> bool:
        return self.active and model_class in self.cfg.egress_classes
