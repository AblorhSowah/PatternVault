"""Test plan: config.py
- defaults are valid and contain no secrets
- update() deep-merges, validates and persists atomically; invalid patches leave state untouched
- secrets cannot be stored in config.json (rejected on update AND on load)
- secret redaction can never be disabled
- non-loopback model host requires explicit opt-in; runtime settings only bind loopback
- corrupt config is quarantined, not silently destroyed
"""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import ConfigError, ConfigManager, Mode, Paths, PermissionLevel, RuntimeSettings, UserConfig

pytestmark = pytest.mark.unit


def test_defaults_are_safe():
    cfg = UserConfig()
    assert cfg.mode is Mode.LEARNING
    assert cfg.permission_level is PermissionLevel.ASSISTED
    assert cfg.privacy.redact_secrets is True
    assert cfg.cloud.consent == {}


def test_update_merges_persists_and_reloads(tmp_path: Path):
    path = tmp_path / "config.json"
    mgr = ConfigManager(path)
    mgr.update({"mode": "offline", "local_model": {"model": "qwen2.5-coder:7b"}})
    assert mgr.config.local_model.model == "qwen2.5-coder:7b"
    assert mgr.config.local_model.base_url == "http://127.0.0.1:11434"  # untouched sibling kept
    reloaded = ConfigManager(path)
    assert reloaded.config.mode is Mode.OFFLINE
    assert json.loads(path.read_text())["local_model"]["model"] == "qwen2.5-coder:7b"


def test_invalid_update_is_rejected_and_state_unchanged(tmp_path: Path):
    mgr = ConfigManager(tmp_path / "config.json")
    with pytest.raises(ConfigError):
        mgr.update({"agents": {"max_fix_attempts": 999}})
    assert mgr.config.agents.max_fix_attempts == 3


def test_unknown_keys_rejected(tmp_path: Path):
    mgr = ConfigManager(tmp_path / "config.json")
    with pytest.raises(ConfigError):
        mgr.update({"nonsense": 1})


@pytest.mark.parametrize(
    "patch",
    [
        {"cloud": {"api_key": "sk-abc123456789"}},
        {"local_model": {"token": "abcdef123456"}},
        {"providers": {"x": {"password": "hunter2hunter2"}}},
    ],
)
def test_secrets_cannot_be_stored(tmp_path: Path, patch):
    mgr = ConfigManager(tmp_path / "config.json")
    with pytest.raises(ConfigError):
        mgr.update(patch)
    assert not (tmp_path / "config.json").exists()


def test_secret_in_existing_file_is_rejected_on_load(tmp_path: Path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"cloud": {"api_key": "sk-live-1234567890abcdef"}}))
    with pytest.raises(ConfigError):
        ConfigManager(p)


def test_redaction_cannot_be_disabled(tmp_path: Path):
    mgr = ConfigManager(tmp_path / "config.json")
    with pytest.raises(ConfigError):
        mgr.update({"privacy": {"redact_secrets": False}})


def test_remote_model_host_requires_opt_in(tmp_path: Path):
    mgr = ConfigManager(tmp_path / "config.json")
    with pytest.raises(ConfigError):
        mgr.update({"local_model": {"base_url": "http://10.0.0.5:11434"}})
    mgr.update({"local_model": {"base_url": "http://10.0.0.5:11434", "allow_remote_host": True}})
    assert mgr.config.local_model.allow_remote_host


@pytest.mark.parametrize("url", ["http://127.0.0.1:11434", "http://localhost:11434", "http://[::1]:11434"])
def test_loopback_hosts_accepted(tmp_path: Path, url):
    ConfigManager(tmp_path / "c.json").update({"local_model": {"base_url": url}})


def test_runtime_settings_reject_public_bind():
    with pytest.raises(ValidationError):
        RuntimeSettings(host="0.0.0.0")


def test_corrupt_config_is_quarantined(tmp_path: Path):
    p = tmp_path / "config.json"
    p.write_text("{not json")
    with pytest.raises(ConfigError):
        ConfigManager(p)
    assert (tmp_path / "config.json.corrupt").read_text() == "{not json"


def test_paths_ensure_creates_layout(tmp_path: Path):
    paths = Paths(tmp_path / "h")
    paths.ensure()
    assert paths.blobs.is_dir() and paths.recovery.is_dir() and paths.backups.is_dir()


def test_reset_restores_defaults(tmp_path: Path):
    mgr = ConfigManager(tmp_path / "config.json")
    mgr.update({"mode": "hybrid"})
    assert mgr.reset().mode is Mode.LEARNING
