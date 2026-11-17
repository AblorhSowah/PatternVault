"""LLM abstraction used by agents: local Ollama (default) and an OpenAI-compatible endpoint (cloud, opt-in)."""

from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from app.generation.ollama import ChatResult, OllamaClient, OllamaError, OllamaUnavailable, build_options

END_EXPLANATION = "<<<END EXPLANATION>>>"
MAX_EXPLANATION_CHARS = 1500
_CHARS_PER_TOKEN = 3.5


def early_stop_reason(text: str) -> str | None:
    """Decide whether to stop reading a model's answer now. ``"finished"``, ``"runaway"`` or ``None`` (keep going).

    Observed with a real 7B model: it wrote every file correctly and then repeated one explanation sentence until
    the output cap (95 s of nothing useful). Only text *after* the last file block closed is judged, so code that
    legitimately repeats lines is never cut short; the hard token cap still bounds everything.
    """
    if END_EXPLANATION in text:
        return "finished"
    if text.rfind("<<<FILE") > text.rfind("<<<END FILE>>>"):
        return None  # a file is still being written
    start = text.rfind("<<<EXPLANATION>>>")
    if start != -1 and len(text) - start > MAX_EXPLANATION_CHARS:
        return "runaway"
    lines = [ln.strip() for ln in text.rsplit("\n", 9)[-8:] if len(ln.strip()) >= 20]
    if lines and max(lines.count(ln) for ln in set(lines)) >= 4:
        return "runaway"
    return None


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
        """Stream the answer so a model that loops after finishing can be cut off (``done_reason`` ``"runaway"``)."""
        t0 = time.monotonic()
        text, ptok, ctok, reason, stopped = "", 0, 0, "", None
        async with contextlib.aclosing(self.client.chat_stream(self.model, messages, self.options)) as stream:
            async for chunk in stream:
                if chunk.get("done"):
                    ptok, ctok = int(chunk.get("prompt_tokens", 0)), int(chunk.get("completion_tokens", 0))
                    reason = str(chunk.get("done_reason", ""))
                elif chunk.get("delta"):
                    text += chunk["delta"]
                    stopped = early_stop_reason(text)
                    if stopped:
                        break  # closing the stream makes Ollama stop generating
        if stopped:
            reason = "runaway" if stopped == "runaway" else "stop"
        return ChatResult(
            text,
            self.model,
            ptok,
            ctok or max(1, int(len(text) / _CHARS_PER_TOKEN)),
            int((time.monotonic() - t0) * 1000),
            reason,
        )

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
            str(data["choices"][0].get("finish_reason") or ""),
        )
