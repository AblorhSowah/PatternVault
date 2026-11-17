"""The development agents: Learning, Extraction, Knowledge, Retrieval, Code Generation, Replay, Review/Security, Debugging, Documentation."""

from __future__ import annotations

import difflib
import hashlib
import re
import secrets
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.agents.base import Agent, AgentContext, AgentResult
from app.extraction.extractor import FileInput, SessionInput, extract_candidates
from app.generation.generator import GenerationError, GenerationRequest
from app.generation.llm import OllamaLLM
from app.generation.ollama import OllamaError
from app.generation.parser import parse_model_output
from app.generation.prompts import SYSTEM_PROMPT
from app.generation.validate import has_errors, validate_file
from app.knowledge.lifecycle import LifecycleError
from app.models import DebuggingSolution, DevSession, ErrorRecord, Project
from app.security.permissions import Decision, PermissionPolicy
from app.security.scanner import SEVERITY_ORDER, scan_text
from app.security.untrusted import scan_untrusted, wrap_untrusted
from app.services.modes import ModeViolation, NoBackendAvailable


def proposal_files(r: AgentResult | None) -> list[dict[str, Any]]:
    """Extract the file proposal produced by a generation/replay/debug step."""
    if r is None:
        return []
    return list((r.output.get("proposal") or {}).get("files", []))


# --------------------------------------------------------------------------------------- A. learning
class LearningObservationAgent(Agent):
    key = "learning-observation"
    name = "Learning & Observation Agent"
    description = "Finds new assistant sessions in supported local logs, imports them and records outcomes. Never reads hidden reasoning."
    permissions = ("read_provider_logs", "write_knowledge")

    async def run(self, ctx: AgentContext, payload: dict[str, Any]) -> AgentResult:
        svc, root = ctx.svc, ctx.project_root
        action = payload.get("action", "scan")
        if ctx.project_id is None or root is None:
            return AgentResult("failed", "a project is required")
        cfg = svc.config.config
        found: list[dict[str, Any]] = []
        for a in svc.adapters.all():
            if (
                not a.capabilities.captures_prompts
                or a.key == "manual"
                or (a.key in cfg.providers and not cfg.providers[a.key].enabled)
            ):
                continue
            try:
                refs = a.discover(root)
            except Exception as exc:
                found.append({"provider": a.key, "error": str(exc)})
                continue
            with svc.session_factory() as db:
                known = {
                    s
                    for (s,) in db.execute(
                        select(DevSession.external_id).where(DevSession.project_id == ctx.project_id)
                    ).all()
                }
            found.extend({"provider": a.key, **r.to_dict(), "imported": r.external_id in known} for r in refs)
        new = [f for f in found if not f.get("imported") and "error" not in f]
        if action == "scan":
            return AgentResult("success", f"{len(new)} new session(s) available", {"sessions": found, "new": len(new)})
        if not cfg.privacy.capture_enabled:
            return AgentResult("blocked", "capture is disabled in privacy settings")
        imported: list[int] = []
        patterns: list[dict[str, Any]] = []
        for f in new:
            with svc.session_factory() as db:
                s = svc.capture.import_session(db, ctx.project_id, f["provider"], f["external_id"])
                db.commit()
                sid = s.id
            imported.append(sid)
            patterns.extend(r.__dict__ for r in svc.learning.extract_session(sid))
        return AgentResult(
            "success",
            f"imported {len(imported)} session(s), {len(patterns)} pattern result(s)",
            {"imported": imported, "patterns": patterns},
        )


# --------------------------------------------------------------------------------------- B. extraction
class PatternExtractionAgent(Agent):
    key = "pattern-extraction"
    name = "Pattern Extraction Agent"
    description = "Turns captured sessions or applied changes into categorised, deduplicated pattern candidates."
    permissions = ("read_knowledge", "write_knowledge")

    async def run(self, ctx: AgentContext, payload: dict[str, Any]) -> AgentResult:
        svc = ctx.svc
        if payload.get("session_id"):
            res = svc.learning.extract_session(int(payload["session_id"]))
            return AgentResult(
                "success", f"{len(res)} pattern(s) from session", {"patterns": [r.__dict__ for r in res]}
            )
        files = payload.get("files") or []
        if not files:
            return AgentResult("skipped", "nothing to extract")
        with svc.session_factory() as db:
            project = db.get(Project, ctx.project_id) if ctx.project_id else None
            lang = (project.conventions or {}).get("primary_language") if project else None
            fws = list(project.frameworks or []) if project else []
        cands = extract_candidates(
            SessionInput(
                prompts=[payload.get("request", ctx.request)],
                files=[FileInput(f["path"], f.get("action", "created"), f["content"]) for f in files],
                project_language=lang,
                project_frameworks=fws,
                provider="patternvault",
                session_id=None,
            )
        )
        results = [
            svc.patterns.add_candidate(
                c,
                project_id=ctx.project_id,
                session_id=None,
                status=payload.get("status", "generated"),
                actor=f"agent:{self.key}",
            )
            for c in cands
        ]
        return AgentResult(
            "success", f"{len(results)} pattern(s) extracted", {"patterns": [r.__dict__ for r in results]}
        )


# --------------------------------------------------------------------------------------- C. knowledge
class KnowledgeManagementAgent(Agent):
    key = "knowledge-management"
    name = "Knowledge Management Agent"
    description = "Maintains the repository: indexing, quality analysis, outcome recording and evidence-gated promotion to tested/verified."
    permissions = ("read_knowledge", "write_knowledge")

    async def run(self, ctx: AgentContext, payload: dict[str, Any]) -> AgentResult:
        svc, action = ctx.svc, payload.get("action", "quality")
        if action == "reindex":
            return AgentResult("success", "reindexed", svc.indexer.reindex_all())
        if action == "quality":
            rep = svc.quality.analyze(apply_relations=bool(payload.get("apply", True)))
            return AgentResult("success", f"{len(rep['recommendations'])} recommendation(s)", rep)
        if action == "record_outcome":
            for pid in payload.get("pattern_ids", []):
                svc.patterns.record_use(int(pid), success=bool(payload["success"]))
            return AgentResult("success", "outcome recorded", {"patterns": payload.get("pattern_ids", [])})
        if action == "promote":
            out, errs = [], []
            for pid in payload.get("pattern_ids", []):
                for target in payload.get("targets", ["tested"]):
                    try:
                        out.append(
                            svc.patterns.promote(
                                int(pid), target, int(payload["testing_session_id"]), actor=f"agent:{self.key}"
                            )
                        )
                    except LifecycleError as exc:
                        errs.append({"pattern_id": pid, "target": target, "reason": str(exc)})
            return AgentResult(
                "success" if out or not errs else "failed",
                f"{len(out)} promotion(s), {len(errs)} refused",
                {"promoted": [(p["id"], p["status"]) for p in out], "refused": errs},
            )
        return AgentResult("failed", f"unknown action '{action}'")


# --------------------------------------------------------------------------------------- D. retrieval
class ContextRetrievalAgent(Agent):
    key = "context-retrieval"
    name = "Context & Retrieval Agent"
    description = (
        "Analyses the project and request, retrieves ranked knowledge and reports missing context before generation."
    )
    permissions = ("read_knowledge", "read_project")

    async def run(self, ctx: AgentContext, payload: dict[str, Any]) -> AgentResult:
        rep = ctx.svc.context.analyze(
            payload.get("request", ctx.request), ctx.project_id, int(payload.get("top_k", 8))
        ).to_dict()
        blockers = [m for m in rep["missing"] if m["severity"] == "blocker"]
        return AgentResult(
            "blocked" if blockers else "success",
            f"intent {rep['intent']}, {len(rep['hits'])} relevant pattern(s), {len(rep['missing'])} gap(s)",
            {"context": rep},
            [
                {"level": "error" if m["severity"] == "blocker" else "warning", "message": m["message"]}
                for m in rep["missing"]
            ],
        )


# --------------------------------------------------------------------------------------- E. codegen
class CodeGenerationAgent(Agent):
    key = "code-generation"
    name = "Code Generation Agent"
    description = "Generates or adapts code with the local model (RAG over stored patterns), validating and self-repairing before proposing."
    permissions = ("read_project", "read_knowledge", "call_local_model")

    async def run(self, ctx: AgentContext, payload: dict[str, Any]) -> AgentResult:
        req = GenerationRequest(
            payload.get("request", ctx.request),
            ctx.project_id,
            payload.get("mode", "auto"),
            payload.get("pattern_ids"),
            payload.get("model"),
            payload.get("backend", "auto"),
            payload.get("cloud_key"),
            int(payload.get("max_repairs", 2)),
            int(payload.get("top_k", 3)),
        )
        try:
            out = await ctx.svc.generator.generate(req)
        except (ModeViolation, NoBackendAvailable, GenerationError, OllamaError) as exc:
            return AgentResult("failed", str(exc), {"error": str(exc)}, [{"level": "error", "message": str(exc)}])
        res = out["result"]
        files = [
            {"path": f["path"], "action": f["action"], "content": f["content"]}
            for f in res["files"]
            if f["action"] != "identical"
        ]
        errors = [i for i in res["issues"] if i["level"] == "error"]
        status = "failed" if (errors or not files) else "success"
        return AgentResult(
            status,
            f"{len(files)} file(s) proposed by {res['model']} in {res['attempts']} attempt(s)",
            {
                "proposal": {"files": files, "explanation": res["explanation"], "dependencies": res["dependencies"]},
                "generation": {k: out[k] for k in ("intent", "mode", "pattern_ids", "resolution")},
                "implementation_id": res["implementation_id"],
            },
            res["issues"],
        )


# --------------------------------------------------------------------------------------- F. replay
class PatternReplayAgent(Agent):
    key = "pattern-replay"
    name = "Pattern Replay Agent"
    description = "Reconstructs a stored implementation inside the current project (no model needed): files, dependencies, compatibility checks."
    permissions = ("read_project", "read_knowledge")

    async def run(self, ctx: AgentContext, payload: dict[str, Any]) -> AgentResult:
        if ctx.project_id is None:
            return AgentResult("failed", "a project is required")
        pid = payload.get("pattern_id") or (
            ctx.results.get("context")
            and (ctx.results["context"].output["context"]["hits"] or [{}])[0].get("pattern_id")
        )
        if not pid:
            return AgentResult(
                "blocked",
                "no pattern to replay was identified",
                {},
                [{"level": "error", "message": "name the implementation to recreate"}],
            )
        plan = ctx.svc.replayer.plan(
            int(pid),
            ctx.project_id,
            path_map=payload.get("path_map"),
            include_tests=payload.get("include_tests", True),
            selected_paths=payload.get("selected_paths"),
        )
        files = [
            {"path": f.target_path, "action": "create" if f.action == "create" else "modify", "content": f.content}
            for f in plan.files
            if f.action in ("create", "conflict")
        ]
        issues = [{"level": "warning", "message": w} for w in plan.warnings] + [
            {"level": "error", "message": f"{f.target_path}: {f.reason}"} for f in plan.files if f.action == "blocked"
        ]
        if not plan.environment["compatible"]:
            return AgentResult(
                "blocked", "pattern is not compatible with this project", {"plan": plan.to_dict()}, issues
            )
        if plan.unresolved_placeholders:
            issues.append(
                {
                    "level": "error",
                    "message": "redacted secrets must be provided before applying: "
                    + ", ".join(sorted({u["kind"] for u in plan.unresolved_placeholders})),
                }
            )
            return AgentResult(
                "blocked", "replay needs secret values", {"plan": plan.to_dict(), "pattern_id": int(pid)}, issues
            )
        return AgentResult(
            "success",
            f"{len(files)} file(s) to write ({len(plan.files) - len(files)} already identical)",
            {
                "proposal": {"files": files, "dependencies": plan.dependencies["missing"]},
                "plan": plan.to_dict(),
                "pattern_id": int(pid),
            },
            issues,
        )


# --------------------------------------------------------------------------------------- H. review & security
class CodeReviewSecurityAgent(Agent):
    key = "code-review-security"
    name = "Code Review & Security Agent"
    description = "Reviews proposed changes: syntax, secrets, injection/XSS/crypto/TLS rules, permission rules, risky dependencies, injected instructions."
    permissions = ("read_project",)

    async def run(self, ctx: AgentContext, payload: dict[str, Any]) -> AgentResult:
        files = payload.get("files") or next(
            (proposal_files(r) for r in reversed(list(ctx.results.values())) if proposal_files(r)), []
        )
        if not files:
            return AgentResult("skipped", "nothing to review")
        issues: list[dict[str, Any]] = []
        findings: list[dict[str, Any]] = []
        policy = ctx.policy
        for f in files:
            path, text = f["path"], f.get("content", "")
            for i in validate_file(path, text):
                issues.append(i)
            findings.extend(scan_text(path, text))
            for flag in scan_untrusted(text):
                if flag.kind in (
                    "ignore-instructions",
                    "exfiltration",
                    "pipe-to-shell",
                    "tool-call-spoof",
                    "agent-directive",
                    "hidden-characters",
                ):
                    findings.append(
                        {
                            "rule": "PV-EMBEDDED-INSTRUCTION",
                            "severity": "high",
                            "cwe": "CWE-74",
                            "title": f"text resembling an instruction to an AI/agent ({flag.kind})",
                            "fix": "Remove it.",
                            "path": path,
                            "line": flag.line,
                            "snippet": flag.excerpt,
                        }
                    )
            if policy is not None:
                v = policy.check_write(path)
                if v.decision is Decision.DENY and v.rule == "read-only":
                    issues.append({"level": "info", "message": f"{path}: read-only mode, proposal only", "path": path})
                elif v.decision is Decision.DENY:
                    issues.append(
                        {"level": "error", "message": f"{path}: write is not permitted ({v.reason})", "path": path}
                    )
                elif v.rule == "sensitive-file":
                    issues.append(
                        {
                            "level": "warning",
                            "message": f"{path}: sensitive file — applying requires approval",
                            "path": path,
                        }
                    )
        for d in payload.get("dependencies") or []:
            name, spec = str(d.get("name", "")), str(d.get("version", ""))
            if spec.strip() in ("*", "latest") or re.match(r"(git\+|https?://|file:)", spec):
                issues.append(
                    {"level": "warning", "message": f"dependency {name} is unpinned or installed from a URL ({spec})"}
                )
        for f in findings:
            issues.append(
                {
                    "level": "error" if SEVERITY_ORDER[f["severity"]] >= 3 else "warning",
                    "message": f"[{f['rule']}/{f['cwe']}] {f['title']} — {f['fix']}",
                    "path": f["path"],
                    "line": f["line"],
                }
            )
        errors = [i for i in issues if i["level"] == "error"]
        return AgentResult(
            "failed" if errors else "success",
            f"{len(files)} file(s) reviewed: {len(errors)} blocking, {len(issues) - len(errors)} advisory",
            {
                "findings": findings,
                "max_severity": max((f["severity"] for f in findings), key=lambda s: SEVERITY_ORDER[s], default="none"),
            },
            issues,
        )


# --------------------------------------------------------------------------------------- G. debugging
_PY_FRAME = re.compile(r'File "([^"]+)", line (\d+)')
_JS_FRAME = re.compile(r"\(?([^\s()]+\.(?:ts|tsx|js|mjs|php)):(\d+)(?::\d+)?\)?")
_MISSING_MOD = re.compile(r"No module named '([\w.]+)'")
_STDLIB = {
    "os",
    "sys",
    "re",
    "json",
    "math",
    "typing",
    "datetime",
    "pathlib",
    "collections",
    "itertools",
    "functools",
    "asyncio",
    "subprocess",
    "logging",
}


def normalise_error(text: str) -> str:
    t = re.sub(r"0x[0-9a-fA-F]+", "0x?", text)
    t = re.sub(r"(/[\w.\-@]+)+", "<path>", t)
    t = re.sub(r"\d+", "N", t)
    return re.sub(r"\s+", " ", t).strip()[:400]


def error_signature(failure: dict[str, Any]) -> str:
    head = (
        (failure.get("actual") or "") + " " + (failure.get("error_details") or "").strip().splitlines()[-1:][0]
        if failure.get("error_details")
        else failure.get("actual", "")
    )
    return hashlib.sha256(normalise_error(head).encode()).hexdigest()


class DebuggingAgent(Agent):
    key = "debugging"
    name = "Debugging Agent"
    description = "Analyses failures, reuses verified solutions, applies deterministic fixes, then asks the local model for a correction."
    permissions = ("read_project", "read_knowledge", "call_local_model")

    async def run(self, ctx: AgentContext, payload: dict[str, Any]) -> AgentResult:
        svc, root = ctx.svc, ctx.require_root()
        failures = [f for f in payload.get("failures", []) if f.get("status") in ("failed", "error")][:5]
        if not failures:
            return AgentResult("skipped", "no failures to debug")
        err_ids, sigs = self._record(ctx, failures)
        files = self._implicated(root, failures, payload.get("changed_files", []))
        # 1. known verified solution
        for sig in sigs:
            fix = self._known_fix(ctx, sig, files)
            if fix:
                return AgentResult(
                    "success",
                    "applied a previously verified solution",
                    {"proposal": {"files": fix}, "source": "known-solution", "error_ids": err_ids, "signature": sig},
                )
        # 2. deterministic: missing python module -> requirements entry
        det = self._missing_module_fix(root, failures)
        if det:
            return AgentResult(
                "success",
                "missing dependency: add it to requirements.txt",
                {"proposal": {"files": det}, "source": "heuristic", "error_ids": err_ids, "signature": sigs[0]},
            )
        # 3. local model
        try:
            llm = OllamaLLM.from_config(svc.config.config.local_model, payload.get("model"))
            res = await llm.chat(self._messages(failures, files))
        except (OllamaError, NoBackendAvailable) as exc:
            return AgentResult(
                "failed",
                f"no known fix and the local model is unavailable: {exc}",
                {"error_ids": err_ids},
                [{"level": "error", "message": str(exc)}],
            )
        parsed = parse_model_output(res.content)
        out_files = []
        for f in parsed.files:
            issues = validate_file(f.path, f.content)
            if not has_errors(issues):
                out_files.append(
                    {"path": f.path, "action": "modify" if (root / f.path).exists() else "create", "content": f.content}
                )
        if not out_files:
            return AgentResult(
                "failed",
                "the model did not produce a usable correction",
                {"error_ids": err_ids, "raw": res.content[:2000]},
            )
        return AgentResult(
            "success",
            f"correction proposed by {res.model}",
            {
                "proposal": {"files": out_files, "explanation": parsed.explanation},
                "source": "llm",
                "root_cause": parsed.explanation[:600],
                "error_ids": err_ids,
                "signature": sigs[0],
            },
        )

    def _record(self, ctx: AgentContext, failures: list[dict[str, Any]]) -> tuple[list[int], list[str]]:
        ids, sigs = [], []
        with ctx.svc.session_factory() as db:
            for f in failures:
                sig = error_signature(f)
                m = _PY_FRAME.findall(f.get("error_details", "")) or _JS_FRAME.findall(f.get("error_details", ""))
                path, line = (m[-1][0], int(m[-1][1])) if m else (f.get("file_path"), None)
                e = ErrorRecord(
                    project_id=ctx.project_id,
                    task_id=ctx.task_id,
                    source="test",
                    message=(f.get("actual") or f["name"])[:2000],
                    stack=f.get("error_details", "")[:8000],
                    signature=sig,
                    file_path=path,
                    line=line,
                )
                db.add(e)
                db.flush()
                ids.append(e.id)
                sigs.append(sig)
            db.commit()
        return ids, sigs

    def _implicated(self, root: Path, failures: list[dict[str, Any]], changed: list[str]) -> dict[str, str]:
        found: dict[str, str] = {}
        paths = list(changed)
        for f in failures:
            for p, _ in _PY_FRAME.findall(f.get("error_details", "")) + _JS_FRAME.findall(f.get("error_details", "")):
                try:
                    paths.append(str(Path(p).resolve().relative_to(root.resolve())) if Path(p).is_absolute() else p)
                except ValueError:
                    continue
            if f.get("file_path"):
                paths.append(f["file_path"])
        for rel in dict.fromkeys(paths):
            fp = root / rel
            if fp.is_file() and "node_modules" not in fp.parts and ".venv" not in fp.parts:
                found[rel] = fp.read_text(encoding="utf-8", errors="replace")[:20_000]
        return found

    def _known_fix(self, ctx: AgentContext, sig: str, files: dict[str, str]) -> list[dict[str, Any]] | None:
        with ctx.svc.session_factory() as db:
            sols = list(
                db.execute(
                    select(DebuggingSolution)
                    .where(DebuggingSolution.signature == sig, DebuggingSolution.verified.is_(True))
                    .order_by(DebuggingSolution.id.desc())
                ).scalars()
            )
            for s in sols:
                out, ok = [], True
                for fx in s.fix.get("files", []):
                    text = files.get(fx["path"]) or (
                        (ctx.require_root() / fx["path"]).read_text(encoding="utf-8", errors="replace")
                        if (ctx.require_root() / fx["path"]).is_file()
                        else None
                    )
                    if text is None:
                        ok = False
                        break
                    for e in fx["edits"]:
                        if text.count(e["old"]) != 1:
                            ok = False
                            break
                        text = text.replace(e["old"], e["new"])
                    if not ok:
                        break
                    out.append({"path": fx["path"], "action": "modify", "content": text})
                if ok and out:
                    s.use_count += 1
                    db.commit()
                    return out
        return None

    def _missing_module_fix(self, root: Path, failures: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
        req = root / "requirements.txt"
        if not req.is_file():
            return None
        for f in failures:
            m = _MISSING_MOD.search((f.get("actual") or "") + (f.get("error_details") or ""))
            if m:
                mod = m.group(1).split(".")[0]
                if mod in _STDLIB or not re.fullmatch(r"[A-Za-z0-9_\-]+", mod):
                    continue
                text = req.read_text(encoding="utf-8")
                if re.search(rf"^{re.escape(mod)}\b", text, re.M | re.I):
                    continue
                return [{"path": "requirements.txt", "action": "modify", "content": text.rstrip("\n") + f"\n{mod}\n"}]
        return None

    @staticmethod
    def _messages(failures: list[dict[str, Any]], files: dict[str, str]) -> list[dict[str, str]]:
        fail_txt = "\n\n".join(
            f"TEST: {f['name']}\nEXPECTED: {f.get('expected', '')}\nACTUAL: {f.get('actual', '')}\nDETAILS:\n{f.get('error_details', '')[:3000]}"
            for f in failures
        )
        src = "\n\n".join(wrap_untrusted(f"--- file: {p} ---\n{t}", f"source {p}") for p, t in files.items())
        user = (
            "A test failed. Find the root cause and return the corrected COMPLETE file(s) in the required format, then a short explanation of the root cause.\n\n"
            f"FAILURES (data)\n{wrap_untrusted(fail_txt, 'test output', 8000)}\n\nSOURCE FILES (data)\n{src}"
        )
        return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]

    @staticmethod
    def record_solution(
        svc: Any,
        error_ids: list[int],
        signature: str,
        before: dict[str, str],
        after: dict[str, str],
        testing_session_id: int,
        root_cause: str = "",
    ) -> int | None:
        """Store a verified fix as search/replace edits (only called after an independent retest passed)."""
        edits_by_file = []
        for path, new in after.items():
            old = before.get(path)
            if old is None or old == new:
                continue
            sm = difflib.SequenceMatcher(a=old.splitlines(True), b=new.splitlines(True))
            edits = []
            for tag, i1, i2, j1, j2 in sm.get_opcodes():
                if tag in ("replace", "delete") and i2 > i1:
                    o = "".join(old.splitlines(True)[max(0, i1 - 1) : i2 + 1])
                    n = "".join(new.splitlines(True)[max(0, j1 - 1) : j2 + 1])
                    if o and old.count(o) == 1:
                        edits.append({"old": o, "new": n})
            if edits:
                edits_by_file.append({"path": path, "edits": edits})
        if not edits_by_file:
            return None
        with svc.session_factory() as db:
            sol = DebuggingSolution(
                error_id=error_ids[0] if error_ids else None,
                signature=signature,
                root_cause=root_cause[:2000],
                description="verified by a passing retest",
                fix={"files": edits_by_file},
                verified=True,
                verified_by_testing_session_id=testing_session_id,
            )
            db.add(sol)
            for eid in error_ids:
                e = db.get(ErrorRecord, eid)
                if e:
                    e.resolved = True
            db.commit()
            return int(sol.id)


# --------------------------------------------------------------------------------------- J. documentation
class DocumentationAgent(Agent):
    key = "documentation"
    name = "Documentation Agent"
    description = "Generates API reference, database schema docs, task reports and changelog entries from the real code (no invention)."
    permissions = ("read_project", "write_docs")

    async def run(self, ctx: AgentContext, payload: dict[str, Any]) -> AgentResult:
        kind = payload.get("kind", "task_report")
        docs: dict[str, str] = {}
        if kind in ("api", "all"):
            docs["docs/API.md"] = self.api_markdown(ctx.svc)
        if kind in ("database", "all"):
            docs["docs/DATABASE_SCHEMA.md"] = self.database_markdown()
        if kind in ("task_report", "all") and ctx.task_id:
            docs[f"docs/tasks/task-{ctx.task_id}.md"] = self.task_markdown(ctx)
        if kind == "changelog":
            entry = f"- {payload.get('summary', ctx.request)[:200]} (task #{ctx.task_id})"
            root = ctx.require_root()
            cl = root / "CHANGELOG.md"
            text = cl.read_text(encoding="utf-8") if cl.is_file() else "# Changelog\n\n## [Unreleased]\n"
            text = (
                text.replace("## [Unreleased]\n", f"## [Unreleased]\n\n{entry}\n", 1)
                if "## [Unreleased]" in text
                else text.rstrip() + f"\n\n## [Unreleased]\n\n{entry}\n"
            )
            docs["CHANGELOG.md"] = text
        if not docs:
            return AgentResult("skipped", "nothing to document")
        files = [
            {
                "path": p,
                "action": "modify" if ctx.project_root and (ctx.project_root / p).exists() else "create",
                "content": c,
            }
            for p, c in docs.items()
        ]
        return AgentResult(
            "success", f"{len(files)} document(s) generated", {"proposal": {"files": files}, "documents": list(docs)}
        )

    @staticmethod
    def api_markdown(svc: Any) -> str:
        from app.main import create_app

        spec = create_app(services=svc, token=secrets.token_urlsafe(16)).openapi()  # the schema is built, never served
        lines = [
            f"# {spec['info']['title']} API reference",
            "",
            f"Version {spec['info']['version']}. All routes except `/health` require `Authorization: Bearer <token>`.",
            "",
        ]
        for path, ops in sorted(spec["paths"].items()):
            for method, op in sorted(ops.items()):
                lines.append(f"## `{method.upper()} {path}`")
                if op.get("summary"):
                    lines.append(f"{op['summary']}")
                params = [
                    f"`{p['name']}` ({p['in']}{', required' if p.get('required') else ''})"
                    for p in op.get("parameters", [])
                ]
                if params:
                    lines.append("Parameters: " + ", ".join(params))
                if op.get("requestBody"):
                    ref = (
                        op["requestBody"]
                        .get("content", {})
                        .get("application/json", {})
                        .get("schema", {})
                        .get("$ref", "")
                    )
                    lines.append(f"Request body: `{ref.split('/')[-1] or 'JSON'}`")
                lines.append("Responses: " + ", ".join(f"`{c}`" for c in op.get("responses", {})))
                lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    @staticmethod
    def database_markdown() -> str:
        from app.models import Base

        lines = ["# Database schema", "", f"{len(Base.metadata.tables)} tables (SQLite, managed by Alembic).", ""]
        for t in sorted(Base.metadata.tables.values(), key=lambda x: x.name):
            lines += [f"## `{t.name}`", "", "| Column | Type | Null | Key |", "|---|---|---|---|"]
            for c in t.columns:
                key = (
                    "PK"
                    if c.primary_key
                    else (f"FK → {next(iter(c.foreign_keys)).target_fullname}" if c.foreign_keys else "")
                )
                lines.append(f"| {c.name} | {c.type} | {'yes' if c.nullable else 'no'} | {key} |")
            idx = [f"`{i.name}` ({', '.join(c.name for c in i.columns)})" for i in t.indexes]
            if idx:
                lines.append(f"\nIndexes: {'; '.join(idx)}")
            lines.append("")
        return "\n".join(lines)

    @staticmethod
    def task_markdown(ctx: AgentContext) -> str:
        lines = [f"# Task {ctx.task_id}", "", f"**Request:** {ctx.request}", "", "## Steps", ""]
        for k, r in ctx.results.items():
            lines.append(f"- **{k}** — {r.status}: {r.summary}")
        return "\n".join(lines) + "\n"


def mk_policy(level: Any, root: Path) -> PermissionPolicy:
    return PermissionPolicy(level, root)
