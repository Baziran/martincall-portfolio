from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from aef_terminal.data.adapters.base import ProviderAdapterBase
from aef_terminal.data.ibkr.exact_history import (
    IbkrHistoryRequest,
    IbkrHistoryResult,
    IbkrHistoryTerminal,
    fetch_ibkr_exact_history,
    ibkr_history_request_max_span,
)
from aef_terminal.data.ibkr.history import (
    IBKR_CONTINUOUS_HISTORY_ROUTE,
    write_continuous_history,
)
from aef_terminal.data.ibkr.rolling_history import (
    IBKR_ROLLING_HISTORY_ADMISSION_CONTRACT_VERSION,
    IBKR_ROLLING_HISTORY_PROVIDER_SOURCE,
    IBKR_ROLLING_HISTORY_REQUEST_CONTRACT_VERSION,
    IbkrRollingHistoryRequest,
    IbkrRollingHistoryResult,
    IbkrRollingHistoryTerminal,
    fetch_ibkr_rolling_history,
    ibkr_rolling_history_request_max_span,
)
from aef_terminal.data.instrument_identity import (
    futures_root,
    identity_payload,
    instrument_asset_class,
    instrument_is_exact_futures_contract,
    instrument_is_futures,
    instrument_is_futures_root,
    provider_contract_id,
    provider_symbol,
    qualified_instrument_id,
    require_provider_identity,
    route_fingerprint,
)
from aef_terminal.data.provider_contract import (
    CanonicalBarCommitReceipt,
    CanonicalHistoryRoute,
    HistoryContractResolution,
    HistoryRangeCompletion,
    HistoryRepairIntent,
    HistoryRequestAdmissionIdentity,
    HistoryRequestUnsupportedError,
    InstrumentRoute,
    ProviderBarBucket,
    ProviderCapabilities,
    ProviderDataPolicy,
    ProviderHistoryFetchResult,
    ProviderHistoryTerminal,
    ProviderManifest,
    ProviderSessionScope,
    QuoteSnapshotRead,
)
from aef_terminal.runtime.math_utils import float_or_none
from aef_terminal.runtime.async_tasks import run_physical_thread_call
from aef_terminal.domain import Bar, BarProviderRequest, BarState
from aef_terminal.runtime.bar_quality import authoritative_bar_reject_reason
from aef_terminal.runtime.timeframes import HistoryRangeWindow, require_aware_utc_datetime
from aef_terminal.data.history_coverage import (
    HISTORY_COVERAGE_ADMISSION_CONTRACT_VERSION,
    HISTORY_COVERAGE_REQUEST_CONTRACT_VERSION,
)


_HISTORY_REQUEST_MODE = "req_historical_data"
_HISTORY_RANGE_NORMALIZATION = "filter_valid_provider_overrun_v1"
_ROLLING_HISTORY_REQUEST_MODE = "rolling_end_now"
_HISTORY_PROVIDER_SOURCE = "IBKR_HISTORICAL"
_HISTORY_DATA_TYPE = "TRADES"


class IbkrDataProvider(ProviderAdapterBase):
    manifest = ProviderManifest(
        key="ibkr",
        name="Interactive Brokers",
        db_providers=("ibkr",),
        source_prefixes=("ibkr:",),
        session_scope=ProviderSessionScope.INSTRUMENT,
        latency="single live source",
        status="ready_when_gateway_running",
        requires=("IB Gateway / TWS",),
        note="Primary trading source. History, live bars, option data, and trade-relevant quotes come from IBKR.",
        capabilities=ProviderCapabilities(
            chart_stream=True,
            native_chart_stream=True,
            live_quote_stream=True,
            quote_trade_events=True,
            gap_repair=True,
            exact_history_snapshot_authority=True,
            trading_hours=True,
            runtime_settings=True,
            instrument_search=True,
            instrument_binding=True,
            canonical_futures_history=True,
            gex=True,
            options=True,
            read_only=False,
        ),
        data_policy=ProviderDataPolicy(cache_ttl_seconds=20),
    )

    def read_quote_trade_events(
        self,
        after_sequence: int,
        route_identities: tuple[tuple[str, str], ...],
    ) -> tuple[int, list[dict[str, Any]], bool]:
        from aef_terminal.data.ibkr.quotes import read_live_quote_trade_events

        return read_live_quote_trade_events(after_sequence, route_identities)

    def history_session_scope(self, instrument: dict[str, Any]) -> str:
        from aef_terminal.data.ibkr.contracts import _history_use_rth

        qualified = require_provider_identity(instrument, provider=self.key)
        return "liquid" if _history_use_rth(qualified) else "trading"

    def bar_data_type(self, instrument: dict[str, Any]) -> str:
        require_provider_identity(instrument, provider=self.key)
        return _HISTORY_DATA_TYPE

    def _history_contract_identity(
        self,
        instrument: dict[str, Any],
    ) -> tuple[str, str]:
        from aef_terminal.data.ibkr.contracts import _what_to_show

        qualified = require_provider_identity(instrument, provider=self.key)
        if instrument_is_futures_root(qualified):
            raise HistoryRequestUnsupportedError(
                provider=self.key,
                code="IBKR_HISTORY_FUTURE_ROOT_EXACT_RANGE_UNSUPPORTED",
            )
        if _what_to_show(qualified) != _HISTORY_DATA_TYPE:
            raise HistoryRequestUnsupportedError(
                provider=self.key,
                code="IBKR_HISTORY_TRADES_REQUIRED",
            )
        contract_id = provider_contract_id(qualified)
        contract_type = str(identity_payload(qualified).get("sec_type") or "")
        if not contract_id or not contract_type:
            raise HistoryRequestUnsupportedError(
                provider=self.key,
                code="IBKR_HISTORY_EXACT_CONTRACT_REQUIRED",
            )
        return contract_id, contract_type

    def history_request_identity(
        self,
        instrument: dict[str, Any],
    ) -> HistoryRequestAdmissionIdentity:
        qualified = require_provider_identity(instrument, provider=self.key)
        contract_identity = identity_payload(qualified)
        is_futures_root = instrument_is_futures_root(qualified)
        if is_futures_root:
            from aef_terminal.data.ibkr.contracts import _what_to_show

            if _what_to_show(qualified) != _HISTORY_DATA_TYPE:
                raise HistoryRequestUnsupportedError(
                    provider=self.key,
                    code="IBKR_HISTORY_TRADES_REQUIRED",
                )
            contract_id = ""
            contract_type = "CONTFUT"
        else:
            contract_id, contract_type = self._history_contract_identity(qualified)
        primary_exchange = str(contract_identity.get("primary_exchange") or "")
        configured_exchange = str(contract_identity.get("exchange") or "")
        request_exchange = (
            primary_exchange if contract_type == "IND" and primary_exchange else configured_exchange
        )
        request_mode_payload = {
            "currency": str(contract_identity.get("currency") or ""),
            "exchange": request_exchange,
            "mode": _ROLLING_HISTORY_REQUEST_MODE if is_futures_root else _HISTORY_REQUEST_MODE,
            "primary_exchange": primary_exchange,
            "use_rth": self.history_session_scope(qualified) == "liquid",
        }
        if not is_futures_root:
            request_mode_payload["range_normalization"] = _HISTORY_RANGE_NORMALIZATION
        request_mode = json.dumps(
            request_mode_payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return HistoryRequestAdmissionIdentity(
            request_contract_version=(
                IBKR_ROLLING_HISTORY_REQUEST_CONTRACT_VERSION
                if is_futures_root
                else HISTORY_COVERAGE_REQUEST_CONTRACT_VERSION
            ),
            admission_contract_version=(
                IBKR_ROLLING_HISTORY_ADMISSION_CONTRACT_VERSION
                if is_futures_root
                else HISTORY_COVERAGE_ADMISSION_CONTRACT_VERSION
            ),
            request_mode=request_mode,
            request_type=BarProviderRequest.HISTORICAL,
            provider_source=(
                IBKR_ROLLING_HISTORY_PROVIDER_SOURCE
                if is_futures_root
                else _HISTORY_PROVIDER_SOURCE
            ),
            provider_contract_id=contract_id,
            provider_contract_type=contract_type,
            data_type=_HISTORY_DATA_TYPE,
            contract_resolution=(
                HistoryContractResolution.PROVIDER_RESPONSE
                if is_futures_root
                else HistoryContractResolution.EXACT
            ),
        )

    def history_request_max_span(
        self,
        instrument: dict[str, Any],
        timeframe: str,
    ) -> timedelta:
        qualified = require_provider_identity(instrument, provider=self.key)
        if instrument_is_futures_root(qualified):
            return ibkr_rolling_history_request_max_span(timeframe)
        self._history_contract_identity(qualified)
        return ibkr_history_request_max_span(timeframe)

    async def async_fetch_history(
        self,
        intent: HistoryRepairIntent,
        timeout: float,
        *,
        instrument: dict[str, Any],
    ) -> ProviderHistoryFetchResult:
        if not isinstance(intent, HistoryRepairIntent):
            raise TypeError("IBKR_HISTORY_REPAIR_INTENT_REQUIRED")
        qualified = require_provider_identity(instrument, provider=self.key)
        identity = self.history_request_identity(qualified)
        if (
            intent.provider,
            intent.instrument_id,
            intent.route_fingerprint,
            intent.request_identity,
        ) != (
            self.key,
            qualified_instrument_id(qualified),
            route_fingerprint(qualified),
            identity,
        ):
            raise ValueError("IBKR_HISTORY_REPAIR_ROUTE_MISMATCH")
        if intent.ends_at - intent.starts_at > self.history_request_max_span(
            qualified,
            intent.timeframe,
        ):
            raise ValueError("IBKR_HISTORY_REPAIR_RANGE_TOO_LARGE")
        if instrument_is_futures_root(qualified):
            request = IbkrRollingHistoryRequest(
                instrument_id=qualified_instrument_id(qualified),
                route_fingerprint=route_fingerprint(qualified),
                provider_symbol=provider_symbol(qualified, self.key),
                timeframe=intent.timeframe,
                starts_at=intent.starts_at,
                ends_at=intent.ends_at,
                requested_at=datetime.now(tz=UTC),
            )
            rolling_result = await fetch_ibkr_rolling_history(
                request,
                timeout=timeout,
                instrument=qualified,
            )
            if (
                not isinstance(rolling_result, IbkrRollingHistoryResult)
                or rolling_result.request != request
            ):
                raise RuntimeError("IBKR_ROLLING_HISTORY_TYPED_RESULT_MISMATCH")
            if rolling_result.terminal is IbkrRollingHistoryTerminal.MALFORMED:
                return ProviderHistoryFetchResult(
                    intent=intent,
                    terminal=ProviderHistoryTerminal.MALFORMED,
                    authoritative_bars=(),
                    error_code=(
                        rolling_result.error_code or "IBKR_ROLLING_HISTORY_RESPONSE_MALFORMED"
                    ),
                )
            terminal = (
                ProviderHistoryTerminal.ROLLING_COMPLETE
                if rolling_result.terminal is IbkrRollingHistoryTerminal.TARGET_CONTAINED
                else ProviderHistoryTerminal.ROLLING_PARTIAL
            )
            return ProviderHistoryFetchResult(
                intent=intent,
                terminal=terminal,
                authoritative_bars=rolling_result.authoritative_bars,
                error_code=rolling_result.error_code,
                resolved_provider_contract_id=(rolling_result.resolved_provider_contract_id),
                response_starts_at=rolling_result.response_starts_at,
                response_ends_at=rolling_result.response_ends_at,
            )

        request = IbkrHistoryRequest(
            provider_contract_id=identity.provider_contract_id,
            provider_contract_type=identity.provider_contract_type,
            instrument_id=qualified_instrument_id(qualified),
            route_fingerprint=route_fingerprint(qualified),
            provider_symbol=provider_symbol(qualified, self.key),
            timeframe=intent.timeframe,
            starts_at=intent.starts_at,
            ends_at=intent.ends_at,
        )
        result = await fetch_ibkr_exact_history(
            request,
            timeout=timeout,
            instrument=qualified,
        )
        if not isinstance(result, IbkrHistoryResult) or result.request != request:
            raise RuntimeError("IBKR_HISTORY_TYPED_RESULT_MISMATCH")
        if result.terminal is IbkrHistoryTerminal.COMPLETE:
            if result.response_count is None:
                raise RuntimeError("IBKR_HISTORY_COMPLETION_PROOF_REQUIRED")
            return ProviderHistoryFetchResult(
                intent=intent,
                terminal=ProviderHistoryTerminal.COMPLETE,
                authoritative_bars=result.authoritative_bars,
                completions=(
                    HistoryRangeCompletion(
                        starts_at=intent.starts_at,
                        ends_at=intent.ends_at,
                        response_count=result.response_count,
                        completed_at=max(datetime.now(tz=UTC), intent.ends_at),
                        provider_source=result.provider_source,
                    ),
                ),
            )
        if result.terminal is IbkrHistoryTerminal.INCOMPLETE:
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
            error_code=result.error_code or "IBKR_HISTORY_RESPONSE_MALFORMED",
        )

    def _provider_bar_bucket_for_qualified(
        self,
        instrument: dict[str, Any],
        ts: datetime,
        interval: str,
        *,
        session_open: datetime | None = None,
        session_close: datetime | None = None,
    ) -> ProviderBarBucket | None:
        """IBKR timestamps the opening partial bar at the exact session open."""

        bucket = super()._provider_bar_bucket_for_qualified(
            instrument,
            ts,
            interval,
            session_open=session_open,
            session_close=session_close,
        )
        if bucket is None or session_open is None:
            return bucket
        opens_at = require_aware_utc_datetime(session_open, field="IBKR session open")
        starts_at = max(bucket.starts_at, opens_at)
        if bucket.closes_at <= starts_at:
            return None
        return ProviderBarBucket(starts_at=starts_at, closes_at=bucket.closes_at)

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
        from aef_terminal.data.adapters.ibkr_bars import load_ibkr_bars

        return load_ibkr_bars(
            instrument,
            interval,
            range_,
            timeout,
            live_refresh=live_refresh,
            store=store,
            adapter=self,
            window=window,
        )

    async def async_load_bars(
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
        from aef_terminal.data.adapters.ibkr_bars import async_load_ibkr_bars

        return await async_load_ibkr_bars(
            instrument,
            interval,
            range_,
            timeout,
            live_refresh=live_refresh,
            store=store,
            adapter=self,
            window=window,
        )

    async def async_chart_live_bars(
        self,
        interval: str,
        range_: str,
        *,
        timeout: float = 1.5,
        tail: int = 24,
        instrument: dict[str, Any],
        consumer_id: str,
    ) -> list[Bar]:
        from aef_terminal.data.ibkr.bars import chart_live_bars_async

        qualified = require_provider_identity(instrument, provider=self.key)
        bars = await chart_live_bars_async(
            interval,
            range_,
            timeout=timeout,
            tail=tail,
            instrument=qualified,
            consumer_id=consumer_id,
        )
        return bars

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
            raise RuntimeError("CHART_COMMIT_STORE_REQUIRED provider=ibkr")
        for bar in confirmed_bars:
            reject_reason = authoritative_bar_reject_reason(
                self.key,
                bar,
                instrument_id=qualified_instrument_id(qualified),
                route_fingerprint=route_fingerprint(qualified),
                data_type=self.bar_data_type(qualified),
            )
            if reject_reason is not None:
                raise RuntimeError(
                    "CHART_COMMIT_BAR_REJECTED "
                    f"provider={self.key} reason={reject_reason} ts={bar.ts.isoformat()}"
                )

        from aef_terminal.data.adapters.ibkr_bars import (
            _select_ibkr_live_bar_storage_route_async,
        )

        receipt, warning = await _select_ibkr_live_bar_storage_route_async(qualified)(
            store,
            confirmed_bars,
            interval,
            "live",
            qualified,
            revision_sequence,
        )
        if warning:
            raise RuntimeError(warning)
        return receipt

    def cached_chart_live_bars(
        self,
        interval: str,
        range_: str,
        *,
        tail: int = 24,
        instrument: dict[str, Any],
    ) -> list[Bar]:
        from aef_terminal.data.ibkr.bars import cached_chart_live_bars

        qualified = require_provider_identity(instrument, provider=self.key)
        return cached_chart_live_bars(
            interval,
            range_,
            tail=tail,
            instrument=qualified,
        )

    async def async_cancel_chart_live_bars(
        self,
        interval: str,
        range_: str,
        *,
        timeout: float = 1.0,
        instrument: dict[str, Any],
        consumer_id: str,
    ) -> bool:
        from aef_terminal.data.ibkr.bars import cancel_chart_live_bars_async

        qualified = require_provider_identity(instrument, provider=self.key)
        return await cancel_chart_live_bars_async(
            interval,
            range_,
            timeout=timeout,
            instrument=qualified,
            consumer_id=consumer_id,
        )

    def quote_subscription_generation(self) -> str:
        from aef_terminal.data.ibkr.quotes import quote_subscription_generation

        return quote_subscription_generation()

    async def async_sync_quote_subscriptions(
        self,
        instruments: list[dict[str, Any]],
        **kwargs: Any,
    ) -> dict[str, int]:
        from aef_terminal.data.ibkr.quotes import sync_quote_subscriptions_async

        qualified = [require_provider_identity(item, provider=self.key) for item in instruments]
        return await sync_quote_subscriptions_async(qualified, **kwargs)

    def cached_quotes(
        self,
        routes: Sequence[InstrumentRoute],
        *,
        after_sequence: int | None = None,
    ) -> QuoteSnapshotRead:
        from aef_terminal.data.ibkr.quotes import cached_quotes

        return cached_quotes(routes, after_sequence=after_sequence)

    async def async_load_gex(self, instrument: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        from aef_terminal.data.gex.context import async_gex_context
        from aef_terminal.data.ibkr.gex_runtime import ibkr_gex_provider_runtime
        from aef_terminal.data.providers import route_instrument

        qualified = require_provider_identity(instrument, provider=self.key)
        if kwargs.get("refresh"):
            quotes = self.cached_quotes([route_instrument(qualified)]).quotes
            fingerprint = route_fingerprint(qualified)
            quote = quotes.get(fingerprint) if isinstance(quotes.get(fingerprint), dict) else {}
            kwargs["underlying_quote"] = dict(quote)
        return await async_gex_context(
            instrument=qualified,
            provider_runtime=ibkr_gex_provider_runtime,
            **kwargs,
        )

    async def async_load_live_gex(
        self, instrument: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        from aef_terminal.data.gex.live import async_live_gex_context
        from aef_terminal.data.ibkr.gex_runtime import ibkr_gex_provider_runtime
        from aef_terminal.data.providers import route_instrument

        qualified = require_provider_identity(instrument, provider=self.key)
        quotes = self.cached_quotes([route_instrument(qualified)]).quotes
        fingerprint = route_fingerprint(qualified)
        quote = quotes.get(fingerprint) if isinstance(quotes.get(fingerprint), dict) else {}
        kwargs["underlying_quote"] = dict(quote)
        return await async_live_gex_context(
            instrument=qualified,
            provider_runtime=ibkr_gex_provider_runtime,
            **kwargs,
        )

    async def async_stop_live_gex(self, instrument: dict[str, Any]) -> dict[str, Any]:
        from aef_terminal.data.gex.live import async_stop_live_gex_context
        from aef_terminal.data.ibkr.gex_runtime import ibkr_gex_provider_runtime

        qualified = require_provider_identity(instrument, provider=self.key)
        return await async_stop_live_gex_context(
            qualified,
            provider_runtime=ibkr_gex_provider_runtime,
        )

    def cached_gex(self, instrument: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        from aef_terminal.data.gex.context import cached_gex_context

        qualified = require_provider_identity(instrument, provider=self.key)
        return cached_gex_context(instrument=qualified, **kwargs)

    def gex_request_config(self, instrument: dict[str, Any], **kwargs: Any) -> Any:
        from aef_terminal.data.ibkr.gex_runtime import ibkr_gex_provider_runtime

        qualified = require_provider_identity(instrument, provider=self.key)
        return ibkr_gex_provider_runtime.request_config(
            instrument=qualified,
            mode=str(kwargs.pop("mode", "manual")),
            **kwargs,
        )

    def gex_request_for_job(self, instrument: dict[str, Any], job: Any, **kwargs: Any) -> Any:
        from aef_terminal.data.gex.scheduler import gex_request_for_scheduler_job
        from aef_terminal.data.ibkr.gex_runtime import ibkr_gex_provider_runtime

        qualified = require_provider_identity(instrument, provider=self.key)
        base = ibkr_gex_provider_runtime.request_config(
            instrument=qualified,
            store=kwargs.pop("store", None),
            mode="auto",
        )
        if kwargs:
            raise ValueError(f"GEX_SCHEDULER_ARGUMENTS_UNSUPPORTED keys={sorted(kwargs)}")
        return gex_request_for_scheduler_job(
            job,
            instrument=qualified,
            base=base,
        )

    def option_contract_universe(
        self,
        instrument: dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        from aef_terminal.data.ibkr.options import option_contract_universe

        qualified = require_provider_identity(instrument, provider=self.key)
        return option_contract_universe(qualified, **kwargs)

    async def async_load_option_board(
        self,
        instrument: dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        from aef_terminal.data.ibkr.options import (
            _option_board_spot_reference_state,
            async_option_board_snapshot,
        )
        from aef_terminal.data.providers import route_instrument
        from aef_terminal.data.ibkr.option_contracts import (
            read_persisted_ibkr_option_expiry_facts,
            require_ibkr_option_expiry_facts,
        )

        qualified = require_provider_identity(instrument, provider=self.key)
        if "spot" in kwargs:
            raise ValueError("IBKR_OPTION_BOARD_SPOT_IS_ADAPTER_OWNED")
        preloaded_expiry_facts = kwargs.pop(
            "option_expiry_facts",
            None,
        )
        store = kwargs.pop("store", None)
        if preloaded_expiry_facts is not None and store is not None:
            raise ValueError("IBKR_OPTION_EXPIRY_FACT_SOURCE_IS_AMBIGUOUS")
        option_expiry_facts = (
            require_ibkr_option_expiry_facts(preloaded_expiry_facts)
            if preloaded_expiry_facts is not None
            else await run_physical_thread_call(
                read_persisted_ibkr_option_expiry_facts,
                instrument_id=qualified_instrument_id(qualified),
                route_fingerprint=route_fingerprint(qualified),
                store=store,
            )
        )
        quotes = self.cached_quotes([route_instrument(qualified)]).quotes
        cached_quote = quotes.get(route_fingerprint(qualified))
        quote = cached_quote if isinstance(cached_quote, dict) else {}
        cached_spot = float_or_none(quote.get("price"))
        spot = (
            cached_spot
            if cached_spot is not None and (instrument_is_futures(qualified) or cached_spot > 0)
            else None
        )
        kwargs["spot"] = spot
        kwargs["spot_price_source"] = (
            str(quote.get("price_source") or "") if spot is not None else ""
        )
        kwargs["spot_market_data_entitlement"] = (
            str(quote.get("market_data_entitlement") or "unknown")
            if spot is not None
            else "unknown"
        )
        kwargs["spot_reference_state"] = (
            _option_board_spot_reference_state(quote) if spot is not None else "reference"
        )
        kwargs["option_expiry_facts"] = option_expiry_facts
        return await async_option_board_snapshot(qualified, **kwargs)

    async def async_stop_option_board(
        self,
        instrument: dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        from aef_terminal.data.ibkr.options import async_stop_option_board

        qualified = require_provider_identity(instrument, provider=self.key)
        return await async_stop_option_board(qualified, **kwargs)

    def option_target_price(self, instrument: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        from aef_terminal.data.gex.option_targets import option_target_price
        from aef_terminal.data.ibkr.options import (
            OPTION_POINT_QUOTE_CONSUMER_ID,
        )

        qualified = require_provider_identity(instrument, provider=self.key)
        if "option_quote_provider" in kwargs or "option_contract_provider" in kwargs:
            raise ValueError("IBKR_OPTION_PROVIDER_IS_ADAPTER_OWNED")
        quote_consumer_id = kwargs.pop(
            "option_quote_consumer_id",
            OPTION_POINT_QUOTE_CONSUMER_ID,
        )
        kwargs["option_contract_provider"] = lambda *, spot, dte, fixed_contract=None, store=None: (
            self.option_contract_universe(
                qualified,
                spot=spot,
                dte=dte,
                fixed_contract=fixed_contract,
                store=store,
            )
        )
        if kwargs.get("con_id"):
            kwargs["option_quote_provider"] = lambda contract: self.live_option_quote(
                qualified,
                contract,
                consumer_id=quote_consumer_id,
            )
        return option_target_price(instrument=qualified, **kwargs)

    def live_option_quote(
        self,
        instrument: dict[str, Any],
        contract: dict[str, Any],
        *,
        consumer_id: str = "option-point",
    ) -> dict[str, Any]:
        from aef_terminal.data.ibkr.options import live_option_quote

        qualified = require_provider_identity(instrument, provider=self.key)
        expected_sec_type = "FOP" if instrument_is_futures(qualified) else "OPT"
        if contract.get("sec_type") != expected_sec_type:
            raise ValueError("IBKR_OPTION_SEC_TYPE_ROUTE_MISMATCH")
        return live_option_quote(contract, consumer_id=consumer_id)

    def cancel_option_quote(
        self,
        instrument: dict[str, Any],
        contract: dict[str, Any],
        *,
        consumer_id: str = "option-point",
    ) -> dict[str, Any]:
        from aef_terminal.data.ibkr.options import cancel_option_quote

        qualified = require_provider_identity(instrument, provider=self.key)
        expected_sec_type = "FOP" if instrument_is_futures(qualified) else "OPT"
        if contract.get("sec_type") != expected_sec_type:
            raise ValueError("IBKR_OPTION_SEC_TYPE_ROUTE_MISMATCH")
        return cancel_option_quote(contract, consumer_id=consumer_id)

    def reconcile_option_quote_consumers(
        self,
        active_consumer_ids: set[str],
    ) -> dict[str, Any]:
        from aef_terminal.data.ibkr.options import (
            reconcile_option_point_quote_consumers,
        )

        return reconcile_option_point_quote_consumers(active_consumer_ids)

    def quote_close_base(self, instrument: dict[str, Any], quote: dict[str, Any]) -> float | None:
        qualified = require_provider_identity(instrument, provider=self.key)
        if instrument_is_futures(qualified):
            return None
        if instrument_asset_class(qualified) not in {"crypto", "stock", "equity", "etf", "index"}:
            return None
        return float_or_none(quote.get("close"))

    def search_instruments(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        from aef_terminal.data.ibkr.search import search_ibkr_instruments

        return search_ibkr_instruments(query, limit=limit)

    def bind_instrument(self, provider_contract_id: str) -> dict[str, Any] | None:
        from aef_terminal.data.ibkr.search import bind_ibkr_instrument

        return bind_ibkr_instrument(provider_contract_id)

    def bind_future_root(
        self, root: str, binding: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        from aef_terminal.data.ibkr.search import bind_ibkr_future_root

        return bind_ibkr_future_root(root, binding=binding)

    def resolve_current_contract(self, instrument: dict[str, Any]) -> dict[str, Any]:
        qualified = require_provider_identity(instrument, provider=self.key)
        if not instrument_is_futures_root(qualified):
            raise ValueError("IBKR_CURRENT_CONTRACT_REQUIRES_FUTURES_ROOT")
        bound = self.bind_future_root(futures_root(qualified), binding=qualified)
        if not isinstance(bound, dict):
            raise RuntimeError(
                f"IBKR_CURRENT_CONTRACT_UNRESOLVED instrument_key={futures_root(qualified)}"
            )
        return bound

    def canonical_history_route(self, instrument: dict[str, Any]) -> CanonicalHistoryRoute | None:
        qualified = require_provider_identity(instrument, provider=self.key)
        if instrument_is_futures_root(qualified):
            return IBKR_CONTINUOUS_HISTORY_ROUTE
        if not instrument_is_exact_futures_contract(qualified):
            return None
        contract_id, contract_type = self._history_contract_identity(qualified)
        if contract_type != "FUT":
            return None
        return CanonicalHistoryRoute.exact_futures_contract(contract_id)

    def commit_continuous_history_result(
        self,
        store: Any,
        bars: tuple[Bar, ...],
        *,
        instrument: dict[str, Any],
        revision_sequence: int,
    ) -> CanonicalBarCommitReceipt:
        qualified = require_provider_identity(instrument, provider=self.key)
        if not instrument_is_futures_root(qualified):
            raise ValueError("IBKR_CONTINUOUS_HISTORY_REQUIRES_FUTURES_ROOT")
        return write_continuous_history(
            store,
            bars,
            instrument=qualified,
            revision_sequence=revision_sequence,
        )

    async def fetch_trading_schedule(
        self,
        instrument: dict[str, Any],
        *,
        timeout: float = 6.0,
        starts_at: datetime | None = None,
        ends_at: datetime | None = None,
    ) -> dict[str, Any]:
        from aef_terminal.data.ibkr.trading_hours import fetch_trading_hours_async

        contract_id = self.session_contract_id(instrument)
        if not contract_id:
            raise ValueError("IBKR_SESSION_CONTRACT_ID_REQUIRED")
        return await fetch_trading_hours_async(
            instrument,
            timeout=timeout,
            starts_at=starts_at,
            ends_at=ends_at,
        )
