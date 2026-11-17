"""Code generation agent logic: retrieve → prompt → local (or cloud) model → parse → validate → repair → propose."""

from __future__ import annotations

import difflib
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.generation.llm import LLM, OllamaLLM, OpenAICompatibleLLM
from app.generation.ollama import OllamaError
from app.generation.parser import parse_model_output
from app.generation.prompts import PatternRef, build_messages
from app.generation.validate import validate_file
from app.models import CodeImplementation, Dependency, Project
from app.security.exclusions import ExclusionPolicy
from app.security.permissions import PermissionPolicy
from app.security.secrets import redact

MAX_EXISTING_BYTES = 200_000


class GenerationError(RuntimeError):
    pass


@dataclass
class GenerationRequest:
    request: str
    project_id: int | None = None
    mode: str = "auto"  # auto | new | adapt
    pattern_ids: list[int] | None = None
    model: str | None = None
    backend: str = "auto"  # auto | local | cloud | compare
    cloud_key: str | None = field(default=None, repr=False)
    max_repairs: int = 2
    top_k: int = 3


@dataclass
class ProposedFile:
    path: str
    action: str  # create | modify | identical
    content: str
    diff: str
    issues: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class BackendResult:
    backend: str
    model: str
    files: list[ProposedFile]
    explanation: str
    dependencies: list[dict[str, str]]
    issues: list[dict[str, Any]]
    attempts: int
    prompt_tokens: int
    completion_tokens: int
    duration_ms: int
    implementation_id: int | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = self.__dict__.copy()
        d["files"] = [f.to_dict() for f in self.files]
        return d


def unified_diff(path: str, before: str, after: str) -> str:
    return "".join(
        difflib.unified_diff(before.splitlines(True), after.splitlines(True), f"a/{path}", f"b/{path}", n=3)
    )[:200_000]


# Added to the repair request when the answer contained no parsable file. A real 7B model answered with markdown fences
# three times in a row when only told "use the required format"; showing the literal format is what a small model needs.
FORMAT_REMINDER = (
    "\nDo NOT use markdown code fences. Write every file exactly like this, with the real path and the COMPLETE content:\n"
    '<<<FILE path="app/example.py" action="create">>>\n'
    "<complete file content>\n"
    "<<<END FILE>>>"
)


class CodeGenerator:
    def __init__(self, svc: Any) -> None:
        self.svc = svc  # app.services.container.Services (duck-typed to keep this module import-light)

    # ------------------------------------------------------------------ helpers
    def _project_info(self, project: Project | None) -> dict[str, Any]:
        if project is None:
            return {}
        with self.svc.session_factory() as db:
            deps = (
                db.execute(select(Dependency).where(Dependency.project_id == project.id, Dependency.is_dev.is_(False)))
                .scalars()
                .all()
            )
            dep_list = [{"name": d.name, "version_spec": d.version_spec} for d in deps][:60]
        conv = project.conventions or {}
        fw = next((f for f in project.frameworks or [] if not f.get("dev")), None)
        return {
            "language": conv.get("primary_language"),
            "framework": fw["name"] if fw else None,
            "framework_version": fw.get("version") if fw else None,
            "conventions": conv,
            "dependencies": dep_list,
        }

    def _read_existing(self, root: Path, rel: str) -> str | None:
        policy = PermissionPolicy(self.svc.config.config.permission_level, root)
        if policy.check_read(rel).denied:
            return None
        excl: ExclusionPolicy = self.svc.exclusion_policy()
        if excl.check_path(rel).excluded:
            return None
        real, _ = policy.resolve_inside(rel)
        if real is None or not real.is_file():
            return None
        data = real.read_bytes()[:MAX_EXISTING_BYTES]
        if excl.check_content(data).excluded:
            return None
        return data.decode("utf-8", errors="replace")

    def _patterns(self, ids: list[int]) -> list[PatternRef]:
        refs = []
        for pid in ids:
            p = self.svc.patterns.get(pid)
            c = p.get("content", {})
            refs.append(
                PatternRef(
                    p["id"],
                    p["title"],
                    p["status"],
                    p["confidence"],
                    p["language"],
                    p["framework"],
                    p["framework_version"],
                    c.get("files", []),
                    c.get("steps", []),
                    c.get("dependencies", []),
                )
            )
        return refs

    def _llm(self, backend: str, req: GenerationRequest) -> LLM:
        cfg = self.svc.config.config
        if backend == "local":
            return OllamaLLM.from_config(cfg.local_model, req.model)
        if not (req.cloud_key and cfg.cloud.openai_compatible_base_url and cfg.cloud.openai_compatible_model):
            raise GenerationError("cloud backend is not configured")
        return OpenAICompatibleLLM(
            cfg.cloud.openai_compatible_base_url, cfg.cloud.openai_compatible_model, req.cloud_key
        )

    # ------------------------------------------------------------------ main entry
    async def generate(self, req: GenerationRequest) -> dict[str, Any]:
        t0 = time.monotonic()
        svc = self.svc
        publish = svc.events.publish
        with svc.session_factory() as db:
            project = db.get(Project, req.project_id) if req.project_id else None
            if req.project_id and project is None:
                raise GenerationError("project not found")
            project_root = Path(project.root_path) if project else None
            if project is not None:
                db.expunge(project)
        resolution = await svc.modes.resolve(req.backend, bool(req.cloud_key), req.model)
        publish("generation.progress", {"stage": "context", "backends": resolution.backends})
        ctx = svc.context.analyze(req.request, req.project_id, req.top_k + 3)
        if req.pattern_ids is not None:
            chosen_ids = list(dict.fromkeys(req.pattern_ids))
        else:
            chosen_ids = [
                h.pattern_id for h in (ctx.search.hits if ctx.search else []) if h.pattern["status"] != "deprecated"
            ][: req.top_k]
        refs = self._patterns(chosen_ids)
        for ref in refs:
            hit = next((h for h in (ctx.search.hits if ctx.search else []) if h.pattern_id == ref.id), None)
            if hit:
                ref.warnings = list(hit.warnings)
        mode = (
            req.mode
            if req.mode in ("new", "adapt")
            else ("adapt" if refs and ctx.intent in ("adapt", "replay") else "new")
        )
        existing: dict[str, str] = {}
        if project_root is not None:
            wanted = list(ctx.signals.get("paths", []))
            if mode == "adapt":
                wanted += [f["path"] for r in refs for f in r.files if f.get("role") != "test"]
            for rel in dict.fromkeys(wanted):
                text = self._read_existing(project_root, rel)
                if text is not None:
                    existing[rel] = text
        cfg = svc.config.config
        messages, stats = build_messages(
            request=req.request,
            mode=mode,
            project=self._project_info(project) or ctx.project,
            patterns=refs,
            existing_files=existing,
            num_ctx=cfg.local_model.num_ctx,
        )
        publish("generation.progress", {"stage": "prompt", **stats})
        results: list[BackendResult] = []
        errors: list[str] = []
        for backend in resolution.backends:
            try:
                results.append(
                    await self._run_backend(backend, req, messages, project_root, existing, chosen_ids, stats)
                )
            except OllamaError as exc:
                errors.append(f"{backend}: {exc}")
        if not results:
            raise GenerationError("; ".join(errors) or "no backend produced a result")
        primary = results[0]
        for pid in chosen_ids:
            svc.patterns.record_use(pid, success=None)
        with svc.session_factory() as db:
            for r in results:
                impl = CodeImplementation(
                    project_id=req.project_id,
                    pattern_id=chosen_ids[0] if chosen_ids else None,
                    kind="adapted" if mode == "adapt" else "generated",
                    request=redact(req.request).text[:4000],
                    language=ctx.project.get("language"),
                    model=r.model,
                    status="proposed",
                    files=[
                        {"path": f.path, "action": f.action, "blob": svc.blobs.put(redact(f.content).text.encode())}
                        for f in r.files
                    ],
                )
                db.add(impl)
                db.flush()
                r.implementation_id = impl.id
            db.commit()
        svc.audit.record(
            "agent:codegen",
            "generation.complete",
            resource_type="implementation",
            resource_id=primary.implementation_id or "",
            detail={
                "backends": [r.backend for r in results],
                "model": primary.model,
                "files": len(primary.files),
                "patterns": chosen_ids,
                "mode": mode,
                "intent": ctx.intent,
            },
        )
        publish("generation.progress", {"stage": "done"})
        out = {
            "intent": ctx.intent,
            "mode": mode,
            "resolution": {
                "backends": resolution.backends,
                "reason": resolution.reason,
                "switched": resolution.switched,
                "warnings": resolution.warnings,
            },
            "context": ctx.to_dict(),
            "pattern_ids": chosen_ids,
            "prompt_stats": stats,
            "result": primary.to_dict(),
            "alternatives": [r.to_dict() for r in results[1:]],
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "errors": errors,
        }
        if mode == "adapt" and refs:
            out["adaptation"] = self._adaptation_summary(refs, primary)
        return out

    async def _run_backend(
        self,
        backend: str,
        req: GenerationRequest,
        messages: list[dict[str, str]],
        root: Path | None,
        existing: dict[str, str],
        pattern_ids: list[int],
        stats: dict[str, Any],
    ) -> BackendResult:
        svc = self.svc
        llm = self._llm(backend, req)
        if backend == "cloud":
            verdict = PermissionPolicy(
                svc.config.config.permission_level, root or Path.cwd()
            ).check_external_transmission(bool(svc.config.config.cloud.consent.get("openai-compatible")))
            if not verdict.allowed:
                raise GenerationError(verdict.reason)
            svc.audit.record(
                "agent:codegen",
                "generation.cloud.send",
                resource_type="cloud",
                outcome="allowed",
                detail={"model": llm.name, "prompt_chars": stats["prompt_chars"], "patterns": pattern_ids},
            )
        convo = list(messages)
        attempts, ptok, ctok, t0 = 0, 0, 0, time.monotonic()
        parsed, files, warnings = None, [], []
        cut_off = looped = False
        while True:
            attempts += 1
            svc.events.publish(
                "generation.progress", {"stage": "model", "backend": backend, "attempt": attempts, "model": llm.name}
            )
            res = await llm.chat(convo)
            ptok += res.prompt_tokens
            ctok += res.completion_tokens
            parsed = parse_model_output(res.content)
            files = [self._propose(f, root, existing) for f in parsed.files]
            problems = [i for f in files for i in f.issues if i["level"] == "error"]
            if not parsed.files:
                problems = [{"level": "error", "message": "no files were produced in the required <<<FILE>>> format"}]
            looped = res.done_reason == "runaway"
            if res.done_reason == "length":
                # The model hit the output cap. Asking again would burn the same budget for the same result, so
                # stop and say so instead of looping.
                cut_off = True
                break
            if not problems or attempts > req.max_repairs:
                break
            svc.events.publish(
                "generation.progress", {"stage": "repair", "backend": backend, "problems": len(problems)}
            )
            convo = [
                *convo,
                {"role": "assistant", "content": res.content[:20_000]},
                {
                    "role": "user",
                    "content": "Your answer has problems:\n"
                    + "\n".join(f"- {p.get('path', '')} {p['message']}" for p in problems[:12])
                    + "\nFix them and return ALL files again in the required format."
                    + (FORMAT_REMINDER if not parsed.files else ""),
                },
            ]
        warnings = list(parsed.warnings) if parsed else []
        all_issues = [i for f in files for i in f.issues]
        if looped and files:
            warnings.append(
                "The model started repeating itself after its files, so generation was stopped early and "
                "the repeated text was dropped."
            )
        if cut_off:
            limit = (getattr(llm, "options", None) or {}).get("num_predict")
            message = (
                "The model's answer was cut off at the output limit"
                + (f" ({limit} tokens)" if limit else "")
                + (", so some of it is missing. " if files else " before it finished the first file. ")
                + "Raise “Max answer length” in Settings → Local model, or ask for fewer files at a time."
            )
            warnings.append(message)
            all_issues.append({"level": "error" if not files else "warning", "message": message})
        if not files and not cut_off:
            all_issues.append({"level": "error", "message": "model produced no usable files"})
        return BackendResult(
            backend,
            llm.name,
            files,
            parsed.explanation if parsed else "",
            parsed.dependencies if parsed else [],
            all_issues,
            attempts,
            ptok,
            ctok,
            int((time.monotonic() - t0) * 1000),
            warnings=warnings,
        )

    def _propose(self, gf: Any, root: Path | None, existing: dict[str, str]) -> ProposedFile:
        before = existing.get(gf.path)
        if before is None and root is not None:
            before = self._read_existing(root, gf.path)
        if before is None:
            action, diff = "create", unified_diff(gf.path, "", gf.content)
        elif before == gf.content:
            action, diff = "identical", ""
        else:
            action, diff = "modify", unified_diff(gf.path, before, gf.content)
        issues = validate_file(gf.path, gf.content)
        if root is not None:
            verdict = PermissionPolicy(self.svc.config.config.permission_level, root).check_write(
                gf.path, creating=before is None
            )
            if verdict.denied and verdict.rule == "read-only":
                issues.append(
                    {
                        "level": "info",
                        "message": "read-only mode: this is a proposal; nothing will be written",
                        "path": gf.path,
                    }
                )
            elif verdict.denied:
                issues.append(
                    {"level": "error", "message": f"write would be denied: {verdict.reason}", "path": gf.path}
                )
            elif verdict.decision.value == "require_approval" and verdict.rule == "sensitive-file":
                issues.append(
                    {
                        "level": "warning",
                        "message": verdict.reason + " — applying will need explicit approval",
                        "path": gf.path,
                    }
                )
        return ProposedFile(gf.path, action, gf.content, diff, issues)

    @staticmethod
    def _adaptation_summary(refs: list[PatternRef], r: BackendResult) -> dict[str, Any]:
        base = {f["path"]: f.get("content", "") for ref in refs for f in ref.files}
        changed, same, new = [], [], []
        for f in r.files:
            if f.path not in base:
                new.append(f.path)
            elif base[f.path].strip() == f.content.strip():
                same.append(f.path)
            else:
                changed.append(
                    {"path": f.path, "diff_vs_reference": unified_diff(f.path, base[f.path], f.content)[:20_000]}
                )
        return {
            "reference_patterns": [ref.id for ref in refs],
            "adapted": changed,
            "unchanged_copies": same,
            "new_files": new,
        }
