from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from aef_terminal.config import AppConfig
from aef_terminal.data.instrument_identity import require_exact_identity_text
from aef_terminal.data.ibkr_tick_feed import (
    IbkrTickFeed,
    IbkrTickFeedConfigurationError,
)
from aef_terminal.data.providers import route_instrument
from aef_terminal.runtime.async_tasks import (
    run_cancellation_deferred,
    run_physical_thread_call,
)
from aef_terminal.settings_contract import (
    TICK_LIVE_SETTING_KEY,
    invalid_client_setting_value_keys,
)
from aef_terminal.ui.services.tick_live_restore import run_tick_live_restore_loop
from aef_terminal.ui.runtime_instruments import lookup_runtime_instrument


class TickLiveConnectorService:
    """Owns IBKR tick-by-tick connector lifecycle outside the indicator module."""

    def __init__(
        self,
        *,
        tick_feed_factory: Callable[..., IbkrTickFeed] = IbkrTickFeed,
        store_factory: Callable[[], Any] | None = None,
        config_factory: Callable[[], AppConfig] = AppConfig,
    ) -> None:
        self._tick_feed_factory = tick_feed_factory
        self._store_factory = store_factory
        self._config_factory = config_factory
        self.feed: IbkrTickFeed | None = None
        self.thread: threading.Thread | None = None
        self.last_error = ""
        self.last_error_code = ""
        self.last_error_retryable = True
        self.lock = asyncio.Lock()
        self._server_sleeping: Callable[[], bool] | None = None
        self._client_settings_snapshot: Callable[[], dict[str, Any]] | None = None
        self._publish_client_settings_patch: Callable[[dict[str, Any], int], None] | None = None
        self._restore_allowed: Callable[[dict[str, Any], str, dict[str, Any]], bool] | None = None
        self._logger: logging.Logger | None = None

    def configure(
        self,
        *,
        server_sleeping: Callable[[], bool],
        client_settings_snapshot: Callable[[], dict[str, Any]],
        publish_client_settings_patch: Callable[[dict[str, Any], int], None],
        restore_allowed: Callable[
            [dict[str, Any], str, dict[str, Any]],
            bool,
        ],
        store_factory: Callable[[], Any],
        logger: logging.Logger,
    ) -> None:
        self._server_sleeping = server_sleeping
        self._client_settings_snapshot = client_settings_snapshot
        self._publish_client_settings_patch = publish_client_settings_patch
        self._restore_allowed = restore_allowed
        self._store_factory = store_factory
        self._logger = logger

    def require_deps(
        self,
    ) -> tuple[
        Callable[[], bool],
        Callable[[], dict[str, Any]],
        Callable[[dict[str, Any], str, dict[str, Any]], bool],
        logging.Logger,
    ]:
        if (
            self._server_sleeping is None
            or self._client_settings_snapshot is None
            or self._publish_client_settings_patch is None
            or self._restore_allowed is None
            or self._logger is None
        ):
            raise RuntimeError("tick live connector dependencies are not configured")
        return (
            self._server_sleeping,
            self._client_settings_snapshot,
            self._restore_allowed,
            self._logger,
        )

    def require_store_factory(self) -> Callable[[], Any]:
        if self._store_factory is None:
            raise RuntimeError("tick live storage dependency is not configured")
        return self._store_factory

    def _record_start_error(
        self,
        error: BaseException | str,
        *,
        code: str,
        retryable: bool,
        display_key: str = "",
        inactive_status: str = "error",
        persist_if_inactive: bool = True,
    ) -> dict[str, Any]:
        message = str(error).strip() or "tick live failed"
        error_payload = {
            "code": code,
            "retryable": retryable,
            "message": message,
        }
        thread = self.thread
        try:
            thread_active = thread is not None and thread.is_alive()
        except Exception:
            thread_active = thread is not None
        if not thread_active and persist_if_inactive:
            self.last_error = message
            self.last_error_code = code
            self.last_error_retryable = retryable
        try:
            current = self.status()
        except Exception:
            current = {
                "enabled": thread_active,
                "running": False,
                "status": "starting" if thread_active else "error",
                "display_keys": [display_key] if display_key else [],
                "route_fingerprints": [],
                "route_identities": [],
                "last_error": "",
            }
        if not thread_active:
            current["enabled"] = False
            current["running"] = False
            current["status"] = inactive_status
        return {
            **current,
            "last_error": message,
            "error": error_payload,
        }

    def run_thread(self, feed: IbkrTickFeed) -> None:
        _, _, _, logger = self.require_deps()
        try:
            asyncio.run(feed.run_forever())
        except Exception as exc:
            self.last_error = str(exc)
            self.last_error_code = "TICK_LIVE_RUNTIME_FAILED"
            self.last_error_retryable = True
            logger.warning("IBKR tick live thread failed: %s", exc)

    def status(self) -> dict[str, Any]:
        feed = self.feed
        thread = self.thread
        feed_status = feed.status() if feed is not None else {}
        thread_active = thread is not None and thread.is_alive()
        running = bool(thread_active and feed_status.get("running"))
        error = str(feed_status.get("last_error") or self.last_error or "")
        buffer_status = (
            feed_status.get("buffer") if isinstance(feed_status.get("buffer"), dict) else {}
        )
        buffer_health = (
            buffer_status.get("health") if isinstance(buffer_status.get("health"), dict) else {}
        )
        if running and int(feed_status.get("tick_subscriptions") or 0) > 0:
            status = "live"
        elif thread_active:
            status = "starting"
        elif error:
            status = "error"
        else:
            status = "off"
        result = {
            "enabled": bool(thread_active),
            "running": running,
            "status": status,
            "display_keys": feed_status.get("display_keys") or [],
            "route_fingerprints": feed_status.get("route_fingerprints") or [],
            "route_identities": feed_status.get("route_identities") or [],
            "started_at": feed_status.get("started_at") or "",
            "tick_subscriptions": int(feed_status.get("tick_subscriptions") or 0),
            "client_id": feed_status.get("client_id") or self._config_factory().ibkr_tick_client_id,
            "last_error": error,
            "health": buffer_health,
            "buffer": buffer_status,
        }
        if error:
            result["error"] = {
                "code": self.last_error_code or "TICK_LIVE_FAILED",
                "retryable": (self.last_error_retryable if self.last_error_code else True),
                "message": error,
            }
        return result

    def _persist_intent(
        self,
        enabled: bool,
        instrument_id: str = "",
        route_fingerprint: str = "",
        *,
        source: str = "api",
    ) -> None:
        _, _, _, logger = self.require_deps()
        if not isinstance(enabled, bool):
            raise ValueError("TICK_LIVE_ENABLED_INVALID")
        identity = require_exact_identity_text(
            instrument_id,
            field="instrument_id",
            allow_empty=not enabled,
        )
        fingerprint = require_exact_identity_text(
            route_fingerprint,
            field="route_fingerprint",
            allow_empty=not enabled,
        )
        store = self.require_store_factory()()
        if store is None:
            error = RuntimeError("TICK_LIVE_INTENT_STORE_REQUIRED")
            logger.warning("Persist tick live intent failed: %s", error)
            raise error
        intent_payload = {
            "enabled": enabled,
            "instrument_id": identity,
            "route_fingerprint": fingerprint,
            "source": source,
            "updated_at": datetime.now(tz=UTC).isoformat(),
        }
        try:
            settings_revision = store.upsert_setting(
                "client",
                TICK_LIVE_SETTING_KEY,
                intent_payload,
            )
            if self._publish_client_settings_patch is None:
                raise RuntimeError("tick live settings projection is not configured")
            self._publish_client_settings_patch(
                {TICK_LIVE_SETTING_KEY: intent_payload},
                settings_revision=settings_revision,
            )
        except Exception as exc:
            logger.warning("Persist tick live intent failed: %s", exc)
            raise

    def intent_from_settings(self, settings: dict[str, Any] | None = None) -> dict[str, Any]:
        if settings is not None and not isinstance(settings, dict):
            raise TypeError("TICK_LIVE_SETTINGS_PAYLOAD_INVALID")
        if settings is None:
            _, client_settings_snapshot, _, _ = self.require_deps()
            values = client_settings_snapshot()
        else:
            values = settings
        if not isinstance(values, dict):
            raise TypeError("TICK_LIVE_SETTINGS_PAYLOAD_INVALID")
        raw = values.get(TICK_LIVE_SETTING_KEY)
        if raw is None:
            return {
                "enabled": False,
                "instrument_id": "",
                "route_fingerprint": "",
                "source": "default",
            }
        if invalid_client_setting_value_keys({TICK_LIVE_SETTING_KEY: raw}):
            raise ValueError("TICK_LIVE_SETTING_INVALID")
        if not isinstance(raw, dict):
            raise ValueError("TICK_LIVE_SETTING_INVALID")
        return {
            "enabled": raw["enabled"],
            "instrument_id": raw["instrument_id"],
            "route_fingerprint": raw["route_fingerprint"],
            "source": raw["source"],
        }

    async def ensure_from_intent(self, reason: str = "monitor") -> dict[str, Any]:
        async with self.lock:
            return await run_cancellation_deferred(
                self._ensure_from_intent_owned(reason),
                task_cancelled_error="TICK_LIVE_RESTORE_TASK_CANCELLED",
            )

    async def _ensure_from_intent_owned(self, reason: str) -> dict[str, Any]:
        (
            server_sleeping,
            client_settings_snapshot,
            restore_allowed,
            logger,
        ) = self.require_deps()
        settings = client_settings_snapshot()
        intent = self.intent_from_settings(settings)
        if (
            not intent.get("enabled")
            or not intent.get("instrument_id")
            or not intent.get("route_fingerprint")
        ):
            status = self.status()
            thread = self.thread
            if status.get("running") or (thread is not None and thread.is_alive()):
                logger.info("Stopping IBKR tick live reason=%s", reason)
                return await self.stop(reason="intent")
            return status
        status = self.status()
        try:
            instrument = lookup_runtime_instrument(intent["instrument_id"])
        except ValueError:
            return status
        calc_enabled = restore_allowed(
            settings,
            intent["instrument_id"],
            instrument,
        )
        if not calc_enabled:
            await run_physical_thread_call(
                self._persist_intent,
                False,
                intent["instrument_id"],
                intent["route_fingerprint"],
                source="calc_off",
            )
            thread = self.thread
            if status.get("running") or (thread is not None and thread.is_alive()):
                logger.info("Stopping IBKR tick live reason=calc_off")
                return await self.stop(reason="calc_off")
            return self.status()
        if server_sleeping():
            return status
        try:
            fingerprint = route_instrument(
                instrument,
                expected_source="ibkr",
            ).fingerprint
        except ValueError:
            return status
        if fingerprint != intent["route_fingerprint"]:
            return status
        current_identities = {
            (entry.get("instrument_id"), entry.get("route_fingerprint"))
            for entry in (status.get("route_identities") or [])
            if isinstance(entry, dict)
            and isinstance(entry.get("instrument_id"), str)
            and isinstance(entry.get("route_fingerprint"), str)
        }
        if status.get("running") and (intent["instrument_id"], fingerprint) in current_identities:
            return status
        logger.info(
            "Restoring IBKR tick live instrument_id=%s reason=%s",
            intent["instrument_id"],
            reason,
        )
        return await self._start(
            intent["instrument_id"],
            intent["route_fingerprint"],
        )

    async def restore_loop(self) -> None:
        async def _restore_once(reason: str) -> None:
            _, _, _, logger = self.require_deps()
            try:
                await self.ensure_from_intent(reason=reason)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Tick live restore monitor failed: %s", exc)

        await run_tick_live_restore_loop(
            startup_delay_seconds=2.0,
            interval_seconds=12.0,
            restore_once=_restore_once,
            reason="startup/monitor",
        )

    async def stop(self, reason: str = "manual") -> dict[str, Any]:
        return await run_cancellation_deferred(
            self._stop_owned(reason),
            task_cancelled_error="TICK_LIVE_STOP_TASK_CANCELLED",
        )

    async def _stop_owned(self, reason: str) -> dict[str, Any]:
        _, _, _, logger = self.require_deps()
        feed = self.feed
        thread = self.thread
        if feed is not None:
            try:
                feed.request_stop()
            except Exception as exc:
                self.last_error = str(exc)
                logger.warning("Tick live stop failed: %s", exc)
        if thread is not None and thread.is_alive():
            await run_physical_thread_call(thread.join, 3.0)
        logger.info("IBKR tick live disabled reason=%s", reason)
        if thread is None or not thread.is_alive():
            self.feed = None
            self.thread = None
            if reason == "manual":
                self.last_error = ""
                self.last_error_code = ""
                self.last_error_retryable = True
        return self.status()

    async def _cleanup_partial_start(
        self,
        feed: IbkrTickFeed | None,
        thread: threading.Thread | None,
        *,
        logger: logging.Logger,
    ) -> bool:
        if feed is not None:
            try:
                feed.request_stop()
            except Exception as exc:
                logger.warning(
                    "Tick live partial feed cleanup failed: %s",
                    exc,
                )
        thread_active = thread is not None
        if thread is not None:
            try:
                thread_active = thread.is_alive()
            except Exception as exc:
                logger.warning(
                    "Tick live partial thread state check failed: %s",
                    exc,
                )
        if thread is not None and thread_active:
            try:
                await run_physical_thread_call(thread.join, 3.0)
            except Exception as exc:
                logger.warning(
                    "Tick live partial thread cleanup failed: %s",
                    exc,
                )
        if thread is not None:
            try:
                thread_active = thread.is_alive()
            except Exception as exc:
                thread_active = True
                logger.warning(
                    "Tick live partial thread state recheck failed: %s",
                    exc,
                )
        if thread_active:
            self.feed = feed
            self.thread = thread
            logger.error("Tick live partial thread did not stop; ownership retained")
            return False
        if self.feed is feed:
            self.feed = None
        if self.thread is thread:
            self.thread = None
        return True

    async def _start(self, instrument_id: str, route_fingerprint: str) -> dict[str, Any]:
        try:
            identity = require_exact_identity_text(instrument_id, field="instrument_id")
            expected_fingerprint = require_exact_identity_text(
                route_fingerprint, field="route_fingerprint"
            )
        except ValueError as exc:
            return self._record_start_error(
                exc,
                code="TICK_LIVE_PAYLOAD_INVALID",
                retryable=False,
            )
        ticker = ""
        try:
            server_sleeping, _, _, logger = self.require_deps()
            sleeping = server_sleeping()
        except Exception as exc:
            return self._record_start_error(
                exc,
                code="TICK_LIVE_START_FAILED",
                retryable=True,
            )
        if sleeping:
            return self._record_start_error(
                "server is sleeping",
                code="SERVER_SLEEPING",
                retryable=True,
                inactive_status="sleeping",
                persist_if_inactive=False,
            )
        try:
            store_factory = self.require_store_factory()
            store = await run_physical_thread_call(store_factory)
            if store is None:
                raise RuntimeError("tick live storage is not configured")
        except Exception as exc:
            return self._record_start_error(
                exc,
                code="TICK_LIVE_STORAGE_FAILED",
                retryable=True,
            )
        try:
            instrument = lookup_runtime_instrument(identity)
            route = route_instrument(instrument, expected_source="ibkr")
        except ValueError as exc:
            return self._record_start_error(
                exc,
                code="TICK_LIVE_ROUTE_INVALID",
                retryable=False,
            )
        except Exception as exc:
            return self._record_start_error(
                exc,
                code="TICK_LIVE_START_FAILED",
                retryable=True,
            )
        if route.fingerprint != expected_fingerprint:
            return self._record_start_error(
                "tick live route fingerprint mismatch",
                code="TICK_LIVE_ROUTE_MISMATCH",
                retryable=False,
            )
        ticker = ticker or route.instrument_key
        fingerprint = route.fingerprint
        try:
            current_status = self.status()
            current_identities = {
                (entry.get("instrument_id"), entry.get("route_fingerprint"))
                for entry in (current_status.get("route_identities") or [])
                if isinstance(entry, dict)
                and isinstance(entry.get("instrument_id"), str)
                and isinstance(entry.get("route_fingerprint"), str)
            }
            if (
                self.thread is not None
                and self.thread.is_alive()
                and (identity, fingerprint) in current_identities
            ):
                self.last_error = ""
                self.last_error_code = ""
                self.last_error_retryable = True
                return self.status()
            await self.stop(reason="switch")
            remaining_thread = self.thread
            if remaining_thread is not None and remaining_thread.is_alive():
                return self._record_start_error(
                    "tick live previous feed did not stop",
                    code="TICK_LIVE_START_FAILED",
                    retryable=True,
                    display_key=ticker,
                )
        except Exception as exc:
            return self._record_start_error(
                exc,
                code="TICK_LIVE_START_FAILED",
                retryable=True,
                display_key=ticker,
            )
        self.last_error = ""
        self.last_error_code = ""
        self.last_error_retryable = True
        feed: IbkrTickFeed | None = None
        thread: threading.Thread | None = None
        try:
            config = self._config_factory()
            feed = self._tick_feed_factory(
                instruments=[instrument],
                store=store,
                use_bidask=True,
                retention_hours=float(config.tick_retention_hours),
                purge_interval=300.0,
                flush_interval=0.75,
            )
            thread = threading.Thread(
                target=self.run_thread,
                args=(feed,),
                name=f"ibkr-tick-live-{ticker}",
                daemon=True,
            )
            self.feed = feed
            self.thread = thread
            thread.start()
            logger.info("IBKR tick live requested for %s", ticker)
            await asyncio.sleep(0.05)
            return self.status()
        except asyncio.CancelledError:
            await self._cleanup_partial_start(
                feed,
                thread,
                logger=logger,
            )
            raise
        except Exception as exc:
            if isinstance(exc, IbkrTickFeedConfigurationError):
                error_code = exc.code
                retryable = exc.retryable
            else:
                error_code = "TICK_LIVE_START_FAILED"
                retryable = True
            message = str(exc).strip() or "tick live failed to start"
            await self._cleanup_partial_start(
                feed,
                thread,
                logger=logger,
            )
            logger.warning(
                "IBKR tick live start rejected code=%s retryable=%s: %s",
                error_code,
                retryable,
                message,
            )
            return self._record_start_error(
                message,
                code=error_code,
                retryable=retryable,
                display_key=ticker,
            )

    async def set_enabled(
        self,
        enabled: bool,
        instrument_id: str,
        route_fingerprint: str,
    ) -> dict[str, Any]:
        async with self.lock:
            return await run_cancellation_deferred(
                self._set_enabled_owned(enabled, instrument_id, route_fingerprint),
                task_cancelled_error="TICK_LIVE_TRANSITION_TASK_CANCELLED",
            )

    async def _set_enabled_owned(
        self,
        enabled: bool,
        instrument_id: str,
        route_fingerprint: str,
    ) -> dict[str, Any]:
        if enabled:
            live = await self._start(instrument_id, route_fingerprint)
            if live.get("status") in {"error", "sleeping"}:
                return live
            try:
                await run_physical_thread_call(
                    self._persist_intent,
                    True,
                    instrument_id,
                    route_fingerprint,
                    source="api",
                )
            except Exception as exc:
                await self.stop(reason="intent_persist_failed")
                return self._record_start_error(
                    exc,
                    code="TICK_LIVE_INTENT_PERSIST_FAILED",
                    retryable=True,
                )
            return live
        try:
            await run_physical_thread_call(
                self._persist_intent,
                False,
                instrument_id,
                route_fingerprint,
                source="api",
            )
        except Exception as exc:
            return self._record_start_error(
                exc,
                code="TICK_LIVE_INTENT_PERSIST_FAILED",
                retryable=True,
            )
        return await self.stop(reason="manual")
