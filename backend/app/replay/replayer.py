"""Pattern replay: reconstruct a stored implementation inside the current project - deterministic, no model needed."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.generation.generator import unified_diff
from app.generation.parser import PathRejected, safe_relative_path
from app.models import Dependency, Project
from app.retrieval.search import major
from app.security.permissions import PermissionPolicy, is_sensitive_write_path

_PLACEHOLDER = re.compile(r"\[REDACTED:([a-z0-9-]+)\]")


@dataclass
class PlannedFile:
    source_path: str
    target_path: str
    action: str  # create | identical | conflict | blocked
    content: str
    diff: str = ""
    role: str = "source"
    placeholders: list[str] = field(default_factory=list)
    needs_approval: bool = False
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class ReplayPlan:
    pattern: dict[str, Any]
    project_id: int
    files: list[PlannedFile]
    dependencies: dict[str, list[dict[str, Any]]]
    environment: dict[str, Any]
    suggested_path_map: dict[str, str]
    warnings: list[str]
    unresolved_placeholders: list[dict[str, str]]

    @property
    def can_apply(self) -> bool:
        return any(f.action in ("create", "conflict") for f in self.files) and self.environment["compatible"]

    def to_dict(self) -> dict[str, Any]:
        d = {k: v for k, v in self.__dict__.items() if k != "files"}
        d["files"] = [f.to_dict() for f in self.files]
        d["can_apply"] = self.can_apply
        return d


class ReplayError(ValueError):
    pass


def _remap(path: str, path_map: dict[str, str]) -> str:
    for src, dst in sorted(path_map.items(), key=lambda kv: -len(kv[0])):
        if path.startswith(src):
            return dst + path[len(src) :]
    return path


class Replayer:
    def __init__(self, svc: Any) -> None:
        self.svc = svc

    def plan(
        self,
        pattern_id: int,
        project_id: int,
        *,
        path_map: dict[str, str] | None = None,
        include_tests: bool = True,
        selected_paths: list[str] | None = None,
    ) -> ReplayPlan:
        pattern = self.svc.patterns.get(pattern_id)
        with self.svc.session_factory() as db:
            project = db.get(Project, project_id)
            if project is None:
                raise ReplayError("project not found")
            root = Path(project.root_path)
            deps = [
                (d.ecosystem, d.name.lower(), d.version_spec)
                for d in db.execute(select(Dependency).where(Dependency.project_id == project.id)).scalars()
            ]
            proj_fw = [f for f in project.frameworks or [] if not f.get("dev")]
            proj_lang = (project.conventions or {}).get("primary_language")
            top_dirs = set((project.conventions or {}).get("top_level_dirs", []))
        if not root.is_dir():
            raise ReplayError("project root no longer exists")
        policy = PermissionPolicy(self.svc.config.config.permission_level, root)
        content = pattern.get("content", {})
        warnings: list[str] = []
        path_map = dict(path_map or {})
        suggested = self._suggest_map(content.get("files", []), top_dirs)
        files: list[PlannedFile] = []
        unresolved: list[dict[str, str]] = []
        for f in content.get("files", []):
            if selected_paths is not None and f["path"] not in selected_paths:
                continue
            if f.get("role") == "test" and not include_tests:
                continue
            try:
                target = safe_relative_path(_remap(f["path"], path_map))
            except PathRejected as exc:  # e.g. a malicious imported pattern containing "../../etc/passwd"
                files.append(
                    PlannedFile(
                        f["path"], f["path"], "blocked", "", reason=f"unsafe path: {exc}", role=f.get("role", "source")
                    )
                )
                warnings.append(f"blocked unsafe path in pattern: {f['path']}")
                continue
            real, why = policy.resolve_inside(target)
            if real is None:
                files.append(PlannedFile(f["path"], target, "blocked", "", reason=why, role=f.get("role", "source")))
                continue
            text = f.get("content", "")
            ph = sorted(set(_PLACEHOLDER.findall(text)))
            for kind in ph:
                unresolved.append({"path": target, "kind": kind})
            exists = real.is_file()
            before = real.read_text(encoding="utf-8", errors="replace") if exists else None
            if before is None:
                action, diff = "create", unified_diff(target, "", text)
            elif before.strip() == text.strip():
                action, diff = "identical", ""
            else:
                action, diff = "conflict", unified_diff(target, before, text)
            files.append(
                PlannedFile(
                    f["path"],
                    target,
                    action,
                    text,
                    diff,
                    f.get("role", "source"),
                    ph,
                    needs_approval=is_sensitive_write_path(target) or action == "conflict",
                    reason="existing file differs: applying overwrites it (a recovery point is taken first)"
                    if action == "conflict"
                    else "",
                )
            )
            if f.get("truncated"):
                warnings.append(f"{target} was truncated when learned; the replay is incomplete")
        if unresolved:
            warnings.append(
                "secrets were redacted when this pattern was learned: provide real values via configuration/environment for "
                + ", ".join(sorted({u["kind"] for u in unresolved}))
            )
        dep_report = self._dependencies(content.get("dependencies", []), deps)
        env = self._environment(pattern, proj_lang, proj_fw)
        warnings.extend(env["warnings"])
        return ReplayPlan(
            {
                k: pattern[k]
                for k in ("id", "title", "status", "confidence", "language", "framework", "framework_version")
            },
            project_id,
            files,
            dep_report,
            env,
            suggested if not path_map else {},
            warnings,
            unresolved,
        )

    @staticmethod
    def _suggest_map(files: list[dict[str, Any]], project_dirs: set[str]) -> dict[str, str]:
        tops = {f["path"].split("/", 1)[0] for f in files if "/" in f["path"] and f.get("role") != "test"}
        if len(tops) == 1:
            (top,) = tuple(tops)
            if top not in project_dirs and project_dirs:
                for alt in ("src", "app", "lib", "backend"):
                    if alt in project_dirs and alt != top:
                        return {f"{top}/": f"{alt}/"}
        return {}

    @staticmethod
    def _dependencies(
        needed: list[dict[str, Any]], have: list[tuple[str, str, str]]
    ) -> dict[str, list[dict[str, Any]]]:
        installed = {(e, n): v for e, n, v in have}
        missing: list[dict[str, Any]] = []
        satisfied: list[dict[str, Any]] = []
        mismatched: list[dict[str, Any]] = []
        for d in needed:
            key = (d["ecosystem"], d["name"].lower())
            if key not in installed:
                missing.append(d)
            else:
                a, b = major(d.get("version")), major(installed[key])
                (mismatched if a is not None and b is not None and a != b else satisfied).append(
                    {**d, "project_version": installed[key]}
                )
        return {"missing": missing, "satisfied": satisfied, "version_mismatch": mismatched}

    @staticmethod
    def _environment(pattern: dict[str, Any], proj_lang: str | None, proj_fw: list[dict[str, Any]]) -> dict[str, Any]:
        warnings: list[str] = []
        compatible = True
        if pattern["language"] and proj_lang and pattern["language"] != proj_lang:
            compatible = False
            warnings.append(f"language mismatch: pattern is {pattern['language']}, project is {proj_lang}")
        if pattern["framework"]:
            match = next((f for f in proj_fw if f["name"].lower() == pattern["framework"].lower()), None)
            if match is None and proj_fw:
                warnings.append(
                    f"framework mismatch: pattern uses {pattern['framework']}, project uses {', '.join(f['name'] for f in proj_fw)}"
                )
            elif match:
                pm, qm = major(pattern["framework_version"]), major(match.get("version"))
                if pm is not None and qm is not None and pm != qm:
                    warnings.append(
                        f"{pattern['framework']} version differs: learned on {pm}, project uses {qm} - review for API changes"
                    )
        if pattern["status"] not in ("verified", "tested"):
            warnings.append(
                f"pattern status is '{pattern['status']}': replayed code will be tested before it counts as verified"
            )
        return {
            "compatible": compatible,
            "warnings": warnings,
            "project_language": proj_lang,
            "pattern_language": pattern["language"],
            "framework": pattern["framework"],
            "framework_version": pattern["framework_version"],
        }
