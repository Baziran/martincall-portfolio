from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from aef_terminal.data.adapters.base import ProviderAdapterBase
from aef_terminal.data.adapters._coinbase.client import (
    CoinbaseQuotePollingRuntime,
    bind_coinbase_product,
    search_coinbase_products,
)
from aef_terminal.data.adapters._coinbase.history import (
    CoinbaseHistoryRequest,
    CoinbaseHistoryResult,
    CoinbaseHistoryTerminal,
    coinbase_history_request_max_span,
    fetch_coinbase_chart_tail,
    fetch_coinbase_history,
)
from aef_terminal.data.instrument_identity import (
    provider_contract_id,
    provider_symbol,
    qualified_instrument_id,
    require_exact_identity_text,
    require_provider_identity,
    route_fingerprint,
)
from aef_terminal.data.provider_contract import (
    CanonicalBarCommitReceipt,
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
from aef_terminal.runtime.timeframes import (
    HistoryRangeWindow,
    interval_bucket,
    interval_seconds,
)
from aef_terminal.runtime.async_tasks import run_physical_thread_call
from aef_terminal.data.history_coverage import (
    HISTORY_COVERAGE_ADMISSION_CONTRACT_VERSION,
    HISTORY_COVERAGE_REQUEST_CONTRACT_VERSION,
)


_HISTORY_REQUEST_MODE = "exchange_get_product_candles"
_HISTORY_PROVIDER_SOURCE = "EXCHANGE"
_HISTORY_PROVIDER_CONTRACT_TYPE = "PRODUCT"
_HISTORY_DATA_TYPE = "TRADES"


class CoinbaseDataProvider(ProviderAdapterBase):
    manifest = ProviderManifest(
        key="coinbase",
        name="Coinbase Exchange",
        db_providers=("coinbase",),
        source_prefixes=("coinbase:",),
        session_scope=ProviderSessionScope.CONTINUOUS,
        latency="public exchange candles",
        status="history_public_api",
        requires=("Public Coinbase Exchange API",),
        note="Exchange-confirmed crypto OHLCV for configured Coinbase products.",
        capabilities=ProviderCapabilities(
            chart_stream=True,
            chart_tail_polling=True,
            gap_repair=True,
            exact_history_snapshot_authority=True,
            live_quote_polling=True,
            instrument_search=True,
            instrument_binding=True,
            read_only=True,
        ),
        data_policy=ProviderDataPolicy(cache_ttl_seconds=20, chart_poll_seconds=1.0),
    )

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
        from aef_terminal.data.provider_history import read_provider_history

        del timeout, live_refresh
        qualified = require_provider_identity(instrument, provider=self.key)
        provider_symbol(qualified, self.key)
        bars = read_provider_history(
            store,
            qualified,
            interval,
            range_,
            adapter=self,
            window=window,
        )
        return bars, ""

    def _history_request(
        self,
        instrument: dict[str, Any],
        timeframe: str,
        starts_at: datetime,
        ends_at: datetime,
    ) -> CoinbaseHistoryRequest:
        qualified = require_provider_identity(instrument, provider=self.key)
        return CoinbaseHistoryRequest(
            product_id=provider_contract_id(qualified),
            instrument_id=qualified_instrument_id(qualified),
            route_fingerprint=route_fingerprint(qualified),
            provider_symbol=provider_symbol(qualified, self.key),
            timeframe=timeframe,
            starts_at=starts_at,
            ends_at=ends_at,
        )

    def bar_data_type(self, instrument: dict[str, Any]) -> str:
        require_provider_identity(instrument, provider=self.key)
        return _HISTORY_DATA_TYPE

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
            provider_source=_HISTORY_PROVIDER_SOURCE,
            provider_contract_id=provider_contract_id(qualified),
            provider_contract_type=_HISTORY_PROVIDER_CONTRACT_TYPE,
            data_type=_HISTORY_DATA_TYPE,
        )

    def history_request_max_span(
        self,
        instrument: dict[str, Any],
        timeframe: str,
    ) -> timedelta:
        require_provider_identity(instrument, provider=self.key)
        return coinbase_history_request_max_span(timeframe)

    async def async_fetch_history(
        self,
        intent: HistoryRepairIntent,
        timeout: float,
        *,
        instrument: dict[str, Any],
    ) -> ProviderHistoryFetchResult:
        if not isinstance(intent, HistoryRepairIntent):
            raise TypeError("COINBASE_HISTORY_REPAIR_INTENT_REQUIRED")
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
            raise ValueError("COINBASE_HISTORY_REPAIR_ROUTE_MISMATCH")
        if intent.ends_at - intent.starts_at > self.history_request_max_span(
            qualified,
            intent.timeframe,
        ):
            raise ValueError("COINBASE_HISTORY_REPAIR_RANGE_TOO_LARGE")
        request = self._history_request(
            qualified,
            intent.timeframe,
            intent.starts_at,
            intent.ends_at,
        )
        result = await run_physical_thread_call(
            fetch_coinbase_history,
            request,
            timeout=timeout,
        )
        if not isinstance(result, CoinbaseHistoryResult) or result.request != request:
            raise RuntimeError("COINBASE_HISTORY_TYPED_RESULT_MISMATCH")
        if result.terminal is CoinbaseHistoryTerminal.COMPLETE:
            if result.response_count is None:
                raise RuntimeError("COINBASE_HISTORY_TERMINAL_COUNT_REQUIRED")
            completed_at = max(datetime.now(tz=UTC), intent.ends_at)
            return ProviderHistoryFetchResult(
                intent=intent,
                terminal=ProviderHistoryTerminal.COMPLETE,
                authoritative_bars=result.authoritative_bars,
                completions=(
                    HistoryRangeCompletion(
                        starts_at=intent.starts_at,
                        ends_at=intent.ends_at,
                        response_count=result.response_count,
                        completed_at=completed_at,
                        provider_source=result.provider_source,
                        provider_limit=result.provider_limit,
                    ),
                ),
            )
        if result.terminal is CoinbaseHistoryTerminal.INCOMPLETE:
            return ProviderHistoryFetchResult(
                intent=intent,
                terminal=ProviderHistoryTerminal.INCOMPLETE,
                authoritative_bars=result.authoritative_bars,
                error_code=result.error_code,
            )
        return ProviderHistoryFetchResult(
            intent=intent,
            terminal=ProviderHistoryTerminal.MALFORMED,
            authoritative_bars=(),
            error_code=result.error_code or "COINBASE_HISTORY_RESPONSE_MALFORMED",
        )

    def create_quote_polling_runtime(self) -> CoinbaseQuotePollingRuntime:
        return CoinbaseQuotePollingRuntime()

    async def async_chart_live_bars(
        self,
        interval: str,
        range_: str,
        *,
        timeout: float = 1.5,
        tail: int = 64,
        instrument: dict[str, Any],
        consumer_id: str,
    ) -> list[Bar]:
        del range_, consumer_id
        qualified = require_provider_identity(instrument, provider=self.key)
        step = timedelta(seconds=interval_seconds(interval))
        active_bucket = interval_bucket(datetime.now(tz=UTC), interval)
        max_tail = max(
            int(self.history_request_max_span(qualified, interval) / step),
            1,
        )
        bounded_tail = max(2, min(int(tail), max_tail))
        ends_at = active_bucket + step
        starts_at = ends_at - bounded_tail * step
        request = self._history_request(
            qualified,
            interval,
            starts_at,
            ends_at,
        )
        result = await run_physical_thread_call(
            fetch_coinbase_chart_tail,
            request,
            timeout=timeout,
            confirmed_before=active_bucket,
        )
        if not isinstance(result, CoinbaseHistoryResult) or result.request != request:
            raise RuntimeError("COINBASE_CHART_TAIL_TYPED_RESULT_MISMATCH")
        if result.terminal is CoinbaseHistoryTerminal.MALFORMED:
            raise RuntimeError(result.error_code or "COINBASE_CHART_TAIL_MALFORMED")
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
        confirmed_bars = [bar for bar in bars if BarState(bar.state) is BarState.CONFIRMED]
        if not confirmed_bars:
            return CanonicalBarCommitReceipt(revision_sequence=revision_sequence)
        if store is None:
            raise RuntimeError("CHART_COMMIT_STORE_REQUIRED provider=coinbase")
        if any(bar.timeframe != interval for bar in confirmed_bars):
            raise ValueError("COINBASE_CHART_COMMIT_TIMEFRAME_MISMATCH")
        receipt = await run_physical_thread_call(
            store.write_bars,
            confirmed_bars,
            self.key,
            instrument=qualified,
            revision_sequence=revision_sequence,
        )
        if not isinstance(receipt, CanonicalBarCommitReceipt):
            raise RuntimeError("COINBASE_CANONICAL_COMMIT_RECEIPT_REQUIRED")
        return receipt

    def search_instruments(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        return search_coinbase_products(query, limit=limit)

    def bind_instrument(self, provider_contract_id: str) -> dict[str, Any] | None:
        return bind_coinbase_product(
            require_exact_identity_text(
                provider_contract_id,
                field="provider_contract_id",
            )
        )

    def continuous_session(self, instrument: dict[str, Any]) -> bool:
        require_provider_identity(instrument, provider=self.key)
        return True
