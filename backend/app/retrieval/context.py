"""Context & retrieval: understand a request in the light of the current project, find knowledge, flag gaps."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from app.extraction.analyzer import FRAMEWORKS
from app.models import Dependency, Project
from app.retrieval.search import KnowledgeSearch, SearchQuery, SearchResponse

_LANGS = {
    "python": "python",
    "php": "php",
    "javascript": "javascript",
    "typescript": "typescript",
    "golang": "go",
    "rust": "rust",
    "ruby": "ruby",
    "java": "java",
    "c#": "c_sharp",
    "csharp": "c_sharp",
}
_FRAMEWORK_WORDS = {label.lower(): label for label in set(FRAMEWORKS.values())}
_FRAMEWORK_WORDS.update(
    {"next": "Next.js", "nextjs": "Next.js", "nest": "NestJS", "nestjs": "NestJS", "tailwind": "Tailwind CSS"}
)
_REPLAY = re.compile(
    r"\b(recreate|reproduce|replay|redo|same as|set (it )?up again|re-?implement|the one (i|we) (previously |already )?(used|built|wrote))\b",
    re.I,
)
_PRIOR = re.compile(r"\b(previous(ly)?|earlier|before|already|we (developed|built|used)|i (used|built|wrote))\b", re.I)
_ADAPT = re.compile(
    r"\b(but|extend|extension|add support|support for|multiple|instead|change|modify|adapt|different|also|except|however|with)\b",
    re.I,
)
_DEBUG = re.compile(r"\b(fix|bug|error|exception|traceback|failing|fails|broken|crash|not working)\b", re.I)


@dataclass
class MissingContext:
    kind: str
    message: str
    severity: str = "info"  # info | warning | blocker

    def to_dict(self) -> dict[str, str]:
        return self.__dict__.copy()


@dataclass
class ContextReport:
    intent: str
    project: dict[str, Any]
    signals: dict[str, Any]
    search: SearchResponse | None
    missing: list[MissingContext] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        s = self.search
        return {
            "intent": self.intent,
            "project": self.project,
            "signals": self.signals,
            "missing": [m.to_dict() for m in self.missing],
            "embedder": s.embedder if s else None,
            "notes": s.notes if s else [],
            "hits": [
                {
                    "pattern_id": h.pattern_id,
                    "score": h.score,
                    "relevance": h.relevance,
                    "trust": h.trust,
                    "reasons": h.reasons,
                    "warnings": h.warnings,
                    "pattern": h.pattern,
                }
                for h in (s.hits if s else [])
            ],
        }


def detect_intent(request: str) -> str:
    if _DEBUG.search(request) and not _REPLAY.search(request):
        return "debug"
    if _REPLAY.search(request) and not re.search(
        r"\b(extend|support for|multiple|instead|modify|add support)\b", request, re.I
    ):
        return "replay"
    if _PRIOR.search(request) and _ADAPT.search(request):
        return "adapt"
    return "new"


def request_signals(request: str) -> dict[str, Any]:
    low = request.lower()
    words = set(re.findall(r"[a-z0-9#.+]+", low))
    langs = sorted({v for k, v in _LANGS.items() if k in words})
    fws = sorted(
        {label for word, label in _FRAMEWORK_WORDS.items() if re.search(rf"(?<![a-z]){re.escape(word)}(?![a-z])", low)}
    )
    paths = re.findall(r"[\w./-]+\.(?:py|ts|tsx|js|php|go|rs|java|rb|cs|sql|md)\b", request)
    return {
        "languages": langs,
        "frameworks": fws,
        "paths": paths,
        "word_count": len(request.split()),
        "refers_to_prior_work": bool(_PRIOR.search(request)),
    }


class ContextAnalyzer:
    def __init__(self, session_factory: sessionmaker[Session], search: KnowledgeSearch) -> None:
        self._factory = session_factory
        self._search = search

    def analyze(self, request: str, project_id: int | None, top_k: int = 8) -> ContextReport:
        intent = detect_intent(request)
        sig = request_signals(request)
        project_info: dict[str, Any] = {}
        language: str | None = None
        framework: str | None = None
        fw_version: str | None = None
        with self._factory() as db:
            project = db.get(Project, project_id) if project_id else None
            if project is not None:
                deps = db.query(Dependency).filter(Dependency.project_id == project.id).count()
                language = (project.conventions or {}).get("primary_language")
                app_fw = [
                    f
                    for f in project.frameworks or []
                    if not f.get("dev") and f["name"] not in ("pytest", "Vitest", "Jest", "TypeScript", "PHPUnit")
                ]
                if app_fw:
                    framework, fw_version = app_fw[0]["name"], app_fw[0].get("version") or None
                project_info = {
                    "id": project.id,
                    "name": project.name,
                    "language": language,
                    "framework": framework,
                    "framework_version": fw_version,
                    "frameworks": project.frameworks,
                    "conventions": project.conventions,
                    "dependency_count": deps,
                    "analyzed": bool((project.conventions or {}).get("analyzed_at")),
                }
        if sig["languages"] and not language:
            language = sig["languages"][0]
        if sig["frameworks"] and not framework:
            framework = sig["frameworks"][0]
        search = self._search.search(SearchQuery(request, project_id, language, framework, fw_version, top_k=top_k))
        report = ContextReport(intent, project_info, sig, search)
        self._missing(report, request)
        return report

    @staticmethod
    def _missing(r: ContextReport, request: str) -> None:
        m = r.missing
        proj = r.project
        if not proj:
            m.append(
                MissingContext("project", "No project selected; conventions and dependencies are unknown.", "warning")
            )
        elif not proj.get("analyzed"):
            m.append(
                MissingContext(
                    "analysis", "The project has not been analysed yet (frameworks, conventions unknown).", "warning"
                )
            )
        if r.signals["word_count"] < 4:
            m.append(
                MissingContext(
                    "vague-request",
                    "The request is very short; describe the expected behaviour in more detail.",
                    "warning",
                )
            )
        for fw in r.signals["frameworks"]:
            pf = {f["name"] for f in proj.get("frameworks", [])} if proj else set()
            if proj and pf and fw not in pf:
                m.append(
                    MissingContext(
                        "framework-mismatch",
                        f"The request mentions {fw} but the project uses {', '.join(sorted(pf))}.",
                        "warning",
                    )
                )
        for lg in r.signals["languages"]:
            if proj.get("language") and lg != proj["language"]:
                m.append(
                    MissingContext(
                        "language-mismatch",
                        f"The request mentions {lg} but the project is mainly {proj['language']}.",
                        "warning",
                    )
                )
        hits = r.search.hits if r.search else []
        if not hits:
            sev = "blocker" if r.intent in ("replay", "adapt") else "warning"
            msg = (
                "The request refers to a previous implementation but nothing matching was found; name the project or files."
                if r.intent in ("replay", "adapt") or r.signals["refers_to_prior_work"]
                else "No stored knowledge matches; generation will rely on the model alone."
            )
            m.append(MissingContext("no-knowledge", msg, sev))
        else:
            best = hits[0].pattern.get("status")
            if best not in ("verified", "tested"):
                m.append(
                    MissingContext(
                        "unverified-knowledge",
                        f"The best match is '{best}', not tested or verified; review its output carefully.",
                        "info",
                    )
                )
            for w in hits[0].warnings:
                m.append(MissingContext("version-or-language-gap", w, "warning"))
        if proj and proj.get("analyzed") and not (proj.get("conventions") or {}).get("test_locations"):
            m.append(
                MissingContext(
                    "no-tests",
                    "No test directory was detected, so generated code cannot be verified automatically.",
                    "info",
                )
            )
