"""Test plan: WebSocket hub
- unauthenticated / wrong-token / garbage first message are closed with 4401
- authenticated client receives ready + replayed history + live events
"""

import pytest
from starlette.websockets import WebSocketDisconnect

from tests.conftest import TOKEN

pytestmark = pytest.mark.integration
WS = "/api/v1/ws"


def test_ws_rejects_bad_token(anon_client):
    with anon_client.websocket_connect(WS) as ws:
        ws.send_json({"type": "auth", "token": "wrong"})
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 4401


def test_ws_rejects_non_auth_first_message(anon_client):
    with anon_client.websocket_connect(WS) as ws:
        ws.send_text("hello")
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 4401


def test_ws_streams_events(client, services):
    services.events.publish("task.started", {"id": 1})
    with client.websocket_connect(WS) as ws:
        ws.send_json({"type": "auth", "token": TOKEN})
        assert ws.receive_json()["type"] == "ready"
        assert ws.receive_json()["type"] == "task.started"  # replayed
        client.put("/api/v1/mode", json={"mode": "offline"})
        types = []
        for _ in range(4):
            ev = ws.receive_json()
            types.append(ev["type"])
            if ev["type"] == "mode.changed":
                assert ev["data"]["mode"] == "offline"
                break
        assert "mode.changed" in types
