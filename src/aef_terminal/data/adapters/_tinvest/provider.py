from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from aef_terminal.config import AppConfig
from aef_terminal.data.adapters._tinvest.history import (
    TInvestHistoryRequest,
    TInvestHistoryResult,
    TInvestHistoryTerminal,
    fetch_tinvest_history,
    tinvest_history_request_max_span,
)
from aef_terminal.data.adapters._tinvest.qualification import (
    bind_tinvest_payload_sync,
    open_tinvest_services,
    search_tinvest_payloads_sync,
)
from aef_terminal.data.adapters._tinvest.quotes import TInvestQuotePollingRuntime
from aef_terminal.data.adapters._tinvest.schedule import (
    TInvestScheduleRequest,
    fetch_tinvest_schedule,
)
from aef_terminal.data.adapters.base import ProviderAdapterBase
from aef_terminal.data.instrument_identity import (
    instrument_is_exact_futures_contract,
    provider_contract_id,
    provider_symbol,
    qualified_instrument_id,
    require_provider_identity,
    route_fingerprint,
)
from aef_terminal.data.provider_contract import (
    CanonicalBarCommitReceipt,
    CanonicalHistoryRoute,
    HistoryRangeCompletion,
    HistoryRepairIntent,
    HistoryRequestAdmissionIdentity,
    ProviderCapabilities,
    ProviderDataPolicy,
    ProviderHistoryFetchResult,
    ProviderHistoryTerminal,
    ProviderManifest,
    ProviderSessionScope,
)
from aef_terminal.domain import Bar, BarProviderRequest, BarState
from aef_terminal.runtime.bar_quality import authoritative_bar_reject_reason
from aef_terminal.runtime.async_tasks import run_physical_thread_call
from aef_terminal.runtime.timeframes import (
    HistoryRangeWindow,
    history_range_window,
    interval_bucket,
    interval_seconds,
)
from aef_terminal.data.history_coverage import (
    HISTORY_COVERAGE_ADMISSION_CONTRACT_VERSION,
    HISTORY_COVERAGE_REQUEST_CONTRACT_VERSION,
)


_PROVIDER_CONTRACT_TYPE = "INSTRUMENT_UID"
_DATA_TYPE = "TRADES"
_PROVIDER_SOURCE = "EXCHANGE"
_HISTORY_REQUEST_MODE = "get_candles_exchange"


class TInvestDataProvider(ProviderAdapterBase):
    """Exact-UID T-Invest adapter implementation."""

    manifest = ProviderManifest(
        key="tinvest",
        name="T-Invest",
        db_providers=("tinvest",),
        source_prefixes=("tinvest:",),
        session_scope=ProviderSessionScope.INSTRUMENT,
        latency="exchange candles and last-price polling via broker API",
        status="exact_uid_exchange_market_data",
        requires=("AEF_TINVEST_TOKEN",),
        note=(
            "Exact T-Invest UID; exchange candles and exchange last prices only, "
            "without dealer-weekend mixing."
        ),
        capabilities=ProviderCapabilities(
            chart_stream=True,
            chart_tail_polling=True,
            live_quote_polling=True,
            gap_repair=True,
            exact_history_snapshot_authority=True,
            trading_hours=True,
            instrument_search=True,
            instrument_binding=True,
            canonical_futures_history=True,
            read_only=True,
        ),
        data_policy=ProviderDataPolicy(
            tail_delay_grace_seconds=0.0,
            cache_ttl_seconds=20,
            authoritative_ohlcv=True,
            sparse_sessions=True,
            chart_poll_seconds=10.0,
            chart_request_timeout_seconds=3.0,
        ),
    )

    @staticmethod
    def _token() -> str | None:
        return AppConfig().tinvest_token

    def search_instruments(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        return search_tinvest_payloads_sync(
            self._token(),
            query,
            max_results=limit,
        )

    def bind_instrument(self, provider_contract_id: str) -> dict[str, Any] | None:
        return bind_tinvest_payload_sync(self._token(), provider_contract_id)

    def create_quote_polling_runtime(self) -> TInvestQuotePollingRuntime:
        return TInvestQuotePollingRuntime(self._token())

    def bar_data_type(self, instrument: dict[str, Any]) -> str:
        require_provider_identity(instrument, provider=self.key)
        return _DATA_TYPE

    def continuous_session(self, instrument: dict[str, Any]) -> bool:
        require_provider_identity(instrument, provider=self.key)
        return False

    def history_session_scope(self, instrument: dict[str, Any]) -> str:
        require_provider_identity(instrument, provider=self.key)
        return ProviderSessionScope.TRADING.value

    async def fetch_trading_schedule(
        self,
        instrument: dict[str, Any],
        *,
        timeout: float = 6.0,
        starts_at: datetime | None = None,
        ends_at: datetime | None = None,
    ) -> dict[str, Any]:
        qualified = require_provider_identity(instrument, provider=self.key)
        if starts_at is None or ends_at is None:
            raise ValueError("TINVEST_SCHEDULE_EXACT_RANGE_REQUIRED")
        request = TInvestScheduleRequest(
            instrument_uid=provider_contract_id(qualified),
            starts_at=starts_at,
            ends_at=ends_at,
        )
        async with open_tinvest_services(self._token()) as services:
            return await fetch_tinvest_schedule(
                services,
                request,
                timeout=timeout,
            )

    def history_request_identity(
        self,
        instrument: dict[str, Any],
    ) -> HistoryRequestAdmissionIdentity:
        qualified = require_provider_identity(instrument, provider=self.key)
        return HistoryRequestAdmissionIdentity(
            request_contract_version=HISTORY_COVERAGE_REQUEST_CONTRACT_VERSION,
            admission_contract_version=HISTORY_COVERAGE_ADMISSION_CONTRACT_VERSION,
            request_mode=_HISTORY_REQUEST_MODE,
            request_type=BarProviderRequest.HISTORICAL,
            provider_source=_PROVIDER_SOURCE,
            provider_contract_id=provider_contract_id(qualified),
            provider_contract_type=_PROVIDER_CONTRACT_TYPE,
            data_type=_DATA_TYPE,
        )

    def history_request_max_span(
        self,
        instrument: dict[str, Any],
        timeframe: str,
    ) -> timedelta:
        require_provider_identity(instrument, provider=self.key)
        return tinvest_history_request_max_span(timeframe)

    async def async_fetch_history(
        self,
        intent: HistoryRepairIntent,
        timeout: float,
        *,
        instrument: dict[str, Any],
    ) -> ProviderHistoryFetchResult:
        if not isinstance(intent, HistoryRepairIntent):
            raise TypeError("TINVEST_HISTORY_REPAIR_INTENT_REQUIRED")
        qualified = require_provider_identity(instrument, provider=self.key)
        if (
            intent.provider,
            intent.instrument_id,
            intent.route_fingerprint,
            intent.request_identity,
        ) != (
            self.key,
            qualified_instrument_id(qualified),
            route_fingerprint(qualified),
            self.history_request_identity(qualified),
        ):
            raise ValueError("TINVEST_HISTORY_REPAIR_ROUTE_MISMATCH")
        if intent.ends_at - intent.starts_at > self.history_request_max_span(
            qualified,
            intent.timeframe,
        ):
            raise ValueError("TINVEST_HISTORY_REPAIR_RANGE_TOO_LARGE")
        request = self._history_request(
            qualified,
            intent.timeframe,
            intent.starts_at,
            intent.ends_at,
        )
        async with open_tinvest_services(self._token()) as services:
            result = await fetch_tinvest_history(
                services,
                request,
                timeout=timeout,
            )
        if not isinstance(result, TInvestHistoryResult) or result.request != request:
            raise RuntimeError("TINVEST_HISTORY_TYPED_RESULT_MISMATCH")
        if result.terminal is TInvestHistoryTerminal.COMPLETE:
            cursor = request.starts_at
            response_count = 0
            if result.source != _PROVIDER_SOURCE or not result.chunks:
                raise RuntimeError("TINVEST_HISTORY_TERMINAL_RESULT_INVALID")
            for chunk in result.chunks:
                if (
                    chunk.terminal is not TInvestHistoryTerminal.COMPLETE
                    or chunk.request.source != _PROVIDER_SOURCE
                    or chunk.request.starts_at != cursor
                    or chunk.request.ends_at > request.ends_at
                    or chunk.response_count is None
                    or chunk.response_count != chunk.authoritative_count
                    or chunk.provisional_count != 0
                ):
                    raise RuntimeError("TINVEST_HISTORY_TERMINAL_RESULT_INVALID")
                cursor = chunk.request.ends_at
                response_count += int(chunk.response_count)
            if (
                cursor != request.ends_at
                or response_count != len(result.authoritative_bars)
                or result.provisional_bars
            ):
                raise RuntimeError("TINVEST_HISTORY_TERMINAL_RESULT_INVALID")
            completed_at = max(datetime.now(tz=UTC), intent.ends_at)
            return ProviderHistoryFetchResult(
                intent=intent,
                terminal=ProviderHistoryTerminal.COMPLETE,
                authoritative_bars=result.authoritative_bars,
                completions=(
                    HistoryRangeCompletion(
                        starts_at=intent.starts_at,
                        ends_at=intent.ends_at,
                        response_count=len(result.authoritative_bars),
                        completed_at=completed_at,
                        provider_source=_PROVIDER_SOURCE,
                    ),
                ),
            )
        if result.terminal is TInvestHistoryTerminal.INCOMPLETE:
            return ProviderHistoryFetchResult(
                intent=intent,
                terminal=ProviderHistoryTerminal.INCOMPLETE,
                authoritative_bars=result.authoritative_bars,
            )
        return ProviderHistoryFetchResult(
            intent=intent,
            terminal=ProviderHistoryTerminal.MALFORMED,
            authoritative_bars=(),
            error_code=result.error_code or "TINVEST_HISTORY_RESPONSE_MALFORMED",
        )

    def canonical_history_route(
        self,
        instrument: dict[str, Any],
    ) -> CanonicalHistoryRoute | None:
        qualified = require_provider_identity(instrument, provider=self.key)
        if not instrument_is_exact_futures_contract(qualified):
            return None
        return CanonicalHistoryRoute.exact_futures_contract(provider_contract_id(qualified))

    def load_bars(
        self,
        instrument: dict[str, Any],
        interval: str,
        range_: str,
        timeout: float,
        live_refresh: bool = False,
        store: Any | None = None,
        *,
        window: HistoryRangeWindow | None = None,
    ) -> tuple[list[Bar], str]:
        del timeout, live_refresh
        from aef_terminal.data.provider_history import read_provider_history

        qualified = require_provider_identity(instrument, provider=self.key)
        requested_window = window or history_range_window(range_, interval)
        return (
            read_provider_history(
                store,
                qualified,
                interval,
                range_,
                adapter=self,
                window=requested_window,
            ),
            "",
        )

    @staticmethod
    def _history_request(
        instrument: dict[str, Any],
        interval: str,
        starts_at: datetime,
        ends_at: datetime,
    ) -> TInvestHistoryRequest:
        qualified = require_provider_identity(instrument, provider="tinvest")
        return TInvestHistoryRequest(
            instrument_uid=provider_contract_id(qualified),
            instrument_id=qualified_instrument_id(qualified),
            route_fingerprint=route_fingerprint(qualified),
            provider_symbol=provider_symbol(qualified, "tinvest"),
            timeframe=interval,
            starts_at=starts_at,
            ends_at=ends_at,
        )

    async def async_chart_live_bars(
        self,
        interval: str,
        range_: str,
        *,
        timeout: float = 3.0,
        tail: int = 64,
        instrument: dict[str, Any],
        consumer_id: str,
    ) -> list[Bar]:
        del range_, consumer_id
        qualified = require_provider_identity(instrument, provider=self.key)
        step = timedelta(seconds=interval_seconds(interval))
        ends_at = interval_bucket(datetime.now(tz=UTC), interval) + step
        starts_at = ends_at - max(int(tail), 1) * step
        request = self._history_request(
            qualified,
            interval,
            starts_at,
            ends_at,
        )
        async with open_tinvest_services(self._token()) as services:
            result = await fetch_tinvest_history(
                services,
                request,
                timeout=timeout,
            )
        if result.terminal is TInvestHistoryTerminal.MALFORMED:
            raise RuntimeError(result.error_code or "TINVEST_CHART_TAIL_MALFORMED")
        return sorted(
            (*result.authoritative_bars, *result.provisional_bars),
            key=lambda bar: bar.ts,
        )

    async def async_commit_chart_bars(
        self,
        bars: list[Bar],
        *,
        interval: str,
        store: Any | None,
        instrument: dict[str, Any],
        revision_sequence: int,
    ) -> CanonicalBarCommitReceipt:
        qualified = require_provider_identity(instrument, provider=self.key)
        confirmed = [bar for bar in bars if BarState(bar.state) is BarState.CONFIRMED]
        if not confirmed:
            return CanonicalBarCommitReceipt(revision_sequence=revision_sequence)
        if store is None:
            raise RuntimeError("CHART_COMMIT_STORE_REQUIRED provider=tinvest")
        for bar in confirmed:
            if bar.timeframe != interval:
                raise ValueError("TINVEST_CHART_COMMIT_TIMEFRAME_MISMATCH")
            reject_reason = authoritative_bar_reject_reason(
                self.key,
                bar,
                instrument_id=qualified_instrument_id(qualified),
                route_fingerprint=route_fingerprint(qualified),
                provider_contract_id=provider_contract_id(qualified),
                provider_contract_type=_PROVIDER_CONTRACT_TYPE,
                data_type=_DATA_TYPE,
            )
            if reject_reason is not None:
                raise RuntimeError(
                    "TINVEST_CHART_COMMIT_BAR_REJECTED "
                    f"reason={reject_reason} ts={bar.ts.isoformat()}"
                )
        history_route = self.canonical_history_route(qualified)
        if history_route is None:
            receipt = await run_physical_thread_call(
                store.write_bars,
                confirmed,
                self.key,
                instrument=qualified,
                revision_sequence=revision_sequence,
            )
        else:
            receipt = await run_physical_thread_call(
                store.write_futures_contract_bars,
                confirmed,
                provider=self.key,
                instrument_id=qualified_instrument_id(qualified),
                route_fingerprint=route_fingerprint(qualified),
                contract_key=history_route.contract_key,
                provider_contract_id=provider_contract_id(qualified),
                provider_contract_type=_PROVIDER_CONTRACT_TYPE,
                data_type=_DATA_TYPE,
                revision_sequence=revision_sequence,
            )
        if not isinstance(receipt, CanonicalBarCommitReceipt):
            raise RuntimeError("TINVEST_CANONICAL_COMMIT_RECEIPT_REQUIRED")
        return receipt
