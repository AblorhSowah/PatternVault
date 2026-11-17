"""Turn a captured development session into reusable pattern candidates.

Deterministic and explainable (no LLM): file changes are parsed with tree-sitter, categorised by weighted
evidence, grouped into coherent implementations, and enriched with dependencies, steps and tests. Output is a
list of :class:`PatternCandidate` that the knowledge store persists (status ``observed``).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from app.extraction.analyzer import Dep, clean_version, is_manifest, parse_manifest
from app.extraction.categorizer import CATEGORY_LABELS, categorize
from app.extraction.language import CODE_LANGUAGES, detect_language, file_role
from app.extraction.structure import extract_structure

EXTRACTOR_VERSION = "1"
MAX_FILE_CHARS = 60_000
MAX_PATTERN_CHARS = 400_000
MIN_CODE_LINES = 3


@dataclass
class FileInput:
    """One changed file handed to the extractor (content already redacted)."""

    path: str
    change_type: str
    after: str | None
    before: str | None = None
    diff: str = ""


@dataclass
class SessionInput:
    prompts: list[str]
    files: list[FileInput]
    commands: list[dict[str, Any]] = field(default_factory=list)  # observed shell commands [{command, ok, is_test}]
    project_language: str | None = None
    project_frameworks: list[dict[str, Any]] = field(default_factory=list)
    provider: str | None = None
    session_id: int | None = None
    model: str | None = None


@dataclass
class PatternCandidate:
    title: str
    summary: str
    category: str
    language: str | None
    framework: str | None
    framework_version: str | None
    tags: list[str]
    content: dict[str, Any]
    fingerprint: str
    content_hash: str
    provenance: dict[str, Any]
    dependencies: list[Dep] = field(default_factory=list)
    category_evidence: list[str] = field(default_factory=list)


def content_hash_of(files: list[dict[str, Any]]) -> str:
    return _h(*sorted(_h(f["path"], f["content"]) for f in files))


def _h(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


# A line made only of wrapper tags, e.g. ``<pasted_content id="5493">`` that a pasted prompt starts with. Using it as the
# title gave "Testing: <pasted_content id="5493">". Tags inside a real sentence (``Fix the <div> layout``) are kept.
_TAG_ONLY_LINE = re.compile(r"(?m)^[ \t]*(?:</?[A-Za-z][\w:.-]*(?:[ \t][^<>\n]{0,200})?>[ \t]*)+$")


def _title_from_prompt(prompt: str, category: str) -> str:
    prompt = _TAG_ONLY_LINE.sub("", prompt)
    first = re.split(r"(?<=[.!?])\s|\n", prompt.strip(), maxsplit=1)[0]
    first = re.sub(r"\s+", " ", first)[:90].rstrip(" .,:;")
    label = CATEGORY_LABELS.get(category, category.title())
    if not first:
        return label
    return first if first.lower().startswith(label.lower()) else f"{label}: {first}"


def _pick_framework(
    frameworks: list[dict[str, Any]], language: str | None, paths: list[str]
) -> tuple[str | None, str | None]:
    """Prefer an application framework matching the language (skip test/tool libs like pytest, Vitest)."""
    tool_like = {
        "pytest",
        "Vitest",
        "Jest",
        "PHPUnit",
        "Pest",
        "Playwright",
        "TypeScript",
        "RSpec",
        "Alembic",
        "Pydantic",
    }
    by_lang = {
        "python": {"pypi"},
        "javascript": {"npm"},
        "typescript": {"npm"},
        "tsx": {"npm"},
        "php": {"composer"},
        "rust": {"cargo"},
        "go": {"go"},
        "ruby": {"gem"},
    }
    eco = by_lang.get(language or "", set())
    candidates = [f for f in frameworks if f["ecosystem"] in eco and f["name"] not in tool_like and not f.get("dev")]
    if any("test" in p.lower() for p in paths) and not candidates:
        candidates = [f for f in frameworks if f["ecosystem"] in eco]
    if not candidates:
        return None, None
    return candidates[0]["name"], candidates[0].get("version") or None


def _dependencies_added(files: list[FileInput]) -> list[Dep]:
    added: list[Dep] = []
    for f in files:
        if not is_manifest(f.path):
            continue
        before = {(d.ecosystem, d.name) for d in parse_manifest(f.path, f.before)} if f.before else set()
        for d in parse_manifest(f.path, f.after or ""):
            if (d.ecosystem, d.name) not in before:
                added.append(d)
    return added


def _file_entry(f: FileInput, language: str | None, role: str) -> dict[str, Any]:
    content = f.after or ""
    truncated = len(content) > MAX_FILE_CHARS
    entry: dict[str, Any] = {
        "path": f.path,
        "role": role,
        "language": language,
        "change_type": f.change_type,
        "content": content[:MAX_FILE_CHARS],
        "truncated": truncated,
    }
    if language:
        st = extract_structure(language, content[:MAX_FILE_CHARS])
        if st.parse_ok:
            entry["symbols"] = [s.to_dict() for s in st.symbols][:80]
            entry["imports"] = st.imports[:40]
            entry["routes"] = st.routes
    return entry


def extract_candidates(session: SessionInput) -> list[PatternCandidate]:
    prompt_text = "\n".join(session.prompts)
    first_prompt = session.prompts[0] if session.prompts else ""
    usable: list[tuple[FileInput, str | None, str]] = []
    for f in session.files:
        if f.change_type == "deleted" or f.after is None:
            continue
        lang = detect_language(f.path)
        role = file_role(f.path)
        if (lang in CODE_LANGUAGES or is_manifest(f.path) or role in ("config", "migration")) and f.after.strip():
            usable.append((f, lang, role))
    code_files = [u for u in usable if u[1] in CODE_LANGUAGES]
    if not code_files or sum(u[0].after.count("\n") + 1 for u in code_files if u[0].after) < MIN_CODE_LINES:
        return []

    # --- score each file, then group by dominant category
    groups: dict[str, list[tuple[FileInput, str | None, str]]] = defaultdict(list)
    evidence: dict[str, list[str]] = defaultdict(list)
    session_cats = categorize(
        paths=[u[0].path for u in usable],
        code="\n".join((u[0].after or "")[:5000] for u in usable),
        prompt=prompt_text,
        top=1,
    )
    fallback = session_cats[0].category if session_cats else "general"
    tests: list[tuple[FileInput, str | None, str]] = []
    for item in usable:
        f, lang, role = item
        if role == "test":
            tests.append(item)
            continue
        cats = categorize(paths=[f.path], code=(f.after or "")[:20000], prompt="", top=1)
        cat = cats[0].category if cats else fallback
        groups[cat].append(item)
        if cats:
            evidence[cat].extend(cats[0].evidence)
    if not groups and tests:
        groups[fallback if fallback != "general" else "testing"].extend(tests)
        tests = []
    # Merge tiny noise groups into the dominant one when the session is clearly one feature
    if len(groups) > 1 and len(session.prompts) <= 1:
        sizes = {c: sum(len(i[0].after or "") for i in items) for c, items in groups.items()}
        dominant = max(sizes, key=lambda c: sizes[c])
        for c in [c for c in groups if c != dominant and sizes[c] < 0.15 * sizes[dominant]]:
            groups[dominant].extend(groups.pop(c))
    all_deps = _dependencies_added([u[0] for u in usable])
    verification = [c for c in session.commands if c.get("is_test") and c.get("ok") is not None]

    out: list[PatternCandidate] = []
    for cat, items in groups.items():
        stems = {re.sub(r"\W+", "", i[0].path.rsplit("/", 1)[-1].split(".")[0]).lower() for i in items}
        group_tests = [
            t for t in tests if any(s and s in t[0].path.lower().replace("_", "").replace("-", "") for s in stems)
        ]
        if not group_tests and len(groups) == 1:
            group_tests = tests
        members = items + [t for t in group_tests if t not in items]
        langs = [i[1] for i in items if i[1] in CODE_LANGUAGES]
        language = max(set(langs), key=langs.count) if langs else session.project_language
        fw, fw_ver = _pick_framework(session.project_frameworks, language, [i[0].path for i in items])
        files = [_file_entry(f, lang, role) for f, lang, role in members]
        total = sum(len(e["content"]) for e in files)
        while total > MAX_PATTERN_CHARS and files:
            biggest = max(files, key=lambda e: len(e["content"]))
            biggest["content"] = biggest["content"][: len(biggest["content"]) // 2]
            biggest["truncated"] = True
            total = sum(len(e["content"]) for e in files)
        steps = [f"{'Create' if e['change_type'] == 'created' else 'Modify'} {e['path']} ({e['role']})" for e in files]
        for d in all_deps:
            steps.insert(0, f"Add {d.ecosystem} dependency {d.name} {d.version_spec}".strip())
        if verification:
            steps.append(
                "Verify: "
                + "; ".join(f"{c['command'][:60]} → {'ok' if c['ok'] else 'failed'}" for c in verification[-2:])
            )
        routes = [r for e in files for r in e.get("routes", [])]
        title = _title_from_prompt(first_prompt, cat)
        summary_bits = [
            f"{len(files)} file(s): " + ", ".join(e["path"] for e in files[:6]) + ("…" if len(files) > 6 else "")
        ]
        symbols = [
            s["name"] for e in files for s in e.get("symbols", []) if s["kind"] in ("class", "function", "method")
        ][:8]
        if symbols:
            summary_bits.append("defines " + ", ".join(symbols))
        if routes:
            summary_bits.append("routes " + ", ".join(f"{r['method']} {r['path']}" for r in routes[:4]))
        content = {
            "kind": "implementation",
            "request": first_prompt[:4000],
            "files": files,
            "steps": steps,
            "tests": [e["path"] for e in files if e["role"] == "test"],
            "dependencies": [
                {"ecosystem": d.ecosystem, "name": d.name, "version": d.version_spec, "dev": d.is_dev} for d in all_deps
            ],
            "observed_verification": verification[-3:],
            "environment": {"language": language, "framework": fw, "framework_version": fw_ver},
        }
        skeleton = sorted(
            f"{e['role']}:{','.join(sorted(s['name'] for s in e.get('symbols', []) if s['kind'] in ('class', 'function')))}"
            for e in files
        )
        fingerprint = _h(cat, language or "", fw or "", json.dumps(skeleton))
        content_hash = content_hash_of(files)
        tags = sorted({cat, *(t for t in (language, fw) if t), *{e["role"] for e in files if e["role"] != "source"}})
        out.append(
            PatternCandidate(
                title=title,
                summary="; ".join(summary_bits),
                category=cat,
                language=language,
                framework=fw,
                framework_version=clean_version(fw_ver or "") or None,
                tags=tags,
                content=content,
                fingerprint=fingerprint,
                content_hash=content_hash,
                category_evidence=sorted(set(evidence.get(cat, [])))[:6],
                dependencies=all_deps,
                provenance={
                    "session_id": session.session_id,
                    "provider": session.provider,
                    "model": session.model,
                    "extractor_version": EXTRACTOR_VERSION,
                    "observed_test_pass": bool(verification and verification[-1].get("ok")),
                    "category_evidence": sorted(set(evidence.get(cat, [])))[:6],
                },
            )
        )
    return out
