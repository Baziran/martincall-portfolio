from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from aef_terminal.data.adapters import discover_provider_adapters
from aef_terminal.data.provider_contract import (
    ChartCommitProvider,
    ChartLiveProvider,
    GexProvider,
    HistoryRepairProvider,
    InstrumentRoute,
    NativeChartLiveProvider,
    OptionsProvider,
    ProviderAdapter,
    ProviderCapabilities,
    ProviderDataPolicy,
    ProviderManifest,
    ProviderSessionScope,
    QuotePollingProvider,
    QuoteStreamProvider,
    TradingScheduleProvider,
)
from aef_terminal.data.provider_history import (
    ProviderHistoryPage,
    read_provider_bar_logical_projection as _read_provider_bar_logical_projection,
    read_provider_confirmed_bar_timestamps as _read_provider_confirmed_bar_timestamps,
    read_provider_history_page as _read_provider_history_page,
    read_recent_provider_bars as _read_recent_provider_bars,
    read_recent_provider_bars_batch as _read_recent_provider_bars_batch,
)
from aef_terminal.data.instrument_identity import (
    instrument_key,
    instrument_provider,
    provider_price_increment,
    provider_symbol,
    require_exact_identity_text,
    require_provider_identity,
    qualified_instrument_id,
    route_fingerprint,
)
from aef_terminal.domain import Bar
from aef_terminal.runtime.derived_timeframes import derive_chart_bars
from aef_terminal.runtime.timeframes import HistoryRangeWindow, canonical_bar_source_interval
from aef_terminal.runtime.bar_projection import ConfirmedBarLogicalProjection
from aef_terminal.runtime.bar_series import require_ordered_bar_list


_PROVIDERS: dict[str, ProviderAdapter] = {}
_SOURCE_PROVIDER_KEYS: dict[str, str] = {}
DEFAULT_DATA_SOURCE = "ibkr"


class ProviderAdapterUnavailableError(ValueError):
    """The exact persisted provider identity has no active runtime adapter."""


def normalize_provider_key(source: str) -> str:
    key = str(source or "").strip().lower()
    if not key:
        raise ValueError("DATA_PROVIDER_REQUIRED")
    return key


def default_data_source() -> str:
    return DEFAULT_DATA_SOURCE


def _require_storage_provider_manifest_values(values: object) -> tuple[str, ...]:
    if not isinstance(values, tuple) or not values:
        raise ValueError("STORAGE_PROVIDER_MANIFEST_REQUIRED")
    invalid = [
        value
        for value in values
        if not isinstance(value, str)
        or not value
        or value != normalize_provider_key(value)
        or not value.isascii()
        or not value[0].isalnum()
        or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for character in value)
    ]
    if invalid or len(set(values)) != len(values):
        raise ValueError(f"STORAGE_PROVIDER_MANIFEST_INVALID providers={invalid or list(values)!r}")
    return values


def register_provider(provider: ProviderAdapter) -> None:
    manifest = getattr(provider, "manifest", None)
    if not isinstance(manifest, ProviderManifest):
        raise TypeError("Data providers must declare one ProviderManifest")
    key = str(manifest.key or "").strip().lower()
    if not key:
        raise ValueError("Data provider key is required")
    if key != manifest.key:
        raise ValueError(f"Data provider manifest key must be normalized: {manifest.key}")
    _require_storage_provider_manifest_values(manifest.db_providers)
    if not isinstance(provider, ProviderAdapter):
        raise TypeError(f"Data provider {key} does not implement the core provider contract")
    _validate_provider_capabilities(provider)
    if key in _PROVIDERS:
        raise ValueError(f"DATA_PROVIDER_ALREADY_REGISTERED provider={key}")
    staged_providers = {**_PROVIDERS, key: provider}
    staged_source_index = _build_source_provider_index(staged_providers)
    _PROVIDERS[key] = provider
    _SOURCE_PROVIDER_KEYS.clear()
    _SOURCE_PROVIDER_KEYS.update(staged_source_index)


def _build_source_provider_index(
    providers: dict[str, ProviderAdapter],
) -> dict[str, str]:
    exact: dict[str, str] = {}
    prefixes: list[tuple[str, str]] = []
    for provider_key, provider in providers.items():
        manifest = provider.manifest
        for raw_source in (manifest.key, *manifest.db_providers):
            source = raw_source
            owner = exact.get(source)
            if owner is not None and owner != provider_key:
                raise ValueError(
                    f"PROVIDER_SOURCE_COLLISION source={source} providers={owner},{provider_key}"
                )
            exact[source] = provider_key
        for raw_prefix in manifest.source_prefixes:
            if (
                not isinstance(raw_prefix, str)
                or not raw_prefix
                or raw_prefix != normalize_provider_key(raw_prefix)
                or not raw_prefix.endswith(":")
            ):
                raise ValueError(
                    f"PROVIDER_SOURCE_PREFIX_INVALID provider={provider_key} prefix={raw_prefix!r}"
                )
            prefix = raw_prefix
            prefixes.append((prefix, provider_key))
    for index, (prefix, owner) in enumerate(prefixes):
        for other_prefix, other_owner in prefixes[index + 1 :]:
            if owner != other_owner and (
                prefix.startswith(other_prefix) or other_prefix.startswith(prefix)
            ):
                raise ValueError(
                    "PROVIDER_SOURCE_PREFIX_COLLISION "
                    f"prefixes={prefix},{other_prefix} providers={owner},{other_owner}"
                )
    return exact


def _provider_overrides_default(provider: ProviderAdapter, method_name: str) -> bool:
    from aef_terminal.data.adapters.base import ProviderAdapterBase

    implementation = getattr(type(provider), method_name, None)
    default = getattr(ProviderAdapterBase, method_name, None)
    return callable(implementation) and implementation is not default


def _validate_provider_capabilities(provider: ProviderAdapter) -> None:
    capabilities = provider.capabilities
    contracts: tuple[tuple[str, bool, bool], ...] = (
        (
            "chart_live",
            bool(capabilities.native_chart_stream or capabilities.chart_tail_polling),
            isinstance(provider, ChartLiveProvider),
        ),
        (
            "chart_commit",
            bool(capabilities.native_chart_stream or capabilities.chart_tail_polling),
            isinstance(provider, ChartCommitProvider),
        ),
        (
            "native_chart_stream",
            capabilities.native_chart_stream,
            isinstance(provider, NativeChartLiveProvider),
        ),
        (
            "live_quote_stream",
            capabilities.live_quote_stream,
            isinstance(provider, QuoteStreamProvider),
        ),
        (
            "live_quote_polling",
            capabilities.live_quote_polling,
            isinstance(provider, QuotePollingProvider),
        ),
        (
            "trading_hours",
            capabilities.trading_hours,
            isinstance(provider, TradingScheduleProvider),
        ),
        ("gex", capabilities.gex, isinstance(provider, GexProvider)),
        ("options", capabilities.options, isinstance(provider, OptionsProvider)),
    )
    mismatches = [name for name, declared, implemented in contracts if declared is not implemented]
    if capabilities.gap_repair and (
        not isinstance(provider, HistoryRepairProvider)
        or not all(
            _provider_overrides_default(provider, method_name)
            for method_name in (
                "history_request_identity",
                "history_request_max_span",
                "async_fetch_history",
            )
        )
    ):
        mismatches.append("gap_repair")
    if capabilities.exact_history_snapshot_authority and not capabilities.gap_repair:
        mismatches.append("exact_history_snapshot_authority")
    if capabilities.chart_stream != bool(
        capabilities.native_chart_stream or capabilities.chart_tail_polling
    ):
        mismatches.append("chart_stream_mode")
    if capabilities.quote_trade_events and (
        "read_quote_trade_events" not in type(provider).__dict__
    ):
        mismatches.append("quote_trade_events")
    if provider.manifest.session_scope is ProviderSessionScope.UNKNOWN:
        mismatches.append("session_scope")
    if (
        provider.manifest.session_scope is ProviderSessionScope.INSTRUMENT
        and not _provider_overrides_default(provider, "history_session_scope")
    ):
        mismatches.append("history_session_scope")
    if provider.capabilities.instrument_search and not _provider_overrides_default(
        provider, "search_instruments"
    ):
        mismatches.append("instrument_search")
    if provider.capabilities.instrument_binding and not any(
        _provider_overrides_default(provider, method_name)
        for method_name in ("bind_instrument", "bind_future_root")
    ):
        mismatches.append("instrument_binding")
    if provider.capabilities.canonical_futures_history and not _provider_overrides_default(
        provider,
        "canonical_history_route",
    ):
        mismatches.append("canonical_futures_history")
    if mismatches:
        raise TypeError(
            f"Data provider {provider.key} capability contract mismatch: {', '.join(mismatches)}"
        )


def get_provider(source: str) -> ProviderAdapter:
    key = normalize_provider_key(source)
    provider = _PROVIDERS.get(key)
    if provider is None:
        raise ProviderAdapterUnavailableError(f"Unsupported data source: {source}")
    return provider


def option_provider_adapters() -> tuple[OptionsProvider, ...]:
    return tuple(
        provider
        for provider in _PROVIDERS.values()
        if provider.capabilities.options and isinstance(provider, OptionsProvider)
    )


def route_instrument(
    instrument: dict[str, Any],
    *,
    expected_source: str | None = None,
) -> InstrumentRoute:
    qualified = require_provider_identity(instrument)
    provider = get_provider(instrument_provider(qualified))
    if expected_source:
        expected = provider_key_for_source(expected_source)
        if expected != provider.key:
            raise ValueError(
                f"INSTRUMENT_PROVIDER_MISMATCH expected={expected} actual={provider.key}"
            )
    return InstrumentRoute(
        instrument=qualified,
        adapter=provider,
        provider=provider.key,
        provider_symbol=provider_symbol(qualified, provider.key),
        instrument_id=qualified_instrument_id(qualified),
        instrument_key=instrument_key(qualified),
        fingerprint=route_fingerprint(qualified),
        price_increment=provider_price_increment(qualified),
    )


def provider_capabilities(source: str) -> ProviderCapabilities:
    return get_provider(source).capabilities


def provider_data_policy(source: str) -> ProviderDataPolicy:
    return get_provider(source).data_policy


def provider_data_policy_for_source(source: str) -> ProviderDataPolicy:
    return get_provider(provider_key_for_source(source)).data_policy


def provider_key_for_source(source: str) -> str:
    source_key = normalize_provider_key(source)
    exact = _SOURCE_PROVIDER_KEYS.get(source_key)
    if exact is not None:
        return exact
    owners = {
        provider_key
        for provider_key, provider in _PROVIDERS.items()
        for prefix in provider.manifest.source_prefixes
        if source_key.startswith(prefix)
    }
    if len(owners) == 1:
        return next(iter(owners))
    if len(owners) > 1:
        raise ValueError(
            f"PROVIDER_SOURCE_AMBIGUOUS source={source_key} providers={','.join(sorted(owners))}"
        )
    raise ValueError(f"PROVIDER_SOURCE_UNKNOWN source={source_key}")


_INFLIGHT_BAR_LOADS: dict[tuple[Any, ...], asyncio.Task[tuple[list[Bar], str]]] = {}
_PROVIDER_BAR_LOADS_ACCEPTING = True


def start_provider_bar_load_runtime() -> None:
    global _PROVIDER_BAR_LOADS_ACCEPTING
    if _INFLIGHT_BAR_LOADS:
        raise RuntimeError("PROVIDER_BAR_LOAD_START_WITH_ACTIVE_TASKS")
    _PROVIDER_BAR_LOADS_ACCEPTING = True


async def shutdown_provider_bar_load_runtime() -> None:
    global _PROVIDER_BAR_LOADS_ACCEPTING
    _PROVIDER_BAR_LOADS_ACCEPTING = False
    tasks = tuple(set(_INFLIGHT_BAR_LOADS.values()))
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _INFLIGHT_BAR_LOADS.clear()


def read_recent_provider_bars(
    route: InstrumentRoute,
    timeframe: str,
    *,
    store: Any,
    limit: int,
) -> list[Bar]:
    return _read_recent_provider_bars(route, timeframe, store=store, limit=limit)


def read_provider_history_page(
    route: InstrumentRoute,
    timeframe: str,
    *,
    before_ts: datetime,
    limit: int,
    store: Any,
) -> ProviderHistoryPage:
    return _read_provider_history_page(
        route,
        timeframe,
        before_ts=before_ts,
        limit=limit,
        store=store,
    )


def read_recent_provider_bars_batch(
    requests: Sequence[tuple[InstrumentRoute, str, int]],
    *,
    store: Any,
) -> dict[tuple[str, str], list[Bar]]:
    return _read_recent_provider_bars_batch(requests, store=store)


def read_provider_bar_logical_projection(
    route: InstrumentRoute,
    timeframe: str,
    *,
    store: Any,
    anchor_timestamps: list[datetime],
) -> ConfirmedBarLogicalProjection:
    return _read_provider_bar_logical_projection(
        route,
        timeframe,
        store=store,
        anchor_timestamps=anchor_timestamps,
    )


def read_provider_confirmed_bar_timestamps(
    route: InstrumentRoute,
    timeframe: str,
    *,
    store: Any,
    anchor_timestamps: list[datetime],
) -> frozenset[datetime]:
    return _read_provider_confirmed_bar_timestamps(
        route,
        timeframe,
        store=store,
        anchor_timestamps=anchor_timestamps,
    )


def _require_provider_bar_load_result(
    result: object,
    *,
    provider: str,
    source_interval: str,
) -> tuple[list[Bar], str]:
    """Validate one adapter result before it becomes shared chart state."""

    if not isinstance(result, tuple) or len(result) != 2:
        raise TypeError(f"{provider} provider bar load must return (list[Bar], str)")
    bars, warning = result
    bars = require_ordered_bar_list(
        bars,
        timeframe=source_interval,
        field=f"{provider} provider bar load",
    )
    if not isinstance(warning, str):
        raise TypeError(f"{provider} provider bar load warning must be a string")
    return bars, warning


def load_provider_bars(
    route: InstrumentRoute,
    interval: str,
    range_: str,
    timeout: float,
    *,
    live_refresh: bool = False,
    store: Any | None = None,
    window: HistoryRangeWindow | None = None,
) -> tuple[list[Bar], str]:
    """Load the persisted/provider source and project virtual chart intervals."""

    if window is not None and not isinstance(window, HistoryRangeWindow):
        raise TypeError("provider bar window must be HistoryRangeWindow or None")
    source_interval = canonical_bar_source_interval(interval)
    bars, warning = _require_provider_bar_load_result(
        route.adapter.load_bars(
            route.instrument,
            source_interval,
            range_,
            timeout,
            live_refresh=live_refresh,
            store=store,
            window=window,
        ),
        provider=route.provider,
        source_interval=source_interval,
    )
    return derive_chart_bars(bars, interval), warning


async def async_load_provider_bars(
    route: InstrumentRoute,
    interval: str,
    range_: str,
    timeout: float,
    *,
    live_refresh: bool = False,
    store: Any | None = None,
    window: HistoryRangeWindow | None = None,
) -> tuple[list[Bar], str]:
    if not _PROVIDER_BAR_LOADS_ACCEPTING:
        raise RuntimeError("PROVIDER_BAR_LOAD_RUNTIME_STOPPING")
    if window is not None and not isinstance(window, HistoryRangeWindow):
        raise TypeError("provider bar window must be HistoryRangeWindow or None")
    loop = asyncio.get_running_loop()
    source_interval = canonical_bar_source_interval(interval)
    window_key = (
        None
        if window is None
        else (
            window.range_key,
            window.starts_at,
            window.ends_at,
        )
    )
    key = (
        id(loop),
        id(store) if store is not None else None,
        route.instrument_id,
        route.fingerprint,
        str(interval),
        str(range_),
        bool(live_refresh),
        round(max(float(timeout), 0.0), 3),
        window_key,
    )
    task = _INFLIGHT_BAR_LOADS.get(key)
    if task is None or task.done():
        task = asyncio.create_task(
            route.adapter.async_load_bars(
                route.instrument,
                source_interval,
                range_,
                timeout,
                live_refresh=live_refresh,
                store=store,
                window=window,
            ),
            name=f"provider-bars:{route.provider}:{route.instrument_id}:{interval}:{range_}",
        )
        _INFLIGHT_BAR_LOADS[key] = task

        def release(completed: asyncio.Task[tuple[list[Bar], str]]) -> None:
            if _INFLIGHT_BAR_LOADS.get(key) is completed:
                _INFLIGHT_BAR_LOADS.pop(key, None)

        task.add_done_callback(release)
    bars, warning = _require_provider_bar_load_result(
        await asyncio.shield(task),
        provider=route.provider,
        source_interval=source_interval,
    )
    return derive_chart_bars(bars, interval), warning


def provider_db_providers(source: str) -> tuple[str, ...]:
    key = normalize_provider_key(source)
    return _require_storage_provider_manifest_values(get_provider(key).db_providers)


def storage_provider_keys() -> tuple[str, ...]:
    """Return the exact provider values accepted by canonical storage."""

    values = {
        storage_provider
        for provider_key in _PROVIDERS
        for storage_provider in provider_db_providers(provider_key)
    }
    if not values:
        raise RuntimeError("STORAGE_PROVIDER_MANIFEST_REQUIRED")
    return tuple(sorted(values))


def live_quote_polling_providers() -> tuple[str, ...]:
    return tuple(
        key for key, provider in _PROVIDERS.items() if provider.capabilities.live_quote_polling
    )


def search_provider_instruments(source: str, query: str, limit: int = 20) -> list[dict[str, Any]]:
    return get_provider(source).search_instruments(query, limit=limit)


def bind_provider_instrument(source: str, provider_contract_id: str) -> dict[str, Any] | None:
    return get_provider(source).bind_instrument(
        require_exact_identity_text(
            provider_contract_id,
            field="provider_contract_id",
        )
    )


def bind_provider_future_root(
    source: str,
    root: str,
    binding: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    return get_provider(source).bind_future_root(root, binding=binding)


def provider_catalog() -> list[dict[str, Any]]:
    return [provider.catalog_entry() for provider in _PROVIDERS.values()]


for _provider_adapter in discover_provider_adapters():
    register_provider(_provider_adapter)
