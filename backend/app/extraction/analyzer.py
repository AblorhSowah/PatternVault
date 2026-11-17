"""Project analysis: languages, frameworks (+versions), dependencies and coding conventions."""

from __future__ import annotations

import contextlib
import json
import re
import tomllib
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.extraction.language import CODE_LANGUAGES, detect_language, file_role
from app.extraction.structure import extract_structure
from app.security.exclusions import ExclusionPolicy, walk_project

MAX_FILES_SAMPLED = 400
MAX_FILE_BYTES = 200_000


@dataclass(frozen=True)
class Dep:
    ecosystem: str
    name: str
    version_spec: str = ""
    is_dev: bool = False
    source_file: str = ""


@dataclass
class ProjectAnalysis:
    languages: dict[str, dict[str, int]] = field(default_factory=dict)  # lang -> {files, lines}
    primary_language: str | None = None
    frameworks: list[dict[str, Any]] = field(default_factory=list)
    dependencies: list[Dep] = field(default_factory=list)
    conventions: dict[str, Any] = field(default_factory=dict)
    file_count: int = 0

    def framework_names(self) -> set[str]:
        return {f["name"] for f in self.frameworks}


# (ecosystem, dependency name) -> framework label
FRAMEWORKS: dict[tuple[str, str], str] = {
    ("pypi", "fastapi"): "FastAPI",
    ("pypi", "django"): "Django",
    ("pypi", "flask"): "Flask",
    ("pypi", "starlette"): "Starlette",
    ("pypi", "sqlalchemy"): "SQLAlchemy",
    ("pypi", "pytest"): "pytest",
    ("pypi", "pydantic"): "Pydantic",
    ("pypi", "celery"): "Celery",
    ("pypi", "alembic"): "Alembic",
    ("npm", "react"): "React",
    ("npm", "next"): "Next.js",
    ("npm", "vue"): "Vue",
    ("npm", "nuxt"): "Nuxt",
    ("npm", "svelte"): "Svelte",
    ("npm", "express"): "Express",
    ("npm", "@nestjs/core"): "NestJS",
    ("npm", "vitest"): "Vitest",
    ("npm", "jest"): "Jest",
    ("npm", "tailwindcss"): "Tailwind CSS",
    ("npm", "@angular/core"): "Angular",
    ("npm", "prisma"): "Prisma",
    ("npm", "typeorm"): "TypeORM",
    ("npm", "playwright"): "Playwright",
    ("npm", "typescript"): "TypeScript",
    ("npm", "fastify"): "Fastify",
    ("composer", "laravel/framework"): "Laravel",
    ("composer", "symfony/framework-bundle"): "Symfony",
    ("composer", "livewire/livewire"): "Livewire",
    ("composer", "laravel/sanctum"): "Laravel Sanctum",
    ("composer", "phpunit/phpunit"): "PHPUnit",
    ("composer", "pestphp/pest"): "Pest",
    ("cargo", "actix-web"): "Actix",
    ("cargo", "axum"): "Axum",
    ("cargo", "tokio"): "Tokio",
    ("go", "github.com/gin-gonic/gin"): "Gin",
    ("go", "github.com/gofiber/fiber"): "Fiber",
    ("gem", "rails"): "Rails",
    ("gem", "sinatra"): "Sinatra",
    ("gem", "rspec"): "RSpec",
}

_VERSION_RE = re.compile(r"(\d+(?:\.\d+){0,2})")


def clean_version(spec: str) -> str:
    m = _VERSION_RE.search(spec or "")
    return m.group(1) if m else ""


def _pep508_name(req: str) -> tuple[str, str]:
    m = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*(.*)", req)
    if not m:
        return "", ""
    return m.group(1).lower().replace("_", "-"), m.group(2).split(";")[0].strip()


def parse_manifest(rel_path: str, text: str) -> list[Dep]:
    """Parse one dependency manifest. Unknown/unsupported manifests return an empty list."""
    name = rel_path.rsplit("/", 1)[-1]
    deps: list[Dep] = []
    try:
        if name == "pyproject.toml":
            data = tomllib.loads(text)
            proj = data.get("project", {})
            for r in proj.get("dependencies", []):
                n, v = _pep508_name(r)
                deps.append(Dep("pypi", n, v, False, rel_path))
            for group in proj.get("optional-dependencies", {}).values():
                for r in group:
                    n, v = _pep508_name(r)
                    deps.append(Dep("pypi", n, v, True, rel_path))
            for group in data.get("dependency-groups", {}).values():
                for r in group:
                    if isinstance(r, str):
                        n, v = _pep508_name(r)
                        deps.append(Dep("pypi", n, v, True, rel_path))
            poetry = data.get("tool", {}).get("poetry", {})
            for n, v in poetry.get("dependencies", {}).items():
                if n.lower() != "python":
                    deps.append(
                        Dep("pypi", n.lower(), v if isinstance(v, str) else str(v.get("version", "")), False, rel_path)
                    )
        elif re.match(r"requirements.*\.txt$", name):
            for line in text.splitlines():
                line = line.split("#")[0].strip()
                if line and not line.startswith(("-", "git+", "http")):
                    n, v = _pep508_name(line)
                    if n:
                        deps.append(Dep("pypi", n, v, "dev" in name or "test" in name, rel_path))
        elif name == "package.json":
            data = json.loads(text)
            for key, dev in (("dependencies", False), ("devDependencies", True), ("peerDependencies", False)):
                for n, v in (data.get(key) or {}).items():
                    deps.append(Dep("npm", n, str(v), dev, rel_path))
        elif name == "composer.json":
            data = json.loads(text)
            for key, dev in (("require", False), ("require-dev", True)):
                for n, v in (data.get(key) or {}).items():
                    if n != "php" and not n.startswith("ext-"):
                        deps.append(Dep("composer", n, str(v), dev, rel_path))
        elif name == "Cargo.toml":
            data = tomllib.loads(text)
            for key, dev in (("dependencies", False), ("dev-dependencies", True)):
                for n, v in (data.get(key) or {}).items():
                    deps.append(Dep("cargo", n, v if isinstance(v, str) else str(v.get("version", "")), dev, rel_path))
        elif name == "go.mod":
            for m in re.finditer(r"^\s*(?:require\s+)?([\w.\-/]+\.[\w.\-/]+)\s+(v[\w.\-+]+)", text, re.M):
                deps.append(Dep("go", m.group(1), m.group(2), False, rel_path))
        elif name == "Gemfile":
            for m in re.finditer(r"""^\s*gem\s+['"]([^'"]+)['"](?:\s*,\s*['"]([^'"]+)['"])?""", text, re.M):
                deps.append(Dep("gem", m.group(1), m.group(2) or "", False, rel_path))
    except (ValueError, KeyError, TypeError, AttributeError):
        return []
    return [d for d in deps if d.name]


MANIFEST_NAMES = {"pyproject.toml", "package.json", "composer.json", "Cargo.toml", "go.mod", "Gemfile"}


def is_manifest(rel: str) -> bool:
    base = rel.rsplit("/", 1)[-1]
    return base in MANIFEST_NAMES or bool(re.match(r"requirements.*\.txt$", base))


def detect_frameworks(deps: list[Dep]) -> list[dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for d in deps:
        label = FRAMEWORKS.get((d.ecosystem, d.name.lower() if d.ecosystem != "go" else d.name))
        if label and label not in found:
            found[label] = {
                "name": label,
                "version": clean_version(d.version_spec),
                "ecosystem": d.ecosystem,
                "dev": d.is_dev,
            }
    return sorted(found.values(), key=lambda f: f["name"])


def _style_of(name: str) -> str | None:
    name = name.strip("_")
    if len(name) < 3:
        return None
    if re.fullmatch(r"[a-z][a-z0-9]*(_[a-z0-9]+)+", name):
        return "snake_case"
    if re.fullmatch(r"[a-z]+[a-z0-9]*([A-Z][a-z0-9]*)+", name):
        return "camelCase"
    if re.fullmatch(r"([A-Z][a-z0-9]+){2,}", name):
        return "PascalCase"
    if re.fullmatch(r"[A-Z][A-Z0-9]*(_[A-Z0-9]+)+", name):
        return "UPPER_SNAKE"
    return None


def _dominant(counter: Counter[str]) -> str | None:
    return counter.most_common(1)[0][0] if counter else None


def analyze_project(root: Path, policy: ExclusionPolicy | None = None) -> ProjectAnalysis:
    policy = policy or ExclusionPolicy()
    files = walk_project(root, policy)
    result = ProjectAnalysis(file_count=len(files))
    lang_files: Counter[str] = Counter()
    lang_lines: Counter[str] = Counter()
    deps: list[Dep] = []
    fn_styles: Counter[str] = Counter()
    class_styles: Counter[str] = Counter()
    indent: Counter[str] = Counter()
    quotes: Counter[str] = Counter()
    test_layout: Counter[str] = Counter()
    top_dirs: Counter[str] = Counter()
    sampled = 0
    for rel in files:
        top = rel.split("/", 1)[0] if "/" in rel else ""
        if top:
            top_dirs[top] += 1
        lang = detect_language(rel)
        full = root / rel
        if is_manifest(rel):
            with contextlib.suppress(OSError):
                deps.extend(parse_manifest(rel, full.read_text(encoding="utf-8", errors="replace")))
        if not lang or lang not in CODE_LANGUAGES:
            continue
        lang_files[lang] += 1
        try:
            data = full.read_bytes()
        except OSError:
            continue
        if len(data) > MAX_FILE_BYTES or policy.check_content(data).excluded:
            continue
        text = data.decode("utf-8", errors="replace")
        lang_lines[lang] += text.count("\n") + 1
        if file_role(rel) == "test":
            test_layout[rel.split("/", 1)[0] if "/" in rel else "(root)"] += 1
        if sampled < MAX_FILES_SAMPLED:
            sampled += 1
            for line in text.splitlines()[:300]:
                if line.startswith("\t"):
                    indent["tabs"] += 1
                elif m := re.match(r"^( +)\S", line):
                    n = len(m.group(1))
                    if n in (2, 4, 8):
                        indent[f"{n} spaces"] += 1
            if lang in {"python", "javascript", "typescript", "tsx", "php"}:
                quotes["single"] += len(re.findall(r"'[^'\n]{1,40}'", text))
                quotes["double"] += len(re.findall(r'"[^"\n]{1,40}"', text))
            st = extract_structure(lang, text)
            for s in st.symbols:
                style = _style_of(s.name)
                if not style:
                    continue
                if s.kind in {"function", "method"}:
                    fn_styles[style] += 1
                elif s.kind in {"class", "interface", "struct", "enum"}:
                    class_styles[style] += 1
    result.languages = {lang: {"files": lang_files[lang], "lines": lang_lines[lang]} for lang in lang_files}
    result.primary_language = max(lang_lines, key=lambda k: lang_lines[k]) if lang_lines else None
    result.dependencies = sorted(set(deps), key=lambda d: (d.ecosystem, d.name, d.source_file))
    result.frameworks = detect_frameworks(result.dependencies)
    result.conventions = {
        "function_naming": _dominant(fn_styles),
        "class_naming": _dominant(class_styles),
        "indentation": _dominant(indent),
        "quote_style": _dominant(quotes),
        "test_locations": [d for d, _ in test_layout.most_common(3)],
        "top_level_dirs": [d for d, _ in top_dirs.most_common(12)],
        "files_sampled": sampled,
    }
    return result
