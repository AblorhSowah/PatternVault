"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import hmac
import json
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.trustedhost import TrustedHostMiddleware

from app.api.routers import chat as chat_router
from app.api.routers import config as config_router
from app.api.routers import engine as engine_router
from app.api.routers import generation as generation_router
from app.api.routers import knowledge as knowledge_router
from app.api.routers import learning as learning_router
from app.api.routers import patterns as patterns_router
from app.api.routers import projects as projects_router
from app.api.routers import system as system_router
from app.config import RuntimeSettings
from app.services.container import Services
from app.version import APP_VERSION

API_PREFIX = "/api/v1"
WS_AUTH_TIMEOUT_S = 5.0


def create_app(
    settings: RuntimeSettings | None = None, services: Services | None = None, token: str | None = None
) -> FastAPI:
    settings = settings or RuntimeSettings()
    token = token or settings.token or secrets.token_urlsafe(32)
    owns_services = services is None
    svc = services or Services(settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        svc.audit.record("system", "backend.start", detail={"version": APP_VERSION})
        yield
        svc.audit.record("system", "backend.stop")
        if owns_services:
            svc.close()

    app = FastAPI(
        title="PatternVault", version=APP_VERSION, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )
    app.state.services = svc
    app.state.token = token
    # Defence in depth against DNS-rebinding: only loopback host headers are served.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]", "testserver"])

    app.include_router(system_router.public, prefix=API_PREFIX)
    for r in (
        system_router.router,
        config_router.router,
        projects_router.router,
        learning_router.router,
        knowledge_router.router,
        generation_router.router,
        chat_router.router,
        engine_router.router,
        patterns_router.router,
    ):
        app.include_router(r, prefix=API_PREFIX)

    @app.websocket(f"{API_PREFIX}/ws")
    async def ws_events(ws: WebSocket) -> None:
        """Live event stream. The first client message must be ``{"type":"auth","token":"..."}``."""
        await ws.accept()
        try:
            raw = await asyncio.wait_for(ws.receive_text(), timeout=WS_AUTH_TIMEOUT_S)
            msg = json.loads(raw)
            supplied = str(msg.get("token", "")) if isinstance(msg, dict) else ""
            if msg.get("type") != "auth" or not hmac.compare_digest(supplied, token):
                await ws.close(code=4401, reason="unauthorized")
                return
        except (TimeoutError, json.JSONDecodeError, WebSocketDisconnect, AttributeError):
            await ws.close(code=4401, reason="unauthorized")
            return
        await ws.send_json({"type": "ready", "seq": 0})
        with svc.events.subscribe(replay=True) as queue:
            try:
                while True:
                    event = await queue.get()
                    await ws.send_json(event)
            except (WebSocketDisconnect, RuntimeError):
                return

    return app
