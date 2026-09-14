from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import logging
from threading import RLock
import time
from typing import Any

from aef_terminal.paper_contract import PaperContractIdentity, require_paper_contract_identity
from aef_terminal.runtime.math_utils import exact_finite_number_or_none
from aef_terminal.runtime.metrics import increment_metric, observe_metric, set_metric


PAPER_OPTION_BBO_MAX_AGE_SECONDS = 10.0

_OPTION_QUOTE_LOCK = RLock()
_OPTION_QUOTES: dict[str, dict[str, Any]] = {}
_OPTION_CONTRACTS: dict[str, PaperContractIdentity] = {}
_OPTION_DEMANDS: dict[str, PaperContractIdentity] = {}
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PaperOptionQuoteRefreshResult:
    demanded: int
    refreshed: int
    failed: int
    released: int


def _option_price_increment_schedule(
    value: object,
) -> tuple[tuple[Decimal, Decimal], ...]:
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError("PAPER_OPTION_PRICE_RULE_UNAVAILABLE")
    schedule: list[tuple[Decimal, Decimal]] = []
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {
            "low_edge",
            "increment",
        }:
            raise ValueError("PAPER_OPTION_PRICE_RULE_INVALID")
        try:
            low_edge = Decimal(str(item["low_edge"]))
            increment = Decimal(str(item["increment"]))
        except InvalidOperation as exc:
            raise ValueError("PAPER_OPTION_PRICE_RULE_INVALID") from exc
        if not low_edge.is_finite() or low_edge < 0 or not increment.is_finite() or increment <= 0:
            raise ValueError("PAPER_OPTION_PRICE_RULE_INVALID")
        schedule.append((low_edge, increment))
    if schedule[0][0] != 0 or any(
        right[0] <= left[0] for left, right in zip(schedule, schedule[1:], strict=False)
    ):
        raise ValueError("PAPER_OPTION_PRICE_RULE_INVALID")
    return tuple(schedule)


def require_option_paper_limit_price(
    value: object,
    *,
    price_increments: object,
) -> float:
    premium = exact_finite_number_or_none(value)
    if premium is None or premium <= 0:
        raise ValueError("PAPER_OPTION_LIMIT_PRICE_INVALID")
    try:
        decimal_price = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("PAPER_OPTION_LIMIT_PRICE_INVALID") from exc
    schedule = _option_price_increment_schedule(price_increments)
    low_edge, decimal_increment = schedule[0]
    for rule_low_edge, rule_increment in schedule[1:]:
        if decimal_price < rule_low_edge:
            break
        low_edge, decimal_increment = rule_low_edge, rule_increment
    units = (decimal_price - low_edge) / decimal_increment
    if units != units.to_integral_value():
        raise ValueError(f"PAPER_OPTION_LIMIT_PRICE_TICK_INVALID increment={decimal_increment}")
    return float(decimal_price)


def _option_contract(
    entity: Mapping[str, Any] | PaperContractIdentity,
) -> PaperContractIdentity | None:
    if isinstance(entity, PaperContractIdentity):
        return entity if entity.scope_kind == "option" else None
    try:
        contract = require_paper_contract_identity(entity)
    except ValueError:
        return None
    return contract if contract.scope_kind == "option" else None


def _option_quote_consumer_id(contract: PaperContractIdentity) -> str:
    digest = hashlib.sha256(contract.scope_key.encode("utf-8")).hexdigest()[:24]
    return f"paper-option:{digest}"


def _bbo_observed_at(quote: Mapping[str, Any]) -> datetime | None:
    raw = quote.get("bid_ask_received_at")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _live_option_bbo(
    quote: Mapping[str, Any],
    *,
    now: datetime,
) -> tuple[float, float, datetime] | None:
    bid = exact_finite_number_or_none(quote.get("bid"))
    ask = exact_finite_number_or_none(quote.get("ask"))
    observed_at = _bbo_observed_at(quote)
    if (
        bid is None
        or ask is None
        or bid < 0
        or ask <= 0
        or ask < bid
        or observed_at is None
        or quote.get("market_data_entitlement") != "live"
        or quote.get("is_delayed") is not False
    ):
        return None
    age_seconds = (now.astimezone(UTC) - observed_at).total_seconds()
    if age_seconds < -1.0 or age_seconds > PAPER_OPTION_BBO_MAX_AGE_SECONDS:
        return None
    return bid, ask, observed_at


def record_paper_option_quote(
    entity: Mapping[str, Any] | PaperContractIdentity,
    quote: Mapping[str, Any],
) -> bool:
    contract = _option_contract(entity)
    if contract is None or not isinstance(quote, Mapping):
        return False
    with _OPTION_QUOTE_LOCK:
        _OPTION_CONTRACTS[contract.scope_key] = contract
        _OPTION_QUOTES[contract.scope_key] = dict(quote)
    return True


def paper_option_execution_snapshot(
    entity: Mapping[str, Any],
    quote: Mapping[str, Any] | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    contract = _option_contract(entity)
    side = str(entity.get("side") or "").lower()
    if contract is None or side not in {"long", "short"}:
        return None
    if quote is None:
        with _OPTION_QUOTE_LOCK:
            current_quote = dict(_OPTION_QUOTES.get(contract.scope_key) or {})
    else:
        current_quote = dict(quote)
    current = now or datetime.now(tz=UTC)
    bbo = _live_option_bbo(current_quote, now=current)
    if bbo is None:
        return None
    bid, ask, observed_at = bbo
    price = (bid + ask) / 2.0
    return {
        "price": price,
        "low": price,
        "high": price,
        "range_kind": "quote",
        "bid": bid,
        "ask": ask,
        "bid_ask_status": "live",
        "bbo_ts": observed_at,
        "ts": observed_at,
        "source": f"{contract.provider}:option-mid",
    }


def paper_option_mark_price(entity: Mapping[str, Any]) -> float | None:
    contract = _option_contract(entity)
    if contract is None:
        return None
    with _OPTION_QUOTE_LOCK:
        quote = dict(_OPTION_QUOTES.get(contract.scope_key) or {})
    bbo = _live_option_bbo(quote, now=datetime.now(tz=UTC))
    if bbo is None:
        return None
    bid, ask, _observed_at = bbo
    return (bid + ask) / 2.0


def set_paper_option_quote_demand(entities: Iterable[Mapping[str, Any]]) -> int:
    prioritized: dict[str, tuple[int, PaperContractIdentity]] = {}
    for entity in entities:
        contract = _option_contract(entity)
        if contract is None or contract.provider != "ibkr":
            continue
        priority = (
            0
            if bool(entity.get("reduce_only"))
            else 1
            if str(entity.get("status") or "").lower() == "open"
            else 2
        )
        previous = prioritized.get(contract.scope_key)
        if previous is None or priority < previous[0]:
            prioritized[contract.scope_key] = (priority, contract)
    demanded = {
        scope_key: contract
        for scope_key, (_priority, contract) in sorted(
            prioritized.items(),
            key=lambda item: (item[1][0], item[0]),
        )
    }
    with _OPTION_QUOTE_LOCK:
        _OPTION_DEMANDS.clear()
        _OPTION_DEMANDS.update(demanded)
    set_metric("paper_option_quote_demand", len(demanded))
    return len(demanded)


def refresh_demanded_paper_option_quotes() -> PaperOptionQuoteRefreshResult:
    started_at = time.monotonic()
    with _OPTION_QUOTE_LOCK:
        demanded = dict(_OPTION_DEMANDS)

    with _OPTION_QUOTE_LOCK:
        previous = dict(_OPTION_CONTRACTS)
    if not demanded and not previous:
        return PaperOptionQuoteRefreshResult(
            demanded=0,
            refreshed=0,
            failed=0,
            released=0,
        )

    from aef_terminal.data.ibkr.options import cancel_option_quote, live_option_quote

    refreshed = 0
    failed = 0
    for scope_key, contract in demanded.items():
        try:
            quote = live_option_quote(
                contract.to_payload(),
                consumer_id=_option_quote_consumer_id(contract),
            )
        except Exception as exc:
            failed += 1
            increment_metric(
                "paper_option_quote_refresh_total",
                status="failed",
                error=exc.__class__.__name__,
            )
            _LOGGER.debug(
                "paper option quote refresh failed for %s",
                scope_key,
                exc_info=True,
            )
            with _OPTION_QUOTE_LOCK:
                _OPTION_QUOTES.pop(scope_key, None)
            continue
        record_paper_option_quote(contract, quote)
        refreshed += 1
        increment_metric("paper_option_quote_refresh_total", status="refreshed")

    released = [scope_key for scope_key in previous if scope_key not in demanded]
    released_count = 0
    for scope_key in released:
        contract = previous[scope_key]
        try:
            cancel_option_quote(
                contract.to_payload(),
                consumer_id=_option_quote_consumer_id(contract),
            )
        except Exception as exc:
            failed += 1
            increment_metric(
                "paper_option_quote_refresh_total",
                status="release_failed",
                error=exc.__class__.__name__,
            )
            _LOGGER.debug(
                "paper option quote release failed for %s",
                scope_key,
                exc_info=True,
            )
            continue
        with _OPTION_QUOTE_LOCK:
            _OPTION_CONTRACTS.pop(scope_key, None)
            _OPTION_QUOTES.pop(scope_key, None)
        released_count += 1
        increment_metric("paper_option_quote_refresh_total", status="released")
    observe_metric(
        "paper_option_quote_refresh_seconds",
        time.monotonic() - started_at,
    )
    return PaperOptionQuoteRefreshResult(
        demanded=len(demanded),
        refreshed=refreshed,
        failed=failed,
        released=released_count,
    )


def release_all_paper_option_quotes() -> None:
    with _OPTION_QUOTE_LOCK:
        contracts = list(_OPTION_CONTRACTS.values())
        _OPTION_DEMANDS.clear()
        _OPTION_CONTRACTS.clear()
        _OPTION_QUOTES.clear()
    set_metric("paper_option_quote_demand", 0)
    if not contracts:
        return
    from aef_terminal.data.ibkr.options import cancel_option_quote

    for contract in contracts:
        try:
            cancel_option_quote(
                contract.to_payload(),
                consumer_id=_option_quote_consumer_id(contract),
            )
        except Exception:
            continue


def clear_paper_option_quote_cache() -> None:
    with _OPTION_QUOTE_LOCK:
        _OPTION_DEMANDS.clear()
        _OPTION_CONTRACTS.clear()
        _OPTION_QUOTES.clear()
    set_metric("paper_option_quote_demand", 0)
