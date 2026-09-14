from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from aef_terminal.data.instrument_identity import (
    parse_exact_positive_decimal_provider_id,
    require_exact_identity_text,
)
from aef_terminal.data.provider_contract import (
    OptionUniverseUnavailableError,
    require_option_expiry_mode,
)
from aef_terminal.domain import OptionRight

_IBKR_OPTION_MULTIPLIER_PATTERN = re.compile(r"[0-9]+(?:\.[0-9]+)?")


def require_ibkr_option_multiplier(value: object) -> float:
    """Parse one exact provider multiplier without accepting coercive text."""

    if isinstance(value, bool) or not isinstance(
        value,
        (int, float, str),
    ):
        raise ValueError("IBKR option contract is missing an exact provider multiplier")
    if isinstance(value, str) and (
        not value
        or value != value.strip()
        or _IBKR_OPTION_MULTIPLIER_PATTERN.fullmatch(value) is None
    ):
        raise ValueError("IBKR option contract is missing an exact provider multiplier")
    try:
        multiplier = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("IBKR option contract is missing an exact provider multiplier") from exc
    if not math.isfinite(multiplier) or not 0 < multiplier <= 1000:
        raise ValueError("IBKR option contract is missing an exact provider multiplier")
    return multiplier


class IbkrOptionUniverseUnavailableError(OptionUniverseUnavailableError):
    """The exact provider route has no usable option universe right now."""

    def __init__(
        self,
        message: str,
        *,
        reason: str = "OPTION_UNIVERSE_UNAVAILABLE",
        diagnostics: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(
            message,
            reason=require_exact_identity_text(
                reason,
                field="IBKR_OPTION_UNIVERSE_REASON",
            ),
            diagnostics=diagnostics,
        )


def ibkr_option_series_key(
    expiry: str,
    trading_class: str,
    exchange: str,
    multiplier: float,
) -> tuple[str, str, str, float]:
    """Build the immutable identity of one exact IBKR option series."""

    return (expiry, trading_class, exchange, multiplier)


@dataclass(frozen=True)
class IbkrOptionSeriesExpiryFact:
    """Exact provider expiry for one immutable IBKR option series."""

    expiry: str
    trading_class: str
    exchange: str
    multiplier: float
    expiry_at: datetime
    contract_count: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.expiry, str) or len(self.expiry) != 8 or not self.expiry.isdigit():
            raise ValueError("IBKR option series expiry fact requires YYYYMMDD")
        try:
            datetime.strptime(self.expiry, "%Y%m%d")
        except ValueError as exc:
            raise ValueError("IBKR option series expiry fact has an invalid expiry") from exc
        trading_class = require_exact_identity_text(
            self.trading_class,
            field="IBKR_OPTION_TRADING_CLASS",
        )
        exchange = require_exact_identity_text(
            self.exchange,
            field="IBKR_OPTION_EXCHANGE",
        )
        if (
            isinstance(self.multiplier, bool)
            or not isinstance(self.multiplier, (int, float))
            or not math.isfinite(float(self.multiplier))
            or float(self.multiplier) <= 0
        ):
            raise ValueError("IBKR option series expiry fact requires a positive multiplier")
        if (
            not isinstance(self.expiry_at, datetime)
            or self.expiry_at.tzinfo is None
            or self.expiry_at.utcoffset() is None
        ):
            raise ValueError(
                "IBKR option series expiry fact requires a timezone-aware provider timestamp"
            )
        if type(self.contract_count) is not int or self.contract_count < 0:
            raise ValueError("IBKR option series expiry fact contract count is invalid")
        object.__setattr__(self, "trading_class", trading_class)
        object.__setattr__(self, "exchange", exchange)
        object.__setattr__(self, "multiplier", float(self.multiplier))
        object.__setattr__(
            self,
            "expiry_at",
            self.expiry_at.astimezone(UTC),
        )

    @property
    def series_key(self) -> tuple[str, str, str, float]:
        return ibkr_option_series_key(
            self.expiry,
            self.trading_class,
            self.exchange,
            self.multiplier,
        )


def require_ibkr_option_expiry_facts(
    value: object,
) -> tuple[IbkrOptionSeriesExpiryFact, ...]:
    """Validate an immutable, conflict-free set of exact provider facts."""

    if not isinstance(value, tuple) or any(
        not isinstance(fact, IbkrOptionSeriesExpiryFact) for fact in value
    ):
        raise TypeError("IBKR option expiry facts must be a typed immutable tuple")
    facts_by_series: dict[
        tuple[str, str, str, float],
        datetime,
    ] = {}
    for fact in value:
        existing = facts_by_series.get(fact.series_key)
        if existing is not None and existing != fact.expiry_at:
            raise ValueError("IBKR option expiry facts conflict for one exact series")
        facts_by_series[fact.series_key] = fact.expiry_at
    return value


def read_persisted_ibkr_option_expiry_facts(
    *,
    instrument_id: str,
    route_fingerprint: str,
    store: Any | None,
) -> tuple[IbkrOptionSeriesExpiryFact, ...]:
    """Read exact provider facts from existing snapshot raw contracts."""

    exact_instrument_id = require_exact_identity_text(
        instrument_id,
        field="IBKR_OPTION_INSTRUMENT_ID",
    )
    exact_route_fingerprint = require_exact_identity_text(
        route_fingerprint,
        field="IBKR_OPTION_ROUTE_FINGERPRINT",
    )
    if store is None:
        return ()
    rows = store.read_gex_option_expiry_facts(exact_instrument_id, exact_route_fingerprint)
    if not isinstance(rows, list):
        raise TypeError("IBKR option expiry fact storage must return a typed list")
    facts_by_series: dict[
        tuple[str, str, str, float],
        IbkrOptionSeriesExpiryFact,
    ] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise TypeError("IBKR option expiry fact storage rows must be mappings")
        expiry_at_values = row.get("expiry_at_values")
        if not isinstance(expiry_at_values, list) or len(expiry_at_values) != 1:
            raise RuntimeError(
                "Persisted IBKR option expiry facts conflict for one exact provider series"
            )
        raw_expiry_at = expiry_at_values[0]
        if not isinstance(raw_expiry_at, str):
            raise ValueError("Persisted IBKR option expiry fact timestamp is invalid")
        try:
            expiry_at = datetime.fromisoformat(raw_expiry_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("Persisted IBKR option expiry fact timestamp is invalid") from exc
        if (
            expiry_at.tzinfo is None
            or expiry_at.utcoffset() is None
            or raw_expiry_at != expiry_at.astimezone(UTC).isoformat()
        ):
            raise ValueError("Persisted IBKR option expiry fact timestamp is not canonical")
        raw_multiplier = row.get("multiplier")
        try:
            multiplier = require_ibkr_option_multiplier(raw_multiplier)
        except ValueError as exc:
            raise ValueError("Persisted IBKR option expiry fact multiplier is invalid") from exc
        fact = IbkrOptionSeriesExpiryFact(
            expiry=row.get("expiry"),
            trading_class=row.get("trading_class"),
            exchange=row.get("exchange"),
            multiplier=multiplier,
            expiry_at=expiry_at,
            contract_count=row.get("contract_count"),
        )
        existing = facts_by_series.get(fact.series_key)
        if existing is not None and existing.expiry_at != fact.expiry_at:
            raise RuntimeError(
                "Persisted IBKR option expiry facts conflict for one exact provider series"
            )
        if existing is None or fact.contract_count > existing.contract_count:
            facts_by_series[fact.series_key] = fact
    return tuple(facts_by_series[key] for key in sorted(facts_by_series))


@dataclass(frozen=True)
class IbkrOptionContractRequest:
    """Bounded exact-chain qualification request; it never requests market data."""

    route_fingerprint: str
    futures_options: bool
    strike_count: int
    max_expirations: int
    max_contracts: int
    expiry_mode: str
    option_expiry_facts: tuple[IbkrOptionSeriesExpiryFact, ...] = ()

    def __post_init__(self) -> None:
        require_exact_identity_text(
            self.route_fingerprint,
            field="OPTION_ROUTE_FINGERPRINT",
        )
        if type(self.futures_options) is not bool:
            raise TypeError("OPTION_FUTURES_OPTIONS_INVALID")
        for field_name in ("strike_count", "max_expirations", "max_contracts"):
            value = getattr(self, field_name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"OPTION_{field_name.upper()}_INVALID")
        if self.max_contracts < (self.strike_count * self.max_expirations * 2):
            raise ValueError("OPTION_CONTRACT_BUDGET_INCOMPLETE")
        try:
            require_option_expiry_mode(self.expiry_mode)
        except ValueError as exc:
            raise ValueError("OPTION_EXPIRY_MODE_INVALID") from exc
        require_ibkr_option_expiry_facts(self.option_expiry_facts)


@dataclass(frozen=True)
class IbkrPriceIncrement:
    """One exact price band returned by ``reqMarketRule``."""

    low_edge: float
    increment: float

    def __post_init__(self) -> None:
        if (
            isinstance(self.low_edge, bool)
            or not isinstance(self.low_edge, (int, float))
            or not math.isfinite(float(self.low_edge))
            or float(self.low_edge) < 0
            or isinstance(self.increment, bool)
            or not isinstance(self.increment, (int, float))
            or not math.isfinite(float(self.increment))
            or float(self.increment) <= 0
        ):
            raise ValueError("IBKR price increment is invalid")
        object.__setattr__(self, "low_edge", float(self.low_edge))
        object.__setattr__(self, "increment", float(self.increment))

    def to_payload(self) -> dict[str, float]:
        return {
            "low_edge": self.low_edge,
            "increment": self.increment,
        }


def require_ibkr_price_increments(
    value: object,
) -> tuple[IbkrPriceIncrement, ...]:
    """Validate one complete, ordered provider market-rule schedule."""

    if not isinstance(value, tuple) or not value:
        raise ValueError("IBKR price increment schedule is unavailable")
    if any(not isinstance(item, IbkrPriceIncrement) for item in value):
        raise TypeError("IBKR price increment schedule must be typed")
    if value[0].low_edge != 0:
        raise ValueError("IBKR price increment schedule must start at zero")
    if any(right.low_edge <= left.low_edge for left, right in zip(value, value[1:], strict=False)):
        raise ValueError("IBKR price increment schedule is not ordered")
    return value


@dataclass(frozen=True)
class IbkrOptionContract:
    """Qualified IBKR option identity plus its broker transport handle."""

    raw: Any = field(repr=False, compare=False)
    con_id: int
    sec_type: str
    symbol: str
    expiry: str
    expiry_at: datetime | None
    strike: float
    right: OptionRight
    multiplier: float
    trading_class: str
    exchange: str
    currency: str
    local_symbol: str
    market_rule_id: int | None = None
    minimum_tick: float | None = None
    price_increments: tuple[IbkrPriceIncrement, ...] = ()

    def __post_init__(self) -> None:
        if self.market_rule_id is not None and (
            type(self.market_rule_id) is not int or self.market_rule_id <= 0
        ):
            raise ValueError("IBKR option market rule id is invalid")
        if self.minimum_tick is not None and (
            isinstance(self.minimum_tick, bool)
            or not isinstance(self.minimum_tick, (int, float))
            or not math.isfinite(float(self.minimum_tick))
            or float(self.minimum_tick) <= 0
        ):
            raise ValueError("IBKR option minimum tick is invalid")
        if self.price_increments:
            require_ibkr_price_increments(self.price_increments)
            if self.market_rule_id is None:
                raise ValueError("IBKR option price increments require a market rule id")
        if self.minimum_tick is not None:
            object.__setattr__(self, "minimum_tick", float(self.minimum_tick))

    @property
    def futures_option(self) -> bool:
        return self.sec_type == "FOP"

    @property
    def series_key(self) -> tuple[str, str, str, float]:
        return ibkr_option_series_key(
            self.expiry,
            self.trading_class,
            self.exchange,
            self.multiplier,
        )


def partition_ibkr_option_contracts_by_expiry(
    contracts: Sequence[IbkrOptionContract],
    *,
    now: datetime | None = None,
) -> tuple[
    tuple[IbkrOptionContract, ...],
    tuple[IbkrOptionContract, ...],
    tuple[IbkrOptionContract, ...],
]:
    """Partition exact contracts without inferring a provider expiry time."""

    current = now or datetime.now(tz=UTC)
    if not isinstance(current, datetime) or current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("IBKR option expiry clock must be timezone-aware")
    current_utc = current.astimezone(UTC)
    active: list[IbkrOptionContract] = []
    expired: list[IbkrOptionContract] = []
    unknown: list[IbkrOptionContract] = []
    for contract in contracts:
        if not isinstance(contract, IbkrOptionContract):
            raise TypeError("IBKR option expiry partition requires typed option contracts")
        expiry_at = contract.expiry_at
        if (
            not isinstance(expiry_at, datetime)
            or expiry_at.tzinfo is None
            or expiry_at.utcoffset() is None
        ):
            unknown.append(contract)
        elif expiry_at.astimezone(UTC) <= current_utc:
            expired.append(contract)
        else:
            active.append(contract)
    return tuple(active), tuple(expired), tuple(unknown)


@dataclass(frozen=True)
class IbkrOptionMarketDataSource:
    """One provider-owned exact option ticker universe reusable by consumers."""

    source_id: str
    instrument_id: str
    route_fingerprint: str
    provider_symbol: str
    expiry_mode: str
    contracts: tuple[IbkrOptionContract, ...]
    tickers: tuple[Any, ...] = field(repr=False, compare=False)
    chain_meta: Mapping[str, Any]
    transport: Any = field(repr=False, compare=False)
    generation: str

    def __post_init__(self) -> None:
        require_exact_identity_text(
            self.source_id,
            field="OPTION_MARKET_DATA_SOURCE_ID",
        )
        require_exact_identity_text(
            self.instrument_id,
            field="OPTION_MARKET_DATA_INSTRUMENT_ID",
        )
        require_exact_identity_text(
            self.route_fingerprint,
            field="OPTION_MARKET_DATA_ROUTE_FINGERPRINT",
        )
        require_exact_identity_text(
            self.provider_symbol,
            field="OPTION_MARKET_DATA_PROVIDER_SYMBOL",
        )
        require_exact_identity_text(
            self.generation,
            field="OPTION_MARKET_DATA_GENERATION",
        )
        try:
            require_option_expiry_mode(self.expiry_mode)
        except ValueError as exc:
            raise ValueError("OPTION_MARKET_DATA_EXPIRY_MODE_INVALID") from exc
        if not self.contracts or len(self.contracts) != len(self.tickers):
            raise ValueError("OPTION_MARKET_DATA_SOURCE_UNIVERSE_INVALID")

    @property
    def key(self) -> tuple[str, str, str]:
        return (
            self.instrument_id,
            self.route_fingerprint,
            self.source_id,
        )


def parse_ibkr_option_contract(
    contract: Any,
    *,
    expiry_at: str | datetime | None = None,
) -> IbkrOptionContract:
    """Validate and snapshot one provider-qualified IBKR OPT/FOP contract."""

    con_id = parse_exact_positive_decimal_provider_id(getattr(contract, "conId", None))
    if con_id <= 0:
        raise ValueError("IBKR option contract is missing an exact provider conId")

    sec_type = getattr(contract, "secType", None)
    if sec_type not in {"OPT", "FOP"}:
        raise ValueError(f"IBKR option contract has unsupported secType={sec_type or 'missing'}")

    symbol = getattr(contract, "symbol", None)
    if not isinstance(symbol, str) or not symbol or symbol != symbol.strip():
        raise ValueError("IBKR option contract is missing its provider symbol")

    expiry = getattr(contract, "lastTradeDateOrContractMonth", None)
    if not isinstance(expiry, str) or len(expiry) != 8 or not expiry.isdigit():
        raise ValueError("IBKR option contract is missing an exact YYYYMMDD expiry")
    try:
        datetime.strptime(expiry, "%Y%m%d")
    except ValueError as exc:
        raise ValueError("IBKR option contract is missing an exact YYYYMMDD expiry") from exc

    try:
        right = OptionRight(getattr(contract, "right", None))
    except ValueError as exc:
        raise ValueError("IBKR option contract right must be C or P") from exc

    raw_strike = getattr(contract, "strike", None)
    if isinstance(raw_strike, bool) or not isinstance(raw_strike, (int, float)):
        raise ValueError("IBKR option contract is missing a positive finite strike")
    strike = float(raw_strike)
    if not math.isfinite(strike) or strike <= 0:
        raise ValueError("IBKR option contract is missing a positive finite strike")

    multiplier = require_ibkr_option_multiplier(getattr(contract, "multiplier", None))

    exchange = getattr(contract, "exchange", None)
    trading_class = getattr(contract, "tradingClass", None)
    if (
        not isinstance(exchange, str)
        or not exchange
        or exchange != exchange.strip()
        or not isinstance(trading_class, str)
        or not trading_class
        or trading_class != trading_class.strip()
    ):
        raise ValueError("IBKR option contract is missing exact exchange or trading class metadata")
    currency = require_exact_identity_text(
        getattr(contract, "currency", None),
        field="IBKR_OPTION_CURRENCY",
    )

    raw_expiry_at = expiry_at
    if raw_expiry_at is None:
        raw_expiry_at = getattr(contract, "aefExpiryAt", None)
    parsed_expiry_at: datetime | None = None
    if isinstance(raw_expiry_at, datetime):
        parsed_expiry_at = raw_expiry_at
    elif raw_expiry_at:
        try:
            parsed_expiry_at = datetime.fromisoformat(str(raw_expiry_at).replace("Z", "+00:00"))
        except ValueError:
            parsed_expiry_at = None
    if parsed_expiry_at is not None:
        if parsed_expiry_at.tzinfo is None:
            parsed_expiry_at = None
        else:
            parsed_expiry_at = parsed_expiry_at.astimezone(UTC)

    return IbkrOptionContract(
        raw=contract,
        con_id=con_id,
        sec_type=sec_type,
        symbol=symbol,
        expiry=expiry,
        expiry_at=parsed_expiry_at,
        strike=strike,
        right=right,
        multiplier=multiplier,
        trading_class=trading_class,
        exchange=exchange,
        currency=currency,
        local_symbol=(
            getattr(contract, "localSymbol", "")
            if isinstance(getattr(contract, "localSymbol", ""), str)
            else ""
        ),
    )
