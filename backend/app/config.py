"""Configuration management.

Two layers:

* :class:`RuntimeSettings` - process-level settings from environment variables (home dir, port, token).
* :class:`UserConfig` - persistent, validated, user-editable configuration stored in
  ``<home>/config.json``. It deliberately contains **no secrets**: API keys live in VS Code SecretStorage and
  are supplied per-request. Any attempt to persist a secret-looking field is rejected.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
import threading
from enum import StrEnum
from ipaddress import ip_address
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.version import CONFIG_SCHEMA_VERSION

DEFAULT_PORT = 8765


class Mode(StrEnum):
    LEARNING = "learning"
    OFFLINE = "offline"
    HYBRID = "hybrid"


class PermissionLevel(StrEnum):
    READ_ONLY = "read_only"
    ASSISTED = "assisted"
    APPROVED_EXECUTION = "approved_execution"
    RESTRICTED_AUTONOMOUS = "restricted_autonomous"


class RuntimeSettings(BaseSettings):
    """Process-level settings (environment variables prefixed ``PATTERNVAULT_``)."""

    model_config = SettingsConfigDict(env_prefix="PATTERNVAULT_", extra="ignore")

    home: Path = Field(default_factory=lambda: Path.home() / ".patternvault")
    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    token: str | None = None
    log_level: str = "info"

    @field_validator("host")
    @classmethod
    def _loopback_only(cls, v: str) -> str:
        if v not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("PatternVault only binds to loopback addresses")
        return v


# --------------------------------------------------------------------------- user config


def is_loopback_url(url: str) -> bool:
    host = urlparse(url).hostname or ""
    if host == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


class LocalModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str = "http://127.0.0.1:11434"
    allow_remote_host: bool = False
    model: str | None = None
    embedding_model: str | None = None
    temperature: float = Field(default=0.2, ge=0, le=2)
    num_ctx: int = Field(default=8192, ge=512, le=262144)
    # Resource controls (passed to Ollama as options). None = let Ollama decide.
    num_thread: int | None = Field(default=None, ge=1, le=512)
    num_gpu: int | None = Field(default=None, ge=0, le=999)
    keep_alive: str = "5m"
    request_timeout_s: float = Field(default=300, ge=1, le=3600)

    @model_validator(mode="after")
    def _check_host(self) -> LocalModelConfig:
        if not is_loopback_url(self.base_url) and not self.allow_remote_host:
            raise ValueError(
                "local_model.base_url is not a loopback address; set allow_remote_host=true to confirm "
                "you accept sending code to that host"
            )
        return self


class CloudConfig(BaseModel):
    """Cloud usage policy. Keys are NOT stored here (VS Code SecretStorage only)."""

    model_config = ConfigDict(extra="forbid")

    # provider key -> explicit consent to transmit code/prompts to that external service
    consent: dict[str, bool] = Field(default_factory=dict)
    openai_compatible_base_url: str | None = None
    openai_compatible_model: str | None = None


class ProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True


class PrivacyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capture_enabled: bool = True
    exclude_globs: list[str] = Field(default_factory=list)  # user additions to built-in exclusions
    redact_secrets: bool = True
    allow_cross_project_retrieval: bool = True
    max_file_bytes: int = Field(default=512_000, ge=1_000, le=50_000_000)

    @field_validator("redact_secrets")
    @classmethod
    def _redaction_is_mandatory(cls, v: bool) -> bool:
        if not v:
            raise ValueError("secret redaction cannot be disabled")
        return v


class StorageConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vector_backend: str = Field(default="sqlite", pattern="^(sqlite|chroma)$")
    max_db_mb: int = Field(default=4096, ge=16)
    max_blobs_mb: int = Field(default=2048, ge=16)


class AgentsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_fix_attempts: int = Field(default=3, ge=0, le=10)
    max_parallel_steps: int = Field(default=4, ge=1, le=16)
    disabled: list[str] = Field(default_factory=list)
    step_timeout_s: float = Field(default=600, ge=1, le=7200)


class UserConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = CONFIG_SCHEMA_VERSION
    mode: Mode = Mode.LEARNING
    auto_fallback: bool = True
    permission_level: PermissionLevel = PermissionLevel.ASSISTED
    local_model: LocalModelConfig = Field(default_factory=LocalModelConfig)
    cloud: CloudConfig = Field(default_factory=CloudConfig)
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    privacy: PrivacyConfig = Field(default_factory=PrivacyConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    agents: AgentsConfig = Field(default_factory=AgentsConfig)


_SECRETISH_KEY = re.compile(r"(api[_-]?key|secret|password|passwd|token|credential|private[_-]?key)", re.I)


def _find_secretish_keys(data: Any, path: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(data, dict):
        for k, v in data.items():
            here = f"{path}.{k}" if path else str(k)
            if _SECRETISH_KEY.search(str(k)):
                found.append(here)
            found.extend(_find_secretish_keys(v, here))
    elif isinstance(data, list):
        for i, v in enumerate(data):
            found.extend(_find_secretish_keys(v, f"{path}[{i}]"))
    return found


class ConfigError(ValueError):
    """Raised when a configuration update is invalid."""


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class Paths:
    """Resolved filesystem layout under the PatternVault home directory."""

    def __init__(self, home: Path) -> None:
        self.home = home
        self.db = home / "patternvault.db"
        self.vectors = home / "vectors.db"
        self.blobs = home / "blobs"
        self.recovery = home / "recovery"
        self.backups = home / "backups"
        self.config = home / "config.json"
        self.logs = home / "logs"

    def ensure(self) -> None:
        for d in (self.home, self.blobs, self.recovery, self.backups, self.logs):
            d.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):  # best effort (e.g. Windows)
            os.chmod(self.home, 0o700)


class ConfigManager:
    """Loads, validates and atomically persists :class:`UserConfig`."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._config = self._load()

    @property
    def config(self) -> UserConfig:
        with self._lock:
            return self._config.model_copy(deep=True)

    def _load(self) -> UserConfig:
        if not self._path.exists():
            return UserConfig()
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            # Keep the unreadable file for the user to inspect rather than silently destroying it.
            backup = self._path.with_suffix(".json.corrupt")
            with contextlib.suppress(OSError):
                self._path.replace(backup)
            raise ConfigError(f"config.json is unreadable and was moved to {backup.name}: {exc}") from exc
        return self._parse(raw)

    @staticmethod
    def _parse(raw: dict[str, Any]) -> UserConfig:
        bad = _find_secretish_keys(raw)
        # ``consent`` maps provider names -> bool, and may legitimately contain e.g. "token-service"; only
        # reject when a value looks like an actual secret string.
        bad = [k for k in bad if _is_secret_value(_get_path(raw, k))]
        if bad:
            raise ConfigError(f"secrets must not be stored in config.json (found: {', '.join(bad)})")
        try:
            return UserConfig.model_validate(raw)
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc

    def update(self, patch: dict[str, Any]) -> UserConfig:
        """Deep-merge ``patch`` into the current config, validate, persist atomically."""
        with self._lock:
            merged = _deep_merge(self._config.model_dump(mode="json"), patch)
            new = self._parse(merged)
            self._write(new)
            self._config = new
            return new.model_copy(deep=True)

    def reset(self) -> UserConfig:
        with self._lock:
            new = UserConfig()
            self._write(new)
            self._config = new
            return new.model_copy(deep=True)

    def _write(self, cfg: UserConfig) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, prefix=".config-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(cfg.model_dump(mode="json"), fh, indent=2, sort_keys=True)
                fh.write("\n")
            os.replace(tmp, self._path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


def _is_secret_value(value: Any) -> bool:
    """Booleans/None/containers are configuration flags, not secrets; non-empty strings and numbers are."""
    if isinstance(value, bool) or value is None:
        return False
    return isinstance(value, str | int | float) and value != ""


def _get_path(data: Any, dotted: str) -> Any:
    cur = data
    for part in re.split(r"\.|\[|\]", dotted):
        if part == "":
            continue
        cur = cur[int(part)] if isinstance(cur, list) else cur.get(part) if isinstance(cur, dict) else None
        if cur is None:
            return None
    return cur
