"""Local models, resources, generation, replay and comparison."""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Any

import psutil
from fastapi import APIRouter, Header, HTTPException

from app.api.deps import Authed, Db, Svc
from app.api.schemas import CompareIn, GenerateIn, ModelPull, ModelSelect, ReplayIn
from app.generation.generator import GenerationError, GenerationRequest, unified_diff
from app.generation.ollama import OllamaClient, OllamaError, OllamaUnavailable
from app.models import CodeImplementation
from app.replay.replayer import ReplayError
from app.services.modes import ModeViolation, NoBackendAvailable

router = APIRouter(tags=["generation"], dependencies=[Authed])
CATALOG = Path(__file__).resolve().parents[2] / "generation" / "model_catalog.json"  # ships inside the wheel
_pulls: dict[str, asyncio.Task[None]] = {}


def _catalog() -> dict[str, Any]:
    try:
        data = json.loads(CATALOG.read_text())
        return {k: v for k, v in data.items() if not k.startswith("_")}
    except (OSError, ValueError):
        return {"generation": [], "embedding": []}


async def model_status(svc: Any) -> dict[str, Any]:
    cfg = svc.config.config.local_model
    client = OllamaClient.from_config(cfg)
    out: dict[str, Any] = {
        "reachable": False,
        "version": None,
        "base_url": cfg.base_url,
        "selected": cfg.model,
        "embedding_selected": cfg.embedding_model,
        "installed": [],
        "running": [],
        "reason": "",
    }
    try:
        out["version"] = await client.version(2.0)
        out["reachable"] = True
        out["installed"] = [m.to_dict() for m in await client.list_models()]
        out["running"] = await client.running()
        names = {m["name"] for m in out["installed"]}
        if not cfg.model:
            out["reason"] = "no local model selected"
        elif cfg.model not in names and f"{cfg.model}:latest" not in names:
            out["reason"] = f"selected model '{cfg.model}' is not installed"
    except OllamaError as exc:
        out["reason"] = str(exc)
    out["ready"] = out["reachable"] and not out["reason"]
    return out


@router.get("/models")
async def models(svc: Svc) -> dict[str, Any]:
    st = await model_status(svc)
    st["recommended"] = _catalog()
    st["pulling"] = [m for m, t in _pulls.items() if not t.done()]
    return st


@router.get("/models/status")
async def status(svc: Svc) -> dict[str, Any]:
    return await model_status(svc)


@router.post("/models/select")
async def select(body: ModelSelect, svc: Svc) -> dict[str, Any]:
    st = await model_status(svc)
    names = {m["name"] for m in st["installed"]}
    if (
        body.model
        and not body.force
        and st["reachable"]
        and body.model not in names
        and f"{body.model}:latest" not in names
    ):
        raise HTTPException(422, f"model '{body.model}' is not installed in Ollama; pull it first")
    patch: dict[str, Any] = {}
    if body.model is not None:
        patch["model"] = body.model
    if body.embedding_model is not None:
        patch["embedding_model"] = body.embedding_model or None
    if not patch:
        raise HTTPException(422, "nothing to select")
    svc.config.update({"local_model": patch})
    svc.embedders.invalidate()
    svc.audit.record("user", "model.select", resource_type="config", detail=patch)
    svc.events.publish("config.changed", {"sections": ["local_model"]})
    return await model_status(svc)


@router.post("/models/pull", status_code=202)
async def pull(body: ModelPull, svc: Svc) -> dict[str, Any]:
    if body.model in _pulls and not _pulls[body.model].done():
        raise HTTPException(409, "already pulling this model")
    client = OllamaClient.from_config(svc.config.config.local_model)
    if not await client.is_available():
        raise HTTPException(503, f"Ollama is not reachable at {client.base_url}")
    svc.audit.record(
        "user", "model.pull", resource_type="model", resource_id=body.model, detail={"host": client.base_url}
    )

    async def run() -> None:
        try:
            async for chunk in client.pull(body.model):
                svc.events.publish(
                    "model.pull.progress",
                    {
                        "model": body.model,
                        "status": chunk.get("status"),
                        "completed": chunk.get("completed"),
                        "total": chunk.get("total"),
                    },
                )
            svc.events.publish("model.pull.done", {"model": body.model})
        except OllamaError as exc:
            svc.events.publish("model.pull.failed", {"model": body.model, "error": str(exc)})

    _pulls[body.model] = asyncio.create_task(run())
    return {"started": True, "model": body.model}


@router.post("/models/test")
async def test_model(svc: Svc, model: str | None = None) -> dict[str, Any]:
    from app.generation.llm import OllamaLLM

    try:
        llm = OllamaLLM.from_config(svc.config.config.local_model, model)
        res = await llm.chat([{"role": "user", "content": "Reply with the single word OK."}])
    except OllamaUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except OllamaError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {
        "ok": True,
        "model": res.model,
        "reply": res.content.strip()[:200],
        "latency_ms": res.total_ms,
        "prompt_tokens": res.prompt_tokens,
        "completion_tokens": res.completion_tokens,
    }


@router.get("/system/resources")
async def resources(svc: Svc) -> dict[str, Any]:
    vm = psutil.virtual_memory()
    out: dict[str, Any] = {
        "cpu_percent": psutil.cpu_percent(interval=0.2),
        "cpu_count": psutil.cpu_count(logical=True),
        "ram_total_mb": vm.total // 2**20,
        "ram_used_mb": (vm.total - vm.available) // 2**20,
        "ram_percent": vm.percent,
        "blob_store_bytes": svc.blobs.total_bytes(),
        "local_model": {"running": []},
    }
    with contextlib.suppress(OllamaError):
        out["local_model"]["running"] = await OllamaClient.from_config(svc.config.config.local_model).running()
    return out


@router.get("/modes/availability")
async def availability(svc: Svc) -> dict[str, Any]:
    return (await svc.modes.availability()).to_dict()


@router.post("/generate")
async def generate(body: GenerateIn, svc: Svc, x_pv_cloud_key: str | None = Header(default=None)) -> dict[str, Any]:
    req = GenerationRequest(
        body.request,
        body.project_id,
        body.mode,
        body.pattern_ids,
        body.model,
        body.backend,
        x_pv_cloud_key,
        body.max_repairs,
        body.top_k,
    )
    try:
        return await svc.generator.generate(req)
    except ModeViolation as exc:
        raise HTTPException(403, str(exc)) from exc
    except NoBackendAvailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except (GenerationError, OllamaError) as exc:
        raise HTTPException(502 if isinstance(exc, OllamaUnavailable) else 422, str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/implementations")
def list_implementations(db: Db, limit: int = 50) -> list[dict[str, Any]]:
    rows = db.query(CodeImplementation).order_by(CodeImplementation.id.desc()).limit(limit).all()
    return [
        {
            "id": r.id,
            "kind": r.kind,
            "request": r.request[:200],
            "model": r.model,
            "status": r.status,
            "verified": r.verified,
            "files": [f["path"] for f in r.files],
            "pattern_id": r.pattern_id,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]


@router.get("/implementations/{impl_id}")
def get_implementation(impl_id: int, db: Db, svc: Svc) -> dict[str, Any]:
    r = db.get(CodeImplementation, impl_id)
    if r is None:
        raise HTTPException(404, "implementation not found")
    return {
        "id": r.id,
        "kind": r.kind,
        "request": r.request,
        "model": r.model,
        "status": r.status,
        "verified": r.verified,
        "pattern_id": r.pattern_id,
        "created_at": r.created_at.isoformat(),
        "files": [
            {"path": f["path"], "action": f.get("action"), "content": svc.blobs.get_text(f["blob"]) or ""}
            for f in r.files
        ],
    }


@router.post("/compare")
def compare(body: CompareIn, db: Db, svc: Svc) -> dict[str, Any]:
    a, b = db.get(CodeImplementation, body.a), db.get(CodeImplementation, body.b)
    if a is None or b is None:
        raise HTTPException(404, "implementation not found")
    fa = {f["path"]: svc.blobs.get_text(f["blob"]) or "" for f in a.files}
    fb = {f["path"]: svc.blobs.get_text(f["blob"]) or "" for f in b.files}
    out = []
    for path in sorted(set(fa) | set(fb)):
        status = (
            "only_a"
            if path not in fb
            else "only_b"
            if path not in fa
            else ("same" if fa[path] == fb[path] else "different")
        )
        out.append(
            {
                "path": path,
                "status": status,
                "diff": unified_diff(path, fa.get(path, ""), fb.get(path, "")) if status == "different" else "",
            }
        )
    return {"a": {"id": a.id, "model": a.model}, "b": {"id": b.id, "model": b.model}, "files": out}


@router.post("/replay/plan")
def replay_plan(body: ReplayIn, svc: Svc) -> dict[str, Any]:
    from app.knowledge.service import PatternNotFound

    try:
        plan = svc.replayer.plan(
            body.pattern_id,
            body.project_id,
            path_map=body.path_map,
            include_tests=body.include_tests,
            selected_paths=body.selected_paths,
        )
    except PatternNotFound as exc:
        raise HTTPException(404, "pattern not found") from exc
    except ReplayError as exc:
        raise HTTPException(422, str(exc)) from exc
    svc.audit.record(
        "user",
        "replay.plan",
        resource_type="pattern",
        resource_id=body.pattern_id,
        detail={"project_id": body.project_id, "files": len(plan.files)},
    )
    return plan.to_dict()
