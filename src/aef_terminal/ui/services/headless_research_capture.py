from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from aef_terminal.config import AppConfig
from aef_terminal.data.instrument_identity import (
    require_exact_instrument_id_sequence,
)
from aef_terminal.data.providers import route_instrument
from aef_terminal.indicators.registry import INDICATOR_REGISTRY
from aef_terminal.runtime.timeframes import interval_minutes
from aef_terminal.ui.market_indicator_params import server_alert_indicator_params
from aef_terminal.ui.services.market_analysis_store import (
    MARKET_ANALYSIS_EFFECTS_RESEARCH_CAPTURE,
)


RESEARCH_CAPTURE_INDICATOR_IDS = frozenset(
    {
        "channel_master",
        "option_reversal",
    }
)
RESEARCH_CAPTURE_DEFAULT_RANGE = "1d"
RESEARCH_CAPTURE_DEFAULT_SIGNAL_RANGE = "1d"
RESEARCH_CAPTURE_MAX_RECONCILE_SECONDS = 30.0

_LOGGER = logging.getLogger("aef_terminal.ui.headless_research_capture")


@dataclass(frozen=True)
class HeadlessResearchCaptureSettings:
    enabled: bool
    instrument_ids: tuple[str, ...]
    timeframe: str = "5m"
    reconcile_seconds: float = 15.0
    range_: str = RESEARCH_CAPTURE_DEFAULT_RANGE
    signal_range: str = RESEARCH_CAPTURE_DEFAULT_SIGNAL_RANGE

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise TypeError("research capture enabled state must be a boolean")
        exact_ids = require_exact_instrument_id_sequence(
            self.instrument_ids,
            allow_empty=True,
        )
        object.__setattr__(self, "instrument_ids", exact_ids)
        if self.enabled and not exact_ids:
            raise ValueError("enabled research capture requires exact instrument IDs")
        if (
            not isinstance(self.timeframe, str)
            or not self.timeframe
            or self.timeframe != self.timeframe.strip()
        ):
            raise ValueError("research capture timeframe must be exact non-empty text")
        interval_minutes(self.timeframe)
        if (
            not isinstance(self.reconcile_seconds, (int, float))
            or isinstance(self.reconcile_seconds, bool)
            or not 0 < float(self.reconcile_seconds) <= RESEARCH_CAPTURE_MAX_RECONCILE_SECONDS
        ):
            raise ValueError("research capture reconcile_seconds must be within (0, 30]")
        object.__setattr__(self, "reconcile_seconds", float(self.reconcile_seconds))
        for field_name, value in (
            ("range", self.range_),
            ("signal_range", self.signal_range),
        ):
            if not isinstance(value, str) or not value or value != value.strip():
                raise ValueError(f"research capture {field_name} must be exact non-empty text")

    @classmethod
    def from_config(cls, config: AppConfig) -> HeadlessResearchCaptureSettings:
        return cls(
            enabled=config.research_capture_enabled,
            instrument_ids=config.research_capture_instrument_ids,
            timeframe=config.research_capture_timeframe,
            reconcile_seconds=config.research_capture_reconcile_seconds,
        )


@dataclass(frozen=True)
class HeadlessResearchCaptureDeps:
    lookup_runtime_instrument: Callable[[str], dict[str, Any]]
    client_settings_snapshot: Callable[[], dict[str, Any]]
    build_analysis_payload: Callable[..., dict[str, Any]]
    analysis_key: Callable[[dict[str, Any]], str]
    register_wanted: Callable[[str, dict[str, Any], str, int], Awaitable[str]]
    renew_client_lease: Callable[[str, int, str, str, str], Awaitable[bool]]
    release_client_lease: Callable[[str, int], Awaitable[bool]]
    server_sleeping: Callable[[], bool]


@dataclass(frozen=True)
class _ResearchLease:
    client_id: str
    sequence: int
    analysis_key: str
    instrument_id: str
    route_fingerprint: str


def _research_client_id(instrument_id: str) -> str:
    digest = hashlib.sha256(instrument_id.encode("utf-8")).hexdigest()[:20]
    return f"research-capture:{digest}"


def _research_indicator_params(settings: Mapping[str, object]) -> dict[str, Any]:
    params = server_alert_indicator_params(settings)
    for indicator_id in INDICATOR_REGISTRY:
        params[indicator_id] = {
            "enabled": indicator_id in RESEARCH_CAPTURE_INDICATOR_IDS,
        }
    return params


class HeadlessResearchCaptureRuntime:
    """Keep exact research-only analysis demand alive without a browser lease."""

    def __init__(
        self,
        settings: HeadlessResearchCaptureSettings,
        deps: HeadlessResearchCaptureDeps,
    ) -> None:
        self.settings = settings
        self._deps = deps
        self._leases: dict[str, _ResearchLease] = {}
        self._cycles = 0
        self._registrations = 0
        self._renewals = 0
        self._releases = 0
        self._failures = 0
        self._last_cycle_at: str | None = None
        self._last_success_at: str | None = None
        self._errors: dict[str, str] = {}

    async def _release(self, instrument_id: str) -> None:
        lease = self._leases.pop(instrument_id, None)
        if lease is None:
            return
        released = await self._deps.release_client_lease(
            lease.client_id,
            lease.sequence,
        )
        if released:
            self._releases += 1

    async def _release_all(self) -> None:
        for instrument_id in tuple(self._leases):
            try:
                await self._release(instrument_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.exception(
                    "headless research lease release failed instrument_id=%s",
                    instrument_id,
                )

    async def _reconcile_instrument(
        self,
        instrument_id: str,
        *,
        indicator_params: dict[str, Any],
    ) -> None:
        instrument = self._deps.lookup_runtime_instrument(instrument_id)
        route = route_instrument(instrument)
        if route.instrument_id != instrument_id:
            raise ValueError("research capture route instrument identity mismatch")
        payload = self._deps.build_analysis_payload(
            source=route.provider,
            instrument_id=route.instrument_id,
            interval=self.settings.timeframe,
            range_=self.settings.range_,
            signal_range=self.settings.signal_range,
            show_visuals=False,
            indicator_params=indicator_params,
            instrument=route.instrument,
            analysis_effects=MARKET_ANALYSIS_EFFECTS_RESEARCH_CAPTURE,
        )
        analysis_key = self._deps.analysis_key(payload)
        current = self._leases.get(instrument_id)
        if (
            current is not None
            and current.analysis_key == analysis_key
            and current.route_fingerprint == route.fingerprint
        ):
            renewed = await self._deps.renew_client_lease(
                current.client_id,
                current.sequence,
                current.analysis_key,
                current.instrument_id,
                current.route_fingerprint,
            )
            if renewed:
                self._renewals += 1
                return

        sequence = current.sequence + 1 if current is not None else 1
        client_id = current.client_id if current is not None else _research_client_id(instrument_id)
        await self._deps.register_wanted(
            analysis_key,
            payload,
            client_id,
            sequence,
        )
        self._leases[instrument_id] = _ResearchLease(
            client_id=client_id,
            sequence=sequence,
            analysis_key=analysis_key,
            instrument_id=route.instrument_id,
            route_fingerprint=route.fingerprint,
        )
        self._registrations += 1

    async def reconcile_once(self) -> None:
        self._cycles += 1
        self._last_cycle_at = datetime.now(tz=UTC).isoformat()
        if not self.settings.enabled or self._deps.server_sleeping():
            await self._release_all()
            return
        try:
            settings = self._deps.client_settings_snapshot()
            if not isinstance(settings, dict):
                raise TypeError("client settings snapshot must be a mapping")
            indicator_params = _research_indicator_params(settings)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._failures += 1
            message = str(exc) or exc.__class__.__name__
            changed = self._errors.get("settings") != message
            self._errors["settings"] = message
            await self._release_all()
            if changed:
                _LOGGER.exception("headless research settings resolution failed")
            return

        desired = set(self.settings.instrument_ids)
        for stale_instrument_id in set(self._leases).difference(desired):
            await self._release(stale_instrument_id)
        successful = 0
        for instrument_id in self.settings.instrument_ids:
            try:
                await self._reconcile_instrument(
                    instrument_id,
                    indicator_params=indicator_params,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._failures += 1
                message = str(exc) or exc.__class__.__name__
                changed = self._errors.get(instrument_id) != message
                self._errors[instrument_id] = message
                await self._release(instrument_id)
                if changed:
                    _LOGGER.exception(
                        "headless research reconciliation failed instrument_id=%s",
                        instrument_id,
                    )
            else:
                self._errors.pop(instrument_id, None)
                successful += 1
        if successful == len(self.settings.instrument_ids):
            self._errors.pop("settings", None)
            self._last_success_at = datetime.now(tz=UTC).isoformat()

    async def run(self) -> None:
        try:
            while True:
                await self.reconcile_once()
                await asyncio.sleep(self.settings.reconcile_seconds)
        finally:
            await asyncio.shield(self._release_all())

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.settings.enabled,
            "effects": MARKET_ANALYSIS_EFFECTS_RESEARCH_CAPTURE,
            "instrument_ids": list(self.settings.instrument_ids),
            "timeframe": self.settings.timeframe,
            "range": self.settings.range_,
            "signal_range": self.settings.signal_range,
            "reconcile_seconds": self.settings.reconcile_seconds,
            "indicators": sorted(RESEARCH_CAPTURE_INDICATOR_IDS),
            "active_leases": [
                {
                    "instrument_id": lease.instrument_id,
                    "route_fingerprint": lease.route_fingerprint,
                    "analysis_key": lease.analysis_key,
                    "sequence": lease.sequence,
                }
                for lease in self._leases.values()
            ],
            "cycles": self._cycles,
            "registrations": self._registrations,
            "renewals": self._renewals,
            "releases": self._releases,
            "failures": self._failures,
            "last_cycle_at": self._last_cycle_at,
            "last_success_at": self._last_success_at,
            "errors": dict(self._errors),
        }


_RUNTIME: HeadlessResearchCaptureRuntime | None = None


def configure_headless_research_capture(
    settings: HeadlessResearchCaptureSettings,
    deps: HeadlessResearchCaptureDeps,
) -> None:
    global _RUNTIME
    _RUNTIME = HeadlessResearchCaptureRuntime(settings, deps)


def _runtime() -> HeadlessResearchCaptureRuntime:
    if _RUNTIME is None:
        raise RuntimeError("headless research capture runtime is not configured")
    return _RUNTIME


async def headless_research_capture_loop() -> None:
    await _runtime().run()


def headless_research_capture_status() -> dict[str, Any]:
    if _RUNTIME is None:
        return {"enabled": False, "configured": False}
    return {"configured": True, **_RUNTIME.status()}
