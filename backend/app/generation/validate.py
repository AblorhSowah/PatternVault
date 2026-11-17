"""Cheap, deterministic validation of generated files (the Review/Testing agents go deeper)."""

from __future__ import annotations

import ast
import json
from typing import Any

from app.extraction.language import PARSEABLE, detect_language
from app.extraction.structure import _parser  # tree-sitter parser cache (offline grammars)
from app.security.secrets import find_secrets

Issue = dict[str, Any]


def validate_file(path: str, content: str) -> list[Issue]:
    issues: list[Issue] = []
    if not content.strip():
        issues.append({"level": "error", "message": "file is empty", "path": path})
        return issues
    lang = detect_language(path)
    if lang == "python":
        try:
            ast.parse(content)
        except SyntaxError as exc:
            issues.append(
                {
                    "level": "error",
                    "message": f"Python syntax error line {exc.lineno}: {exc.msg}",
                    "path": path,
                    "line": exc.lineno,
                }
            )
    elif path.endswith(".json"):
        try:
            json.loads(content)
        except ValueError as exc:
            issues.append({"level": "error", "message": f"invalid JSON: {exc}", "path": path})
    elif lang in PARSEABLE:
        try:
            root = _parser(lang).parse(content.encode("utf-8", errors="replace")).root_node
            if root.has_error:
                issues.append({"level": "error", "message": f"{lang} syntax error detected by parser", "path": path})
        except Exception as exc:  # parser problems must not break generation
            issues.append({"level": "info", "message": f"could not parse {lang}: {exc}", "path": path})
    if "[REDACTED:" in content:
        issues.append(
            {
                "level": "warning",
                "message": "contains a [REDACTED:…] placeholder that must be replaced with a real value from configuration",
                "path": path,
            }
        )
    for s in find_secrets(content):
        issues.append(
            {
                "level": "error",
                "message": f"possible hard-coded secret ({s.kind}) on line {s.line}",
                "path": path,
                "line": s.line,
            }
        )
    return issues


def has_errors(issues: list[Issue]) -> bool:
    return any(i["level"] == "error" for i in issues)
