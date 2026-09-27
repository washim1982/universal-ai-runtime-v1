from __future__ import annotations

from ...config import ProviderCfg
from .base import Adapter


def build_adapter(cfg: ProviderCfg) -> Adapter:
    if cfg.type == "ollama":
        from .ollama import OllamaAdapter
        return OllamaAdapter(cfg)
    if cfg.type == "openai_compat":
        from .openai_compat import OpenAICompatAdapter
        return OpenAICompatAdapter(cfg)
    if cfg.type == "openai":
        from .openai_responses import OpenAIResponsesAdapter
        return OpenAIResponsesAdapter(cfg)
    if cfg.type == "anthropic":
        from .anthropic_messages import AnthropicAdapter
        return AnthropicAdapter(cfg)
    if cfg.type == "fake":
        from .fake import FakeAdapter
        return FakeAdapter(cfg)
    if cfg.type == "azure_openai":
        from .azure_openai import AzureOpenAIAdapter
        return AzureOpenAIAdapter(cfg)
    if cfg.type == "vertex":
        from .vertex import VertexAdapter
        return VertexAdapter(cfg)
    if cfg.type == "plugin":
        from .base import UnavailableAdapter
        return UnavailableAdapter(cfg, "model plugin not active yet")
    raise ValueError(f"unknown provider type {cfg.type}")
