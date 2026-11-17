"""Operating-mode policy: which AI backends may be used for a request, with automatic fallback."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from app.config import Mode
from app.generation.ollama import OllamaClient, OllamaError

CLOUD_PROVIDER = "openai-compatible"


class ModeViolation(RuntimeError):
    """The request needs a backend that the current mode/consent forbids."""


class NoBackendAvailable(RuntimeError):
    pass


@dataclass
class Availability:
    local_reachable: bool
    local_reason: str
    local_model: str | None
    local_model_installed: bool
    cloud_consent: bool
    cloud_configured: bool
    cloud_key_supplied: bool

    @property
    def local_ready(self) -> bool:
        return self.local_reachable and self.local_model_installed

    @property
    def cloud_ready(self) -> bool:
        return self.cloud_consent and self.cloud_configured and self.cloud_key_supplied

    def to_dict(self) -> dict[str, Any]:
        d = self.__dict__.copy()
        d.update(local_ready=self.local_ready, cloud_ready=self.cloud_ready)
        return d


@dataclass
class Resolution:
    backends: list[str]
    reason: str
    switched: bool = False
    warnings: list[str] = field(default_factory=list)


class ModeController:
    def __init__(self, config_getter: Any, audit: Any, events: Any) -> None:
        self._cfg = config_getter
        self._audit = audit
        self._events = events

    async def availability(self, cloud_key_supplied: bool = False, model: str | None = None) -> Availability:
        cfg = self._cfg()
        lm = cfg.local_model
        client = OllamaClient.from_config(lm)
        reachable, reason, installed = False, "", False
        chosen = model or lm.model
        try:
            await client.version(2.0)
            reachable = True
            names = {m.name for m in await client.list_models()}
            installed = bool(chosen) and (chosen in names or f"{chosen}:latest" in names)
            if not chosen:
                reason = "no local model selected"
            elif not installed:
                reason = f"model '{chosen}' is not installed in Ollama (pull it first)"
        except OllamaError as exc:
            reason = str(exc)
        return Availability(
            reachable,
            reason,
            chosen,
            installed,
            bool(cfg.cloud.consent.get(CLOUD_PROVIDER)),
            bool(cfg.cloud.openai_compatible_base_url and cfg.cloud.openai_compatible_model),
            cloud_key_supplied,
        )

    async def resolve(
        self, preference: str = "auto", cloud_key_supplied: bool = False, model: str | None = None
    ) -> Resolution:
        cfg = self._cfg()
        av = await self.availability(cloud_key_supplied, model)
        mode = cfg.mode
        if preference not in ("auto", "local", "cloud", "compare"):
            raise ValueError(f"unknown backend preference '{preference}'")
        if mode in (Mode.OFFLINE, Mode.LEARNING):
            if preference in ("cloud", "compare"):
                raise ModeViolation(f"cloud generation is disabled in {mode.value} mode")
            if not av.local_ready:
                raise NoBackendAvailable(f"local model unavailable: {av.local_reason or 'unknown'}")
            return Resolution(["local"], f"{mode.value} mode: local model only")
        # hybrid
        wants = {"auto": ["local", "cloud"], "local": ["local"], "cloud": ["cloud"], "compare": ["local", "cloud"]}[
            preference
        ]
        ready = [b for b in wants if (b == "local" and av.local_ready) or (b == "cloud" and av.cloud_ready)]
        if preference == "compare":
            if len(ready) == 2:
                return Resolution(ready, "hybrid compare: both backends")
            if ready and cfg.auto_fallback:
                return self._fallback(wants[0], ready, av, "compare needs both backends")
            raise NoBackendAvailable("compare needs both a local model and a consented, configured cloud endpoint")
        if preference == "auto":
            if av.local_ready:
                return Resolution(["local"], "hybrid auto: local preferred")
            if av.cloud_ready and cfg.auto_fallback:
                return self._fallback("local", ["cloud"], av, av.local_reason or "local unavailable")
            raise NoBackendAvailable(
                "no backend available: " + (av.local_reason or "local unavailable") + "; cloud not ready"
            )
        if ready:
            return Resolution(ready, f"hybrid: {preference}")
        other = [
            b
            for b in ("local", "cloud")
            if b != preference and ((b == "local" and av.local_ready) or (b == "cloud" and av.cloud_ready))
        ]
        if other and cfg.auto_fallback:
            return self._fallback(preference, other, av, f"{preference} backend unavailable")
        raise NoBackendAvailable(
            f"{preference} backend unavailable and automatic fallback is {'unavailable' if cfg.auto_fallback else 'off'}"
        )

    def _fallback(self, wanted: str, got: Sequence[str], av: Availability, why: str) -> Resolution:
        self._audit.record(
            "system", "mode.fallback", resource_type="config", detail={"wanted": wanted, "used": got, "why": why}
        )
        self._events.publish("mode.fallback", {"wanted": wanted, "used": got, "why": why})
        return Resolution(
            list(got),
            f"fell back to {', '.join(got)}: {why}",
            switched=True,
            warnings=[f"{wanted} was unavailable ({why}); used {', '.join(got)}"],
        )
