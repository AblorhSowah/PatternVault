"""Parse model output into files. Model output is UNTRUSTED: every path is validated before anything touches disk."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any


class PathRejected(ValueError):
    pass


@dataclass
class GeneratedFile:
    path: str
    content: str
    action: str = "unknown"  # create | modify | unknown


@dataclass
class ParsedOutput:
    files: list[GeneratedFile] = field(default_factory=list)
    explanation: str = ""
    dependencies: list[dict[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


_FILE_BLOCK = re.compile(
    r"<<<FILE\s+path\s*=\s*[\"']?(?P<path>[^\"'>\n]+?)[\"']?(?:\s+action\s*=\s*[\"']?(?P<action>[a-z]+)[\"']?)?\s*>>>\s*\n(?P<body>.*?)\n?<<<END FILE>>>",
    re.S | re.I,
)
_EXPLANATION = re.compile(r"<<<EXPLANATION>>>\s*(?P<t>.*?)\s*<<<END EXPLANATION>>>", re.S | re.I)
_DEPS = re.compile(r"<<<DEPENDENCIES>>>\s*(?P<t>.*?)\s*<<<END DEPENDENCIES>>>", re.S | re.I)
# Markdown fallback: a heading/bold/"File:" line naming a path, followed by a fenced block
_MD_BLOCK = re.compile(
    r"(?:^|\n)(?:#{1,6}\s*|\*\*|File:\s*|`)(?P<path>[\w./\\-]+\.[A-Za-z0-9]+)(?:\*\*|`)?[ \t]*:?[ \t]*\n+```[\w+-]*[ \t]*\n(?P<body>.*?)\n```",
    re.S,
)
_FENCE_PATH = re.compile(
    r"```[\w+-]*[ \t]*\n(?:#|//|/\*|<!--|--)\s*(?:path|file):\s*(?P<path>[\w./\\-]+)[^\n]*\n(?P<body>.*?)\n```",
    re.S | re.I,
)
_BAD_SEGMENTS = {".git", ".patternvault"}


def safe_relative_path(raw: str) -> str:
    """Validate and normalise a model-supplied path. Raises :class:`PathRejected`."""
    p = raw.strip().strip("`'\"").replace("\\", "/")
    if not p or "\x00" in p or len(p) > 300:
        raise PathRejected("empty or oversized path")
    if p.startswith(("/", "~")) or re.match(r"^[A-Za-z]:", p):
        raise PathRejected(f"absolute path not allowed: {p}")
    while p.startswith("./"):
        p = p[2:]
    parts = PurePosixPath(p).parts
    if not parts or any(seg in ("..", "") for seg in parts):
        raise PathRejected(f"path traversal not allowed: {p}")
    if any(seg in _BAD_SEGMENTS for seg in parts):
        raise PathRejected(f"protected path: {p}")
    if re.search(r"[<>|*?\"\n\r\t]", p):
        raise PathRejected(f"illegal characters in path: {p!r}")
    return PurePosixPath(*parts).as_posix()


def _clean_body(body: str) -> str:
    lines = body.split("\n")
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    text = "\n".join(lines).strip("\n")
    return text + "\n" if text else ""


def parse_model_output(text: str) -> ParsedOutput:
    out = ParsedOutput()
    seen: dict[str, int] = {}

    def add(path: str, body: str, action: str) -> None:
        try:
            safe = safe_relative_path(path)
        except PathRejected as exc:
            out.warnings.append(f"dropped file: {exc}")
            return
        gf = GeneratedFile(safe, _clean_body(body), action if action in ("create", "modify") else "unknown")
        if safe in seen:
            out.warnings.append(f"duplicate file '{safe}': using the last version")
            out.files[seen[safe]] = gf
        else:
            seen[safe] = len(out.files)
            out.files.append(gf)

    for m in _FILE_BLOCK.finditer(text):
        add(m.group("path"), m.group("body"), (m.group("action") or "").lower())
    if not out.files:  # fall back to markdown conventions that small models often produce
        for m in _FENCE_PATH.finditer(text):
            add(m.group("path"), m.group("body"), "unknown")
        if not out.files:
            for m in _MD_BLOCK.finditer(text):
                add(m.group("path"), m.group("body"), "unknown")
        if out.files:
            out.warnings.append("model did not use the requested file-block format; parsed markdown instead")
    if (e := _EXPLANATION.search(text)) is not None:
        out.explanation = e.group("t").strip()
    else:
        residue = _FILE_BLOCK.sub("", text)
        residue = re.sub(r"<<</?[A-Z ]+[^>]*>>>", "", residue).strip()
        out.explanation = residue[:2000] if out.files else ""
    if (d := _DEPS.search(text)) is not None:
        for line in d.group("t").splitlines():
            parts = line.strip(" -*\t").split()
            if len(parts) >= 2 and re.fullmatch(r"[A-Za-z0-9@_./-]{1,100}", parts[1]):
                out.dependencies.append(
                    {"ecosystem": parts[0].lower(), "name": parts[1], "version": " ".join(parts[2:])}
                )
    if not out.files:
        out.warnings.append("no files could be parsed from the model output")
    return out


def to_dicts(files: list[GeneratedFile]) -> list[dict[str, Any]]:
    return [{"path": f.path, "action": f.action, "content": f.content} for f in files]
