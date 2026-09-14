from __future__ import annotations

import asyncio
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from aef_terminal.data.gex.option_target_contract import (
    OPTION_TARGET_COMPRESSION_FIELDS,
    OPTION_TARGET_LIVE_QUOTE_MAX_AGE_SECONDS,
    option_target_finite_number,
    option_target_market_sample,
    option_target_positive_number,
    require_option_target_dte,
    require_option_target_underlying_quote,
)
from aef_terminal.data.gex.utils import parse_gex_timestamp
from aef_terminal.data.instrument_identity import (
    instrument_is_futures,
    parse_exact_positive_decimal_provider_id,
    require_exact_identity_text,
)
from aef_terminal.data.providers import option_provider_adapters, route_instrument
from aef_terminal.runtime.async_tasks import (
    run_cancellation_deferred,
    run_physical_thread_call,
)
from aef_terminal.runtime.option_target_changes import option_targets_revision
from aef_terminal.ui.runtime.quote_stream import QuoteCacheRevision
from aef_terminal.ui.runtime_instruments import lookup_runtime_instrument


OPTION_TARGET_REPRICE_POLL_SECONDS = 2.0
OPTION_TARGET_REPRICE_MAX_ROWS = 12


@dataclass
class _OptionTargetRepriceState:
    store: Any = None
    rows: list[dict[str, Any]] | None = None
    revision: int | None = None


async def _commit_option_target_updates(
    store: Any,
    updates: list[tuple[str, str, str, dict[str, Any]]],
    option_target_samples_committed: Callable[..., Any],
) -> None:
    async def commit_and_publish() -> None:
        committed = await run_physical_thread_call(
            store.update_option_target_market_samples,
            updates,
        )
        if committed:
            await option_target_samples_committed(
                committed,
                store=store,
            )

    await run_cancellation_deferred(
        commit_and_publish(),
        task_cancelled_error="OPTION_TARGET_COMMIT_TASK_CANCELLED",
    )


async def _run_option_target_reprice_cycle(
    *,
    state: _OptionTargetRepriceState,
    store_factory: Callable[[], Any],
    option_target_caps_settings: Callable[[], dict[str, float]],
    quote_cache_for_instruments: Callable[
        ...,
        tuple[dict[str, dict[str, Any]] | None, str, QuoteCacheRevision],
    ],
    parse_iso_ts: Callable[[str | None], datetime | None],
    option_target_samples_committed: Callable[..., Any],
) -> None:
    async def reconcile_reprice_commit() -> None:
        store = await run_physical_thread_call(store_factory)
        if store is None:
            return
        revision = option_targets_revision()
        if state.store is not store or state.rows is None or state.revision != revision:
            rows = await run_physical_thread_call(store.read_option_targets)
            if revision != option_targets_revision():
                return
            state.store = store
            state.rows = rows
            state.revision = revision
        rows = state.rows
        await run_physical_thread_call(
            reconcile_option_target_quote_consumers,
            rows,
        )
        updates = await run_physical_thread_call(
            reprice_option_target_rows,
            rows,
            store=store,
            option_target_caps_settings=option_target_caps_settings,
            quote_cache_for_instruments=quote_cache_for_instruments,
            parse_iso_ts=parse_iso_ts,
        )
        if updates:
            await _commit_option_target_updates(
                store,
                updates,
                option_target_samples_committed,
            )

    await run_cancellation_deferred(
        reconcile_reprice_commit(),
        task_cancelled_error="OPTION_TARGET_REPRICE_CYCLE_TASK_CANCELLED",
    )


def reconcile_option_target_quote_consumers(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    adapters = option_provider_adapters()
    active_by_provider = {adapter.key: set() for adapter in adapters}
    for row in rows:
        item = row.get("payload") if isinstance(row, dict) else None
        if not isinstance(item, dict):
            continue
        try:
            provider = require_exact_identity_text(
                item.get("provider"),
                field="option_target.provider",
            )
            target_id = require_exact_identity_text(
                item.get("id") or row.get("id"),
                field="option_target.id",
            )
        except ValueError:
            continue
        active = active_by_provider.get(provider)
        if active is not None:
            active.add(f"option-point:{target_id}")
    return [
        adapter.reconcile_option_quote_consumers(active_by_provider[adapter.key])
        for adapter in adapters
    ]


def _same_option_contract(base: Mapping[str, Any], payload: Mapping[str, Any]) -> bool:
    base_con_id = parse_exact_positive_decimal_provider_id(base.get("con_id"))
    next_con_id = parse_exact_positive_decimal_provider_id(payload.get("con_id"))
    base_strike = option_target_positive_number(base.get("strike"))
    next_strike = option_target_positive_number(payload.get("strike"))
    return bool(
        base_con_id > 0
        and next_con_id == base_con_id
        and base.get("right") in {"C", "P"}
        and payload.get("right") == base.get("right")
        and isinstance(base.get("expiry"), str)
        and len(base["expiry"]) == 8
        and base["expiry"].isdigit()
        and payload.get("expiry") == base.get("expiry")
        and parse_gex_timestamp(base.get("expiry_at")) is not None
        and payload.get("expiry_at") == base.get("expiry_at")
        and isinstance(base.get("exchange"), str)
        and bool(base.get("exchange"))
        and payload.get("exchange") == base.get("exchange")
        and isinstance(base.get("trading_class"), str)
        and bool(base.get("trading_class"))
        and payload.get("trading_class") == base.get("trading_class")
        and isinstance(base.get("currency"), str)
        and bool(base.get("currency"))
        and payload.get("currency") == base.get("currency")
        and option_target_positive_number(base.get("multiplier")) is not None
        and payload.get("multiplier") == base.get("multiplier")
        and not isinstance(base.get("strike"), bool)
        and isinstance(base.get("strike"), (int, float))
        and not isinstance(payload.get("strike"), bool)
        and isinstance(payload.get("strike"), (int, float))
        and base_strike is not None
        and next_strike is not None
        and abs(base_strike - next_strike) <= 0.000001
    )


def _compression_values_changed(base: Mapping[str, Any], payload: Mapping[str, Any]) -> bool:
    for key in (
        "live_quote_status",
        "live_quote_source",
        "live_quote_message",
        "fair_price_status",
        "fair_price_message",
        "live_quote_ts",
        "live_quote_time_basis",
        "live_quote_entitlement",
        "reference_option_price_source",
        "underlying_quote_price_source",
        "underlying_quote_entitlement",
        "underlying_quote_status",
        "underlying_quote_ts",
        "underlying_quote_time_basis",
        "previous_live_quote_ts",
        "previous_live_quote_time_basis",
        "previous_underlying_quote_ts",
        "previous_underlying_quote_time_basis",
    ):
        if base.get(key) != payload.get(key):
            return True
    base_horizon = base.get("theta_horizon_seconds")
    next_horizon = payload.get("theta_horizon_seconds")
    temporal_scenario_active = any(
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
        and float(value) > 0
        for value in (base_horizon, next_horizon)
    )
    if temporal_scenario_active and base.get("valuation_ts") != payload.get("valuation_ts"):
        return True
    for key in OPTION_TARGET_COMPRESSION_FIELDS:
        before = option_target_positive_number(base.get(key))
        after = option_target_positive_number(payload.get(key))
        if before is None and after is None:
            continue
        if before is None or after is None:
            return True
        threshold = max(abs(before) * 0.002, 0.01)
        if abs(after - before) >= threshold:
            return True
    return False


def _merge_repriced_payload(
    base: dict[str, Any],
    result: dict[str, Any],
    *,
    sampled_at: datetime | None = None,
) -> dict[str, Any] | None:
    intent = base.get("intent")
    raw_previous_sample = base.get("market_sample")
    if not isinstance(intent, Mapping) or not isinstance(raw_previous_sample, Mapping):
        raise ValueError("option target canonical intent/market_sample contract is required")
    if not _same_option_contract(intent, result):
        return None
    exact_sec_type = str(intent.get("sec_type") or "")
    merged = option_target_market_sample(result, sec_type=exact_sec_type)
    previous_fair = (
        option_target_positive_number(raw_previous_sample.get("fair_price"))
        if raw_previous_sample.get("fair_price_status") == "ok"
        else None
    )
    previous_display_fair = option_target_positive_number(
        raw_previous_sample.get("display_fair_price")
    )
    if merged.get("fair_price_status") == "stale":
        display_fair = previous_display_fair or previous_fair
        if display_fair is not None:
            merged["display_fair_price"] = display_fair
    else:
        merged.pop("display_fair_price", None)
    try:
        previous_sample = option_target_market_sample(
            raw_previous_sample,
            sec_type=exact_sec_type,
        )
    except ValueError:
        previous_sample = {}
    previous_reference = option_target_positive_number(
        previous_sample.get("reference_option_price")
    )
    previous_underlying = option_target_finite_number(previous_sample.get("live_underlying_price"))
    current_reference = option_target_positive_number(merged.get("reference_option_price"))
    current_underlying = option_target_finite_number(merged.get("live_underlying_price"))
    previous_option_at = parse_gex_timestamp(previous_sample.get("live_quote_ts"))
    previous_underlying_at = parse_gex_timestamp(previous_sample.get("underlying_quote_ts"))
    current_option_at = parse_gex_timestamp(merged.get("live_quote_ts"))
    current_underlying_at = parse_gex_timestamp(merged.get("underlying_quote_ts"))
    previous_pair_is_authoritative = (
        previous_reference is not None
        and previous_underlying is not None
        and current_reference is not None
        and current_underlying is not None
        and previous_option_at is not None
        and previous_underlying_at is not None
        and current_option_at is not None
        and current_underlying_at is not None
        and previous_sample.get("live_quote_time_basis") == merged.get("live_quote_time_basis")
        and previous_sample.get("underlying_quote_time_basis")
        == merged.get("underlying_quote_time_basis")
        and abs((previous_option_at - previous_underlying_at).total_seconds())
        <= OPTION_TARGET_LIVE_QUOTE_MAX_AGE_SECONDS
        and abs((current_option_at - current_underlying_at).total_seconds())
        <= OPTION_TARGET_LIVE_QUOTE_MAX_AGE_SECONDS
        and 0.0
        <= (current_option_at - previous_option_at).total_seconds()
        <= OPTION_TARGET_LIVE_QUOTE_MAX_AGE_SECONDS
        and 0.0
        <= (current_underlying_at - previous_underlying_at).total_seconds()
        <= OPTION_TARGET_LIVE_QUOTE_MAX_AGE_SECONDS
    )
    if previous_pair_is_authoritative:
        merged["previous_reference_option_price"] = previous_reference
        merged["previous_underlying_price"] = previous_underlying
        merged["previous_live_quote_ts"] = previous_sample["live_quote_ts"]
        merged["previous_live_quote_time_basis"] = previous_sample["live_quote_time_basis"]
        merged["previous_underlying_quote_ts"] = previous_sample["underlying_quote_ts"]
        merged["previous_underlying_quote_time_basis"] = previous_sample[
            "underlying_quote_time_basis"
        ]
    compression_sample_at = sampled_at or datetime.now(tz=UTC)
    if (
        not isinstance(compression_sample_at, datetime)
        or compression_sample_at.tzinfo is None
        or compression_sample_at.utcoffset() is None
    ):
        raise ValueError("option target sampled_at must be timezone-aware")
    merged["compression_sample_at"] = compression_sample_at.isoformat()
    return option_target_market_sample(merged, sec_type=exact_sec_type)


def _target_point(item: Mapping[str, Any]) -> dict[str, Any]:
    point = item.get("point")
    return dict(point) if isinstance(point, dict) else {}


def _target_price(item: Mapping[str, Any]) -> float:
    point = _target_point(item)
    value = option_target_finite_number(point.get("price"))
    return value if value is not None else 0.0


def _target_ts(item: Mapping[str, Any]) -> str:
    point = _target_point(item)
    value = point.get("ts")
    return value if isinstance(value, str) else ""


def option_target_underlying_quote(
    instrument: dict[str, Any],
    route_fingerprint: str,
    quote_cache_for_instruments: Callable[
        ...,
        tuple[dict[str, dict[str, Any]] | None, str, QuoteCacheRevision],
    ],
    *,
    observed_at: datetime | None = None,
) -> tuple[float | None, dict[str, Any] | None]:
    quotes, _warning, _cache_revision = quote_cache_for_instruments([instrument])
    quote = quotes.get(route_fingerprint)
    if not isinstance(quote, dict):
        return None, None
    quote_facts = require_option_target_underlying_quote(
        quote,
        observed_at=observed_at or datetime.now(tz=UTC),
        futures_options=instrument_is_futures(instrument),
    )
    return quote_facts["execution_price"], dict(quote)


def reprice_option_target_rows(
    rows: list[dict[str, Any]],
    *,
    store: Any,
    option_target_caps_settings: Callable[[], dict[str, float]],
    quote_cache_for_instruments: Callable[
        ...,
        tuple[dict[str, dict[str, Any]] | None, str, QuoteCacheRevision],
    ],
    parse_iso_ts: Callable[[str | None], datetime | None],
    max_rows: int = OPTION_TARGET_REPRICE_MAX_ROWS,
) -> list[tuple[str, str, str, dict[str, Any]]]:
    if type(max_rows) is not int or max_rows <= 0:
        raise ValueError("option target reprice max_rows must be a positive integer")
    premium_caps = option_target_caps_settings()
    updates: list[tuple[str, str, str, dict[str, Any]]] = []
    valuation_now = datetime.now(tz=UTC)
    underlying_by_route: dict[
        tuple[str, str],
        tuple[float | None, dict[str, Any] | None] | Exception,
    ] = {}
    route_by_scope: dict[tuple[str, str], Any | Exception] = {}
    for row in rows[:max_rows]:
        item = row.get("payload") if isinstance(row, dict) else None
        if not isinstance(item, dict):
            continue
        raw_target_id = item.get("id") or row.get("id")
        target_id = raw_target_id if isinstance(raw_target_id, str) else ""
        payload = item.get("payload")
        if not target_id or not isinstance(payload, dict):
            continue
        intent = payload.get("intent")
        market_sample = payload.get("market_sample")
        if not isinstance(intent, dict) or not isinstance(market_sample, dict):
            continue
        target_price = _target_price(item)
        if target_price <= 0:
            continue
        instrument_id = ""
        expected_fingerprint = ""
        try:
            instrument_id = require_exact_identity_text(
                item.get("instrument_id"),
                field="option_target.instrument_id",
            )
            expected_fingerprint = require_exact_identity_text(
                item.get("route_fingerprint"),
                field="option_target.route_fingerprint",
            )
            route_key = (instrument_id, expected_fingerprint)
            if route_key not in route_by_scope:
                try:
                    resolved_instrument = lookup_runtime_instrument(instrument_id)
                    resolved_route = route_instrument(resolved_instrument)
                    if resolved_route.fingerprint != expected_fingerprint:
                        raise ValueError("option target route fingerprint mismatch")
                    route_by_scope[route_key] = resolved_route
                except Exception as exc:
                    route_by_scope[route_key] = exc
            route = route_by_scope[route_key]
            if isinstance(route, Exception):
                raise route
            instrument = route.instrument
            option_contract_id = parse_exact_positive_decimal_provider_id(intent.get("con_id"))
            if not option_contract_id:
                raise ValueError("option target provider contract id is required")
            raw_local_symbol = intent.get("local_symbol")
            if raw_local_symbol is not None and not isinstance(raw_local_symbol, str):
                raise ValueError("option target local_symbol must be an exact string")
            raw_exchange = require_exact_identity_text(
                intent.get("exchange"),
                field="option_target.exchange",
            )
            if intent.get("right") not in {"C", "P"}:
                raise ValueError("option target right must be exactly C or P")
            if intent.get("mode") not in {"conservative", "normal", "aggressive"}:
                raise ValueError("option target mode is invalid")
            try:
                require_option_target_dte(intent.get("target_dte"))
            except ValueError as exc:
                raise ValueError("option target target_dte is invalid") from exc
            raw_expiry = intent.get("expiry")
            if not isinstance(raw_expiry, str) or len(raw_expiry) != 8 or not raw_expiry.isdigit():
                raise ValueError("option target expiry must be exact YYYYMMDD")
            try:
                datetime.strptime(raw_expiry, "%Y%m%d")
            except ValueError as exc:
                raise ValueError("option target expiry must be a valid provider date") from exc
            raw_expiry_at = intent.get("expiry_at")
            if parse_gex_timestamp(raw_expiry_at) is None:
                raise ValueError("option target expiry_at must be an exact provider timestamp")
            for field_name in ("trading_class", "currency"):
                require_exact_identity_text(
                    intent.get(field_name),
                    field=f"option_target.{field_name}",
                )
            raw_multiplier = intent.get("multiplier")
            if option_target_positive_number(raw_multiplier) is None:
                raise ValueError("option target multiplier must be positive and finite")
            raw_strike = intent.get("strike")
            if (
                isinstance(raw_strike, bool)
                or not isinstance(raw_strike, (int, float))
                or option_target_positive_number(raw_strike) is None
            ):
                raise ValueError("option target strike must be positive and finite")
            if not route.adapter.capabilities.options:
                raise ValueError(
                    f"OPTION_PROVIDER_UNSUPPORTED provider={route.provider} instrument_key={route.instrument_key}"
                )
            target_at = parse_iso_ts(_target_ts(item))
            if (
                not isinstance(target_at, datetime)
                or target_at.tzinfo is None
                or target_at.utcoffset() is None
            ):
                raise ValueError("option target target_ts must be timezone-aware")
            point = _target_point(item)
            raw_target_slot = point.get("barSlot")
            if raw_target_slot is not None or point.get("future") is True:
                if isinstance(raw_target_slot, bool) or type(raw_target_slot) is not int:
                    raise ValueError("future option target requires an exact provider bar slot")
                target_timeframe = item.get("timeframe")
                if not isinstance(target_timeframe, str) or not target_timeframe:
                    raise ValueError("future option target requires an exact timeframe")
                # Exact target timestamp/slot membership is admitted once by
                # the durable intent writer. Repricing consumes that canonical
                # point and must not rebuild the provider session axis every
                # two seconds for immutable user intent.
            underlying_key = (instrument_id, expected_fingerprint)
            if underlying_key not in underlying_by_route:
                try:
                    underlying_by_route[underlying_key] = option_target_underlying_quote(
                        instrument,
                        expected_fingerprint,
                        quote_cache_for_instruments,
                        observed_at=valuation_now,
                    )
                except Exception as exc:
                    underlying_by_route[underlying_key] = exc
            underlying_sample = underlying_by_route[underlying_key]
            if isinstance(underlying_sample, Exception):
                raise underlying_sample
            underlying_price, underlying_quote = underlying_sample
            result = route.adapter.option_target_price(
                route.instrument,
                target_price=target_price,
                target_ts=target_at,
                right=str(intent["right"]),
                mode=str(intent["mode"]),
                dte=str(intent["target_dte"]),
                strike=intent.get("strike"),
                expiry=intent.get("expiry"),
                expiry_at=intent.get("expiry_at"),
                con_id=option_contract_id,
                sec_type=intent.get("sec_type"),
                local_symbol=raw_local_symbol,
                exchange=raw_exchange,
                trading_class=intent.get("trading_class"),
                multiplier=intent.get("multiplier"),
                currency=intent.get("currency"),
                underlying_price=underlying_price,
                underlying_quote=underlying_quote,
                now=valuation_now,
                premium_caps=premium_caps,
                store=store,
                option_quote_consumer_id=f"option-point:{target_id}",
            )
        except Exception as exc:
            if not instrument_id or not expected_fingerprint:
                continue
            next_payload = {
                "live_quote_status": "error",
                "live_quote_message": str(exc) or exc.__class__.__name__,
                "compression_sample_at": valuation_now.isoformat(),
            }
        else:
            if not result.get("ok"):
                next_payload = {
                    "live_quote_status": "error",
                    "live_quote_message": str(
                        result.get("message") or "Option target repricing failed."
                    ),
                    "compression_sample_at": valuation_now.isoformat(),
                }
            else:
                result_identity_matches = (
                    result.get("instrument_id") == instrument_id
                    and result.get("route_fingerprint") == expected_fingerprint
                )
                next_payload = (
                    _merge_repriced_payload(
                        payload,
                        result,
                        sampled_at=valuation_now,
                    )
                    if result_identity_matches
                    else None
                )
                if next_payload is None:
                    next_payload = {
                        "live_quote_status": "error",
                        "live_quote_message": "Provider route or contract facts changed for the stored conId.",
                        "compression_sample_at": valuation_now.isoformat(),
                    }
        if not _compression_values_changed(market_sample, next_payload):
            continue
        updates.append((instrument_id, expected_fingerprint, target_id, next_payload))
    return updates


async def run_option_target_reprice_loop(
    *,
    poll_seconds: float,
    server_sleeping: Callable[[], bool],
    store_factory: Callable[[], Any],
    option_target_caps_settings: Callable[[], dict[str, float]],
    quote_cache_for_instruments: Callable[
        ...,
        tuple[dict[str, dict[str, Any]] | None, str, QuoteCacheRevision],
    ],
    parse_iso_ts: Callable[[str | None], datetime | None],
    option_target_samples_committed: Callable[..., Any],
    logger_debug: Callable[..., None],
) -> None:
    if (
        isinstance(poll_seconds, bool)
        or not isinstance(poll_seconds, (int, float))
        or not math.isfinite(float(poll_seconds))
        or poll_seconds < 1.0
    ):
        raise ValueError("option target reprice poll_seconds must be finite and at least 1 second")
    poll_interval = float(poll_seconds)
    state = _OptionTargetRepriceState()
    await asyncio.sleep(poll_interval)
    while True:
        try:
            if not server_sleeping():
                await _run_option_target_reprice_cycle(
                    state=state,
                    store_factory=store_factory,
                    option_target_caps_settings=option_target_caps_settings,
                    quote_cache_for_instruments=quote_cache_for_instruments,
                    parse_iso_ts=parse_iso_ts,
                    option_target_samples_committed=option_target_samples_committed,
                )
        except Exception as exc:
            logger_debug("option target server reprice loop failed: %s", exc)
        await asyncio.sleep(poll_interval)
