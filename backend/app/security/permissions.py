"""Central permission gate for every file operation, command and dependency install.

Modes (least → most autonomous):

* ``read_only``            - reads only; every mutation is denied.
* ``assisted``             - every mutation needs explicit per-operation approval.
* ``approved_execution``   - once a *plan* is approved, safe in-project writes and allow-listed commands run
                             without further prompts; anything else still needs approval.
* ``restricted_autonomous``- safe in-project writes and allow-listed commands run on their own; installs,
                             sensitive files, deletion of pre-existing files and outward-facing commands
                             still need approval.

Regardless of mode: paths outside the project root, ``.git`` internals and destructive commands are
**denied**, never merely prompted.
"""

from __future__ import annotations

import fnmatch
import os
import re
import shlex
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path, PurePosixPath

from app.config import PermissionLevel


class Decision(StrEnum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


@dataclass(frozen=True)
class Verdict:
    decision: Decision
    reason: str
    rule: str = ""

    @property
    def allowed(self) -> bool:
        return self.decision is Decision.ALLOW

    @property
    def denied(self) -> bool:
        return self.decision is Decision.DENY


def _allow(reason: str, rule: str = "") -> Verdict:
    return Verdict(Decision.ALLOW, reason, rule)


def _ask(reason: str, rule: str = "") -> Verdict:
    return Verdict(Decision.REQUIRE_APPROVAL, reason, rule)


def _deny(reason: str, rule: str = "") -> Verdict:
    return Verdict(Decision.DENY, reason, rule)


# Files whose modification can change execution/deployment/security posture → always need approval.
SENSITIVE_WRITE_GLOBS: tuple[str, ...] = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "id_rsa*",
    ".npmrc",
    ".pypirc",
    ".netrc",
    ".github/workflows/*",
    ".github/actions/*",
    ".gitlab-ci.yml",
    "Jenkinsfile",
    "azure-pipelines.yml",
    ".circleci/*",
    "Dockerfile",
    "Dockerfile.*",
    "docker-compose*.yml",
    "docker-compose*.yaml",
    "*.tf",
    "*.tfvars",
    "package.json",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "requirements*.txt",
    "Pipfile",
    "poetry.lock",
    "uv.lock",
    "composer.json",
    "composer.lock",
    "Cargo.toml",
    "go.mod",
    ".pre-commit-config.yaml",
    ".gitignore",
    ".gitattributes",
    "Makefile",
    "*.sh",
    "*.bat",
    "*.ps1",
    ".vscode/settings.json",
    ".vscode/tasks.json",
    ".vscode/launch.json",
    "CODEOWNERS",
    "*.pfx",
    "*.p12",
    "*.keystore",
)


def is_sensitive_write_path(rel_path: str) -> bool:
    rel = PurePosixPath(rel_path.replace("\\", "/")).as_posix()
    base = rel.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(base, g) or fnmatch.fnmatch(rel, g) for g in SENSITIVE_WRITE_GLOBS)


# --------------------------------------------------------------------------------- commands

_DESTRUCTIVE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "rm-recursive-or-force",
        re.compile(r"(^|[\s;&|(`])rm\s+(-[a-zA-Z]*[rRfF][a-zA-Z]*|--recursive|--force|--no-preserve-root)\b"),
    ),
    ("rm-root-or-home", re.compile(r"(^|[\s;&|(`])rm\s+.*(\s/(\s|$)|\s~(/|\s|$)|\s\$HOME|\s/\*|\s\*(\s|$))")),
    ("git-hard-reset", re.compile(r"\bgit\s+reset\s+.*--hard\b")),
    ("git-clean", re.compile(r"\bgit\s+clean\b.*-[a-zA-Z]*[fdx]")),
    ("git-force-push", re.compile(r"\bgit\s+push\b.*(--force\b|--force-with-lease\b|\s-f\b|\s\+\w)")),
    ("git-discard-all", re.compile(r"\bgit\s+(checkout|restore)\s+(--\s+)?\.(\s|$)")),
    ("git-branch-delete", re.compile(r"\bgit\s+branch\s+-D\b")),
    ("disk-wipe", re.compile(r"(^|[\s;&|])(dd|mkfs(\.\w+)?|fdisk|parted|shred|wipefs|diskutil\s+erase\w*)\b")),
    ("shutdown", re.compile(r"(^|[\s;&|])(shutdown|reboot|halt|poweroff|init\s+0)\b")),
    ("sudo", re.compile(r"(^|[\s;&|(])(sudo|doas|su)\s")),
    ("chmod-chown-recursive", re.compile(r"\b(chmod|chown|chgrp)\s+(-[a-zA-Z]*R|--recursive)")),
    ("pipe-to-shell", re.compile(r"\b(curl|wget|fetch)\b[^|;]*\|\s*(sudo\s+)?(ba|z|da|k)?sh\b")),
    ("fork-bomb", re.compile(r":\(\)\s*\{")),
    (
        "sql-destructive",
        re.compile(r"(?i)\b(drop\s+(table|database|schema)|truncate\s+table|delete\s+from\s+\w+\s*(;|$))"),
    ),
    ("find-delete", re.compile(r"\bfind\b.*(\s-delete\b|-exec\s+rm\b)")),
    ("redirect-to-device", re.compile(r">\s*/dev/(sd|nvme|disk|hd)\w*")),
    ("kill-all", re.compile(r"(^|[\s;&|])(killall|pkill)\s|\bkill\s+-9\s+-1\b")),
    ("terraform-destroy", re.compile(r"\bterraform\s+(destroy|apply)\b")),
    ("truncate-file", re.compile(r"(^|[\s;&|])truncate\s")),
    (
        "windows-delete",
        re.compile(r"(?i)(^|[\s;&|])(del\s+/[sfq]|rd\s+/s|rmdir\s+/s|format\s+[a-z]:|Remove-Item\s+.*-Recurse)"),
    ),
)

# Leading executables that are outward-facing/production-affecting: allowed only with approval.
_OUTWARD = frozenset(
    {
        "kubectl",
        "helm",
        "aws",
        "gcloud",
        "az",
        "ssh",
        "scp",
        "rsync",
        "ftp",
        "sftp",
        "docker",
        "podman",
        "twine",
        "gh",
        "vercel",
        "netlify",
        "heroku",
        "fly",
        "flyctl",
        "pulumi",
        "ansible",
        "ansible-playbook",
        "vsce",
        "ovsx",
    }
)
_OUTWARD_SUBCOMMANDS = (
    re.compile(r"\bnpm\s+(publish|unpublish|deprecate|login|adduser|token)\b"),
    re.compile(r"\byarn\s+(publish|npm\s+publish)\b"),
    re.compile(r"\bpnpm\s+publish\b"),
    re.compile(r"\bgit\s+push\b"),
    re.compile(r"\bgit\s+remote\s+(add|set-url)\b"),
    re.compile(r"\bcomposer\s+(publish|config)\b"),
    re.compile(r"\bcargo\s+(publish|login)\b"),
)

_SHELL_WRAPPERS = frozenset(
    {
        "sh",
        "bash",
        "zsh",
        "dash",
        "ksh",
        "fish",
        "cmd",
        "powershell",
        "pwsh",
        "env",
        "xargs",
        "nohup",
        "time",
        "timeout",
        "nice",
    }
)
_INLINE_CODE = {"python": "-c", "python3": "-c", "node": "-e", "ruby": "-e", "perl": "-e", "php": "-r"}

# (executable, required leading args or None) → safe by themselves
_SAFE_COMMANDS: tuple[tuple[str, tuple[str, ...] | None], ...] = (
    ("pytest", None),
    ("py.test", None),
    ("ruff", None),
    ("mypy", None),
    ("bandit", None),
    ("pip-audit", None),
    ("tsc", None),
    ("eslint", None),
    ("vitest", None),
    ("jest", None),
    ("phpunit", None),
    ("pest", None),
    ("flake8", None),
    ("pylint", None),
    ("black", ("--check",)),
    ("prettier", ("--check",)),
    ("go", ("test",)),
    ("go", ("vet",)),
    ("go", ("build",)),
    ("cargo", ("test",)),
    ("cargo", ("check",)),
    ("cargo", ("clippy",)),
    ("cargo", ("build",)),
    ("composer", ("test",)),
    ("composer", ("validate",)),
    ("npm", ("test",)),
    ("npm", ("audit",)),
    ("npm", ("ls",)),
    ("npm", ("run", "test")),
    ("npm", ("run", "lint")),
    ("npm", ("run", "build")),
    ("npm", ("run", "typecheck")),
    ("npm", ("run", "check")),
    ("npm", ("run", "compile")),
    ("pnpm", ("test",)),
    ("pnpm", ("run", "test")),
    ("pnpm", ("run", "lint")),
    ("pnpm", ("run", "build")),
    ("yarn", ("test",)),
    ("yarn", ("lint",)),
    ("yarn", ("build",)),
    ("npx", ("vitest",)),
    ("npx", ("jest",)),
    ("npx", ("tsc",)),
    ("npx", ("eslint",)),
    ("git", ("status",)),
    ("git", ("diff",)),
    ("git", ("log",)),
    ("git", ("show",)),
    ("git", ("rev-parse",)),
    ("git", ("ls-files",)),
    ("git", ("branch",)),
    ("git", ("check-ignore",)),
    ("git", ("blame",)),
    ("uv", ("run", "pytest")),
    ("uv", ("run", "ruff")),
    ("uv", ("run", "mypy")),
    ("uv", ("run", "bandit")),
    ("python", ("-m", "pytest")),
    ("python3", ("-m", "pytest")),
    ("python", ("-m", "ruff")),
    ("python3", ("-m", "ruff")),
    ("python", ("-m", "mypy")),
    ("python3", ("-m", "mypy")),
    ("python", ("-m", "bandit")),
    ("python3", ("-m", "bandit")),
    ("python", ("-m", "pip_audit")),
    ("python", ("-m", "unittest")),
    ("python3", ("-m", "unittest")),
    ("python", ("-m", "compileall")),
    ("python3", ("-m", "compileall")),
    ("php", ("artisan", "test")),
)

_INSTALL_PATTERNS = (
    re.compile(r"^(pip|pip3|uv\s+pip|uv\s+add|poetry\s+add|pipenv\s+install)(\s|$)"),
    re.compile(r"^python3?\s+-m\s+pip\s+install\b"),
    re.compile(r"^(npm|pnpm|yarn|bun)\s+(install|i|add|ci)\b"),
    re.compile(r"^composer\s+(require|install|update)\b"),
    re.compile(r"^cargo\s+(add|install)\b"),
    re.compile(r"^go\s+(get|install)\b"),
    re.compile(r"^(brew|apt|apt-get|yum|dnf|pacman|choco|winget)\s+install\b"),
)


def _is_trusted_script(arg: str) -> bool:
    try:
        p = Path(arg).resolve()
    except (OSError, RuntimeError):
        return False
    return p.suffix == ".py" and any(d in p.parents for d in TRUSTED_SCRIPT_DIRS)


def split_command(command: str | list[str]) -> list[str]:
    if isinstance(command, list):
        return [str(c) for c in command]
    try:
        return shlex.split(command, posix=os.name != "nt")
    except ValueError:
        return command.split()


def command_text(command: str | list[str]) -> str:
    return command if isinstance(command, str) else " ".join(shlex.quote(c) for c in command)


def classify_command(command: str | list[str]) -> Verdict:
    """Classify a command independent of the permission mode: DENY / REQUIRE_APPROVAL / ALLOW(safe)."""
    text = command_text(command).strip()
    if not text:
        return _deny("empty command", "empty")
    for name, pattern in _DESTRUCTIVE_PATTERNS:
        if pattern.search(text):
            return _deny(f"destructive command blocked ({name})", f"destructive:{name}")
    argv = split_command(command)
    exe = Path(argv[0]).name if argv else ""
    if re.fullmatch(r"python[\d.]*", exe):
        exe = "python"  # python3, python3.13, ... all behave like `python` for allow-listing
        if len(argv) > 1 and _is_trusted_script(argv[1]) and not re.search(r"[;&|<>`$]", text):
            return _allow("PatternVault's own verification harness", "trusted-harness")
    # Unwrap shell wrappers and re-check the inner text.
    if exe in _SHELL_WRAPPERS:
        inner = " ".join(argv[1:])
        for name, pattern in _DESTRUCTIVE_PATTERNS:
            if pattern.search(inner):
                return _deny(f"destructive command blocked inside wrapper ({name})", f"destructive:{name}")
        return _ask(f"shell wrapper '{exe}' can run arbitrary code", "shell-wrapper")
    if exe in _INLINE_CODE and _INLINE_CODE[exe] in argv[1:]:
        return _ask(f"inline code execution via '{exe} {_INLINE_CODE[exe]}'", "inline-code")
    if exe in _OUTWARD or any(p.search(text) for p in _OUTWARD_SUBCOMMANDS):
        return _ask("outward-facing or production-affecting command", "outward")
    if any(p.search(text) for p in _INSTALL_PATTERNS):
        return _ask("installs dependencies", "install")
    rest = argv[1:]
    for safe_exe, lead in _SAFE_COMMANDS:
        if exe == safe_exe and (lead is None or tuple(rest[: len(lead)]) == lead):
            # Reject output redirection / chaining smuggled via a single-string command
            if isinstance(command, str) and re.search(r"[;&|<>`$]|\$\(", text):
                return _ask("command contains shell operators", "shell-operators")
            return _allow("allow-listed verification command", "safe-command")
    return _ask(f"'{exe}' is not on the allow-list", "unknown-command")


# --------------------------------------------------------------------------------- policy


TRUSTED_SCRIPT_DIRS: tuple[Path, ...] = (Path(__file__).resolve().parents[1] / "testing" / "harness",)


@dataclass
class PermissionPolicy:
    """Evaluates operations for one project under one permission level."""

    level: PermissionLevel
    project_root: Path
    plan_approved: bool = False
    created_paths: set[str] = field(default_factory=set)  # paths created by the current task (safe to delete)

    def __post_init__(self) -> None:
        self.project_root = Path(self.project_root).resolve()

    # -- path helpers
    def resolve_inside(self, rel_or_abs: str | Path) -> tuple[Path | None, str]:
        """Resolve to a real path inside the project root, rejecting traversal and symlink escapes."""
        p = Path(rel_or_abs)
        candidate = p if p.is_absolute() else self.project_root / p
        try:
            # resolve() follows symlinks for the existing part of the path
            real = candidate.resolve()
        except (OSError, RuntimeError):
            return None, "path could not be resolved"
        try:
            real.relative_to(self.project_root)
        except ValueError:
            return None, "path is outside the project root"
        return real, ""

    def rel(self, real: Path) -> str:
        return real.relative_to(self.project_root).as_posix()

    # -- decisions
    def check_read(self, path: str | Path) -> Verdict:
        real, why = self.resolve_inside(path)
        if real is None:
            return _deny(why, "outside-root")
        return _allow("read within project", "read")

    def check_write(self, path: str | Path, *, creating: bool = False) -> Verdict:
        if self.level is PermissionLevel.READ_ONLY:
            return _deny("read-only mode", "read-only")
        real, why = self.resolve_inside(path)
        if real is None:
            return _deny(why, "outside-root")
        rel = self.rel(real)
        if rel == ".git" or rel.startswith(".git/"):
            return _deny("writing to .git internals is not allowed", "git-internals")
        if rel == ".patternvault" or rel.startswith(".patternvault/"):
            return _deny("PatternVault's own data directory is protected", "own-data")
        if is_sensitive_write_path(rel):
            return _ask(f"'{rel}' is a sensitive configuration file", "sensitive-file")
        if self.level is PermissionLevel.ASSISTED:
            return _ask("assisted mode: approve each write", "assisted")
        if self.level is PermissionLevel.APPROVED_EXECUTION and not self.plan_approved:
            return _ask("plan has not been approved", "plan-unapproved")
        return _allow("in-project write permitted", "in-project-write")

    def check_delete(self, path: str | Path) -> Verdict:
        if self.level is PermissionLevel.READ_ONLY:
            return _deny("read-only mode", "read-only")
        real, why = self.resolve_inside(path)
        if real is None:
            return _deny(why, "outside-root")
        rel = self.rel(real)
        if rel in ("", ".") or rel == ".git" or rel.startswith(".git/"):
            return _deny("refusing to delete the project root or .git", "protected-path")
        if real.is_dir():
            return _ask("deleting a directory requires approval", "delete-dir")
        if rel in self.created_paths and self.level in (
            PermissionLevel.APPROVED_EXECUTION,
            PermissionLevel.RESTRICTED_AUTONOMOUS,
        ):
            return _allow("file was created by this task", "delete-own-file")
        return _ask("deleting a pre-existing file requires approval", "delete-existing")

    def check_command(self, command: str | list[str]) -> Verdict:
        if self.level is PermissionLevel.READ_ONLY:
            # Pure read-only git inspection is harmless even in read-only mode.
            base = classify_command(command)
            argv = split_command(command)
            if base.decision is Decision.ALLOW and argv and Path(argv[0]).name == "git":
                return base
            return _deny("read-only mode", "read-only") if base.decision is not Decision.DENY else base
        base = classify_command(command)
        if base.decision is not Decision.ALLOW:
            return base
        if self.level is PermissionLevel.ASSISTED:
            return _ask("assisted mode: approve each command", "assisted")
        if self.level is PermissionLevel.APPROVED_EXECUTION and not self.plan_approved:
            return _ask("plan has not been approved", "plan-unapproved")
        return base

    def check_install(self, ecosystem: str, package: str) -> Verdict:
        if self.level is PermissionLevel.READ_ONLY:
            return _deny("read-only mode", "read-only")
        if not re.fullmatch(r"[@A-Za-z0-9_.\-/]{1,214}", package):
            return _deny("invalid package name", "bad-package-name")
        return _ask(f"install {ecosystem} package '{package}'", "install")

    def check_external_transmission(self, consent: bool) -> Verdict:
        if not consent:
            return _deny("no consent recorded for sending data to this external service", "no-consent")
        return _allow("explicit consent recorded", "consent")
