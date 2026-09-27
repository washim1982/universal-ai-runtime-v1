"""Model name grammar.

    model := [class ":"] [provider "/"] name
    class := "local" | "cloud" | "enterprise"

The class prefix is recognised only when the text before the FIRST colon is a class name,
so "local:llama3:8b" is (local, None, "llama3:8b") and "llama3:8b" has no class.
"""
from __future__ import annotations

from dataclasses import dataclass

CLASSES = ("local", "cloud", "enterprise")


@dataclass(frozen=True)
class ModelName:
    model_class: str | None
    provider: str | None
    name: str

    def __str__(self) -> str:
        s = f"{self.provider}/{self.name}" if self.provider else self.name
        return f"{self.model_class}:{s}" if self.model_class else s


def parse(text: str) -> ModelName:
    text = (text or "").strip()
    if not text:
        raise ValueError("model is required")
    cls = None
    head, sep, rest = text.partition(":")
    if sep and head in CLASSES:
        cls, text = head, rest
    if not text:
        raise ValueError("model name is empty")
    provider = None
    if "/" in text:
        p, _, n = text.partition("/")
        # Provider ids are simple identifiers; "org/model" ids (LM Studio, Hugging Face) are
        # disambiguated by the resolver, which only treats p as a provider if one is configured.
        provider, text = p, n
    if not text:
        raise ValueError("model name is empty")
    return ModelName(cls, provider, text)
