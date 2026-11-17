"""Path exclusion rules: what PatternVault may never read, capture, index or embed."""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import pathspec

# Directories that are never walked.
EXCLUDED_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        ".venv",
        "venv",
        "env",
        "__pycache__",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        ".tox",
        ".nox",
        "dist",
        "build",
        ".next",
        ".nuxt",
        ".gradle",
        "target",
        ".idea",
        ".vscode-test",
        "coverage",
        "htmlcov",
        ".terraform",
        ".cache",
        ".parcel-cache",
        ".patternvault",
        "site-packages",
        "bower_components",
        ".serverless",
        ".ssh",
        ".gnupg",
        ".aws",
    }
)

# Sensitive file globs (matched against basename and posix relative path). Never captured.
SENSITIVE_GLOBS: tuple[str, ...] = (
    ".env",
    ".env.*",
    "*.env",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.keystore",
    "*.kdbx",
    "*.asc",
    "*.gpg",
    "id_rsa*",
    "id_dsa*",
    "id_ecdsa*",
    "id_ed25519*",
    "*.ppk",
    "credentials",
    "credentials.*",
    "*credentials*.json",
    "service-account*.json",
    "*serviceaccount*.json",
    "secrets.*",
    "*.secrets",
    "secret.*",
    ".npmrc",
    ".pypirc",
    ".netrc",
    ".htpasswd",
    ".pgpass",
    ".git-credentials",
    "*.tfstate",
    "*.tfstate.*",
    "*.tfvars",
    "terraform.tfvars",
    "kubeconfig",
    ".kube/config",
    "wp-config.php",
    "local_settings.py",
    "appsettings.*.json",
)

# Allowed despite matching a sensitive glob (templates carry placeholders, and are still redacted).
SENSITIVE_ALLOW = (".env.example", ".env.sample", ".env.template", ".env.dist", ".env.defaults")

# Not sensitive, just not useful (or unsafe) to learn from.
LOW_VALUE_GLOBS: tuple[str, ...] = (
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "uv.lock",
    "Pipfile.lock",
    "composer.lock",
    "Cargo.lock",
    "go.sum",
    "*.min.js",
    "*.min.css",
    "*.map",
    "*.lock",
    "*.log",
    "*.sqlite",
    "*.sqlite3",
    "*.db",
    "*.db-wal",
    "*.db-shm",
    "*.pyc",
    "*.class",
    "*.o",
    "*.so",
    "*.dylib",
    "*.dll",
    "*.exe",
    "*.bin",
    "*.zip",
    "*.tar",
    "*.gz",
    "*.tgz",
    "*.7z",
    "*.rar",
    "*.jar",
    "*.war",
    "*.png",
    "*.jpg",
    "*.jpeg",
    "*.gif",
    "*.webp",
    "*.ico",
    "*.bmp",
    "*.tiff",
    "*.pdf",
    "*.mp3",
    "*.mp4",
    "*.mov",
    "*.avi",
    "*.woff",
    "*.woff2",
    "*.ttf",
    "*.eot",
    "*.otf",
    "*.vsix",
    "*.DS_Store",
)

_PRIVATE_KEY_MARKER = b"-----BEGIN "
_PRIVATE_KEY_WORD = b"PRIVATE KEY"


@dataclass(frozen=True)
class Exclusion:
    excluded: bool
    reason: str = ""
    category: str = ""  # sensitive | low_value | directory | user_rule | size | binary

    def __bool__(self) -> bool:
        return self.excluded


def _match_any(patterns: tuple[str, ...], rel_posix: str, base: str) -> bool:
    return any(fnmatch.fnmatch(base, p) or fnmatch.fnmatch(rel_posix, p) for p in patterns)


class ExclusionPolicy:
    """Decides whether a project-relative path may be read/captured."""

    def __init__(self, extra_globs: list[str] | None = None, max_file_bytes: int = 512_000) -> None:
        self.extra_globs = list(extra_globs or [])
        self.max_file_bytes = max_file_bytes
        self._user_spec = pathspec.GitIgnoreSpec.from_lines(self.extra_globs)

    def check_path(self, rel_path: str) -> Exclusion:
        rel = PurePosixPath(rel_path.replace("\\", "/"))
        parts = rel.parts
        if any(p in EXCLUDED_DIRS for p in parts[:-1]):
            return Exclusion(
                True, f"inside excluded directory ({next(p for p in parts if p in EXCLUDED_DIRS)})", "directory"
            )
        base = rel.name
        posix = rel.as_posix()
        if base in SENSITIVE_ALLOW:
            pass
        elif _match_any(SENSITIVE_GLOBS, posix, base):
            return Exclusion(True, "sensitive file (credentials/keys/env)", "sensitive")
        if _match_any(LOW_VALUE_GLOBS, posix, base):
            return Exclusion(True, "binary, generated or low-value file", "low_value")
        if self.extra_globs and self._user_spec.match_file(posix):
            return Exclusion(True, "matches a user exclusion rule", "user_rule")
        return Exclusion(False)

    def check_content(self, data: bytes) -> Exclusion:
        """Content-level checks: size, binary, private key material."""
        if len(data) > self.max_file_bytes:
            return Exclusion(True, f"file larger than {self.max_file_bytes} bytes", "size")
        if b"\x00" in data[:8192]:
            return Exclusion(True, "binary content", "binary")
        head = data[:200_000]
        if _PRIVATE_KEY_MARKER in head and _PRIVATE_KEY_WORD in head:
            return Exclusion(True, "contains private key material", "sensitive")
        return Exclusion(False)


def walk_project(root: Path, policy: ExclusionPolicy, gitignore: bool = True) -> list[str]:
    """List project-relative posix paths that pass the exclusion policy (honours ``.gitignore``)."""
    spec = None
    if gitignore and (root / ".gitignore").is_file():
        try:
            spec = pathspec.GitIgnoreSpec.from_lines((root / ".gitignore").read_text(errors="ignore").splitlines())
        except OSError:
            spec = None
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIRS]
        for name in filenames:
            full = Path(dirpath) / name
            if full.is_symlink():
                continue
            rel = full.relative_to(root).as_posix()
            if spec and spec.match_file(rel):
                continue
            if not policy.check_path(rel).excluded:
                out.append(rel)
    return sorted(out)
