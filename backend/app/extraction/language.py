"""Language detection and file-role heuristics."""

from __future__ import annotations

import re
from pathlib import PurePosixPath

EXT_LANGUAGE: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".php": "php",
    ".go": "go",
    ".java": "java",
    ".rb": "ruby",
    ".rs": "rust",
    ".cs": "c_sharp",
    ".kt": "kotlin",
    ".swift": "swift",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".cc": "cpp",
    ".sql": "sql",
    ".sh": "shell",
    ".bash": "shell",
    ".html": "html",
    ".css": "css",
    ".scss": "css",
    ".vue": "vue",
    ".svelte": "svelte",
    ".json": "json",
    ".yml": "yaml",
    ".yaml": "yaml",
    ".toml": "toml",
    ".md": "markdown",
    ".blade.php": "php",
}

# Languages with a bundled tree-sitter grammar (see extraction/structure.py)
PARSEABLE = {"python", "javascript", "typescript", "tsx", "php", "go", "java", "ruby", "rust", "c_sharp"}
CODE_LANGUAGES = PARSEABLE | {"kotlin", "swift", "c", "cpp", "shell", "sql", "vue", "svelte"}


def detect_language(path: str) -> str | None:
    p = path.lower()
    if p.endswith(".blade.php"):
        return "php"
    return EXT_LANGUAGE.get(PurePosixPath(p).suffix)


_ROLE_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "test",
        re.compile(
            r"(^|/)(tests?|__tests__|spec|specs|e2e)(/|$)|(\.|_)(test|spec)\.[a-z]+$|(^|/)test_[^/]+\.py$|Test\.(php|java|cs)$"
        ),
    ),
    ("migration", re.compile(r"(^|/)(migrations?|alembic/versions|db/migrate)(/|$)|\d{4}_\d{2}_\d{2}_\d+_.*\.php$")),
    ("model", re.compile(r"(^|/)(models?|entities|entity|schemas?)(/|\.py$|\.ts$|\.php$)|(^|/)app/Models/")),
    (
        "controller",
        re.compile(r"(^|/)(controllers?|handlers?|views|resources)(/|\.py$)|Controller\.(php|java|ts|js|cs)$"),
    ),
    ("route", re.compile(r"(^|/)(routes?|routers?|urls)(/|\.py$|\.ts$|\.js$|\.php$)|(^|/)api\.(py|ts|js)$")),
    ("middleware", re.compile(r"(^|/)middlewares?(/|\.)")),
    ("service", re.compile(r"(^|/)(services?|usecases?|domain)(/|\.py$|\.ts$)|Service\.(php|java|ts|cs)$")),
    ("repository", re.compile(r"(^|/)(repositor(y|ies)|dao)(/|\.)|Repository\.(php|java|ts|cs)$")),
    (
        "config",
        re.compile(
            r"(^|/)(config|settings|configuration)(/|\.)|\.(config|rc)\.(js|ts|json|cjs|mjs)$|(^|/)(pyproject\.toml|package\.json|composer\.json|Cargo\.toml|go\.mod|Dockerfile|docker-compose\.ya?ml|\.env\.example)$"
        ),
    ),
    ("component", re.compile(r"(^|/)(components?|pages?|ui|widgets)(/)|\.(vue|svelte|tsx|jsx)$")),
    ("job", re.compile(r"(^|/)(jobs?|tasks?|workers?|queues?)(/|\.)")),
    ("util", re.compile(r"(^|/)(utils?|helpers?|lib|common|shared)(/|\.)")),
)


def file_role(path: str) -> str:
    p = path.replace("\\", "/")
    for role, pattern in _ROLE_RULES:
        if pattern.search(p):
            return role
    return "source"
