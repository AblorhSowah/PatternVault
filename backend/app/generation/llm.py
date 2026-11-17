"""LLM abstraction used by agents: local Ollama (default) and an OpenAI-compatible endpoint (cloud, opt-in)."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from app.generation.ollama import ChatResult, OllamaClient, OllamaError, OllamaUnavailable, build_options


class LLM(Protocol):
    location: str  # "local" | "cloud"

    @property
    def name(self) -> str: ...

    async def chat(self, messages: list[dict[str, str]]) -> ChatResult: ...


@dataclass
class OllamaLLM:
    client: OllamaClient
    model: str
    options: dict[str, Any]
    location: str = "local"

    @property
    def name(self) -> str:
        return f"ollama:{self.model}"

    async def chat(self, messages: list[dict[str, str]]) -> ChatResult:
        return await self.client.chat(self.model, messages, self.options)

    @classmethod
    def from_config(cls, cfg: Any, model: str | None = None) -> OllamaLLM:
        chosen = model or cfg.model
        if not chosen:
            raise OllamaError("no local model selected: choose one in Settings or pass `model`")
        return cls(OllamaClient.from_config(cfg), chosen, build_options(cfg))


@dataclass
class OpenAICompatibleLLM:
    """Chat-completions client for any OpenAI-compatible endpoint. The key lives only in memory for the call."""

    base_url: str
    model: str
    api_key: str
    timeout: float = 120.0
    location: str = "cloud"

    @property
    def name(self) -> str:
        return f"cloud:{self.model}"

    async def chat(self, messages: list[dict[str, str]]) -> ChatResult:
        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as c:
                r = await c.post(
                    f"{self.base_url.rstrip('/')}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json={"model": self.model, "messages": messages},
                )
        except httpx.HTTPError as exc:
            raise OllamaUnavailable(f"cloud endpoint unreachable: {exc}") from exc
        if r.status_code >= 400:
            raise OllamaError(f"cloud endpoint returned HTTP {r.status_code}", r.status_code)
        data = r.json()
        usage = data.get("usage", {})
        return ChatResult(
            str(data["choices"][0]["message"]["content"]),
            self.model,
            int(usage.get("prompt_tokens", 0)),
            int(usage.get("completion_tokens", 0)),
            int((time.monotonic() - t0) * 1000),
        )
