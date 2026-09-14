from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from aef_terminal.data.gex.contracts import (
    GexCaptureMode,
    GexCaptureModeConflictError,
)
from aef_terminal.data.instrument_identity import require_exact_identity_text


@dataclass
class GexRuntimeErrorState:
    message: str
    at: datetime


@dataclass
class GexFailureBackoffState:
    consecutive_failures: int = 0
    retry_at: datetime | None = None
    source: str = ""
    kind: str = ""
    reason: str = ""
    message: str = ""
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def snapshot(self, now: datetime) -> dict[str, Any]:
        retry_at = self.retry_at
        return {
            "active": bool(retry_at and retry_at > now),
            "retry_at": retry_at.isoformat() if retry_at else "",
            "retry_in_seconds": (
                round(max((retry_at - now).total_seconds(), 0.0), 1) if retry_at else 0.0
            ),
            "consecutive_failures": self.consecutive_failures,
            "source": self.source,
            "kind": self.kind,
            "reason": self.reason,
            "message": self.message,
            "diagnostics": dict(self.diagnostics),
        }


@dataclass
class GexRouteRuntimeState:
    provider_symbol: str
    active_capture_mode: GexCaptureMode | None = None
    errors: dict[str, GexRuntimeErrorState] = field(default_factory=dict)
    last_sync_at: datetime | None = None
    last_request: str = ""
    last_diagnostics: dict[str, Any] = field(default_factory=dict)
    request_count: int = 0
    failure_backoffs: dict[str, GexFailureBackoffState] = field(default_factory=dict)


@dataclass
class GexRuntimeState:
    routes: dict[tuple[str, str], GexRouteRuntimeState] = field(default_factory=dict)
    _routes_lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def acquire_capture_mode(
        self,
        instrument_id: str,
        route_fingerprint: str,
        mode: GexCaptureMode,
        *,
        provider_symbol: str = "",
    ) -> None:
        with self._routes_lock:
            state = self._route_state(
                instrument_id,
                route_fingerprint,
                provider_symbol=provider_symbol,
            )
            active = state.active_capture_mode
            if active is not None and active != mode:
                raise GexCaptureModeConflictError(
                    instrument_id=instrument_id,
                    route_fingerprint=route_fingerprint,
                    active=active,
                    requested=mode,
                )
            state.active_capture_mode = mode

    def release_capture_mode(
        self,
        instrument_id: str,
        route_fingerprint: str,
        mode: GexCaptureMode,
    ) -> None:
        with self._routes_lock:
            state = self.routes.get(
                (
                    require_exact_identity_text(instrument_id, field="instrument_id"),
                    require_exact_identity_text(route_fingerprint, field="route_fingerprint"),
                )
            )
            if state is not None and state.active_capture_mode == mode:
                state.active_capture_mode = None

    def release_all_capture_modes(self, mode: GexCaptureMode) -> None:
        with self._routes_lock:
            for state in self.routes.values():
                if state.active_capture_mode == mode:
                    state.active_capture_mode = None

    def active_capture_mode(
        self,
        instrument_id: str,
        route_fingerprint: str,
    ) -> GexCaptureMode | None:
        with self._routes_lock:
            state = self.routes.get(
                (
                    require_exact_identity_text(instrument_id, field="instrument_id"),
                    require_exact_identity_text(
                        route_fingerprint,
                        field="route_fingerprint",
                    ),
                )
            )
            return state.active_capture_mode if state is not None else None

    def _route_state(
        self,
        instrument_id: str,
        route_fingerprint: str,
        *,
        provider_symbol: str = "",
    ) -> GexRouteRuntimeState:
        key = (
            require_exact_identity_text(instrument_id, field="instrument_id"),
            require_exact_identity_text(route_fingerprint, field="route_fingerprint"),
        )
        state = self.routes.get(key)
        if state is None:
            provider_symbol_key = require_exact_identity_text(
                provider_symbol,
                field="provider_symbol",
                allow_empty=True,
            )
            state = GexRouteRuntimeState(provider_symbol=provider_symbol_key)
            self.routes[key] = state
        elif provider_symbol and not state.provider_symbol:
            state.provider_symbol = require_exact_identity_text(
                provider_symbol,
                field="provider_symbol",
            )
        return state

    def record_error(
        self,
        instrument_id: str,
        route_fingerprint: str,
        message: str,
        *,
        provider_symbol: str = "",
        source: str,
        at: datetime | None = None,
    ) -> None:
        with self._routes_lock:
            state = self._route_state(
                instrument_id, route_fingerprint, provider_symbol=provider_symbol
            )
            source_key = require_exact_identity_text(
                source,
                field="GEX_RUNTIME_ERROR_SOURCE",
            )
            state.errors[source_key] = GexRuntimeErrorState(
                message=str(message or "").strip(),
                at=at or datetime.now(tz=UTC),
            )

    def clear_error(
        self,
        instrument_id: str,
        route_fingerprint: str,
        *,
        source: str,
    ) -> None:
        with self._routes_lock:
            state = self.routes.get(
                (
                    require_exact_identity_text(instrument_id, field="instrument_id"),
                    require_exact_identity_text(route_fingerprint, field="route_fingerprint"),
                )
            )
            if state is None:
                return
            source_key = require_exact_identity_text(
                source,
                field="GEX_RUNTIME_ERROR_SOURCE",
            )
            state.errors.pop(source_key, None)

    def record_failure(
        self,
        instrument_id: str,
        route_fingerprint: str,
        message: str,
        *,
        provider_symbol: str = "",
        source: str,
        lane: str,
        kind: str = "error",
        reason: str = "",
        diagnostics: Mapping[str, Any] | None = None,
        at: datetime | None = None,
        base_backoff_seconds: float = 30.0,
        max_backoff_seconds: float = 300.0,
    ) -> dict[str, Any]:
        failed_at = at or datetime.now(tz=UTC)
        with self._routes_lock:
            state = self._route_state(
                instrument_id, route_fingerprint, provider_symbol=provider_symbol
            )
            source_key = require_exact_identity_text(
                source,
                field="GEX_RUNTIME_FAILURE_SOURCE",
            )
            failure_kind = require_exact_identity_text(
                kind,
                field="GEX_RUNTIME_FAILURE_KIND",
            )
            failure_reason = require_exact_identity_text(
                reason,
                field="GEX_RUNTIME_FAILURE_REASON",
                allow_empty=True,
            )
            failure_diagnostics = dict(diagnostics or {})
            failure_lane = require_exact_identity_text(
                lane,
                field="GEX_RUNTIME_FAILURE_LANE",
            )
            if failure_lane not in {"request", "live"}:
                raise ValueError("GEX runtime failure lane must be request or live")
            state.errors[source_key] = GexRuntimeErrorState(
                message=str(message or "").strip(),
                at=failed_at,
            )
            failure = state.failure_backoffs.setdefault(
                failure_lane,
                GexFailureBackoffState(),
            )
            failure.consecutive_failures += 1
            delay_seconds = min(
                max(float(base_backoff_seconds), 1.0)
                * (2 ** max(failure.consecutive_failures - 1, 0)),
                max(float(max_backoff_seconds), 1.0),
            )
            failure.retry_at = failed_at + timedelta(seconds=delay_seconds)
            failure.source = source_key
            failure.kind = failure_kind
            failure.reason = failure_reason
            failure.message = str(message or "").strip()
            failure.diagnostics = failure_diagnostics
            return {
                "active": True,
                "retry_at": failure.retry_at.isoformat(),
                "retry_in_seconds": round(delay_seconds, 1),
                "consecutive_failures": failure.consecutive_failures,
                "source": source_key,
                "lane": failure_lane,
                "kind": failure_kind,
                "reason": failure_reason,
                "diagnostics": dict(failure_diagnostics),
                "message": str(message or "").strip(),
            }

    def clear_failure(
        self,
        instrument_id: str,
        route_fingerprint: str,
        *,
        lane: str,
    ) -> None:
        with self._routes_lock:
            state = self.routes.get(
                (
                    require_exact_identity_text(instrument_id, field="instrument_id"),
                    require_exact_identity_text(route_fingerprint, field="route_fingerprint"),
                )
            )
            if state is None:
                return
            failure_lane = require_exact_identity_text(
                lane,
                field="GEX_RUNTIME_FAILURE_LANE",
            )
            if failure_lane not in {"request", "live"}:
                raise ValueError("GEX runtime failure lane must be request or live")
            state.failure_backoffs.pop(failure_lane, None)

    def failure_backoff(
        self,
        instrument_id: str,
        route_fingerprint: str,
        *,
        lane: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        current = now or datetime.now(tz=UTC)
        with self._routes_lock:
            state = self.routes.get(
                (
                    require_exact_identity_text(instrument_id, field="instrument_id"),
                    require_exact_identity_text(route_fingerprint, field="route_fingerprint"),
                )
            )
            if state is None:
                return {
                    "active": False,
                    "retry_at": "",
                    "retry_in_seconds": 0.0,
                    "consecutive_failures": 0,
                    "source": "",
                    "lane": lane,
                    "kind": "",
                    "reason": "",
                    "message": "",
                    "diagnostics": {},
                }
            failure_lane = require_exact_identity_text(
                lane,
                field="GEX_RUNTIME_FAILURE_LANE",
            )
            if failure_lane not in {"request", "live"}:
                raise ValueError("GEX runtime failure lane must be request or live")
            failure = state.failure_backoffs.get(failure_lane)
            if failure is None:
                return {
                    "active": False,
                    "retry_at": "",
                    "retry_in_seconds": 0.0,
                    "consecutive_failures": 0,
                    "source": "",
                    "lane": failure_lane,
                    "kind": "",
                    "reason": "",
                    "message": "",
                    "diagnostics": {},
                }
            return {
                **failure.snapshot(current),
                "lane": failure_lane,
            }

    def clear_request_errors(self, instrument_id: str, route_fingerprint: str) -> None:
        with self._routes_lock:
            state = self.routes.get(
                (
                    require_exact_identity_text(instrument_id, field="instrument_id"),
                    require_exact_identity_text(route_fingerprint, field="route_fingerprint"),
                )
            )
            if state is None:
                return
            for source in list(state.errors):
                if source != "live":
                    state.errors.pop(source, None)

    def error_for_route(
        self,
        instrument_id: str,
        route_fingerprint: str,
        *,
        sources: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        with self._routes_lock:
            state = self.routes.get(
                (
                    require_exact_identity_text(instrument_id, field="instrument_id"),
                    require_exact_identity_text(route_fingerprint, field="route_fingerprint"),
                )
            )
            if state is None:
                return {"message": "", "at": None, "source": ""}
            allowed = {
                require_exact_identity_text(
                    source,
                    field="GEX_RUNTIME_ERROR_SOURCE",
                )
                for source in sources
            }
            candidates = [
                (source, error)
                for source, error in state.errors.items()
                if not allowed or source in allowed
            ]
            if not candidates:
                return {"message": "", "at": None, "source": ""}
            source, error = max(candidates, key=lambda item: item[1].at)
            return {
                "message": error.message,
                "at": error.at,
                "source": source,
            }

    def mark_request_started(
        self,
        instrument_id: str,
        route_fingerprint: str,
        label: str,
        *,
        provider_symbol: str,
    ) -> None:
        with self._routes_lock:
            self._route_state(
                instrument_id, route_fingerprint, provider_symbol=provider_symbol
            ).last_request = str(label or "")

    def mark_request_finished(self, instrument_id: str, route_fingerprint: str) -> None:
        with self._routes_lock:
            state = self.routes.get(
                (
                    require_exact_identity_text(instrument_id, field="instrument_id"),
                    require_exact_identity_text(route_fingerprint, field="route_fingerprint"),
                )
            )
            if state is not None:
                state.last_request = ""

    def record_diagnostics(
        self,
        instrument_id: str,
        route_fingerprint: str,
        diagnostics: dict[str, Any],
    ) -> None:
        with self._routes_lock:
            state = self._route_state(instrument_id, route_fingerprint)
            state.last_diagnostics = dict(diagnostics)
            state.request_count += 1

    def record_sync(
        self,
        instrument_id: str,
        route_fingerprint: str,
        captured_at: datetime,
        *,
        provider_symbol: str = "",
    ) -> None:
        with self._routes_lock:
            self._route_state(
                instrument_id, route_fingerprint, provider_symbol=provider_symbol
            ).last_sync_at = captured_at

    def sync_at(self, instrument_id: str, route_fingerprint: str) -> datetime | None:
        with self._routes_lock:
            state = self.routes.get(
                (
                    require_exact_identity_text(instrument_id, field="instrument_id"),
                    require_exact_identity_text(route_fingerprint, field="route_fingerprint"),
                )
            )
            return state.last_sync_at if state is not None else None

    def routes_snapshot(self) -> dict[str, dict[str, Any]]:
        now = datetime.now(tz=UTC)
        with self._routes_lock:
            default_backoff = GexFailureBackoffState().snapshot(now)
            return {
                json.dumps([instrument_id, route_fingerprint], separators=(",", ":")): {
                    "instrument_id": instrument_id,
                    "route_fingerprint": route_fingerprint,
                    "provider_symbol": state.provider_symbol,
                    "active_capture_mode": state.active_capture_mode or "",
                    "errors": {
                        source: {
                            "message": error.message,
                            "at": error.at.isoformat(),
                        }
                        for source, error in sorted(state.errors.items())
                    },
                    "last_sync_at": state.last_sync_at.isoformat() if state.last_sync_at else "",
                    "last_request": state.last_request,
                    "last_diagnostics": dict(state.last_diagnostics),
                    "request_count": state.request_count,
                    "failure_backoff": max(
                        (
                            {
                                **failure.snapshot(now),
                                "lane": lane,
                            }
                            for lane, failure in state.failure_backoffs.items()
                        ),
                        key=lambda row: (
                            bool(row["active"]),
                            str(row["retry_at"]),
                        ),
                        default={
                            **default_backoff,
                            "lane": "",
                        },
                    ),
                    "failure_backoffs": {
                        lane: {
                            **failure.snapshot(now),
                            "lane": lane,
                        }
                        for lane, failure in sorted(state.failure_backoffs.items())
                    },
                }
                for (instrument_id, route_fingerprint), state in sorted(self.routes.items())
            }


_GEX_RUNTIME = GexRuntimeState()
