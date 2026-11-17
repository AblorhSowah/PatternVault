"""The Orchestrator: plans requests, coordinates agents, serialises writes, runs the verification gate and correction loop."""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.agents.base import AgentContext, AgentResult
from app.agents.dev_agents import DebuggingAgent, proposal_files
from app.config import PermissionLevel
from app.execution.file_ops import ChangeDenied, ChangeSpec
from app.models import AgentDefinition, AgentExecution, CodeImplementation, Project, Task
from app.models.base import utcnow
from app.orchestration import planner
from app.orchestration.approvals import ApprovalDenied, ApprovalTimeout
from app.orchestration.locks import FileLockManager
from app.security.permissions import PermissionPolicy
from app.testing.report import annotate_correction
from app.testing.workspace import TargetWorkspace

SUITE_AGENTS = (
    "unit-testing",
    "integration-testing",
    "e2e-testing",
    "api-testing",
    "extension-testing",
)  # evidence providers
TERMINAL = {"completed", "verified", "failed", "cancelled"}


@dataclass
class TaskRequest:
    kind: str
    request: str
    project_id: int | None = None
    params: dict[str, Any] = field(default_factory=dict)


class Orchestrator:
    def __init__(self, svc: Any) -> None:
        self.svc = svc
        self.locks = FileLockManager()
        self._running: dict[int, asyncio.Task[None]] = {}

    # ------------------------------------------------------------------ public API
    async def submit(self, req: TaskRequest) -> int:
        svc = self.svc
        if req.kind not in planner.TASK_KINDS:
            raise ValueError(f"unknown task kind '{req.kind}'")
        root: Path | None = None
        if req.project_id is not None:
            with svc.session_factory() as db:
                p = db.get(Project, req.project_id)
                if p is None:
                    raise LookupError("project not found")
                root = Path(p.root_path)
        elif req.kind != "document":
            raise ValueError("this task kind needs a project")
        ws = TargetWorkspace.detect(root) if root else None
        steps = planner.plan(req.kind, req.params, ws)
        cfg = svc.config.config
        with svc.session_factory() as db:
            t = Task(
                project_id=req.project_id,
                kind=req.kind,
                request=req.request[:8000],
                status="pending",
                mode=cfg.mode.value,
                permission_level=cfg.permission_level.value,
                plan=[s.to_plan() for s in steps],
            )
            db.add(t)
            db.commit()
            tid = t.id
        svc.audit.record(
            "user",
            "task.submit",
            resource_type="task",
            resource_id=tid,
            permission_level=cfg.permission_level.value,
            detail={"kind": req.kind, "project_id": req.project_id},
        )
        svc.events.publish("task.created", {"id": tid, "kind": req.kind, "request": req.request[:140]})
        self._running[tid] = asyncio.create_task(self._run(tid, req, steps, root))
        return tid

    async def wait(self, task_id: int, timeout: float | None = None) -> dict[str, Any]:
        t = self._running.get(task_id)
        if t is not None:
            await asyncio.wait_for(asyncio.shield(t), timeout)
        return self.get(task_id)

    def cancel(self, task_id: int) -> bool:
        t = self._running.get(task_id)
        if t is None or t.done():
            return False
        t.cancel()
        return True

    def get(self, task_id: int) -> dict[str, Any]:
        with self.svc.session_factory() as db:
            t = db.get(Task, task_id)
            if t is None:
                raise LookupError("task not found")
            execs = (
                db.execute(select(AgentExecution).where(AgentExecution.task_id == task_id).order_by(AgentExecution.id))
                .scalars()
                .all()
            )
            return {
                "id": t.id,
                "kind": t.kind,
                "request": t.request,
                "status": t.status,
                "mode": t.mode,
                "permission_level": t.permission_level,
                "project_id": t.project_id,
                "plan": t.plan,
                "result": t.result,
                "error": t.error,
                "verified": t.verified,
                "created_at": t.created_at.isoformat(),
                "started_at": t.started_at.isoformat() if t.started_at else None,
                "finished_at": t.finished_at.isoformat() if t.finished_at else None,
                "executions": [
                    {
                        "id": e.id,
                        "agent": e.agent_key,
                        "step": e.step_key,
                        "attempt": e.attempt,
                        "status": e.status,
                        "error": e.error,
                        "duration_ms": e.duration_ms,
                        "summary": (e.output or {}).get("summary", ""),
                    }
                    for e in execs
                ],
            }

    def list_tasks(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.svc.session_factory() as db:
            rows = db.execute(select(Task).order_by(Task.id.desc()).limit(limit)).scalars().all()
            return [
                {
                    "id": t.id,
                    "kind": t.kind,
                    "request": t.request[:200],
                    "status": t.status,
                    "verified": t.verified,
                    "project_id": t.project_id,
                    "created_at": t.created_at.isoformat(),
                    "finished_at": t.finished_at.isoformat() if t.finished_at else None,
                }
                for t in rows
            ]

    def report_markdown(self, task_id: int) -> str:
        t = self.get(task_id)
        r = t["result"]
        lines = [
            f"# Task {t['id']} — {t['kind']}",
            "",
            f"**Request:** {t['request']}",
            f"**Status:** {t['status']} · **Verified:** {'yes' if t['verified'] else 'no'}",
            f"**Mode:** {t['mode']} · **Permission level:** {t['permission_level']}",
            "",
            "## Execution history",
            "",
        ]
        lines += [
            f"{i}. `{e['step']}` ({e['agent']}, attempt {e['attempt']}): {e['status']} — {e['summary']}"
            for i, e in enumerate(t["executions"], 1)
        ]
        if r.get("files_applied"):
            lines += ["", "## Files changed", "", *[f"- {p}" for p in r["files_applied"]]]
        if r.get("recovery_point_id"):
            lines += [
                "",
                f"Recovery point: #{r['recovery_point_id']}" + (" (rolled back)" if r.get("rolled_back") else ""),
            ]
        if r.get("gate"):
            g = r["gate"]
            lines += [
                "",
                "## Verification",
                "",
                f"- executed and passed: {', '.join(g['passed']) or 'none'}",
                f"- failed: {', '.join(g['failed']) or 'none'}",
                f"- skipped: {', '.join(g['skipped']) or 'none'}",
            ]
        if t["error"]:
            lines += ["", f"## Problem\n\n{t['error']}"]
        return "\n".join(lines) + "\n"

    # ------------------------------------------------------------------ execution
    async def _run(self, tid: int, req: TaskRequest, steps: list[planner.StepSpec], root: Path | None) -> None:
        svc = self.svc
        cfg = svc.config.config
        level = cfg.permission_level
        policy = PermissionPolicy(level, root) if root else None
        ctx = AgentContext(svc, tid, req.project_id, root, policy, req.request, dict(req.params))
        self._set(tid, status="running", started_at=utcnow())
        svc.events.publish("task.started", {"id": tid})
        state: dict[str, Any] = {
            "files_applied": [],
            "recovery_point_id": None,
            "rolled_back": False,
            "fix_attempts": 0,
            "warnings": [],
        }
        try:
            if (
                level is PermissionLevel.APPROVED_EXECUTION
                and root is not None
                and req.kind in ("generate", "replay", "debug")
            ):
                self._set(tid, status="awaiting_approval")
                aid = svc.approvals.request(
                    tid,
                    "approve_plan",
                    f"Approve plan for {req.kind}: {req.request[:150]}",
                    {"steps": [s.key for s in steps]},
                    requested_by="agent:orchestrator",
                )
                await svc.approvals.wait(aid)
                assert policy is not None
                policy.plan_approved = True
                self._set(tid, status="running")
            lock = (
                self.locks.project(req.project_id)
                if req.project_id is not None and any(s.kind in ("apply", "verify") for s in steps)
                else contextlib.nullcontext()
            )
            async with lock:
                await self._execute(tid, req, steps, ctx, state)
            self._finish(tid, ctx, state)
        except asyncio.CancelledError:
            self._set(tid, status="cancelled", finished_at=utcnow(), error="cancelled by user")
            svc.events.publish("task.finished", {"id": tid, "status": "cancelled"})
            raise
        except (ApprovalDenied, ApprovalTimeout) as exc:
            self._set(tid, status="failed", finished_at=utcnow(), error=f"not approved: {exc}")
            svc.events.publish("task.finished", {"id": tid, "status": "failed"})
        except Exception as exc:
            svc.audit.record(
                "agent:orchestrator",
                "task.error",
                resource_type="task",
                resource_id=tid,
                outcome="error",
                detail={"error": f"{type(exc).__name__}: {exc}"[:500]},
            )
            self._set(tid, status="failed", finished_at=utcnow(), error=f"{type(exc).__name__}: {exc}")
            svc.events.publish("task.finished", {"id": tid, "status": "failed"})
        finally:
            self._running.pop(tid, None)

    async def _execute(
        self, tid: int, req: TaskRequest, steps: list[planner.StepSpec], ctx: AgentContext, state: dict[str, Any]
    ) -> None:
        done: dict[str, str] = {}
        propagated: set[str] = (
            set()
        )  # steps skipped *because* something upstream failed (an agent's own skip is benign)
        pending = {s.key: s for s in steps}
        while pending:
            ready = [s for s in pending.values() if all(d in done for d in s.depends)]
            if not ready:
                for s in pending.values():
                    self._plan_step(tid, s.key, "skipped", "dependency did not complete")
                break
            results = await asyncio.gather(*[self._step(tid, s, ctx, state, req) for s in ready])
            for s, r in zip(ready, results, strict=True):
                pending.pop(s.key)
                ctx.results[s.key] = r
                done[s.key] = r.status
                self._plan_step(tid, s.key, r.status, r.summary)
            changed = True
            while changed:  # propagate to a fixpoint: a skipped step also blocks everything downstream of it
                changed = False
                blocked = {k for k, st in done.items() if st in ("failed", "blocked") or k in propagated}
                for k in list(pending):
                    spec = pending[k]
                    if spec.always or not any(d in blocked for d in spec.depends):
                        continue  # `always` steps run once their dependencies have finished, whatever the outcome
                    why = ", ".join(d for d in spec.depends if d in blocked)
                    self._plan_step(tid, k, "skipped", f"blocked by {why}")
                    done[k] = "skipped"
                    propagated.add(k)
                    ctx.results[k] = AgentResult("skipped", f"blocked by {why}")
                    pending.pop(k)
                    changed = True

    async def _step(
        self, tid: int, s: planner.StepSpec, ctx: AgentContext, state: dict[str, Any], req: TaskRequest
    ) -> AgentResult:
        self._plan_step(tid, s.key, "running", "")
        self.svc.events.publish("task.step", {"task_id": tid, "key": s.key, "status": "running", "agent": s.agent})
        if s.kind == "agent":
            return await self._agent_step(
                tid, s.key, s.agent or "", {**s.payload, **self._agent_params(s.agent or "", req)}, ctx
            )
        if s.kind == "apply":
            return await self._apply_step(tid, s, ctx, state)
        if s.kind == "verify":
            return await self._verify_step(tid, s, ctx, state)
        if s.kind == "learn":
            return await self._learn_step(tid, s, ctx, state)
        return AgentResult("failed", f"unknown step kind {s.kind}")

    @staticmethod
    def _agent_params(agent: str, req: TaskRequest) -> dict[str, Any]:
        p = req.params
        if agent == "code-generation":
            return {
                k: p[k]
                for k in ("backend", "model", "pattern_ids", "mode", "cloud_key", "max_repairs", "top_k")
                if k in p
            }
        if agent == "pattern-replay":
            return {k: p[k] for k in ("pattern_id", "path_map", "include_tests", "selected_paths") if k in p}
        if agent == "code-review-security":
            return {k: p[k] for k in ("files",) if p.get(k)}
        return {}

    async def _agent_step(
        self, tid: int, step_key: str, agent_key: str, payload: dict[str, Any], ctx: AgentContext, attempt: int = 1
    ) -> AgentResult:
        svc = self.svc
        agent = svc.agents.get(agent_key)
        if not svc.agents.is_enabled(agent_key):
            res = AgentResult("skipped", f"agent '{agent_key}' is disabled")
            self._exec_log(tid, agent_key, step_key, attempt, res, 0)
            return res
        exec_id = self._exec_begin(tid, agent_key, step_key, attempt)
        t0 = time.monotonic()
        sub = AgentContext(
            svc,
            ctx.task_id,
            ctx.project_id,
            ctx.project_root,
            ctx.policy,
            ctx.request,
            ctx.params,
            ctx.results,
            attempt,
        )
        try:
            res = await asyncio.wait_for(agent.run(sub, payload), svc.config.config.agents.step_timeout_s)
        except TimeoutError:
            res = AgentResult("failed", "agent timed out")
        except asyncio.CancelledError:
            self._exec_end(exec_id, AgentResult("failed", "cancelled"), t0)
            raise
        except Exception as exc:
            res = AgentResult("failed", f"{type(exc).__name__}: {exc}")
        if agent_key == "code-review-security" and res.status == "failed" and ctx.params.get("override_review"):
            svc.audit.record(
                "user",
                "review.override",
                resource_type="task",
                resource_id=tid,
                outcome="allowed",
                detail={"blocking": [i["message"][:160] for i in res.issues if i["level"] == "error"][:10]},
            )
            res = AgentResult(
                "success", res.summary + " — blocking findings OVERRIDDEN by the user", res.output, res.issues
            )
        self._exec_end(exec_id, res, t0)
        return res

    # ------------------------------------------------------------------ apply
    async def _apply_step(
        self,
        tid: int,
        s: planner.StepSpec,
        ctx: AgentContext,
        state: dict[str, Any],
        files: list[dict[str, Any]] | None = None,
        label: str = "",
    ) -> AgentResult:
        svc = self.svc
        source = s.payload.get("source", "generate")
        files = files if files is not None else proposal_files(ctx.results.get(source))
        policy = ctx.policy
        if policy is None or ctx.project_id is None:
            return AgentResult("failed", "no project")
        review = ctx.results.get("review")
        if review is not None and review.status == "failed" and not ctx.params.get("override_review"):
            return AgentResult("blocked", "code review blocked this change", {}, review.issues)
        if not files:
            return AgentResult("skipped", "nothing to apply")
        if any("[REDACTED:" in f["content"] for f in files):
            return AgentResult(
                "blocked",
                "proposal still contains [REDACTED:…] placeholders that need real values",
                {},
                [{"level": "error", "message": "unresolved redacted secrets"}],
            )
        if policy.level is PermissionLevel.READ_ONLY:
            return AgentResult("skipped", "read-only mode: proposal only, nothing written")
        exec_id = self._exec_begin(tid, "orchestrator", label or s.key, 1)
        t0 = time.monotonic()
        changes = [ChangeSpec(f["path"], f["content"], f.get("action", "modify")) for f in files]
        try:
            async with self.locks.paths(ctx.project_id, [c.path for c in changes], f"task-{tid}"):
                svc.events.publish(
                    "task.step",
                    {"task_id": tid, "key": s.key, "status": "applying", "files": [c.path for c in changes]},
                )
                res = await svc.file_ops.apply(
                    ctx.project_id, policy, changes, task_id=tid, label=label or f"before task {tid}"
                )
        except ChangeDenied as exc:
            r = AgentResult(
                "failed",
                f"change denied: {exc}",
                {},
                [{"level": "error", "message": d["reason"], "path": d["path"]} for d in exc.denied],
            )
            self._exec_end(exec_id, r, t0)
            return r
        state["files_applied"] = sorted(set(state["files_applied"]) | set(res.applied))
        if state["recovery_point_id"] is None:
            state["recovery_point_id"] = res.recovery_point_id
        r = AgentResult(
            "success",
            f"applied {len(res.applied)} file(s); recovery point #{res.recovery_point_id}",
            {"applied": res.applied, "recovery_point_id": res.recovery_point_id, "approvals": res.approvals},
        )
        self._exec_end(exec_id, r, t0)
        return r

    # ------------------------------------------------------------------ verify + fix loop
    async def _run_testing(self, tid: int, keys: list[str], ctx: AgentContext, step: str) -> dict[str, AgentResult]:
        async def one(k: str) -> tuple[str, AgentResult]:
            return k, await self._agent_step(
                tid, f"{step}:{k}", k, dict(ctx.params.get("agent_payloads", {}).get(k, {})), ctx
            )

        out = dict(await asyncio.gather(*[one(k) for k in keys]))
        for k, r in out.items():
            ctx.results[f"test:{k}"] = r
        return out

    @staticmethod
    def evaluate_gate(results: dict[str, AgentResult]) -> dict[str, Any]:
        passed = [k for k, r in results.items() if r.status == "success"]
        failed = [k for k, r in results.items() if r.status == "failed"]
        skipped = [k for k, r in results.items() if r.status in ("skipped", "blocked")]
        evidence = next((k for k in SUITE_AGENTS if k in passed), None)
        security_ok = "security-testing" not in failed
        verified = bool(passed) and not failed and security_ok
        return {
            "verified": verified,
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
            "evidence_agent": evidence,
            "evidence_session_id": results[evidence].output.get("testing_session_id") if evidence else None,
        }

    async def _verify_step(
        self, tid: int, s: planner.StepSpec, ctx: AgentContext, state: dict[str, Any]
    ) -> AgentResult:
        svc = self.svc
        keys = list(s.payload["agents"])
        if ctx.policy is None:
            return AgentResult("failed", "no project")
        if ctx.policy.level is PermissionLevel.READ_ONLY:
            state["gate"] = {
                "verified": False,
                "passed": [],
                "failed": [],
                "skipped": keys,
                "evidence_agent": None,
                "evidence_session_id": None,
            }
            return AgentResult("skipped", "read-only mode: tests were not executed")
        results = await self._run_testing(tid, keys, ctx, "test")
        gate = self.evaluate_gate(results)
        max_attempts = svc.config.config.agents.max_fix_attempts
        attempts = 0
        while s.payload.get("fix") and gate["failed"] and attempts < max_attempts:
            attempts += 1
            state["fix_attempts"] = attempts
            ok = await self._fix_attempt(tid, ctx, state, results, gate, attempts)
            results.update(await self._run_testing(tid, list(gate["failed"]), ctx, f"retest-{attempts}"))
            new_gate = self.evaluate_gate(results)
            for agent_key in gate["failed"]:
                prev = ctx.results.get(f"test0:{agent_key}")
                if prev and prev.output.get("testing_session_id"):
                    annotate_correction(
                        svc,
                        prev.output["testing_session_id"],
                        state.get("last_correction", "automatic correction"),
                        "passed" if results[agent_key].status == "success" else "failed",
                    )
            gate = new_gate
            if not ok:
                break
            if not gate["failed"]:
                await self._record_solution(ctx, state, gate)
        if (
            gate["failed"]
            and state["recovery_point_id"] is not None
            and s.payload.get("fix")
            and ctx.policy is not None
            and ctx.project_root is not None
        ):
            svc.recovery.restore(
                state["recovery_point_id"], ctx.project_root
            )  # failed modification: restore the original files
            state["rolled_back"] = True
            self.svc.events.publish("task.rollback", {"task_id": tid, "recovery_point_id": state["recovery_point_id"]})
        state["gate"] = gate
        status = "success" if gate["verified"] else "failed"
        return AgentResult(
            status,
            f"verification {'PASSED' if gate['verified'] else 'did not pass'}: passed={gate['passed']} failed={gate['failed']} skipped={gate['skipped']}"
            + (f"; rolled back to recovery point #{state['recovery_point_id']}" if state["rolled_back"] else ""),
            {"gate": gate},
        )

    async def _fix_attempt(
        self,
        tid: int,
        ctx: AgentContext,
        state: dict[str, Any],
        results: dict[str, AgentResult],
        gate: dict[str, Any],
        attempt: int,
    ) -> bool:
        for k in gate["failed"]:  # remember the *original* failing sessions for correction annotations
            ctx.results.setdefault(f"test0:{k}", results[k])
        failures = [
            c
            for k in gate["failed"]
            for c in results[k].output["report"]["cases"]
            if c["status"] in ("failed", "error")
        ]
        root = ctx.require_root()
        before = {
            p: (root / p).read_text(encoding="utf-8", errors="replace")
            for p in state["files_applied"]
            if (root / p).is_file()
        }
        dbg = await self._agent_step(
            tid,
            f"fix-{attempt}:debugging",
            "debugging",
            {"failures": failures, "changed_files": state["files_applied"]},
            ctx,
            attempt,
        )
        if dbg.status != "success":
            return False
        files = proposal_files(dbg)
        review = await self._agent_step(
            tid, f"fix-{attempt}:review", "code-review-security", {"files": files}, ctx, attempt
        )
        if review.status == "failed":
            return False
        spec = planner.StepSpec(f"fix-{attempt}", "apply")
        applied = await self._apply_step(
            tid, spec, ctx, state, files, label=f"before fix attempt {attempt} (task {tid})"
        )
        if applied.status != "success":
            return False
        state["last_correction"] = f"{dbg.output.get('source')}: {dbg.summary}"
        state["pending_solution"] = {
            "error_ids": dbg.output.get("error_ids", []),
            "signature": dbg.output.get("signature", ""),
            "before": before,
            "paths": [f["path"] for f in files],
            "root_cause": dbg.output.get("root_cause", ""),
            "source": dbg.output.get("source"),
        }
        return True

    async def _record_solution(self, ctx: AgentContext, state: dict[str, Any], gate: dict[str, Any]) -> None:
        ps = state.get("pending_solution")
        if not ps or not gate.get("evidence_session_id") or ps.get("source") == "known-solution":
            return
        root = ctx.require_root()
        after = {
            p: (root / p).read_text(encoding="utf-8", errors="replace") for p in ps["paths"] if (root / p).is_file()
        }
        DebuggingAgent.record_solution(
            self.svc,
            ps["error_ids"],
            ps["signature"],
            ps["before"],
            after,
            gate["evidence_session_id"],
            ps["root_cause"],
        )

    # ------------------------------------------------------------------ learn
    async def _learn_step(self, tid: int, s: planner.StepSpec, ctx: AgentContext, state: dict[str, Any]) -> AgentResult:
        svc = self.svc
        gate = state.get("gate") or {}
        gen = ctx.results.get("generate")
        replay = ctx.results.get("replay")
        used = list((gen.output.get("generation", {}).get("pattern_ids") if gen else []) or [])
        impl_id = gen.output.get("implementation_id") if gen else None
        if not gate.get("verified"):
            for pid in used:
                svc.patterns.record_use(int(pid), success=False if gate.get("failed") else None, count_use=False)
            if replay and replay.output.get("pattern_id"):
                svc.patterns.record_use(int(replay.output["pattern_id"]), success=False if gate.get("failed") else None)
            return AgentResult("skipped", "nothing learned as verified: the verification gate did not pass")
        notes: list[str] = []
        ev = gate["evidence_session_id"]
        if impl_id:
            with svc.session_factory() as db:
                impl = db.get(CodeImplementation, impl_id)
                if impl:
                    impl.status, impl.verified = "applied", True
                    db.commit()
        if gen and state["files_applied"]:
            root = ctx.require_root()
            files = [
                {"path": p, "content": (root / p).read_text(encoding="utf-8", errors="replace")}
                for p in state["files_applied"]
                if (root / p).is_file()
            ]
            ex = await self._agent_step(
                tid,
                "learn:extract",
                "pattern-extraction",
                {"files": files, "request": ctx.request, "status": "generated"},
                ctx,
            )
            for r in ex.output.get("patterns", []):
                try:
                    svc.patterns.promote(r["pattern_id"], "tested", ev, actor="agent:orchestrator")
                    notes.append(f"pattern {r['pattern_id']} → tested")
                except Exception as exc:
                    notes.append(f"pattern {r['pattern_id']} not promoted: {exc}")
            for pid in used:
                svc.patterns.record_use(int(pid), success=True, count_use=False)
                notes.append(f"pattern {pid}: successful adaptation recorded")
        if replay and replay.output.get("pattern_id"):
            pid = int(replay.output["pattern_id"])
            svc.patterns.record_use(pid, success=True)
            for target in ("tested", "verified"):
                try:
                    svc.patterns.promote(pid, target, ev, actor="agent:orchestrator")
                    notes.append(f"pattern {pid} → {target}")
                except Exception as exc:
                    notes.append(f"pattern {pid} not promoted to {target}: {exc}")
                    break
        return AgentResult("success", "; ".join(notes) or "recorded", {"notes": notes})

    # ------------------------------------------------------------------ bookkeeping
    def _finish(self, tid: int, ctx: AgentContext, state: dict[str, Any]) -> None:
        svc = self.svc
        results = ctx.results
        gate = state.get("gate") or {
            "verified": False,
            "passed": [],
            "failed": [],
            "skipped": list(state.get("planned_agents", [])),
            "evidence_agent": None,
            "evidence_session_id": None,
            "note": "verification did not run",
        }
        failed_steps = [
            k
            for k, r in results.items()
            if r.status in ("failed", "blocked") and not k.startswith(("test:", "test0:", "retest"))
        ]
        verified = bool(gate.get("verified")) and not state["rolled_back"]
        if verified:
            status = "verified"
        elif failed_steps or state["rolled_back"] or gate.get("failed"):
            status = "failed"
        else:
            status = "completed"
        err = None
        if status == "failed":
            if gate.get("failed") and not failed_steps:
                err = (
                    f"verification failed: {', '.join(gate['failed'])} still failing"
                    + (f" after {state['fix_attempts']} correction attempt(s)" if state["fix_attempts"] else "")
                    + (
                        f"; the project was restored to recovery point #{state['recovery_point_id']}"
                        if state["rolled_back"]
                        else ""
                    )
                )
            elif failed_steps:
                fs = failed_steps[0]
                err = f"{fs}: {results[fs].summary}"
            else:
                err = "verification did not pass"
        prop = next((proposal_files(r) for r in results.values() if proposal_files(r)), [])
        result = {
            "files_applied": state["files_applied"],
            "recovery_point_id": state["recovery_point_id"],
            "rolled_back": state["rolled_back"],
            "fix_attempts": state["fix_attempts"],
            "gate": gate,
            "proposal": [{"path": f["path"], "action": f.get("action")} for f in prop],
            "steps": {
                k: {"status": r.status, "summary": r.summary}
                for k, r in results.items()
                if not k.startswith(("test:", "test0:"))
            },
        }
        self._set(tid, status=status, finished_at=utcnow(), verified=verified, result=result, error=err)
        svc.audit.record(
            "agent:orchestrator",
            "task.finish",
            resource_type="task",
            resource_id=tid,
            detail={"status": status, "verified": verified, "files": len(state["files_applied"])},
        )
        svc.events.publish("task.finished", {"id": tid, "status": status, "verified": verified})

    def _set(self, tid: int, **fields: Any) -> None:
        with self.svc.session_factory() as db:
            t = db.get(Task, tid)
            if t is not None:
                for k, v in fields.items():
                    setattr(t, k, v)
                db.commit()
        if "status" in fields:
            self.svc.events.publish("task.status", {"id": tid, "status": fields["status"]})

    def _plan_step(self, tid: int, key: str, status: str, summary: str) -> None:
        with self.svc.session_factory() as db:
            t = db.get(Task, tid)
            if t is None:
                return
            plan = [dict(p) for p in t.plan]
            for p in plan:
                if p["key"] == key:
                    p["status"], p["summary"] = status, summary[:300]
            t.plan = plan
            db.commit()
        self.svc.events.publish("task.step", {"task_id": tid, "key": key, "status": status})

    def _exec_begin(self, tid: int, agent_key: str, step: str, attempt: int) -> int:
        with self.svc.session_factory() as db:
            agent = db.execute(select(AgentDefinition).where(AgentDefinition.key == agent_key)).scalar_one_or_none()
            e = AgentExecution(
                task_id=tid,
                agent_id=agent.id if agent else None,
                agent_key=agent_key,
                step_key=step,
                attempt=attempt,
                status="running",
            )
            db.add(e)
            db.commit()
            return int(e.id)

    def _exec_end(self, exec_id: int, res: AgentResult, t0: float) -> None:
        with self.svc.session_factory() as db:
            e = db.get(AgentExecution, exec_id)
            if e is not None:
                e.status = "success" if res.status == "success" else res.status
                e.output = {
                    "summary": res.summary,
                    "issues": res.issues[:20],
                    **({"gate": res.output["gate"]} if "gate" in res.output else {}),
                }
                e.error = res.summary if res.status in ("failed", "blocked") else None
                e.finished_at, e.duration_ms = utcnow(), int((time.monotonic() - t0) * 1000)
                db.commit()

    def _exec_log(self, tid: int, agent_key: str, step: str, attempt: int, res: AgentResult, t0: float) -> None:
        self._exec_end(
            self._exec_begin(tid, agent_key, step, attempt), res, time.monotonic() - t0 if t0 else time.monotonic()
        )
