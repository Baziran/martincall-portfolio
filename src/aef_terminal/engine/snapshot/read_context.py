from __future__ import annotations

import asyncio
import math
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from typing import Any

from aef_terminal.data.gex.constants import (
    GEX_CONTEXT_MAX_LEVELS,
)
from aef_terminal.data.gex.contracts import GexCaptureMode, gex_capture_lane
from aef_terminal.data.gex.history import (
    get_gex_history_series,
    gex_analysis_day_start,
    read_gex_snapshot_rows,
)
from aef_terminal.data.providers import (
    default_data_source,
    normalize_provider_key,
    route_instrument,
)
from aef_terminal.domain import Bar
from aef_terminal.engine.analyze.context import (
    option_flow_context_from_gex_history,
)
from aef_terminal.engine.analyze.inputs import resolve_analysis_as_of_utc
from aef_terminal.engine.snapshot.db_context import snapshot_db_context
from aef_terminal.engine.snapshot.market_inputs import (
    load_gex_history_input,
    load_option_targets_input,
    load_tick_flow_input,
)
from aef_terminal.engine.snapshot.vix_context import load_vix_context_input
from aef_terminal.engine.analyze.features import VixContextInput
from aef_terminal.indicators.registry import (
    enabled_confirmed_bar_context_requests,
    indicator_enabled,
    indicator_ids_for_shared_context,
)
from aef_terminal.runtime.mtf import ProviderBarSlotSequence
from aef_terminal.runtime.async_tasks import (
    run_cancellation_deferred,
    run_physical_thread_call,
)
from aef_terminal.runtime.timeframes import interval_minutes


class SnapshotReadContext:
    """Cohesive context object that manages database and analytics inputs for market snapshot assembly.

    Keeps each consumer on its canonical storage window and provides unified
    synchronous and asynchronous prefetching.
    """

    def __init__(
        self,
        store: Any | None,
        symbol: str,
        interval: str,
        range_: str,
        bars: Sequence[Bar],
        provider_key: str = default_data_source(),
        instrument: dict[str, Any] | None = None,
        indicator_params: dict[str, Any] | None = None,
        gex_context_active: bool = False,
        gex_capture_mode: GexCaptureMode = "request",
        analysis_as_of_utc: datetime | None = None,
    ) -> None:
        if type(gex_context_active) is not bool:
            raise TypeError("GEX context active state must be a boolean")
        gex_snapshot_source, exact_gex_capture_mode = gex_capture_lane(gex_capture_mode)
        self.store = store
        self.symbol = symbol
        self.interval = interval
        self.range_ = range_
        self.bars = bars
        self.provider_key = normalize_provider_key(provider_key)
        self.instrument = instrument
        self.indicator_params = dict(indicator_params or {})
        self.gex_context_active = gex_context_active
        self.gex_capture_mode = exact_gex_capture_mode
        self._gex_snapshot_source = gex_snapshot_source
        self.analysis_as_of_utc = resolve_analysis_as_of_utc(bars, analysis_as_of_utc)

        self._bar_slots: ProviderBarSlotSequence | None = None
        self._mtf_context: dict[str, list[Bar]] | None = None
        self._mtf_context_slots: dict[str, ProviderBarSlotSequence] | None = None
        self._mtf_quality: dict[str, dict[str, Any]] | None = None
        self._db_context_loaded: bool = False

        self._selected_gex_snapshot_rows: list[dict[str, Any]] | None = None
        self._selected_gex_snapshot_rows_max_hours: int = 0
        self._gex_history: list[dict[str, Any]] | None = None
        self._option_targets: list[dict[str, Any]] | None = None
        self._option_flow: dict[str, Any] | None = None
        self._tick_flow: dict[str, Any] | None = None
        self._vix_context_input: VixContextInput | None = None
        self._vix_context_loaded = False

    @property
    def gex_needed(self) -> bool:
        return self.gex_context_active

    def shared_context_consumers(self, context_ref: str) -> tuple[str, ...]:
        return tuple(
            indicator_id
            for indicator_id in indicator_ids_for_shared_context(context_ref)
            if indicator_enabled(self.indicator_params, indicator_id)
        )

    def shared_context_needed(self, context_ref: str) -> bool:
        return bool(self.shared_context_consumers(context_ref))

    def shared_context_hours(self, context_ref: str, default: int) -> int:
        param_key = f"{context_ref}_hours"
        required_hours = default
        for indicator_id in self.shared_context_consumers(context_ref):
            raw_params = self.indicator_params.get(indicator_id)
            if not isinstance(raw_params, dict) or param_key not in raw_params:
                continue
            value = raw_params[param_key]
            if type(value) is not int or not 1 <= value <= 24 * 30:
                raise ValueError(f"Snapshot {param_key} must be an integer from 1 to 720")
            required_hours = max(required_hours, value)
        return required_hours

    @property
    def option_targets_needed(self) -> bool:
        return self.shared_context_needed("option_targets")

    @property
    def option_flow_needed(self) -> bool:
        return self.shared_context_needed("option_flow")

    @property
    def tick_flow_needed(self) -> bool:
        return self.shared_context_needed("tick_flow")

    @property
    def vix_context_needed(self) -> bool:
        return self.shared_context_needed("vix_context")

    @property
    def provider_mtf_quality_needed(self) -> bool:
        return bool(self.confirmed_bar_context_timeframes) or self.shared_context_needed(
            "provider_mtf_quality"
        )

    @property
    def confirmed_bar_context_consumers(
        self,
    ) -> dict[str, tuple[tuple[str, Any], ...]]:
        return enabled_confirmed_bar_context_requests(self.indicator_params)

    @property
    def confirmed_bar_context_timeframes(self) -> tuple[str, ...]:
        parent_minutes = interval_minutes(self.interval)
        return tuple(
            timeframe
            for timeframe in self.confirmed_bar_context_consumers
            if interval_minutes(timeframe) < parent_minutes
        )

    @property
    def confirmed_bar_context_history_bars(self) -> dict[str, int]:
        return {
            timeframe: max(request.history_bars for _indicator_id, request in consumers)
            for timeframe, consumers in (self.confirmed_bar_context_consumers.items())
            if timeframe in self.confirmed_bar_context_timeframes
        }

    @property
    def option_flow_hours(self) -> int:
        return self.shared_context_hours("option_flow", 6)

    @property
    def gex_provider_symbol(self) -> str | None:
        route = route_instrument(self.instrument, expected_source=self.provider_key)
        return route.provider_symbol if route.adapter.capabilities.gex else None

    def _ensure_selected_gex_snapshot_rows(
        self,
        required_hours: int,
    ) -> list[dict[str, Any]]:
        if self.store is None:
            return []
        if type(required_hours) is not int or not 1 <= required_hours <= 24 * 30:
            raise ValueError("GEX snapshot lookback must be an integer from 1 to 720")
        lookback_hours = required_hours
        provider_symbol = self.gex_provider_symbol
        if not provider_symbol:
            return []
        if not self.option_flow_needed:
            return []
        if (
            self._selected_gex_snapshot_rows is not None
            and lookback_hours <= self._selected_gex_snapshot_rows_max_hours
        ):
            return self._selected_gex_snapshot_rows
        route = route_instrument(self.instrument, expected_source=self.provider_key)
        end = self.analysis_as_of_utc
        start = end - timedelta(hours=lookback_hours)
        if self.gex_needed:
            start = min(start, gex_analysis_day_start(end))
        rows = read_gex_snapshot_rows(
            start,
            end,
            instrument_id=route.instrument_id,
            route_fingerprint=route.fingerprint,
            limit=max(
                24 * 30,
                lookback_hours * 12 + 2,
                math.ceil(max((end - start).total_seconds(), 0.0) / 300.0) + 2,
            ),
            store=self.store,
            source=self._gex_snapshot_source,
        )
        if not isinstance(rows, list):
            raise TypeError("GEX snapshot storage must return a typed list")
        self._selected_gex_snapshot_rows = rows
        self._selected_gex_snapshot_rows_max_hours = lookback_hours
        return self._selected_gex_snapshot_rows or []

    def get_db_context(
        self,
    ) -> tuple[
        ProviderBarSlotSequence | None,
        dict[str, list[Bar]],
        dict[str, ProviderBarSlotSequence],
        dict[str, dict[str, Any]],
    ]:
        if not self._db_context_loaded:
            (
                self.bars,
                self._bar_slots,
                self._mtf_context,
                self._mtf_context_slots,
                self._mtf_quality,
            ) = snapshot_db_context(
                self.store,
                self.symbol,
                self.interval,
                self.range_,
                self.bars,
                provider_key=self.provider_key,
                instrument=self.instrument,
                include_mtf_quality=self.provider_mtf_quality_needed,
                mtf_timeframes=self.confirmed_bar_context_timeframes,
                mtf_history_bars=self.confirmed_bar_context_history_bars,
                analysis_as_of_utc=self.analysis_as_of_utc,
            )
            self._db_context_loaded = True
        return (
            self._bar_slots,
            self._mtf_context or {},
            self._mtf_context_slots or {},
            self._mtf_quality or {},
        )

    def get_gex_history(self) -> list[dict[str, Any]]:
        if not self.gex_needed:
            return []
        provider_symbol = self.gex_provider_symbol
        if provider_symbol is None:
            return []
        if self._gex_history is None:
            route = route_instrument(self.instrument, expected_source=self.provider_key)
            preloaded_rows = None
            if self.option_flow_needed:
                preloaded_rows = self._ensure_selected_gex_snapshot_rows(
                    self.option_flow_hours,
                )
            self._gex_history = load_gex_history_input(
                provider_symbol,
                instrument_id=route.instrument_id,
                route_fingerprint=route.fingerprint,
                capture_mode=self.gex_capture_mode,
                now=self.analysis_as_of_utc,
                store=self.store,
                preloaded_rows=preloaded_rows,
            )
        return self._gex_history

    def get_option_targets(self) -> list[dict[str, Any]]:
        if not self.option_targets_needed:
            return []
        if self._option_targets is None:
            route = route_instrument(self.instrument, expected_source=self.provider_key)
            self._option_targets = load_option_targets_input(
                self.store,
                instrument_id=route.instrument_id,
                route_fingerprint=route.fingerprint,
                timeframe=self.interval,
            )
        return self._option_targets

    def get_option_flow(self) -> dict[str, Any] | None:
        if not self.option_flow_needed:
            return None
        provider_symbol = self.gex_provider_symbol
        if provider_symbol is None:
            return None
        if self._option_flow is None:
            route = route_instrument(self.instrument, expected_source=self.provider_key)
            flow_hours = self.option_flow_hours
            if self._selected_gex_snapshot_rows is None:
                self._ensure_selected_gex_snapshot_rows(flow_hours)
            history = get_gex_history_series(
                provider_symbol,
                instrument_id=route.instrument_id,
                route_fingerprint=route.fingerprint,
                hours=flow_hours,
                max_levels=GEX_CONTEXT_MAX_LEVELS,
                now=self.analysis_as_of_utc,
                store=None,
                preloaded_rows=self._selected_gex_snapshot_rows,
                sources=(self._gex_snapshot_source,),
            )
            self._option_flow = option_flow_context_from_gex_history(
                provider_symbol,
                instrument_id=route.instrument_id,
                route_fingerprint=route.fingerprint,
                history=history,
                analysis_as_of_utc=self.analysis_as_of_utc,
            )
        return self._option_flow

    def get_tick_flow(self) -> dict[str, Any] | None:
        if not self.tick_flow_needed:
            return None
        if self._tick_flow is None:
            hours = self.shared_context_hours("tick_flow", 24)
            self._tick_flow = (
                load_tick_flow_input(self.store, self.instrument, self.bars, hours=hours) or {}
            )
        return self._tick_flow if isinstance(self._tick_flow, dict) and self._tick_flow else None

    def get_vix_context_input(self) -> VixContextInput | None:
        if not self.vix_context_needed:
            return None
        if not self._vix_context_loaded:
            self._vix_context_input = load_vix_context_input(
                self.store,
                analysis_as_of_utc=self.analysis_as_of_utc,
            )
            self._vix_context_loaded = True
        return self._vix_context_input

    def prefetch_sync(self) -> None:
        """Preload snapshot context and required database data synchronously."""
        self.get_db_context()
        if self.gex_needed or self.option_flow_needed:
            self._prefetch_gex_inputs()
        if self.option_targets_needed:
            self.get_option_targets()
        if self.tick_flow_needed and self.store is not None and self.bars:
            self.get_tick_flow()
        if self.vix_context_needed:
            self.get_vix_context_input()

    async def async_prefetch(self) -> None:
        """Preload snapshot context and required database data concurrently."""
        await run_cancellation_deferred(
            self._async_prefetch_owned(),
            task_cancelled_error="SNAPSHOT_PREFETCH_TASK_CANCELLED",
        )
        if self.tick_flow_needed and self.store is not None and self.bars:
            await run_physical_thread_call(self.get_tick_flow)

    async def _async_prefetch_owned(self) -> None:
        operations: list[Callable[[], Any]] = [self.get_db_context]
        if self.gex_needed or self.option_flow_needed:
            operations.append(self._prefetch_gex_inputs)
        if self.option_targets_needed:
            operations.append(self.get_option_targets)
        if self.vix_context_needed:
            operations.append(self.get_vix_context_input)
        outcomes = await asyncio.gather(
            *(asyncio.to_thread(operation) for operation in operations),
            return_exceptions=True,
        )
        for outcome in outcomes:
            if isinstance(outcome, BaseException):
                raise outcome

    def _prefetch_gex_inputs(self) -> None:
        if self.option_flow_needed:
            self._ensure_selected_gex_snapshot_rows(self.option_flow_hours)
        if self.gex_needed:
            self.get_gex_history()
