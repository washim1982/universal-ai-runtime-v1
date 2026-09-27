"""Azure OpenAI: the OpenAI-compatible adapter with Azure's authentication and URL layout.

base_url forms:
- v1 API (default):  https://<resource>.openai.azure.com/openai/v1      (model = deployment name)
- classic API:       https://<resource>.openai.azure.com/openai/deployments  plus provider `api_version`
                     -> POST {base}/{deployment}/chat/completions?api-version=<api_version>
The key is sent in the `api-key` header (api_key_env, default AZURE_OPENAI_API_KEY).
"""
from __future__ import annotations

import os

import httpx

from ...config import ProviderCfg
from .base import ChatRequest
from .openai_compat import OpenAICompatAdapter


class AzureOpenAIAdapter(OpenAICompatAdapter):
    def __init__(self, cfg: ProviderCfg):
        super().__init__(cfg)
        key = os.environ.get(cfg.api_key_env or "AZURE_OPENAI_API_KEY")
        headers = dict(cfg.extra_headers)
        if key:
            headers["api-key"] = key
        self._has_key = bool(key)
        self.api_version = cfg.api_version
        self.http = httpx.AsyncClient(base_url=cfg.base_url.rstrip("/"), headers=headers, follow_redirects=False,
                                      timeout=httpx.Timeout(cfg.timeout_s, connect=cfg.connect_timeout_s))

    def _check_key(self) -> None:
        if not self._has_key:
            from ...errors import UARError
            raise UARError("unavailable", f"{self.id}: credential {self.cfg.api_key_env or 'AZURE_OPENAI_API_KEY'} "
                           "is not configured", retryable=False)

    def _path(self, req: ChatRequest) -> str:
        if self.api_version:
            return f"/{req.model}/chat/completions?api-version={self.api_version}"
        return "/chat/completions"

    async def list_models(self) -> list[str]:
        return list(self.cfg.models)
