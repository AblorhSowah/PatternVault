"""Chat: local conversations and streamed, knowledge-grounded answers.

A message starts a background *turn*. Tokens are pushed as ``chat.turn.delta`` events (WebSocket) and the accumulated state
is always available from ``GET /chat/turns/{id}`` so clients without a live event stream can poll.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException

from app.api.deps import Authed, Svc
from app.api.schemas import ChatMessageIn, ConversationCreate, ConversationUpdate
from app.chat.service import ChatBusy, ChatError, TurnRequest
from app.services.modes import ModeViolation

router = APIRouter(tags=["chat"], dependencies=[Authed])


@router.get("/chat/conversations")
def list_conversations(svc: Svc, limit: int = 50) -> list[dict[str, Any]]:
    return svc.chat.list_conversations(max(1, min(limit, 200)))


@router.post("/chat/conversations", status_code=201)
def create_conversation(body: ConversationCreate, svc: Svc) -> dict[str, Any]:
    try:
        return svc.chat.create_conversation(body.title, body.project_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.delete("/chat/conversations")
def delete_all_conversations(svc: Svc) -> dict[str, int]:
    return {"deleted": svc.chat.delete_all()}


@router.get("/chat/conversations/{conversation_id}")
def get_conversation(conversation_id: int, svc: Svc) -> dict[str, Any]:
    try:
        return svc.chat.get_conversation(conversation_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.patch("/chat/conversations/{conversation_id}")
def update_conversation(conversation_id: int, body: ConversationUpdate, svc: Svc) -> dict[str, Any]:
    try:
        svc.chat.set_project(conversation_id, body.project_id)
        return svc.chat.get_conversation(conversation_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.delete("/chat/conversations/{conversation_id}")
def delete_conversation(conversation_id: int, svc: Svc) -> dict[str, bool]:
    try:
        svc.chat.delete_conversation(conversation_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"deleted": True}


@router.post("/chat/conversations/{conversation_id}/messages", status_code=202)
async def send_message(
    conversation_id: int,
    body: ChatMessageIn,
    svc: Svc,
    x_pv_cloud_key: str | None = Header(default=None),
) -> dict[str, Any]:
    req = TurnRequest(body.message, body.backend, body.model, body.use_knowledge, x_pv_cloud_key, body.top_k)
    try:
        return await svc.chat.start_turn(conversation_id, req)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ModeViolation as exc:
        raise HTTPException(403, str(exc)) from exc
    except ChatBusy as exc:
        raise HTTPException(409, str(exc)) from exc
    except ChatError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/chat/turns/{turn_id}")
def get_turn(turn_id: int, svc: Svc) -> dict[str, Any]:
    try:
        return svc.chat.get_turn(turn_id)
    except LookupError as exc:
        raise HTTPException(404, "turn not found") from exc


@router.post("/chat/turns/{turn_id}/cancel")
def cancel_turn(turn_id: int, svc: Svc) -> dict[str, bool]:
    try:
        return {"cancelled": svc.chat.cancel_turn(turn_id)}
    except LookupError as exc:
        raise HTTPException(404, "turn not found") from exc
