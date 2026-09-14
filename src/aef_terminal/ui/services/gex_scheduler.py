from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from aef_terminal.config import AppConfig
from aef_terminal.data.gex.constants import (
    GEX_CHART_HISTORY_HOURS,
    GEX_CONTEXT_MAX_LEVELS,
    GEX_CONTEXT_STALE_MINUTES,
)
from aef_terminal.data.provider_sessions import provider_schedule_open_state
from aef_terminal.data.gex.utils import parse_gex_timestamp
from aef_terminal.data.providers import route_instrument
from aef_terminal.data.provider_contract import InstrumentRoute
from aef_terminal.runtime.async_tasks import (
    run_cancellation_deferred,
    run_physical_thread_call,
)
from aef_terminal.settings_contract import GEX_SCHEDULER_SETTING_KEY
from aef_terminal.ui.runtime_services import (
    gex_scheduler_runtime_settings as build_gex_scheduler_runtime_settings,
)


_GEX_SCHEDULER_MAX_ATTEMPTS = 2
_GEX_SCHEDULER_RETRY_SECONDS = 30.0


@dataclass
class GexSchedulerRouteState:
    due_at: datetime
    attempts: int = 0
    retry_at: datetime | None = None
    completed: bool = False


async def _run_gex_scheduler_attempt(
    *,
    route: InstrumentRoute,
    job: Any,
    store: Any,
    state: GexSchedulerRouteState,
    logger_warning: Callable[[str, Any, BaseException], None],
) -> None:
    async def request_and_acknowledge() -> None:
        state.attempts += 1
        try:
            if not route.adapter.capabilities.gex:
                raise ValueError(
                    f"GEX_PROVIDER_UNSUPPORTED provider={route.provider} "
                    f"instrument_key={route.instrument_key}"
                )
            request = await run_physical_thread_call(
                route.adapter.gex_request_for_job,
                route.instrument,
                job,
                store=store,
            )
            result = await route.adapter.async_load_gex(
                route.instrument,
                enabled=True,
                refresh=True,
                max_levels=GEX_CONTEXT_MAX_LEVELS,
                stale_minutes=GEX_CONTEXT_STALE_MINUTES,
                history_hours=GEX_CHART_HISTORY_HOURS,
                project_capture_history=False,
                request=request,
                store=store,
            )
            if isinstance(result, dict) and result.get("status") == "unavailable":
                backoff = result.get("backoff") if isinstance(result.get("backoff"), dict) else {}
                retry_at = parse_gex_timestamp(backoff.get("retry_at"))
                if (
                    backoff.get("reason") == "OPTION_UNIVERSE_ROLLOVER"
                    and backoff.get("active")
                    and retry_at is not None
                    and state.attempts < _GEX_SCHEDULER_MAX_ATTEMPTS
                ):
                    state.retry_at = retry_at.astimezone(UTC)
                    return
                state.completed = True
                state.retry_at = None
                return
            result_diagnostics = (
                result.get("diagnostics")
                if isinstance(result, dict) and isinstance(result.get("diagnostics"), dict)
                else {}
            )
            if (
                isinstance(result, dict)
                and result.get("status") == "partial"
                and result_diagnostics.get("diag_frame_quality_code")
                == "UNDERLYING_SPOT_OBSERVATION_INCOMPLETE"
            ):
                state.completed = True
                state.retry_at = None
                return
            if (
                not isinstance(result, dict)
                or result.get("ok") is False
                or str(result.get("status") or "")
                in {
                    "error",
                    "missing",
                    "refresh_error",
                    "timeout",
                }
            ):
                backoff = result.get("backoff") if isinstance(result, dict) else {}
                if isinstance(backoff, dict) and backoff.get("active"):
                    state.completed = True
                    state.retry_at = None
                    return
                raise RuntimeError(
                    str(
                        result.get("message")
                        or result.get("last_error")
                        or "GEX scheduler refresh failed"
                    )
                    if isinstance(result, dict)
                    else "GEX scheduler refresh returned no payload"
                )
            state.completed = True
            state.retry_at = None
        except Exception as exc:
            if state.attempts < _GEX_SCHEDULER_MAX_ATTEMPTS:
                state.retry_at = datetime.now(tz=UTC) + timedelta(
                    seconds=_GEX_SCHEDULER_RETRY_SECONDS
                )
            else:
                state.completed = True
            logger_warning("GEX scheduler %s failed: %s", job.provider_symbol, exc)

    await run_cancellation_deferred(
        request_and_acknowledge(),
        task_cancelled_error="GEX_SCHEDULER_ATTEMPT_TASK_CANCELLED",
    )


class GexSchedulerSettingsRuntime:
    """Own the durable scheduler setting and its process-local projection."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._store_factory: Callable[[], Any] | None = None
        self._config_factory: Callable[[], AppConfig] = AppConfig
        self._settings: dict[str, Any] | None = None
        self._settings_revision: int | None = None

    def configure(
        self,
        *,
        store_factory: Callable[[], Any] | None,
        config_factory: Callable[[], AppConfig] = AppConfig,
    ) -> None:
        with self._lock:
            self._store_factory = store_factory
            self._config_factory = config_factory
            self._settings = None
            self._settings_revision = None

    @staticmethod
    def _require_revision(value: Any) -> int:
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or not 0 <= value <= 9_007_199_254_740_991
        ):
            raise ValueError("GEX_SCHEDULER_SETTINGS_REVISION_INVALID")
        return value

    def _publish_committed_locked(
        self,
        raw_setting: Any,
        *,
        settings_revision: int,
    ) -> dict[str, Any]:
        revision = self._require_revision(settings_revision)
        config = self._config_factory()
        settings = build_gex_scheduler_runtime_settings(
            env_enabled=bool(config.gex_scheduler_enabled),
            env_instrument_ids=config.gex_scheduler_instrument_ids,
            raw_setting=raw_setting,
        )
        if self._settings_revision is not None and revision < self._settings_revision:
            if self._settings is None:
                raise RuntimeError("GEX_SCHEDULER_SETTINGS_RUNTIME_UNAVAILABLE")
            return deepcopy(self._settings)
        if self._settings_revision == revision and self._settings != settings:
            raise RuntimeError("GEX_SCHEDULER_SETTINGS_REVISION_MISMATCH")
        self._settings = deepcopy(settings)
        self._settings_revision = revision
        return settings

    def settings(self) -> dict[str, Any]:
        with self._lock:
            if self._settings is not None:
                return deepcopy(self._settings)
            config = self._config_factory()
            store = self._store_factory() if self._store_factory is not None else None
            if store is None:
                settings = build_gex_scheduler_runtime_settings(
                    env_enabled=bool(config.gex_scheduler_enabled),
                    env_instrument_ids=config.gex_scheduler_instrument_ids,
                )
                self._settings = deepcopy(settings)
                return settings
            try:
                raw, present, settings_revision = store.read_setting_snapshot(
                    "server",
                    GEX_SCHEDULER_SETTING_KEY,
                )
            except Exception as exc:
                return build_gex_scheduler_runtime_settings(
                    env_enabled=bool(config.gex_scheduler_enabled),
                    env_instrument_ids=config.gex_scheduler_instrument_ids,
                    settings_error=exc,
                )
            return self._publish_committed_locked(
                raw if present else None,
                settings_revision=settings_revision,
            )

    def save(self, setting: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            store = self._store_factory() if self._store_factory is not None else None
            if store is None:
                raise RuntimeError("GEX_SCHEDULER_SETTINGS_STORAGE_REQUIRED")
            exact_setting = {
                "enabled": setting["enabled"],
                "instrument_ids": list(setting["instrument_ids"]),
            }
            settings_revision = store.upsert_setting(
                "server",
                GEX_SCHEDULER_SETTING_KEY,
                exact_setting,
            )
            return self._publish_committed_locked(
                exact_setting,
                settings_revision=settings_revision,
            )

    def publish_committed_snapshot(
        self,
        raw_setting: Any,
        present: bool,
        settings_revision: int,
    ) -> dict[str, Any]:
        if not isinstance(present, bool):
            raise TypeError("GEX_SCHEDULER_SETTINGS_PRESENCE_INVALID")
        with self._lock:
            return self._publish_committed_locked(
                raw_setting if present else None,
                settings_revision=settings_revision,
            )


async def run_gex_scheduler_loop(
    *,
    interval_seconds: float,
    runtime_settings: Callable[[], dict[str, Any]],
    server_sleeping: Callable[[], bool],
    live_gex_status: Callable[[], dict[str, Any]] | None = None,
    scheduler_jobs: Callable[..., list[Any]],
    instrument_for_job: Callable[[str], dict[str, Any]],
    store_factory: Callable[[], Any],
    scheduler_state: dict[tuple[str, str], GexSchedulerRouteState],
    logger_warning: Callable[[str, Any, BaseException], None],
    provider_router: Callable[[dict[str, Any]], InstrumentRoute] = route_instrument,
) -> None:
    while True:
        await asyncio.sleep(max(float(interval_seconds), 1.0))
        scheduler = await run_physical_thread_call(runtime_settings)
        if (
            scheduler.get("settings_error") is not None
            or not scheduler["enabled"]
            or server_sleeping()
        ):
            continue
        now = datetime.now(tz=UTC)
        store = await run_physical_thread_call(store_factory)
        instruments = [
            await run_physical_thread_call(instrument_for_job, instrument_id)
            for instrument_id in scheduler["instrument_ids"]
        ]
        jobs = await run_physical_thread_call(
            scheduler_jobs,
            instruments,
            now=now,
            session_open=lambda instrument, ts: provider_schedule_open_state(
                ts,
                liquid=False,
                store=store,
                instrument=dict(instrument),
            ),
        )
        active_keys = {(job.instrument_id, job.route_fingerprint) for job in jobs}
        for key in list(scheduler_state):
            if key not in active_keys:
                scheduler_state.pop(key, None)
        live_status = live_gex_status() if live_gex_status is not None else {}
        live_routes = {
            (session["instrument_id"], session["route_fingerprint"])
            for session in live_status.get("sessions", [])
            if isinstance(session, dict)
            and isinstance(session.get("instrument_id"), str)
            and bool(session.get("instrument_id"))
            and isinstance(session.get("route_fingerprint"), str)
            and bool(session.get("route_fingerprint"))
        }
        for job in jobs:
            if not job.enabled:
                continue
            instrument = await run_physical_thread_call(
                instrument_for_job,
                job.instrument_id,
            )
            route = await run_physical_thread_call(provider_router, instrument)
            state_key = (job.instrument_id, job.route_fingerprint)
            if (route.instrument_id, route.fingerprint) != state_key:
                scheduler_state.pop(state_key, None)
                logger_warning(
                    "GEX scheduler %s skipped stale route: %s",
                    job.provider_symbol,
                    ValueError(
                        "GEX scheduler job route no longer matches the qualified instrument"
                    ),
                )
                continue
            if (route.instrument_id, route.fingerprint) in live_routes:
                continue
            state = scheduler_state.get(state_key)
            retry_pending = state is not None and not state.completed
            due_at = job.next_due_at.astimezone(UTC)
            if not retry_pending and due_at > now:
                continue
            if not retry_pending and (state is None or state.due_at < due_at):
                state = GexSchedulerRouteState(due_at=due_at)
                scheduler_state[state_key] = state
            if state is None:
                continue
            if state.completed:
                continue
            if state.retry_at is not None and state.retry_at > now:
                continue
            await _run_gex_scheduler_attempt(
                route=route,
                job=job,
                store=store,
                state=state,
                logger_warning=logger_warning,
            )
