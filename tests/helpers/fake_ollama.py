"""A PROTOCOL DOUBLE for Ollama's documented HTTP API, backed by a real local HTTP server.

It is NOT a model: it returns whatever the supplied ``responder`` callback returns. Use it to verify PatternVault's
client, prompt building, parsing, repair loop and policy logic. Model *quality* can only be judged with a real
model (see tests/e2e/test_live_ollama.py, enabled with PV_OLLAMA_URL).
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

Responder = Callable[[str, list[dict[str, str]], dict[str, Any]], str]


class FakeOllama:
    def __init__(self, models: list[str] | None = None, responder: Responder | None = None) -> None:
        self.models = models if models is not None else ["qwen2.5-coder:7b"]
        self.responder: Responder = responder or (lambda model, msgs, opts: "OK")
        self.requests: list[dict[str, Any]] = []  # every POST body received, in order
        self.fail_status: int | None = None
        self.stream_error: str | None = None
        self.pull_steps = [
            {"status": "pulling manifest"},
            {"status": "downloading", "completed": 50, "total": 100},
            {"status": "downloading", "completed": 100, "total": 100},
            {"status": "success"},
        ]
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def _send(self, code: int, body: Any, ctype: str = "application/json") -> None:
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                if outer.fail_status:
                    return self._send(outer.fail_status, {"error": "boom"})
                if self.path == "/api/version":
                    return self._send(200, {"version": "0.0.0-double"})
                if self.path == "/api/tags":
                    return self._send(
                        200,
                        {
                            "models": [
                                {
                                    "name": m,
                                    "size": 4_000_000_000,
                                    "modified_at": "2026-01-01T00:00:00Z",
                                    "details": {
                                        "family": "qwen2",
                                        "parameter_size": "7B",
                                        "quantization_level": "Q4_K_M",
                                    },
                                }
                                for m in outer.models
                            ]
                        },
                    )
                if self.path == "/api/ps":
                    return self._send(
                        200,
                        {
                            "models": [{"name": outer.models[0], "size": 5_000_000_000, "size_vram": 0}]
                            if outer.models
                            else []
                        },
                    )
                self._send(404, {"error": "not found"})

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0")) or 0) or b"{}")
                outer.requests.append({"path": self.path, **body})
                if outer.fail_status:
                    return self._send(outer.fail_status, {"error": "boom"})
                model = body.get("model", "")
                if self.path == "/api/show":
                    return (
                        self._send(200, {"details": {"family": "qwen2"}})
                        if model in outer.models
                        else self._send(404, {"error": f"model '{model}' not found"})
                    )
                if self.path == "/api/chat":
                    if model not in outer.models and f"{model}:latest" not in outer.models:
                        return self._send(404, {"error": f"model '{model}' not found, try pulling it first"})
                    text = outer.responder(model, body.get("messages", []), body.get("options", {}))
                    if body.get("stream"):
                        self.send_response(200)
                        self.send_header("Content-Type", "application/x-ndjson")
                        self.end_headers()
                        words = text.split(" ")
                        for i, w in enumerate(words):
                            self.wfile.write(
                                (
                                    json.dumps(
                                        {
                                            "message": {
                                                "role": "assistant",
                                                "content": w + (" " if i < len(words) - 1 else ""),
                                            },
                                            "done": False,
                                        }
                                    )
                                    + "\n"
                                ).encode()
                            )
                        if outer.stream_error:
                            self.wfile.write((json.dumps({"error": outer.stream_error}) + "\n").encode())
                            return
                        self.wfile.write(
                            (
                                json.dumps({"done": True, "prompt_eval_count": 11, "eval_count": len(words)}) + "\n"
                            ).encode()
                        )
                        return
                    return self._send(
                        200,
                        {
                            "model": model,
                            "message": {"role": "assistant", "content": text},
                            "done": True,
                            "done_reason": "stop",
                            "prompt_eval_count": 11,
                            "eval_count": max(1, len(text.split())),
                        },
                    )
                if self.path == "/api/embed":
                    inputs = body.get("input", [])
                    return self._send(200, {"embeddings": [[float(len(t) % 7 + 1), 2.0, 3.0] for t in inputs]})
                if self.path == "/api/pull":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/x-ndjson")
                    self.end_headers()
                    for step in outer.pull_steps:
                        self.wfile.write((json.dumps(step) + "\n").encode())
                    return
                self._send(404, {"error": "not found"})

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self._srv.server_port}"
        self._t = threading.Thread(target=self._srv.serve_forever, daemon=True)

    def __enter__(self) -> FakeOllama:
        self._t.start()
        return self

    def __exit__(self, *a: Any) -> None:
        self._srv.shutdown()
        self._srv.server_close()

    def chats(self) -> list[dict[str, Any]]:
        return [r for r in self.requests if r["path"] == "/api/chat"]


class FakeCloud:
    """OpenAI-compatible /chat/completions double (records Authorization headers)."""

    def __init__(self, responder: Callable[[list[dict[str, str]]], str]) -> None:
        self.calls: list[dict[str, Any]] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.calls.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
                data = json.dumps(
                    {
                        "choices": [{"message": {"role": "assistant", "content": responder(body["messages"])}}],
                        "usage": {"prompt_tokens": 5, "completion_tokens": 7},
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self._srv.server_port}/v1"
        self._t = threading.Thread(target=self._srv.serve_forever, daemon=True)

    def __enter__(self) -> FakeCloud:
        self._t.start()
        return self

    def __exit__(self, *a: Any) -> None:
        self._srv.shutdown()
        self._srv.server_close()
