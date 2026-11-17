"""Secret detection and redaction.

Everything that is persisted or embedded passes through :func:`redact` first. Detection is regex-based
(provider-specific token formats + generic ``key = "value"`` assignments). Obvious placeholders such as
``your_api_key_here`` or ``process.env.TOKEN`` are *not* redacted so that templates stay useful.
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

REDACTION_MARK = "[REDACTED:{kind}]"


@dataclass(frozen=True)
class Finding:
    kind: str
    line: int
    start: int
    end: int
    severity: str = "high"

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "line": self.line, "severity": self.severity}


@dataclass(frozen=True)
class RedactionResult:
    text: str
    findings: tuple[Finding, ...]

    @property
    def count(self) -> int:
        return len(self.findings)


@dataclass(frozen=True)
class _Rule:
    kind: str
    pattern: re.Pattern[str]
    group: int = 0  # which capture group holds the secret (0 = whole match)
    validator: Callable[[str], bool] | None = None
    severity: str = "high"


_PLACEHOLDER = re.compile(
    r"""(?ix)
    (
        .{0,3}(x{3,}|\*{3,}|\.{3,}|-{3,}).{0,3}                 # xxxx, ****, ...
      | <[^>]{1,60}>                                          # <API_KEY>
      | \$\{[^}]*\} | \$[A-Z_][A-Z0-9_]*                       # ${VAR}, $VAR
      | %\([^)]*\)s | %[A-Z_]+%                               # %(x)s, %VAR%
      | \{\{[^}]*\}\}                                          # {{ template }}
      | (your|my|the)[_\-\s]?(api|secret|access|auth|private|client)?[_\-\s]?(key|token|secret|password|pass)?[_\-\s]?(goes[_\-\s]?)?(here)?
      | change[_\-\s]?me\w{0,6} | replace[_\-\s]?(me|this|with)[\w\-]{0,30}
      | (example|sample|dummy|fake|placeholder|redacted|secret|password|passwd|token|none|null|undefined|empty|todo|tbd)[\w\-]{0,4}
      | process\.env\..* | os\.(environ|getenv).* | env\(.*
      | \[redacted.*\]
    )
    """
)


def _looks_like_placeholder(value: str) -> bool:
    v = value.strip().strip("'\"")
    if len(v) < 4:
        return True
    if _PLACEHOLDER.fullmatch(v):
        return True
    # e.g. getenv("X"), config.get("x"), settings.SECRET -> code expressions, not literals
    return bool(re.search(r"[(){}\[\]]", v) and re.search(r"[A-Za-z_]\w*\(", v))


def _not_placeholder(value: str) -> bool:
    return not _looks_like_placeholder(value)


_ASSIGN = re.compile(r"""(?P<key>[A-Za-z_][\w.\-]*)["']?\s*[:=]\s*(["'])(?P<val>[^"'\n]{6,})\2""")
# Unquoted env-style lines: export API_TOKEN=abc123...  (UPPER_CASE names only, to avoid code false positives)
_ENV_ASSIGN = re.compile(r"""(?m)^\s*(?:export\s+)?(?P<key>[A-Z][A-Z0-9_]*)\s*=\s*(?P<val>[^\s"'#$()]{8,})\s*$""")
_CONTINUES = re.compile(r"\s*(?:[+%*]|\.format\(|\.join\()")
_CODE_EXPR = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+")  # settings.SECRET_KEY: an attribute lookup, not a literal
_SECRET_WORDS = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "pass",
        "passphrase",
        "secret",
        "token",
        "credential",
        "credentials",
        "bearer",
        "apikey",
        "secretkey",
        "privatekey",
        "accesskey",
        "authtoken",
        "accesstoken",
        "refreshtoken",
        "clientsecret",
    }
)
_SECRET_PAIRS = frozenset(
    {
        ("api", "key"),
        ("access", "key"),
        ("private", "key"),
        ("secret", "key"),
        ("signing", "key"),
        ("encryption", "key"),
        ("auth", "key"),
        ("master", "key"),
        ("client", "secret"),
        ("db", "pass"),
    }
)


def _words(key: str) -> list[str]:
    out: list[str] = []
    for part in re.split(r"[._\-]", key):
        spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", part)
        out.extend(w.lower() for w in spaced.split() if w)
    return out


def _key_is_secret_name(key: str) -> bool:
    """True if an identifier is *named* like a secret: a whole word of it must be a secret word.

    ``tokenize``, ``passed``, ``bypass``, ``compass`` are not secret names; ``apiKey``, ``DB_PASSWORD``, ``access_token``,
    ``client-secret`` are.
    """
    words = _words(key)
    return any(w in _SECRET_WORDS for w in words) or any(p in _SECRET_PAIRS for p in itertools.pairwise(words))


def _assignment_spans(text: str) -> list[tuple[int, int, str, str]]:
    out: list[tuple[int, int, str, str]] = []
    for rx, kind in ((_ASSIGN, "credential-assignment"), (_ENV_ASSIGN, "env-assignment")):
        for m in rx.finditer(text):
            val = m.group("val")
            if val.startswith("[REDACTED:") or _looks_like_placeholder(val) or not _key_is_secret_name(m.group("key")):
                continue
            if kind == "credential-assignment" and (
                val.endswith(("-", "_", ":", "/")) or _CONTINUES.match(text, m.end())
            ):
                continue  # a prefix ("probe-token-") or a value built by concatenation/formatting is not a literal secret
            if kind == "env-assignment" and _CODE_EXPR.fullmatch(val):
                continue  # SECRET_KEY = settings.SECRET_KEY : an attribute lookup, not a literal
            out.append((m.start("val"), m.end("val"), kind, "high"))
    return out


_RULES: tuple[_Rule, ...] = (
    _Rule(
        "private-key",
        re.compile(
            r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----[\s\S]*?(?:-----END (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----|\Z)"
        ),
    ),
    _Rule("aws-access-key-id", re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA|ANVA|AIPA)[A-Z0-9]{16}\b")),
    _Rule(
        "aws-secret-access-key",
        re.compile(r"(?i)aws[_\-]?secret[_\-]?access[_\-]?key[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9/+=]{40})\b"),
        group=1,
    ),
    _Rule("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{22,})\b")),
    _Rule("gitlab-token", re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}\b")),
    _Rule("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}\b")),
    _Rule("stripe-key", re.compile(r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{16,}\b")),
    _Rule("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    _Rule("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b")),
    _Rule("openai-key", re.compile(r"\bsk-(?:proj-|live-|svcacct-)?[A-Za-z0-9_\-]{32,}\b")),
    _Rule("npm-token", re.compile(r"\bnpm_[A-Za-z0-9]{36}\b")),
    _Rule("sendgrid-key", re.compile(r"\bSG\.[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}\b")),
    _Rule("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{8,}\b")),
    _Rule(
        "bearer-token", re.compile(r"(?i)\bBearer\s+([A-Za-z0-9._~+/\-]{24,}=*)"), group=1, validator=_not_placeholder
    ),
    _Rule(
        "url-credentials",
        re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.\-]*://[^/\s:@'\"]+:([^/\s@'\"]{3,})@"),
        group=1,
        validator=_not_placeholder,
    ),
)


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def find_secrets(text: str) -> list[Finding]:
    """Locate secrets without modifying the text. Overlapping matches are merged (first/longest wins)."""
    spans: list[tuple[int, int, str, str]] = []
    for rule in _RULES:
        for m in rule.pattern.finditer(text):
            start, end = (m.start(rule.group), m.end(rule.group)) if rule.group else (m.start(), m.end())
            if start < 0 or start == end:
                continue
            value = text[start:end]
            if value.startswith("[REDACTED:"):
                continue
            if rule.validator and not rule.validator(value):
                continue
            spans.append((start, end, rule.kind, rule.severity))
    spans.extend(_assignment_spans(text))
    spans.sort(key=lambda s: (s[0], -(s[1] - s[0])))
    merged: list[tuple[int, int, str, str]] = []
    for s in spans:
        if merged and s[0] < merged[-1][1]:
            continue  # overlaps with an earlier (longer-or-equal) span
        merged.append(s)
    return [Finding(kind=k, line=_line_of(text, a), start=a, end=b, severity=sev) for a, b, k, sev in merged]


def redact(text: str) -> RedactionResult:
    """Replace every detected secret with ``[REDACTED:<kind>]``. Idempotent."""
    findings = find_secrets(text)
    if not findings:
        return RedactionResult(text, ())
    out: list[str] = []
    cursor = 0
    for f in findings:
        out.append(text[cursor : f.start])
        out.append(REDACTION_MARK.format(kind=f.kind))
        cursor = f.end
    out.append(text[cursor:])
    return RedactionResult("".join(out), tuple(findings))


def redact_obj(obj: Any) -> Any:
    """Recursively redact every string inside JSON-like structures (used for audit details, API echoes)."""
    if isinstance(obj, str):
        return redact(obj).text
    if isinstance(obj, dict):
        return {k: redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [redact_obj(v) for v in obj]
    return obj


def contains_secret(text: str) -> bool:
    return bool(find_secrets(text))
