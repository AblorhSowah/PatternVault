"""E2E fixtures: a REAL backend process (as the extension would start it) + the REAL webview sources served over HTTP.

The extension host is replaced by a thin Python shim that forwards the webview's postMessage RPC to the real
backend (the host allow-list/confirmation logic is covered by Vitest). Confirmations are recorded and auto-accepted.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
MEDIA = ROOT / "vscode-extension" / "media"
GOLDEN = ROOT / "vscode-extension" / "test" / "fixtures" / "shell.golden.html"
SHOTS = ROOT / "test-results"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class Backend:
    def __init__(self, home: Path, claude_dir: Path) -> None:
        self.port = free_port()
        self.token = secrets.token_urlsafe(24)
        self.home = home
        env = {
            **os.environ,
            "PATTERNVAULT_TOKEN": self.token,
            "PATTERNVAULT_PORT": str(self.port),
            "PATTERNVAULT_HOME": str(home),
            "CLAUDE_CONFIG_DIR": str(claude_dir),
            "PYTHONUNBUFFERED": "1",
        }
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "app", "--port", str(self.port)],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            cwd=ROOT / "backend",
        )
        self.base = f"http://127.0.0.1:{self.port}"
        deadline = time.time() + 40
        while time.time() < deadline:
            try:
                if httpx.get(f"{self.base}/api/v1/health", timeout=1).status_code == 200:
                    return
            except httpx.HTTPError:
                time.sleep(0.2)
        self.proc.kill()
        raise RuntimeError("backend did not start")

    def call(self, method: str, path: str, body=None, query=None):
        r = httpx.request(
            method,
            f"{self.base}/api/v1{path}",
            json=body,
            params=query,
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=60,
        )
        return r

    def stop(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(8)
        except subprocess.TimeoutExpired:
            self.proc.kill()


@pytest.fixture()
def backend(tmp_path: Path) -> Iterator[Backend]:
    claude = tmp_path / "claude"
    (claude / "projects").mkdir(parents=True)
    b = Backend(tmp_path / "pv-home", claude)
    b.claude_dir = claude  # type: ignore[attr-defined]
    yield b
    b.stop()


class _Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


@pytest.fixture(scope="session")
def static_server() -> Iterator[str]:
    handler = partial(_Quiet, directory=str(MEDIA))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


SHIM = """
window.__pvMessages = [];
window.acquireVsCodeApi = () => ({
  postMessage: (m) => { window.__pvMessages.push(m); window.__pvHost(JSON.stringify(m)).then((r) => { if (r) window.postMessage(JSON.parse(r), '*'); }); },
  getState: () => undefined, setState: () => undefined,
});
"""


class Workbench:
    def __init__(self, page, backend: Backend, base: str) -> None:
        self.page, self.backend, self.base = page, backend, base
        self.confirms: list[str] = []
        self.saved: list[tuple[str, object]] = []
        self.commands: list[str] = []
        self.auto_confirm = True
        page.expose_function("__pvHost", self._host)
        page.add_init_script(SHIM)
        self.errors: list[str] = []
        page.on("console", lambda m: self.errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: self.errors.append(f"pageerror: {e}"))

    def _host(self, raw: str) -> str | None:
        m = json.loads(raw)
        t = m["type"]
        if t == "api":
            r = self.backend.call(m["method"], m["path"], m.get("body"), m.get("query"))
            try:
                data = r.json() if r.content else None
            except ValueError:
                data = None
            err = None if r.is_success else (data.get("detail") if isinstance(data, dict) else r.text)
            return json.dumps(
                {
                    "type": "apiResult",
                    "id": m["id"],
                    "ok": r.is_success,
                    "status": r.status_code,
                    "data": data,
                    "error": err if err is None or isinstance(err, str) else json.dumps(err),
                }
            )
        if t == "confirm":
            self.confirms.append(m["message"])
            return json.dumps({"type": "confirmResult", "id": m["id"], "ok": self.auto_confirm})
        if t == "saveJson":
            self.saved.append((m["suggestedName"], m["data"]))
            return json.dumps({"type": "saveResult", "id": m["id"], "ok": True})
        if t == "command":
            self.commands.append(m["command"])
            return json.dumps({"type": "commandResult", "id": m["id"], "ok": True})
        return None

    def open(self) -> None:
        html = (
            GOLDEN.read_text()
            .replace("{{CSP_SOURCE}}", self.base)
            .replace("{{NONCE}}", secrets.token_hex(8))
            .replace("{{SCRIPT_URI}}", f"{self.base}/webview/workbench.js")
            .replace("{{STYLE_URI}}", f"{self.base}/webview/workbench.css")
        )
        # Serve through a route so the page has a real http origin (module imports need it).
        self.page.route(
            f"{self.base}/index.html", lambda route: route.fulfill(status=200, content_type="text/html", body=html)
        )
        self.page.goto(f"{self.base}/index.html")
        self.page.wait_for_selector("#content")

    def nav(self, section: str) -> None:
        self.page.click(f'.nav-item[data-section="{section}"]')

    def shot(self, name: str) -> Path:
        SHOTS.mkdir(exist_ok=True)
        p = SHOTS / f"{name}.png"
        self.page.screenshot(path=str(p), full_page=True)
        return p


@pytest.fixture(scope="session")
def browser():
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:  # browser binary missing → visible skip, never a silent pass
            pytest.skip(f"Playwright Chromium is not installed: {exc}")
        yield b
        b.close()


@pytest.fixture()
def wb(browser, backend, static_server) -> Iterator[Workbench]:
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    w = Workbench(page, backend, static_server)
    yield w
    ctx.close()
