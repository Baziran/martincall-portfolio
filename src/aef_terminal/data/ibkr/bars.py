from __future__ import annotations
import asyncio
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from aef_terminal.config import AppConfig
from aef_terminal.data.ibkr.manager import ibkr_market_data_manager
from aef_terminal.domain import Bar, BarProvenance, BarState
from aef_terminal.data.ibkr.contracts import (
    _bar_from_ibkr_item,
    _bar_size_for_interval,
    _chart_contract_for_instrument_async,
    _duration_for_chart_stream,
    _duration_for_range,
    _history_use_rth,
    _ibkr_bar_provenance,
    _instrument_contract_cache_token,
    _normalize_ts,
    _qualified_contract_async,
    _require_ibkr_instrument,
    _what_to_show,
)
from aef_terminal.data.ibkr.runtime import (
    _AsyncIbkrActivityWatchdog,
    _IBKR_RUNTIME,
    _LOGGER,
)
from aef_terminal.data.ibkr.session import (
    _connected_chart_ib_async,
    _connected_ib_async,
    _ibkr_session_ready,
    _reset_ibkr_async_chart_session,
    _reset_ibkr_async_session,
)
from aef_terminal.data.instrument_identity import (
    instrument_is_futures_root,
    qualified_instrument_id,
    require_exact_identity_text,
    route_fingerprint,
)
from aef_terminal.data.providers import route_instrument
from aef_terminal.features.provider_session import provider_session_intervals
from aef_terminal.runtime.chart_events import live_chart_bars_snapshot, publish_live_chart_bars
from aef_terminal.runtime.timeframes import interval_minutes, require_aware_utc_datetime

# keepUpToDate chart lane is the live OHLC source for chart rendering.
_CHART_LIVE_BARS_ENABLED = True
_CHART_LIVE_EVENT_TAIL = 64
_CHART_LIVE_UPDATE_TAIL = 4
_CHART_LIVE_INITIAL_TIMEOUT_SECONDS = 8.0


class IbkrBarFeed:
    """Interactive Brokers historical bar adapter.

    Keep all IBKR-specific reconnects, subscriptions, and pacing behavior here.
    The rest of the terminal consumes normalized Bar objects only.
    """

    def __init__(
        self,
        interval: str,
        range_: str,
        timeout: float = 8.0,
        host: str | None = None,
        port: int | None = None,
        client_id: int | None = None,
        readonly: bool | None = None,
        end_datetime: datetime | str | None = None,
        duration: str | None = None,
        *,
        instrument: dict[str, Any],
    ) -> None:
        config = AppConfig()
        self.instrument = _require_ibkr_instrument(instrument)
        self.provider_symbol = route_instrument(
            self.instrument,
            expected_source="ibkr",
        ).provider_symbol
        self.interval = interval
        self.range_ = range_
        self.timeout = timeout
        self.host = host or config.ibkr_host
        self.port = port or config.ibkr_port
        self.client_id = client_id or config.ibkr_history_client_id
        self.readonly = config.ibkr_readonly if readonly is None else readonly
        self.end_datetime = end_datetime
        self.duration = duration
        self._history_response_contract_identity: tuple[str, str] | None = None

    @property
    def history_response_contract_identity(self) -> tuple[str, str] | None:
        """Return the exact contract qualified for this owned historical response."""

        return self._history_response_contract_identity

    def bars(self) -> Iterator[Bar]:
        timeout = max(float(self.timeout) + 10.0, 15.0)
        bars = ibkr_market_data_manager.run_coroutine_blocking(
            "history",
            f"{self.provider_symbol} {self.interval} {self.range_}",
            lambda: self._bars_async_owned(),
            timeout=timeout,
        )
        yield from bars

    async def bars_async(self) -> list[Bar]:
        timeout = max(float(self.timeout) + 10.0, 15.0)
        return await ibkr_market_data_manager.run_coroutine(
            "history",
            f"{self.provider_symbol} {self.interval} {self.range_}",
            lambda: self._bars_async_owned(),
            timeout=timeout,
        )

    async def _bars_async_owned(self) -> list[Bar]:
        runtime = _IBKR_RUNTIME
        started = time.monotonic()
        self._history_response_contract_identity = None
        runtime.history_request_started_at = datetime.now(tz=UTC)
        runtime.history_last_activity_at = runtime.history_request_started_at
        runtime.history_request_label = f"{self.provider_symbol} {self.interval} {self.range_}"
        try:
            ib = await _connected_ib_async(
                self.host, self.port, self.client_id, self.readonly, self.timeout
            )
            contract = await _qualified_contract_async(
                ib,
                self.host,
                self.port,
                self.client_id,
                self.readonly,
                instrument=self.instrument,
            )
            contract_id = int(getattr(contract, "conId", 0) or 0)
            contract_type = str(getattr(contract, "secType", "") or "").strip().upper()
            if contract_id <= 0 or not contract_type:
                raise ValueError("IBKR_HISTORY_QUALIFIED_CONTRACT_IDENTITY_REQUIRED")
            self._history_response_contract_identity = (str(contract_id), contract_type)
            use_rth = _history_use_rth(self.instrument)
            history_timeout = max(float(self.timeout), 8.0)
            idle_timeout = max(history_timeout + 3.0, 5.0)
            watchdog = _AsyncIbkrActivityWatchdog(
                label=f"{self.provider_symbol} {self.interval} {self.range_}",
                idle_timeout=idle_timeout,
                on_timeout_disconnect=_reset_ibkr_async_session,
            )
            try:
                detach_watchdog = watchdog.attach_ib_update_event(ib)
            except Exception:
                _reset_ibkr_async_session()
                raise
            watchdog.start()
            try:
                is_futures_root = instrument_is_futures_root(self.instrument)
                raw_bars = await ib.reqHistoricalDataAsync(
                    contract,
                    endDateTime=("" if is_futures_root else (self.end_datetime or "")),
                    durationStr=self.duration or _duration_for_range(self.range_),
                    barSizeSetting=_bar_size_for_interval(self.interval),
                    whatToShow=_what_to_show(self.instrument),
                    useRTH=use_rth,
                    formatDate=2,
                    keepUpToDate=False,
                    # ib_async otherwise clears timed-out bars and returns a false-success [].
                    timeout=0,
                )
                watchdog.beat()
            except TimeoutError as exc:
                _reset_ibkr_async_session()
                raise TimeoutError(
                    f"IBKR async history request idle-timeout after {idle_timeout:.1f}s ({self.provider_symbol} {self.interval} {self.range_})"
                ) from exc
            except Exception as exc:
                if watchdog.timed_out:
                    _reset_ibkr_async_session()
                    raise TimeoutError(
                        f"IBKR async history request idle-timeout after {idle_timeout:.1f}s ({self.provider_symbol} {self.interval} {self.range_})"
                    ) from exc
                raise
            finally:
                detach_watchdog()
                await watchdog.cancel()
            confirmed_through: datetime | None = None
            if isinstance(self.end_datetime, datetime):
                confirmed_through = require_aware_utc_datetime(
                    self.end_datetime,
                    field="IBKR history end datetime",
                )
            bars = _bars_from_ibkr_sequence(
                raw_bars,
                self.provider_symbol,
                self.interval,
                provenance=_ibkr_bar_provenance(
                    request_type="historical",
                    what_to_show=_what_to_show(self.instrument),
                    instrument=self.instrument,
                    contract=contract,
                ),
                confirm_elapsed_latest=True,
                provider_response_complete=True,
                confirmed_through=confirmed_through,
                instrument=self.instrument,
            )
            runtime.last_history_error = ""
            return bars
        except asyncio.CancelledError:
            _reset_ibkr_async_session()
            runtime.last_history_error = f"IBKR history request cancelled ({self.provider_symbol} {self.interval} {self.range_})"
            raise
        except Exception as exc:
            runtime.last_history_error = str(exc) or exc.__class__.__name__
            if "ib" in locals() and not _ibkr_session_ready(ib):
                _reset_ibkr_async_session()
            raise
        finally:
            runtime.last_hist_duration_seconds = time.monotonic() - started
            runtime.history_request_started_at = None
            runtime.history_last_activity_at = None
            runtime.history_request_label = ""


async def chart_live_bars_async(
    interval: str,
    range_: str,
    timeout: float = 1.5,
    host: str | None = None,
    port: int | None = None,
    client_id: int | None = None,
    readonly: bool | None = None,
    tail: int = 24,
    *,
    instrument: dict[str, Any],
    consumer_id: str,
) -> list[Bar]:
    resolved_instrument = _require_ibkr_instrument(instrument)
    symbol = route_instrument(resolved_instrument, expected_source="ibkr").provider_symbol
    lease = str(consumer_id or "").strip()
    if not lease:
        raise ValueError("IBKR_CHART_CONSUMER_ID_REQUIRED")
    request_timeout = max(float(timeout), _CHART_LIVE_INITIAL_TIMEOUT_SECONDS)
    return await ibkr_market_data_manager.run_coroutine(
        "chart",
        f"{symbol} {interval}",
        lambda: _chart_live_bars_async_owned(
            interval,
            range_,
            request_timeout,
            host,
            port,
            client_id,
            readonly,
            tail,
            instrument=resolved_instrument,
            consumer_id=lease,
        ),
        timeout=request_timeout + 4.0,
    )


async def _chart_live_bars_async_owned(
    interval: str,
    range_: str,
    timeout: float = 1.5,
    host: str | None = None,
    port: int | None = None,
    client_id: int | None = None,
    readonly: bool | None = None,
    tail: int = 24,
    *,
    instrument: dict[str, Any],
    consumer_id: str,
) -> list[Bar]:
    """Async keepUpToDate chart bars without blocking FastAPI's threadpool."""
    if not _CHART_LIVE_BARS_ENABLED:
        return []
    instrument = _require_ibkr_instrument(instrument)
    symbol = route_instrument(instrument, expected_source="ibkr").provider_symbol
    runtime = _IBKR_RUNTIME
    cache_key = _chart_stream_cache_key(
        interval, range_, host, port, client_id, readonly, instrument=instrument
    )
    resolved_host, resolved_port, resolved_client_id, resolved_readonly = cache_key[:4]
    request_timeout = max(float(timeout), _CHART_LIVE_INITIAL_TIMEOUT_SECONDS)
    lease_registered = False
    try:
        ib = await _connected_chart_ib_async(
            resolved_host, resolved_port, resolved_client_id, resolved_readonly, request_timeout
        )
        contract = await _chart_contract_for_instrument_async(
            ib,
            resolved_host,
            resolved_port,
            resolved_client_id,
            resolved_readonly,
            instrument=instrument,
        )
        provenance = _ibkr_bar_provenance(
            request_type="keep_up_to_date",
            what_to_show=_what_to_show(instrument),
            instrument=instrument,
            contract=contract,
        )
        raw_bars = runtime.async_chart_streams.get(cache_key)
        if raw_bars is None:
            _cancel_replaced_chart_streams(
                cache_key,
                instrument_id=qualified_instrument_id(instrument),
            )
            raw_bars = await ib.reqHistoricalDataAsync(
                contract,
                endDateTime="",
                durationStr=_duration_for_chart_stream(interval),
                barSizeSetting=_bar_size_for_interval(interval),
                whatToShow=_what_to_show(instrument),
                useRTH=_history_use_rth(instrument),
                formatDate=2,
                keepUpToDate=True,
                timeout=request_timeout,
            )
            runtime.async_chart_streams[cache_key] = raw_bars
        runtime.async_chart_stream_consumers.setdefault(cache_key, set()).add(str(consumer_id))
        lease_registered = True
        callback_attached = _ensure_chart_stream_callback(
            cache_key,
            raw_bars,
            symbol=symbol,
            interval=interval,
            instrument=instrument,
            provenance=provenance,
        )
        if not callback_attached:
            runtime.discard_chart_stream(cache_key, cancel=True)
            raise RuntimeError("IBKR_CHART_UPDATE_CALLBACK_REQUIRED")
        await asyncio.sleep(0)
        runtime.last_chart_sync_at = datetime.now(tz=UTC)
        runtime.last_chart_error = ""
        bars = _chart_bars_from_raw(
            raw_bars,
            symbol,
            interval,
            max(int(tail), _CHART_LIVE_EVENT_TAIL),
            instrument=instrument,
            provenance=provenance,
        )
        publish_live_chart_bars(
            "ibkr",
            interval,
            instrument_id=qualified_instrument_id(instrument),
            route_fingerprint=route_fingerprint(instrument),
            bars=bars,
            reason="subscription_snapshot",
        )
        return bars[-max(int(tail), 1) :]
    except asyncio.CancelledError:
        if lease_registered:
            _release_chart_stream_lease(cache_key, consumer_id)
        raise
    except Exception as exc:
        runtime.discard_chart_stream(cache_key, cancel=True)
        if "ib" in locals() and not _ibkr_session_ready(ib):
            _reset_ibkr_async_chart_session()
        runtime.last_chart_error = str(exc)
        raise


def cached_chart_live_bars(
    interval: str,
    range_: str,
    host: str | None = None,
    port: int | None = None,
    client_id: int | None = None,
    readonly: bool | None = None,
    tail: int = 24,
    *,
    instrument: dict[str, Any],
) -> list[Bar]:
    """Read the immutable snapshot published by the broker-owner loop."""
    instrument = _require_ibkr_instrument(instrument)
    _chart_stream_cache_key(
        interval,
        range_,
        host,
        port,
        client_id,
        readonly,
        instrument=instrument,
    )
    snapshot = live_chart_bars_snapshot(
        interval,
        route_fingerprint(instrument),
        instrument_id=qualified_instrument_id(instrument),
    )
    if snapshot is None:
        return []
    return list(snapshot.bars[-max(int(tail), 1) :])


def _ensure_chart_stream_callback(
    cache_key: tuple,
    raw_bars: Any,
    *,
    symbol: str,
    interval: str,
    instrument: dict[str, Any],
    provenance: BarProvenance,
) -> bool:
    runtime = _IBKR_RUNTIME
    instrument_id = qualified_instrument_id(instrument)
    fingerprint = route_fingerprint(instrument)
    runtime.async_chart_stream_routes[cache_key] = (instrument_id, fingerprint, str(interval))
    if cache_key in runtime.async_chart_stream_callbacks:
        return True

    def publish_update(updated_bars: Any = raw_bars, *_args: Any) -> None:
        try:
            bars = _chart_bars_from_raw(
                updated_bars or raw_bars,
                symbol,
                interval,
                _CHART_LIVE_UPDATE_TAIL,
                instrument=instrument,
                provenance=provenance,
            )
            publish_live_chart_bars(
                "ibkr",
                interval,
                instrument_id=instrument_id,
                route_fingerprint=fingerprint,
                bars=bars,
            )
        except Exception as exc:
            runtime.last_chart_error = str(exc) or exc.__class__.__name__
            _LOGGER.debug(
                "IBKR chart update normalization failed for %s %s: %s",
                symbol,
                interval,
                exc,
                exc_info=True,
            )

    try:
        raw_bars.updateEvent += publish_update
    except Exception:
        return False
    runtime.async_chart_stream_callbacks[cache_key] = publish_update
    return True


def _chart_bars_from_raw(
    raw_bars,
    symbol: str,
    interval: str,
    tail: int,
    *,
    instrument: dict[str, Any],
    provenance: BarProvenance,
) -> list[Bar]:
    return _bars_from_ibkr_sequence(
        raw_bars,
        symbol,
        interval,
        provenance=provenance,
        instrument=instrument,
        tail=tail,
    )


def _bars_from_ibkr_sequence(
    raw_bars: Any,
    symbol: str,
    interval: str,
    *,
    provenance: BarProvenance,
    instrument: dict[str, Any],
    tail: int | None = None,
    confirm_elapsed_latest: bool = False,
    provider_response_complete: bool = False,
    confirmed_through: datetime | None = None,
) -> list[Bar]:
    if tail is not None:
        bounded_tail = max(int(tail), 1)
        try:
            items = list(raw_bars[-bounded_tail:])
        except TypeError, IndexError:
            items = list(raw_bars)[-bounded_tail:]
    else:
        items = list(raw_bars)
    if not items:
        return []
    normalized: list[Bar] = []
    last_index = len(items) - 1
    route = route_instrument(instrument, expected_source="ibkr")
    if (
        provenance.provider != route.provider
        or provenance.instrument_id != route.instrument_id
        or provenance.route_fingerprint != route.fingerprint
    ):
        raise ValueError("IBKR_BAR_PROVENANCE_ROUTE_MISMATCH")
    canonical_data_type = route.adapter.bar_data_type(route.instrument)
    provisional_data_type = provenance.data_type != canonical_data_type
    session_key = "liquid_intervals" if _history_use_rth(route.instrument) else "trading_intervals"
    session_intervals = provider_session_intervals(route.instrument, session_key)
    for index, item in enumerate(items):
        state = (
            BarState.AWAITING_PROVIDER_CONFIRMATION if provisional_data_type else BarState.CONFIRMED
        )
        if index == last_index and not provisional_data_type:
            ts = _normalize_ts(item.date)
            session_interval = next(
                (candidate for candidate in session_intervals if candidate.contains(ts)),
                None,
            )
            bucket = (
                route.adapter.provider_bar_bucket(
                    route.instrument,
                    ts,
                    interval,
                    session_open=session_interval.opens_at,
                    session_close=session_interval.closes_at,
                )
                if session_interval is not None
                else None
            )
            elapsed = bool(bucket is not None and datetime.now(tz=UTC) >= bucket.closes_at)
            conservative_close = ts + timedelta(minutes=max(interval_minutes(interval), 1))
            authoritative_close = bucket.closes_at if bucket is not None else conservative_close
            explicit_past_response = bool(
                confirm_elapsed_latest
                and provider_response_complete
                and confirmed_through is not None
                and authoritative_close <= confirmed_through.astimezone(UTC)
            )
            provider_confirmation_reached = (
                explicit_past_response if confirmed_through is not None else elapsed
            )
            state = (
                BarState.CONFIRMED
                if confirm_elapsed_latest and provider_confirmation_reached
                else BarState.AWAITING_PROVIDER_CONFIRMATION
                if (
                    elapsed
                    or bucket is None
                    or (
                        confirm_elapsed_latest
                        and provider_response_complete
                        and confirmed_through is not None
                        and ts < confirmed_through.astimezone(UTC)
                    )
                )
                else BarState.FORMING
            )
        normalized.append(
            _bar_from_ibkr_item(
                item,
                symbol,
                interval,
                provenance=provenance,
                state=state,
            )
        )
    return normalized


async def cancel_chart_live_bars_async(
    interval: str,
    range_: str,
    timeout: float = 1.0,
    host: str | None = None,
    port: int | None = None,
    client_id: int | None = None,
    readonly: bool | None = None,
    *,
    instrument: dict[str, Any],
    consumer_id: str,
) -> bool:
    resolved_instrument = _require_ibkr_instrument(instrument)
    symbol = route_instrument(resolved_instrument, expected_source="ibkr").provider_symbol
    lease = str(consumer_id or "").strip()
    if not lease:
        raise ValueError("IBKR_CHART_CONSUMER_ID_REQUIRED")
    return await ibkr_market_data_manager.run_coroutine(
        "chart",
        f"cancel {symbol} {interval}",
        lambda: _cancel_chart_live_bars_async_owned(
            interval,
            range_,
            timeout,
            host,
            port,
            client_id,
            readonly,
            instrument=resolved_instrument,
            consumer_id=lease,
        ),
        timeout=max(float(timeout) + 2.0, 3.0),
    )


async def _cancel_chart_live_bars_async_owned(
    interval: str,
    range_: str,
    timeout: float = 1.0,
    host: str | None = None,
    port: int | None = None,
    client_id: int | None = None,
    readonly: bool | None = None,
    *,
    instrument: dict[str, Any],
    consumer_id: str,
) -> bool:
    """Cancel one async keepUpToDate chart subscription if it is active."""
    runtime = _IBKR_RUNTIME
    cache_key = _chart_stream_cache_key(
        interval, range_, host, port, client_id, readonly, instrument=instrument
    )
    removed = _release_chart_stream_lease(cache_key, consumer_id)
    runtime.last_chart_error = ""
    return removed


def _release_chart_stream_lease(cache_key: tuple, consumer_id: str) -> bool:
    runtime = _IBKR_RUNTIME
    consumers = runtime.async_chart_stream_consumers.get(cache_key)
    if consumers is not None:
        consumers.discard(str(consumer_id))
        if consumers:
            return True
    return runtime.discard_chart_stream(cache_key, cancel=True)


def _chart_stream_base_key(
    interval: str,
    range_: str,
    host: str | None = None,
    port: int | None = None,
    client_id: int | None = None,
    readonly: bool | None = None,
) -> tuple[str, int, int, bool, str, str]:
    config = AppConfig()
    resolved_host = host or config.ibkr_host
    resolved_port = int(port or config.ibkr_port)
    resolved_client_id = int(client_id or config.ibkr_chart_client_id)
    resolved_readonly = config.ibkr_readonly if readonly is None else bool(readonly)
    # keepUpToDate duration is derived from the timeframe, not the viewport range.
    # Range belongs to DB recovery only and must not create another broker stream.
    return (resolved_host, resolved_port, resolved_client_id, resolved_readonly, str(interval), "")


def _chart_stream_cache_key(
    interval: str,
    range_: str,
    host: str | None = None,
    port: int | None = None,
    client_id: int | None = None,
    readonly: bool | None = None,
    *,
    instrument: dict[str, Any],
) -> tuple[str, int, int, bool, str, str, str]:
    base_key = _chart_stream_base_key(interval, range_, host, port, client_id, readonly)
    resolved_instrument = _require_ibkr_instrument(instrument)
    identity_token = _instrument_contract_cache_token(resolved_instrument)
    return (*base_key, identity_token)


def _cancel_replaced_chart_streams(wanted_key: tuple, *, instrument_id: str) -> None:
    """Cancel only the prior identity route for the same logical chart."""
    runtime = _IBKR_RUNTIME
    exact_instrument_id = require_exact_identity_text(
        instrument_id,
        field="instrument_id",
    )
    for key in list(runtime.async_chart_streams):
        route = runtime.async_chart_stream_routes.get(key)
        if (
            key == wanted_key
            or key[:6] != wanted_key[:6]
            or route is None
            or route[0] != exact_instrument_id
        ):
            continue
        runtime.discard_chart_stream(key, cancel=True)
