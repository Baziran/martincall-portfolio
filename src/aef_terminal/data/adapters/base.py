from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from aef_terminal.data.instrument_identity import (
    current_futures_contract_id,
    instrument_is_futures_root,
    provider_contract_id,
    require_provider_identity,
)
from aef_terminal.data.provider_contract import (
    CanonicalBarCommitReceipt,
    CanonicalHistoryRoute,
    HistoryRepairIntent,
    HistoryRequestAdmissionIdentity,
    ProviderCapabilities,
    ProviderBarBucket,
    ProviderDataPolicy,
    ProviderHistoryFetchResult,
    ProviderManifest,
    ProviderSessionScope,
)
from aef_terminal.domain import Bar
from aef_terminal.runtime.math_utils import float_or_none
from aef_terminal.runtime.async_tasks import run_physical_thread_call
from aef_terminal.runtime.timeframes import (
    HistoryRangeWindow,
    interval_bucket,
    interval_minutes,
    require_aware_utc_datetime,
)


class ProviderAdapterBase:
    manifest = ProviderManifest(
        key="",
        name="",
        db_providers=(),
        capabilities=ProviderCapabilities(),
        data_policy=ProviderDataPolicy(),
    )

    @property
    def key(self) -> str:
        return self.manifest.key

    @property
    def db_providers(self) -> tuple[str, ...]:
        return self.manifest.db_providers

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self.manifest.capabilities

    @property
    def data_policy(self) -> ProviderDataPolicy:
        return self.manifest.data_policy

    def catalog_entry(self) -> dict[str, Any]:
        return self.manifest.as_dict()

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
        return await run_physical_thread_call(
            self.load_bars,
            instrument,
            interval,
            range_,
            timeout,
            live_refresh,
            store,
            window=window,
        )

    def quote_close_base(self, instrument: dict[str, Any], quote: dict[str, Any]) -> float | None:
        require_provider_identity(instrument, provider=self.key)
        return float_or_none(quote.get("close"))

    def read_quote_trade_events(
        self,
        after_sequence: int,
        route_identities: tuple[tuple[str, str], ...],
    ) -> tuple[int, list[dict[str, Any]], bool]:
        del route_identities
        return int(after_sequence), [], False

    def bind_instrument(self, _provider_contract_id: str) -> dict[str, Any] | None:
        return None

    def bind_future_root(
        self, _root: str, binding: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        _ = binding
        return None

    def resolve_current_contract(self, instrument: dict[str, Any]) -> dict[str, Any]:
        require_provider_identity(instrument, provider=self.key)
        raise ValueError(f"{self.key.upper()}_HAS_NO_FUTURES_CONTRACT_LIFECYCLE")

    def session_contract_id(self, instrument: dict[str, Any]) -> str:
        qualified = require_provider_identity(instrument, provider=self.key)
        return (
            current_futures_contract_id(qualified)
            if instrument_is_futures_root(qualified)
            else provider_contract_id(qualified)
        )

    def bar_data_type(self, instrument: dict[str, Any]) -> str:
        require_provider_identity(instrument, provider=self.key)
        raise ValueError(f"PROVIDER_BAR_DATA_TYPE_UNKNOWN provider={self.key}")

    def continuous_session(self, instrument: dict[str, Any]) -> bool:
        require_provider_identity(instrument, provider=self.key)
        scope = self.manifest.session_scope
        if scope is ProviderSessionScope.UNKNOWN:
            raise ValueError(f"PROVIDER_SESSION_SCOPE_UNKNOWN provider={self.key}")
        return scope is ProviderSessionScope.CONTINUOUS

    def history_session_scope(self, instrument: dict[str, Any]) -> str:
        require_provider_identity(instrument, provider=self.key)
        scope = self.manifest.session_scope
        if scope is ProviderSessionScope.UNKNOWN:
            raise ValueError(f"PROVIDER_SESSION_SCOPE_UNKNOWN provider={self.key}")
        if scope is ProviderSessionScope.INSTRUMENT:
            raise ValueError(f"PROVIDER_INSTRUMENT_SESSION_SCOPE_REQUIRED provider={self.key}")
        return scope.value

    def provider_bar_bucket(
        self,
        instrument: dict[str, Any],
        ts: datetime,
        interval: str,
        *,
        session_open: datetime | None = None,
        session_close: datetime | None = None,
    ) -> ProviderBarBucket | None:
        """Return this provider's authoritative timestamp grid for one bar."""

        qualified = require_provider_identity(instrument, provider=self.key)
        return self._provider_bar_bucket_for_qualified(
            qualified,
            ts,
            interval,
            session_open=session_open,
            session_close=session_close,
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
        """Resolve one bucket after the public boundary qualified its route."""

        _ = instrument
        timestamp = require_aware_utc_datetime(ts, field="provider bar timestamp")
        opens_at = (
            require_aware_utc_datetime(session_open, field="provider session open")
            if session_open is not None
            else None
        )
        closes_at = (
            require_aware_utc_datetime(session_close, field="provider session close")
            if session_close is not None
            else None
        )
        if opens_at is not None and timestamp < opens_at:
            return None
        if closes_at is not None and timestamp >= closes_at:
            return None
        starts_at = interval_bucket(timestamp, interval)
        bucket_close = starts_at + timedelta(minutes=max(interval_minutes(interval), 1))
        if closes_at is not None:
            bucket_close = min(bucket_close, closes_at)
        if bucket_close <= starts_at:
            return None
        return ProviderBarBucket(starts_at=starts_at, closes_at=bucket_close)

    def provider_session_bar_buckets(
        self,
        instrument: dict[str, Any],
        interval: str,
        *,
        session_open: datetime,
        session_close: datetime,
    ) -> tuple[ProviderBarBucket, ...]:
        """Return one provider-session grid after one route qualification."""

        qualified = require_provider_identity(instrument, provider=self.key)
        opens_at = require_aware_utc_datetime(session_open, field="provider session open")
        closes_at = require_aware_utc_datetime(session_close, field="provider session close")
        if closes_at <= opens_at:
            return ()
        buckets: list[ProviderBarBucket] = []
        cursor = opens_at
        while cursor < closes_at:
            bucket = self._provider_bar_bucket_for_qualified(
                qualified,
                cursor,
                interval,
                session_open=opens_at,
                session_close=closes_at,
            )
            if (
                not isinstance(bucket, ProviderBarBucket)
                or bucket.closes_at <= cursor
                or bucket.closes_at <= bucket.starts_at
            ):
                break
            buckets.append(bucket)
            cursor = bucket.closes_at
        return tuple(buckets)

    def canonical_history_route(self, instrument: dict[str, Any]) -> CanonicalHistoryRoute | None:
        require_provider_identity(instrument, provider=self.key)
        return None

    def commit_continuous_history_result(
        self,
        store: Any,
        bars: tuple[Bar, ...],
        *,
        instrument: dict[str, Any],
        revision_sequence: int,
    ) -> CanonicalBarCommitReceipt:
        _ = store, bars, revision_sequence
        require_provider_identity(instrument, provider=self.key)
        raise TypeError(f"{self.key.upper()} continuous history commit is not implemented")

    def history_request_identity(
        self,
        instrument: dict[str, Any],
    ) -> HistoryRequestAdmissionIdentity:
        require_provider_identity(instrument, provider=self.key)
        raise TypeError(f"{self.key.upper()} history request identity is not implemented")

    def history_request_max_span(
        self,
        instrument: dict[str, Any],
        timeframe: str,
    ) -> timedelta:
        _ = timeframe
        require_provider_identity(instrument, provider=self.key)
        raise TypeError(f"{self.key.upper()} history request bound is not implemented")

    async def async_fetch_history(
        self,
        intent: HistoryRepairIntent,
        timeout: float,
        *,
        instrument: dict[str, Any],
    ) -> ProviderHistoryFetchResult:
        _ = intent, timeout
        require_provider_identity(instrument, provider=self.key)
        raise TypeError(f"{self.key.upper()} history fetch is not implemented")
