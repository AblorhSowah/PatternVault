"""User configuration endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException

from app.api.deps import Authed, Svc
from app.config import ConfigError

router = APIRouter(prefix="/config", tags=["config"], dependencies=[Authed])


@router.get("")
def get_config(svc: Svc) -> dict[str, Any]:
    return svc.config.config.model_dump(mode="json")


@router.patch("")
def patch_config(svc: Svc, patch: dict[str, Any] = Body(...)) -> dict[str, Any]:
    before = svc.config.config
    try:
        new = svc.config.update(patch)
    except ConfigError as exc:
        svc.audit.record("user", "config.update", resource_type="config", outcome="denied", detail={"error": str(exc)})
        raise HTTPException(422, str(exc)) from exc
    changed = sorted(k for k in patch if getattr(before, k, None) != getattr(new, k, None))
    svc.audit.record(
        "user",
        "config.update",
        resource_type="config",
        permission_level=new.permission_level.value,
        detail={"changed_sections": changed},
    )
    svc.events.publish("config.changed", {"sections": changed})
    return new.model_dump(mode="json")


@router.post("/reset")
def reset_config(svc: Svc) -> dict[str, Any]:
    new = svc.config.reset()
    svc.audit.record("user", "config.reset", resource_type="config")
    svc.events.publish("config.changed", {"sections": ["*"]})
    return new.model_dump(mode="json")
