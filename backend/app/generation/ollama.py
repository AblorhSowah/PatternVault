"""Async client for Ollama's documented HTTP API (https://github.com/ollama/ollama/blob/main/docs/api.md).

Endpoints used: GET /api/version, /api/tags, /api/ps · POST /api/show, /api/chat, /api/embed, /api/pull.
Nothing here is specific to a particular model; resource limits come from the user's ``LocalModelConfig``.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx


class OllamaError(RuntimeError):
    """Ollama answered but refused/failed (e.g. unknown model)."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class OllamaUnavailable(OllamaError):
    """Ollama is not reachable (not installed, not running, wrong URL)."""


@dataclass
class ChatResult:
    content: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_ms: int = 0
    done_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class ModelInfo:
    name: str
    size_bytes: int = 0
    family: str = ""
    parameter_size: str = ""
    quantization: str = ""
    modified_at: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if k != "extra"}


def build_options(cfg: Any) -> dict[str, Any]:
    """Map the user's resource configuration to Ollama ``options``."""
    opts: dict[str, Any] = {"temperature": cfg.temperature, "num_ctx": cfg.num_ctx}
    if cfg.num_thread is not None:
        opts["num_thread"] = cfg.num_thread
    if cfg.num_gpu is not None:
        opts["num_gpu"] = cfg.num_gpu
    return opts


class OllamaClient:
    def __init__(self, base_url: str, timeout: float = 300.0, keep_alive: str = "5m") -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.keep_alive = keep_alive

    @classmethod
    def from_config(cls, cfg: Any) -> OllamaClient:
        return cls(cfg.base_url, cfg.request_timeout_s, cfg.keep_alive)

    # ------------------------------------------------------------------ plumbing
    def _client(self, timeout: float | None = None) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.base_url, timeout=timeout or self.timeout)

    async def _json(
        self, method: str, path: str, body: dict[str, Any] | None = None, timeout: float | None = None
    ) -> Any:
        try:
            async with self._client(timeout) as c:
                r = await c.request(method, path, json=body)
        except httpx.ConnectError as exc:
            raise OllamaUnavailable(f"cannot reach Ollama at {self.base_url} (is it running?)") from exc
        except httpx.TimeoutException as exc:
            raise OllamaUnavailable(f"Ollama at {self.base_url} timed out") from exc
        except httpx.HTTPError as exc:
            raise OllamaUnavailable(f"Ollama request failed: {exc}") from exc
        if r.status_code >= 400:
            raise OllamaError(self._error_text(r), r.status_code)
        try:
            return r.json()
        except ValueError as exc:
            raise OllamaError("Ollama returned a non-JSON response") from exc

    @staticmethod
    def _error_text(r: httpx.Response) -> str:
        try:
            body = r.json()
            return str(body.get("error", r.text))[:500]
        except ValueError:
            return r.text[:500]

    # ------------------------------------------------------------------ discovery & status
    async def version(self, timeout: float = 3.0) -> str:
        data = await self._json("GET", "/api/version", timeout=timeout)
        return str(data.get("version", ""))

    async def is_available(self, timeout: float = 2.0) -> bool:
        try:
            await self.version(timeout)
            return True
        except OllamaError:
            return False

    async def list_models(self) -> list[ModelInfo]:
        data = await self._json("GET", "/api/tags", timeout=15)
        out = []
        for m in data.get("models", []):
            d = m.get("details", {}) or {}
            out.append(
                ModelInfo(
                    str(m.get("name") or m.get("model", "")),
                    int(m.get("size", 0)),
                    str(d.get("family", "")),
                    str(d.get("parameter_size", "")),
                    str(d.get("quantization_level", "")),
                    str(m.get("modified_at", "")),
                )
            )
        return out

    async def running(self) -> list[dict[str, Any]]:
        data = await self._json("GET", "/api/ps", timeout=5)
        return [
            {
                "name": m.get("name") or m.get("model"),
                "size": int(m.get("size", 0)),
                "size_vram": int(m.get("size_vram", 0)),
                "expires_at": m.get("expires_at"),
            }
            for m in data.get("models", [])
        ]

    async def show(self, model: str) -> dict[str, Any]:
        out: dict[str, Any] = await self._json("POST", "/api/show", {"model": model}, timeout=15)
        return out

    # ------------------------------------------------------------------ generation
    def _chat_body(
        self, model: str, messages: list[dict[str, str]], options: dict[str, Any] | None, stream: bool
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"model": model, "messages": messages, "stream": stream, "keep_alive": self.keep_alive}
        if options:
            body["options"] = options
        return body

    async def chat(
        self, model: str, messages: list[dict[str, str]], options: dict[str, Any] | None = None
    ) -> ChatResult:
        t0 = time.monotonic()
        data = await self._json("POST", "/api/chat", self._chat_body(model, messages, options, False))
        msg = data.get("message", {}) or {}
        return ChatResult(
            str(msg.get("content", "")),
            str(data.get("model", model)),
            int(data.get("prompt_eval_count", 0)),
            int(data.get("eval_count", 0)),
            int((time.monotonic() - t0) * 1000),
            str(data.get("done_reason", "")),
        )

    async def chat_stream(
        self, model: str, messages: list[dict[str, str]], options: dict[str, Any] | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield NDJSON chunks: ``{"delta": str}`` per token group, then a final ``{"done": True, ...}``."""
        try:
            async with (
                self._client() as c,
                c.stream("POST", "/api/chat", json=self._chat_body(model, messages, options, True)) as r,
            ):
                if r.status_code >= 400:
                    raise OllamaError(self._error_text(await _read(r)), r.status_code)
                async for line in r.aiter_lines():
                    if not line.strip():
                        continue
                    chunk = json.loads(line)
                    if chunk.get("error"):
                        raise OllamaError(str(chunk["error"]))
                    if chunk.get("done"):
                        yield {
                            "done": True,
                            "prompt_tokens": chunk.get("prompt_eval_count", 0),
                            "completion_tokens": chunk.get("eval_count", 0),
                        }
                    else:
                        yield {"delta": (chunk.get("message") or {}).get("content", "")}
        except httpx.ConnectError as exc:
            raise OllamaUnavailable(f"cannot reach Ollama at {self.base_url}") from exc
        except httpx.HTTPError as exc:
            raise OllamaUnavailable(f"Ollama stream failed: {exc}") from exc

    async def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        data = await self._json("POST", "/api/embed", {"model": model, "input": texts})
        return [list(map(float, v)) for v in data.get("embeddings", [])]

    async def pull(self, model: str) -> AsyncIterator[dict[str, Any]]:
        """Yield pull progress objects (``status``, ``completed``, ``total``)."""
        try:
            async with (
                self._client(3600) as c,
                c.stream("POST", "/api/pull", json={"model": model, "stream": True}) as r,
            ):
                if r.status_code >= 400:
                    raise OllamaError(self._error_text(await _read(r)), r.status_code)
                async for line in r.aiter_lines():
                    if line.strip():
                        chunk = json.loads(line)
                        if chunk.get("error"):
                            raise OllamaError(str(chunk["error"]))
                        yield chunk
        except httpx.ConnectError as exc:
            raise OllamaUnavailable(f"cannot reach Ollama at {self.base_url}") from exc


async def _read(r: httpx.Response) -> httpx.Response:
    await r.aread()
    return r
