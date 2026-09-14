from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Final, Literal, TypedDict, cast

from aef_terminal.data.gex.constants import (
    GEX_LIVE_SNAPSHOT_SOURCE,
    GEX_OPTION_ACTIVITY_MIN_BASELINE_SAMPLES,
    GEX_REQUEST_SNAPSHOT_SOURCE,
    GEX_SCHEDULER_INTERVAL_MINUTES,
)
from aef_terminal.data.market_data import (
    KnownMarketDataEntitlement,
    MarketDataEntitlement,
)

GexCaptureMode = Literal["request", "live"]
GexOptionActivityStatus = Literal["BASELINING", "READY"]
GexKnownMarketDataEntitlement = KnownMarketDataEntitlement
GexMarketDataEntitlement = MarketDataEntitlement


class GexCaptureModeConflictError(RuntimeError):
    def __init__(
        self,
        *,
        instrument_id: str,
        route_fingerprint: str,
        active: GexCaptureMode,
        requested: GexCaptureMode,
    ) -> None:
        self.instrument_id = instrument_id
        self.route_fingerprint = route_fingerprint
        self.active = active
        self.requested = requested
        super().__init__(
            "GEX_CAPTURE_MODE_CONFLICT "
            f"instrument_id={instrument_id} route_fingerprint={route_fingerprint} "
            f"active={active} requested={requested}"
        )


class GexOptionSeriesIdentity(TypedDict):
    expiry: str
    trading_class: str
    exchange: str
    multiplier: float


class GexComparisonScope(TypedDict):
    capture_mode: GexCaptureMode
    strike_count: int
    strike_ladder: list[float]
    contract_con_ids: list[int]
    expiries: list[str]
    futures_options: bool
    series: list[GexOptionSeriesIdentity]
    risk_free_rate: float
    dividend_yield: float
    market_data_entitlement: GexMarketDataEntitlement


GexComparisonLaneKey = tuple[
    GexCaptureMode,
    int,
    tuple[float, ...],
    tuple[int, ...],
    tuple[str, ...],
    bool,
    tuple[tuple[str, str, str, float], ...],
    float,
    float,
    GexKnownMarketDataEntitlement,
]


class GexOptionActivitySide(TypedDict):
    volume_delta: float
    rate_per_minute: float
    rvol: float | None
    baseline_samples: int


class GexOptionActivityContractUniverse(TypedDict):
    selected_strikes: int
    selected_contracts: int
    matched_contracts: int


class GexOptionActivity(TypedDict):
    status: GexOptionActivityStatus
    scope: Literal["selected_chain"]
    elapsed_seconds: float
    contract_universe: GexOptionActivityContractUniverse
    call: GexOptionActivitySide
    put: GexOptionActivitySide
    total: GexOptionActivitySide


class GexContractMarketFacts(TypedDict):
    gamma: float | None
    open_interest: float | None
    volume: float | None
    gex: float | None
    abs_gex: float | None


class GexValuationRates(TypedDict):
    risk_free_rate: float
    dividend_yield: float


GexOptionReferenceSource = Literal[
    "unavailable",
    "bid_ask_mid",
    "ibkr_market_price",
    "last",
    "previous_close",
    "bid",
    "ask",
]


class GexOptionReference(TypedDict):
    reference_option_price: float | None
    reference_option_price_source: GexOptionReferenceSource


GexObservationTimeBasis = Literal["client_receive", "provider_event"]


class GexContractObservation(TypedDict):
    market_data_type: int | None
    market_data_entitlement: GexMarketDataEntitlement
    model_greeks_received_at: str | None
    open_interest_received_at: str | None
    observation_time_basis: Literal["client_receive"]
    open_interest_as_of: Literal["previous_settlement"]


class GexUnderlyingQuoteObservation(TypedDict):
    price: float
    price_source: str
    bid: float | None
    ask: float | None
    last: float | None
    received_at: str | None
    provider_ts: str | None
    time_basis: GexObservationTimeBasis | None
    market_data_type: int | None
    market_data_entitlement: GexMarketDataEntitlement


class GexUnderlyingQuoteUnavailableError(RuntimeError):
    """Report an expected request precondition without opening option lines."""

    def __init__(
        self,
        message: str,
        *,
        reason: str = "UNDERLYING_SPOT_OBSERVATION_INCOMPLETE",
        diagnostics: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.diagnostics = dict(diagnostics or {})


_CAPTURE_LANES: Final[dict[GexCaptureMode, str]] = {
    "request": GEX_REQUEST_SNAPSHOT_SOURCE,
    "live": GEX_LIVE_SNAPSHOT_SOURCE,
}
_COMPARISON_SCOPE_FIELDS = frozenset(
    {
        "capture_mode",
        "strike_count",
        "strike_ladder",
        "contract_con_ids",
        "expiries",
        "futures_options",
        "series",
        "risk_free_rate",
        "dividend_yield",
        "market_data_entitlement",
    }
)
_OPTION_SERIES_IDENTITY_FIELDS = frozenset({"expiry", "trading_class", "exchange", "multiplier"})
_GEX_UNDERLYING_QUOTE_SOURCES: Final[frozenset[str]] = frozenset(
    {
        "unavailable",
        "bid_ask_mid",
        "ibkr_market_price",
        "last",
        "previous_close",
    }
)
_KNOWN_MARKET_DATA_ENTITLEMENTS: Final[frozenset[str]] = frozenset(
    {"live", "frozen", "delayed", "delayed_frozen"}
)
_OPTION_ACTIVITY_FIELDS = frozenset(
    {
        "status",
        "scope",
        "elapsed_seconds",
        "contract_universe",
        "call",
        "put",
        "total",
    }
)
_OPTION_ACTIVITY_SIDE_FIELDS = frozenset(
    {"volume_delta", "rate_per_minute", "rvol", "baseline_samples"}
)
_OPTION_ACTIVITY_UNIVERSE_FIELDS = frozenset(
    {"selected_strikes", "selected_contracts", "matched_contracts"}
)
_OPTION_ACTIVITY_SUM_ABS_TOLERANCE: Final[float] = 0.0002
_FORBIDDEN_GEX_CONTRACT_MARKET_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "open_interest_live",
        "open_interest_source",
        "volume_available",
        "volume_source",
        "risk_free_rate",
        "dividend_yield",
    }
)
_GEX_OPTION_REFERENCE_SOURCES: Final[frozenset[str]] = frozenset(
    {
        "bid_ask_mid",
        "ibkr_market_price",
        "last",
        "previous_close",
        "bid",
        "ask",
    }
)


def require_gex_market_data_entitlement(
    value: object,
    *,
    allow_unknown: bool = False,
) -> GexMarketDataEntitlement:
    """Validate one provider-aggregated actual market-data entitlement."""

    allowed = (
        _KNOWN_MARKET_DATA_ENTITLEMENTS | {"unknown"}
        if allow_unknown
        else _KNOWN_MARKET_DATA_ENTITLEMENTS
    )
    if not isinstance(value, str) or value not in allowed:
        raise ValueError("GEX market-data entitlement is not an exact supported value")
    return cast(GexMarketDataEntitlement, value)


def _canonical_gex_observation_timestamp(
    value: object,
    *,
    field: str,
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"GEX {field} must be a canonical timestamp or null")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"GEX {field} must be a canonical timestamp or null") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"GEX {field} must be timezone-aware")
    canonical = parsed.astimezone(UTC).isoformat()
    if value != canonical:
        raise ValueError(f"GEX {field} must use canonical UTC form")
    return canonical


def require_gex_option_universe_expiry(
    value: object,
    *,
    captured_at: datetime | None = None,
    allow_unknown: bool = False,
) -> datetime | None:
    """Validate the exact provider timestamp that bounds snapshot authority."""

    canonical = _canonical_gex_observation_timestamp(
        value,
        field="option_universe_expires_at",
    )
    if canonical is None:
        if allow_unknown:
            return None
        raise ValueError("GEX option_universe_expires_at requires an exact provider timestamp")
    expiry_at = datetime.fromisoformat(canonical)
    if captured_at is not None:
        if (
            not isinstance(captured_at, datetime)
            or captured_at.tzinfo is None
            or captured_at.utcoffset() is None
        ):
            raise ValueError("GEX option universe capture timestamp must be timezone-aware")
        if expiry_at <= captured_at.astimezone(UTC):
            raise ValueError("GEX option universe must expire after its exact capture")
    return expiry_at


def require_gex_contract_observation(value: object) -> GexContractObservation:
    """Validate field-specific client-receive evidence for one option row."""

    if not isinstance(value, Mapping):
        raise ValueError("GEX contract observation requires a mapping")
    required = {
        "market_data_type",
        "market_data_entitlement",
        "model_greeks_received_at",
        "open_interest_received_at",
        "observation_time_basis",
        "open_interest_as_of",
    }
    if not required.issubset(value):
        raise ValueError("GEX contract observation requires its exact typed fields")
    raw_market_data_type = value.get("market_data_type")
    if raw_market_data_type is not None and type(raw_market_data_type) is not int:
        raise ValueError("GEX contract market_data_type must be exact or null")
    market_data_type = cast(int | None, raw_market_data_type)
    entitlement = require_gex_market_data_entitlement(
        value.get("market_data_entitlement"),
        allow_unknown=True,
    )
    if value.get("observation_time_basis") != "client_receive":
        raise ValueError("GEX option observations require client_receive time basis")
    if value.get("open_interest_as_of") != "previous_settlement":
        raise ValueError("GEX option open interest requires previous_settlement as-of")
    return {
        "market_data_type": market_data_type,
        "market_data_entitlement": entitlement,
        "model_greeks_received_at": _canonical_gex_observation_timestamp(
            value.get("model_greeks_received_at"),
            field="model_greeks_received_at",
        ),
        "open_interest_received_at": _canonical_gex_observation_timestamp(
            value.get("open_interest_received_at"),
            field="open_interest_received_at",
        ),
        "observation_time_basis": "client_receive",
        "open_interest_as_of": "previous_settlement",
    }


def require_gex_underlying_quote_observation(
    value: object,
) -> GexUnderlyingQuoteObservation:
    """Select one exact IBKR spot observation without inventing event time."""

    if not isinstance(value, Mapping):
        raise ValueError("GEX underlying quote observation requires a mapping")

    def positive_number(raw: object, *, field: str) -> float | None:
        if raw is None:
            return None
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError(f"GEX underlying quote {field} must be numeric or null")
        number = float(raw)
        if not math.isfinite(number) or number <= 0:
            raise ValueError(f"GEX underlying quote {field} must be finite positive or null")
        return number

    bid = positive_number(value.get("bid"), field="bid")
    ask = positive_number(value.get("ask"), field="ask")
    last = positive_number(value.get("last"), field="last")
    received_at = _canonical_gex_observation_timestamp(
        value.get("received_at"),
        field="underlying received_at",
    )
    provider_ts = _canonical_gex_observation_timestamp(
        value.get("provider_ts"),
        field="underlying provider_ts",
    )
    raw_source = value.get("price_source")
    if not isinstance(raw_source, str) or raw_source not in _GEX_UNDERLYING_QUOTE_SOURCES:
        raise ValueError("GEX underlying quote requires an exact price source")
    raw_price = positive_number(value.get("price"), field="price")
    raw_basis = value.get("time_basis")
    if raw_basis not in {None, "client_receive", "provider_event"}:
        raise ValueError("GEX underlying quote requires an exact time basis")
    if raw_basis == "provider_event" and provider_ts is None:
        raise ValueError("Provider-event GEX quote requires provider_ts")
    if raw_basis == "client_receive" and received_at is None:
        raise ValueError("Client-receive GEX quote requires received_at")

    if raw_price is None or raw_source == "unavailable":
        raise ValueError("GEX underlying quote requires a positive display price")
    if raw_source == "bid_ask_mid":
        if bid is None or ask is None or ask < bid:
            raise ValueError("Midpoint GEX quote requires an exact valid bid/ask pair")
        midpoint = (bid + ask) / 2.0
        if not math.isclose(raw_price, midpoint, rel_tol=0.0, abs_tol=1e-12):
            raise ValueError("Midpoint GEX quote price disagrees with bid/ask")
        expected_basis = "client_receive" if received_at is not None else None
        if raw_basis != expected_basis or provider_ts is not None:
            raise ValueError("Midpoint GEX quote requires exact client-receive provenance")
        exact_provider_ts = None
    elif raw_source == "last":
        if last is None or raw_price != last:
            raise ValueError("Last-trade GEX quote price disagrees with last")
        expected_basis = "provider_event" if provider_ts is not None else None
        if raw_basis != expected_basis:
            raise ValueError("Last-trade GEX quote requires exact provider-event provenance")
        exact_provider_ts = provider_ts
    else:
        if raw_basis is not None or provider_ts is not None:
            raise ValueError("Display-only GEX quote sources cannot claim an observation clock")
        exact_provider_ts = None
    price = raw_price
    price_source = raw_source
    time_basis = cast(GexObservationTimeBasis | None, raw_basis)

    raw_market_data_type = value.get("market_data_type")
    if raw_market_data_type is not None and type(raw_market_data_type) is not int:
        raise ValueError("GEX underlying market_data_type must be exact or null")
    market_data_type = cast(int | None, raw_market_data_type)
    entitlement = require_gex_market_data_entitlement(
        value.get("market_data_entitlement"),
        allow_unknown=True,
    )
    return {
        "price": price,
        "price_source": price_source,
        "bid": bid,
        "ask": ask,
        "last": last,
        "received_at": received_at,
        "provider_ts": exact_provider_ts,
        "time_basis": time_basis,
        "market_data_type": market_data_type,
        "market_data_entitlement": entitlement,
    }


def gex_underlying_quote_observation_from_cache(
    value: object,
) -> GexUnderlyingQuoteObservation:
    """Project the freshest exact price fact from one canonical quote envelope."""

    if not isinstance(value, Mapping):
        raise ValueError("GEX underlying quote cache entry requires a mapping")

    def positive_number(raw: object) -> float | None:
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return None
        number = float(raw)
        return number if math.isfinite(number) and number > 0 else None

    bid = positive_number(value.get("bid"))
    ask = positive_number(value.get("ask"))
    last = positive_number(value.get("last"))
    candidates: list[tuple[datetime, dict[str, object]]] = []

    bid_ask_at = _canonical_gex_observation_timestamp(
        value.get("bid_ask_received_at") or value.get("received_at"),
        field="underlying bid/ask received_at",
    )
    if bid is not None and ask is not None and ask >= bid and bid_ask_at is not None:
        candidates.append(
            (
                datetime.fromisoformat(bid_ask_at),
                {
                    "price": (bid + ask) / 2.0,
                    "price_source": "bid_ask_mid",
                    "received_at": bid_ask_at,
                    "provider_ts": None,
                    "time_basis": "client_receive",
                },
            )
        )

    last_at = _canonical_gex_observation_timestamp(
        value.get("last_provider_ts")
        or (value.get("provider_ts") if value.get("price_source") == "last" else None),
        field="underlying last provider_ts",
    )
    if last is not None and last_at is not None:
        candidates.append(
            (
                datetime.fromisoformat(last_at),
                {
                    "price": last,
                    "price_source": "last",
                    "received_at": value.get("received_at"),
                    "provider_ts": last_at,
                    "time_basis": "provider_event",
                },
            )
        )

    if candidates:
        _observed_at, selected = max(candidates, key=lambda item: item[0])
        projected = {
            **selected,
            "bid": bid,
            "ask": ask,
            "last": last,
            "market_data_type": value.get("market_data_type"),
            "market_data_entitlement": value.get("market_data_entitlement"),
        }
        return require_gex_underlying_quote_observation(projected)

    return require_gex_underlying_quote_observation(
        {
            "price": value.get("price"),
            "price_source": value.get("price_source"),
            "bid": bid,
            "ask": ask,
            "last": last,
            "received_at": value.get("received_at"),
            "provider_ts": value.get("provider_ts"),
            "time_basis": value.get("time_basis"),
            "market_data_type": value.get("market_data_type"),
            "market_data_entitlement": value.get("market_data_entitlement"),
        }
    )


def require_gex_option_series_identity(
    value: object,
) -> GexOptionSeriesIdentity:
    """Validate the exact provider option-series identity carried by a row."""

    if not isinstance(value, Mapping):
        raise ValueError("GEX option series identity must be a mapping")
    expiry = value.get("expiry")
    if not isinstance(expiry, str) or len(expiry) != 8 or not expiry.isdigit():
        raise ValueError("GEX option series requires an exact YYYYMMDD expiry")
    try:
        datetime.strptime(expiry, "%Y%m%d")
    except ValueError as exc:
        raise ValueError("GEX option series expiry is invalid") from exc
    exact_text: dict[str, str] = {}
    for field in ("trading_class", "exchange"):
        raw = value.get(field)
        if not isinstance(raw, str) or not raw or raw != raw.strip():
            raise ValueError(f"GEX option series requires exact {field}")
        exact_text[field] = raw
    raw_multiplier = value.get("multiplier")
    if (
        isinstance(raw_multiplier, bool)
        or not isinstance(raw_multiplier, (int, float))
        or not math.isfinite(float(raw_multiplier))
        or float(raw_multiplier) <= 0
    ):
        raise ValueError("GEX option series requires a finite positive multiplier")
    return {
        "expiry": expiry,
        "trading_class": exact_text["trading_class"],
        "exchange": exact_text["exchange"],
        "multiplier": float(raw_multiplier),
    }


def require_gex_valuation_rates(value: object) -> GexValuationRates:
    """Return the one bounded request-wide option-valuation rate contract."""

    if not isinstance(value, Mapping):
        raise ValueError("GEX valuation rates must be a mapping")
    rates: dict[str, float] = {}
    for field in ("risk_free_rate", "dividend_yield"):
        raw = value.get(field)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError(f"GEX {field} must be a finite numeric rate")
        rate = float(raw)
        if not math.isfinite(rate) or not 0.0 <= rate <= 0.25:
            raise ValueError(f"GEX {field} must be between 0 and 0.25")
        rates[field] = rate
    return cast(GexValuationRates, rates)


def require_gex_option_reference(value: object) -> GexOptionReference:
    """Return one producer-selected option reference with exact provenance."""

    if not isinstance(value, Mapping):
        raise ValueError("GEX option reference must be a mapping")
    if "option_price" in value:
        raise ValueError("GEX option contract contains deleted legacy option_price")
    if not {
        "reference_option_price",
        "reference_option_price_source",
    }.issubset(value):
        raise ValueError("GEX option contract requires canonical reference provenance")
    raw_price = value.get("reference_option_price")
    source = value.get("reference_option_price_source")
    if raw_price is None:
        if source != "unavailable":
            raise ValueError("Unavailable GEX option reference requires unavailable provenance")
        return {
            "reference_option_price": None,
            "reference_option_price_source": "unavailable",
        }
    if isinstance(raw_price, bool) or not isinstance(raw_price, (int, float)):
        raise ValueError("GEX option reference price must be numeric or null")
    price = float(raw_price)
    if not math.isfinite(price) or price <= 0:
        raise ValueError("GEX option reference price must be finite and positive")
    if source not in _GEX_OPTION_REFERENCE_SOURCES:
        raise ValueError("GEX option reference source is not an exact supported value")
    return {
        "reference_option_price": price,
        "reference_option_price_source": cast(GexOptionReferenceSource, source),
    }


def select_gex_option_reference(
    *,
    bid: object,
    ask: object,
    market_price: object,
    last: object,
    close: object,
) -> GexOptionReference:
    """Select one option reference once under the shared producer policy."""

    def positive(value: object) -> float | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        number = float(value)
        return number if math.isfinite(number) and number > 0 else None

    exact_bid = positive(bid)
    exact_ask = positive(ask)
    exact_market_price = positive(market_price)
    exact_last = positive(last)
    exact_close = positive(close)
    candidates: list[tuple[str, float | None]] = []
    if exact_bid is not None and exact_ask is not None and exact_ask >= exact_bid:
        candidates.append(("bid_ask_mid", (exact_bid + exact_ask) / 2.0))
    candidates.append(("last", exact_last))
    if exact_market_price is not None:
        candidates.append(
            (
                "previous_close"
                if exact_close is not None and exact_market_price == exact_close
                else "ibkr_market_price",
                exact_market_price,
            )
        )
    candidates.extend(
        (
            ("previous_close", exact_close),
            ("bid", exact_bid),
            ("ask", exact_ask),
        )
    )
    for source, price in candidates:
        if price is not None:
            return require_gex_option_reference(
                {
                    "reference_option_price": price,
                    "reference_option_price_source": source,
                }
            )
    return require_gex_option_reference(
        {
            "reference_option_price": None,
            "reference_option_price_source": "unavailable",
        }
    )


def require_gex_contract_market_facts(
    value: object,
    *,
    spot: object | None = None,
) -> GexContractMarketFacts:
    """Return the one nullable broker-market contract or fail closed."""

    if not isinstance(value, Mapping):
        raise ValueError("GEX option contract row must be a mapping")
    if set(value).intersection(_FORBIDDEN_GEX_CONTRACT_MARKET_FIELDS):
        raise ValueError("GEX option contract row contains forbidden duplicate market-state fields")
    required_fields = {"gamma", "open_interest", "volume", "gex", "abs_gex"}
    if not required_fields.issubset(value):
        raise ValueError("GEX option contract row requires canonical market facts")

    parsed: dict[str, float | None] = {}
    for field in required_fields:
        raw = value.get(field)
        if raw is None:
            parsed[field] = None
            continue
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError(f"GEX option contract {field} must be numeric or null")
        number = float(raw)
        if not math.isfinite(number):
            raise ValueError(f"GEX option contract {field} must be finite or null")
        parsed[field] = number

    gamma = parsed["gamma"]
    open_interest = parsed["open_interest"]
    volume = parsed["volume"]
    gex = parsed["gex"]
    abs_gex = parsed["abs_gex"]
    if gamma is not None and gamma < 0:
        raise ValueError("GEX option contract gamma must be non-negative or null")
    if open_interest is not None and open_interest < 0:
        raise ValueError("GEX option contract open_interest must be non-negative or null")
    if volume is not None and volume < 0:
        raise ValueError("GEX option contract volume must be non-negative or null")

    exposure_available = gamma is not None and open_interest is not None
    if exposure_available != (gex is not None and abs_gex is not None):
        raise ValueError("GEX option contract exposure requires both gamma and open_interest")
    if exposure_available:
        assert gamma is not None
        assert open_interest is not None
        assert gex is not None
        assert abs_gex is not None
        right = value.get("right")
        if right not in {"C", "P"}:
            raise ValueError("GEX option contract right must be exactly C or P")
        if abs_gex < 0 or not math.isclose(
            abs_gex,
            abs(gex),
            rel_tol=1e-12,
            abs_tol=1e-8,
        ):
            raise ValueError("GEX option contract abs_gex must equal abs(gex)")
        if (right == "C" and gex < 0) or (right == "P" and gex > 0):
            raise ValueError("GEX option contract exposure sign disagrees with right")
        if (gamma == 0 or open_interest == 0) and gex != 0:
            raise ValueError("Zero gamma or open_interest requires zero GEX")

        if spot is not None:
            multiplier = value.get("multiplier")
            if (
                isinstance(spot, bool)
                or not isinstance(spot, (int, float))
                or isinstance(multiplier, bool)
                or not isinstance(multiplier, (int, float))
            ):
                raise ValueError(
                    "GEX option contract exposure requires positive spot and multiplier"
                )
            exact_spot = float(spot)
            exact_multiplier = float(multiplier)
            if (
                not math.isfinite(exact_spot)
                or exact_spot <= 0
                or not math.isfinite(exact_multiplier)
                or exact_multiplier <= 0
            ):
                raise ValueError(
                    "GEX option contract exposure requires positive spot and multiplier"
                )
            sign = 1.0 if right == "C" else -1.0
            expected_gex = (
                sign * gamma * open_interest * exact_multiplier * exact_spot * exact_spot * 0.01
            )
            if not math.isclose(
                gex,
                expected_gex,
                rel_tol=1e-9,
                abs_tol=1e-6,
            ):
                raise ValueError("GEX option contract exposure disagrees with its facts")

    return {
        "gamma": gamma,
        "open_interest": open_interest,
        "volume": volume,
        "gex": gex,
        "abs_gex": abs_gex,
    }


def _require_nonnegative_finite_activity_number(
    value: object,
    *,
    field: str,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"GEX option activity {field} must be numeric")
    numeric = float(value)
    if not math.isfinite(numeric) or numeric < 0:
        raise ValueError(f"GEX option activity {field} must be finite and non-negative")
    return numeric


def _require_gex_option_activity_side(
    value: object,
    *,
    side: str,
) -> GexOptionActivitySide:
    if not isinstance(value, Mapping) or set(value) != _OPTION_ACTIVITY_SIDE_FIELDS:
        raise ValueError(f"GEX option activity {side} requires its exact typed fields")
    volume_delta = _require_nonnegative_finite_activity_number(
        value.get("volume_delta"),
        field=f"{side}.volume_delta",
    )
    rate_per_minute = _require_nonnegative_finite_activity_number(
        value.get("rate_per_minute"),
        field=f"{side}.rate_per_minute",
    )
    raw_rvol = value.get("rvol")
    rvol = (
        None
        if raw_rvol is None
        else _require_nonnegative_finite_activity_number(
            raw_rvol,
            field=f"{side}.rvol",
        )
    )
    baseline_samples = value.get("baseline_samples")
    if type(baseline_samples) is not int or baseline_samples < 0:
        raise ValueError(
            f"GEX option activity {side}.baseline_samples must be a non-negative integer"
        )
    return {
        "volume_delta": volume_delta,
        "rate_per_minute": rate_per_minute,
        "rvol": rvol,
        "baseline_samples": baseline_samples,
    }


def require_gex_option_activity(
    value: object,
    *,
    expected_status: object | None = None,
) -> GexOptionActivity:
    """Return the one exact persisted option-activity fact or fail closed."""

    if not isinstance(value, Mapping) or set(value) != _OPTION_ACTIVITY_FIELDS:
        raise ValueError("GEX option activity requires its exact typed fields")
    status = value.get("status")
    if status not in {"BASELINING", "READY"}:
        raise ValueError("GEX option activity requires an exact status")
    if expected_status is not None and status != expected_status:
        raise ValueError("GEX option activity status does not match its snapshot status")
    if value.get("scope") != "selected_chain":
        raise ValueError("GEX option activity requires scope=selected_chain")

    elapsed_seconds = _require_nonnegative_finite_activity_number(
        value.get("elapsed_seconds"),
        field="elapsed_seconds",
    )
    if elapsed_seconds <= 0:
        raise ValueError("GEX option activity elapsed_seconds must be positive")

    raw_universe = value.get("contract_universe")
    if (
        not isinstance(raw_universe, Mapping)
        or set(raw_universe) != _OPTION_ACTIVITY_UNIVERSE_FIELDS
    ):
        raise ValueError("GEX option activity contract_universe requires its exact typed fields")
    selected_strikes = raw_universe.get("selected_strikes")
    selected_contracts = raw_universe.get("selected_contracts")
    matched_contracts = raw_universe.get("matched_contracts")
    if type(selected_strikes) is not int or selected_strikes <= 0:
        raise ValueError("GEX option activity selected_strikes must be a positive integer")
    if (
        type(selected_contracts) is not int
        or selected_contracts < selected_strikes * 2
        or selected_contracts % 2 != 0
    ):
        raise ValueError(
            "GEX option activity selected_contracts must describe complete call/put pairs"
        )
    if type(matched_contracts) is not int or matched_contracts != selected_contracts:
        raise ValueError("GEX option activity requires the complete selected contract universe")
    contract_universe: GexOptionActivityContractUniverse = {
        "selected_strikes": selected_strikes,
        "selected_contracts": selected_contracts,
        "matched_contracts": matched_contracts,
    }

    call = _require_gex_option_activity_side(value.get("call"), side="call")
    put = _require_gex_option_activity_side(value.get("put"), side="put")
    total = _require_gex_option_activity_side(value.get("total"), side="total")
    for side_name, side in (("call", call), ("put", put), ("total", total)):
        expected_rate = round(
            side["volume_delta"] / (elapsed_seconds / 60.0),
            4,
        )
        if not math.isclose(
            side["rate_per_minute"],
            expected_rate,
            rel_tol=0.0,
            abs_tol=_OPTION_ACTIVITY_SUM_ABS_TOLERANCE,
        ):
            raise ValueError(f"GEX option activity {side_name}.rate_per_minute is inconsistent")
        baseline_ready = side["baseline_samples"] >= GEX_OPTION_ACTIVITY_MIN_BASELINE_SAMPLES
        if (side["rvol"] is not None) != baseline_ready:
            raise ValueError(f"GEX option activity {side_name}.rvol readiness is inconsistent")
    for field in ("volume_delta", "rate_per_minute"):
        if not math.isclose(
            total[field],
            call[field] + put[field],
            rel_tol=0.0,
            abs_tol=_OPTION_ACTIVITY_SUM_ABS_TOLERANCE,
        ):
            raise ValueError(f"GEX option activity total.{field} must equal call plus put")
    if (status == "READY") != (total["rvol"] is not None):
        raise ValueError("GEX option activity status must match total.rvol readiness")

    return {
        "status": cast(GexOptionActivityStatus, status),
        "scope": "selected_chain",
        "elapsed_seconds": elapsed_seconds,
        "contract_universe": contract_universe,
        "call": call,
        "put": put,
        "total": total,
    }


def require_exact_gex_capture_lane(
    *,
    source: object,
    capture_mode: object,
) -> tuple[str, GexCaptureMode]:
    """Require an exact persisted GEX source/capture lane without coercion."""

    exact_source, exact_capture_mode = gex_capture_lane(capture_mode)
    if not isinstance(source, str) or source != exact_source:
        raise ValueError("GEX snapshot source must exactly match capture mode")
    return exact_source, exact_capture_mode


def gex_capture_lane(capture_mode: object) -> tuple[str, GexCaptureMode]:
    """Return the one exact source/mode lane without aliases or normalization."""

    if not isinstance(capture_mode, str) or capture_mode not in _CAPTURE_LANES:
        raise ValueError("GEX snapshot requires an exact capture mode")
    exact_capture_mode = cast(GexCaptureMode, capture_mode)
    return _CAPTURE_LANES[exact_capture_mode], exact_capture_mode


def gex_capture_revision_at(
    captured_at: datetime,
    *,
    capture_mode: object,
) -> datetime:
    """Return the authoritative request capture or live persistence-bucket revision."""

    if (
        not isinstance(captured_at, datetime)
        or captured_at.tzinfo is None
        or captured_at.utcoffset() is None
    ):
        raise ValueError("GEX capture revision requires an aware timestamp")
    _, exact_capture_mode = gex_capture_lane(capture_mode)
    captured_utc = captured_at.astimezone(UTC)
    if exact_capture_mode == "request":
        return captured_utc
    interval_seconds = GEX_SCHEDULER_INTERVAL_MINUTES * 60
    revision_seconds = int(captured_utc.timestamp()) // interval_seconds * interval_seconds
    return datetime.fromtimestamp(revision_seconds, tz=UTC)


def require_gex_comparison_scope(
    value: object,
    *,
    capture_mode: object | None = None,
    allow_unknown_entitlement: bool = False,
) -> GexComparisonScope:
    """Return one exact, typed comparison scope or fail closed."""

    if not isinstance(value, Mapping) or set(value) != _COMPARISON_SCOPE_FIELDS:
        raise ValueError("GEX comparison scope requires its exact typed fields")

    scope_capture_mode = value.get("capture_mode")
    if not isinstance(scope_capture_mode, str) or scope_capture_mode not in _CAPTURE_LANES:
        raise ValueError("GEX comparison scope requires an exact capture mode")
    if capture_mode is not None:
        if not isinstance(capture_mode, str) or capture_mode not in _CAPTURE_LANES:
            raise ValueError("Expected GEX capture mode must be exact")
        if scope_capture_mode != capture_mode:
            raise ValueError("GEX comparison scope must match the snapshot capture mode")

    strike_count = value.get("strike_count")
    if type(strike_count) is not int or strike_count <= 0:
        raise ValueError("GEX comparison scope requires a positive integer strike count")

    raw_strike_ladder = value.get("strike_ladder")
    if not isinstance(raw_strike_ladder, list) or not raw_strike_ladder:
        raise ValueError("GEX comparison scope requires the exact selected strike ladder")
    strike_ladder: list[float] = []
    for raw_strike in raw_strike_ladder:
        if (
            isinstance(raw_strike, bool)
            or not isinstance(raw_strike, (int, float))
            or not math.isfinite(float(raw_strike))
            or float(raw_strike) <= 0
        ):
            raise ValueError("GEX comparison strike ladder must be finite and positive")
        strike_ladder.append(float(raw_strike))
    if strike_ladder != sorted(set(strike_ladder)):
        raise ValueError("GEX comparison strike ladder must be sorted and unique")
    if len(strike_ladder) != strike_count:
        raise ValueError("GEX comparison strike count must match the exact strike ladder")

    raw_contract_con_ids = value.get("contract_con_ids")
    if not isinstance(raw_contract_con_ids, list) or not raw_contract_con_ids:
        raise ValueError("GEX comparison scope requires exact provider contract conIds")
    contract_con_ids: list[int] = []
    for con_id in raw_contract_con_ids:
        if type(con_id) is not int or con_id <= 0:
            raise ValueError("GEX comparison contract conIds must be positive integers")
        contract_con_ids.append(con_id)
    if contract_con_ids != sorted(set(contract_con_ids)):
        raise ValueError("GEX comparison contract conIds must be sorted and unique")
    if len(contract_con_ids) < strike_count * 2 or len(contract_con_ids) % 2 != 0:
        raise ValueError("GEX comparison scope requires paired contracts for every strike")

    expiries = value.get("expiries")
    valid_expiries = isinstance(expiries, list) and bool(expiries)
    if valid_expiries:
        for expiry in expiries:
            if not isinstance(expiry, str) or len(expiry) != 8 or not expiry.isdigit():
                valid_expiries = False
                break
            try:
                datetime.strptime(expiry, "%Y%m%d")
            except ValueError:
                valid_expiries = False
                break
    if not valid_expiries:
        raise ValueError("GEX comparison scope requires exact YYYYMMDD expiries")
    assert isinstance(expiries, list)
    exact_expiries = cast(list[str], expiries)
    if len(set(exact_expiries)) != len(exact_expiries) or exact_expiries != sorted(exact_expiries):
        raise ValueError("GEX comparison scope expiries must be sorted and unique")

    futures_options = value.get("futures_options")
    if type(futures_options) is not bool:
        raise ValueError("GEX comparison scope requires typed futures_options")

    raw_series = value.get("series")
    if not isinstance(raw_series, list) or not raw_series:
        raise ValueError("GEX comparison scope requires exact option series")
    exact_series: list[GexOptionSeriesIdentity] = []
    series_keys: list[tuple[str, str, str, float]] = []
    for raw_identity in raw_series:
        if (
            not isinstance(raw_identity, Mapping)
            or set(raw_identity) != _OPTION_SERIES_IDENTITY_FIELDS
        ):
            raise ValueError("GEX comparison series requires its exact fields")
        identity = require_gex_option_series_identity(raw_identity)
        key = (
            identity["expiry"],
            identity["trading_class"],
            identity["exchange"],
            identity["multiplier"],
        )
        exact_series.append(identity)
        series_keys.append(key)
    if series_keys != sorted(series_keys) or len(set(series_keys)) != len(series_keys):
        raise ValueError("GEX comparison series must be sorted and unique")
    if sorted({identity["expiry"] for identity in exact_series}) != exact_expiries:
        raise ValueError("GEX comparison expiries must match exact option series")

    valuation_rates = require_gex_valuation_rates(value)
    entitlement = require_gex_market_data_entitlement(
        value.get("market_data_entitlement"),
        allow_unknown=allow_unknown_entitlement,
    )

    return {
        "capture_mode": cast(GexCaptureMode, scope_capture_mode),
        "strike_count": strike_count,
        "strike_ladder": strike_ladder,
        "contract_con_ids": contract_con_ids,
        "expiries": list(exact_expiries),
        "futures_options": futures_options,
        "series": exact_series,
        "risk_free_rate": valuation_rates["risk_free_rate"],
        "dividend_yield": valuation_rates["dividend_yield"],
        "market_data_entitlement": cast(
            GexMarketDataEntitlement,
            entitlement,
        ),
    }


def gex_comparison_lane_key(
    value: object,
    *,
    capture_mode: object | None = None,
) -> GexComparisonLaneKey:
    """Return the exact lane key used for GEX time-series comparisons."""

    scope = require_gex_comparison_scope(value, capture_mode=capture_mode)
    return (
        scope["capture_mode"],
        scope["strike_count"],
        tuple(scope["strike_ladder"]),
        tuple(scope["contract_con_ids"]),
        tuple(scope["expiries"]),
        scope["futures_options"],
        tuple(
            (
                identity["expiry"],
                identity["trading_class"],
                identity["exchange"],
                identity["multiplier"],
            )
            for identity in scope["series"]
        ),
        scope["risk_free_rate"],
        scope["dividend_yield"],
        scope["market_data_entitlement"],
    )


def gex_comparison_scope_from_contract_universe(
    contract_rows: Sequence[Mapping[str, object]],
    *,
    source: object,
    capture_mode: object,
    futures_options: object,
    risk_free_rate: object,
    dividend_yield: object,
    market_data_entitlement: object,
    allow_unknown_entitlement: bool = False,
) -> GexComparisonScope:
    """Build one exact scope from the provider-qualified selected universe."""

    _source, exact_capture_mode = require_exact_gex_capture_lane(
        source=source,
        capture_mode=capture_mode,
    )
    if not isinstance(contract_rows, Sequence) or isinstance(
        contract_rows,
        (str, bytes, bytearray),
    ):
        raise ValueError("GEX comparison contract universe must be a sequence")
    if not contract_rows:
        raise ValueError("GEX comparison contract universe cannot be empty")

    exact_series_by_key: dict[
        tuple[str, str, str, float],
        GexOptionSeriesIdentity,
    ] = {}
    rights_by_series_strike: dict[
        tuple[str, str, str, float, float],
        set[str],
    ] = {}
    strike_ladder: set[float] = set()
    contract_con_ids: set[int] = set()
    for row in contract_rows:
        if not isinstance(row, Mapping):
            raise ValueError("GEX comparison scope requires exact contract rows")
        identity = require_gex_option_series_identity(row)
        series_key = (
            identity["expiry"],
            identity["trading_class"],
            identity["exchange"],
            identity["multiplier"],
        )
        exact_series_by_key[series_key] = identity
        raw_strike = row.get("strike")
        if (
            isinstance(raw_strike, bool)
            or not isinstance(raw_strike, (int, float))
            or not math.isfinite(float(raw_strike))
            or float(raw_strike) <= 0
        ):
            raise ValueError("GEX comparison contract requires an exact positive strike")
        strike = float(raw_strike)
        strike_ladder.add(strike)
        con_id = row.get("con_id")
        if type(con_id) is not int or con_id <= 0 or con_id in contract_con_ids:
            raise ValueError("GEX comparison contracts require unique positive conIds")
        contract_con_ids.add(con_id)
        right = row.get("right")
        if right not in {"C", "P"}:
            raise ValueError("GEX comparison contracts require exact C/P rights")
        pair_key = (*series_key, strike)
        pair_rights = rights_by_series_strike.setdefault(pair_key, set())
        if right in pair_rights:
            raise ValueError("GEX comparison contract universe contains a duplicate option right")
        pair_rights.add(right)
    if any(rights != {"C", "P"} for rights in rights_by_series_strike.values()):
        raise ValueError("GEX comparison contract universe requires complete exact C/P pairs")

    series = [exact_series_by_key[key] for key in sorted(exact_series_by_key)]
    valuation_rates = require_gex_valuation_rates(
        {
            "risk_free_rate": risk_free_rate,
            "dividend_yield": dividend_yield,
        }
    )
    entitlement = require_gex_market_data_entitlement(
        market_data_entitlement,
        allow_unknown=allow_unknown_entitlement,
    )
    return require_gex_comparison_scope(
        {
            "capture_mode": exact_capture_mode,
            "strike_count": len(strike_ladder),
            "strike_ladder": sorted(strike_ladder),
            "contract_con_ids": sorted(contract_con_ids),
            "expiries": sorted({identity["expiry"] for identity in series}),
            "futures_options": futures_options,
            "series": series,
            "risk_free_rate": valuation_rates["risk_free_rate"],
            "dividend_yield": valuation_rates["dividend_yield"],
            "market_data_entitlement": entitlement,
        },
        capture_mode=exact_capture_mode,
        allow_unknown_entitlement=allow_unknown_entitlement,
    )


def gex_comparison_scope_from_payload(
    payload: object,
    *,
    allow_unknown_entitlement: bool = False,
) -> GexComparisonScope:
    """Validate the canonical comparison scope carried by one producer payload."""

    if not isinstance(payload, Mapping):
        raise ValueError("GEX comparison scope requires a payload object")
    _, capture_mode = require_exact_gex_capture_lane(
        source=payload.get("source"),
        capture_mode=payload.get("capture_mode"),
    )
    raw = payload.get("raw")
    if not isinstance(raw, Mapping):
        raise ValueError("GEX comparison scope requires canonical raw facts")
    raw_meta = raw.get("meta")
    raw_contracts = raw.get("contracts")
    if not isinstance(raw_meta, Mapping) or not isinstance(raw_contracts, list):
        raise ValueError("GEX comparison scope requires raw meta and contracts")
    raw_strike_count = raw_meta.get("strike_count")
    if type(raw_strike_count) is not int or raw_strike_count <= 0:
        raise ValueError("GEX comparison scope requires an exact positive strike count")
    derived = gex_comparison_scope_from_contract_universe(
        raw_contracts,
        source=payload.get("source"),
        capture_mode=capture_mode,
        futures_options=raw_meta.get("futures_options"),
        risk_free_rate=raw_meta.get("risk_free_rate"),
        dividend_yield=raw_meta.get("dividend_yield"),
        market_data_entitlement=raw_meta.get("market_data_entitlement"),
        allow_unknown_entitlement=allow_unknown_entitlement,
    )
    persisted = require_gex_comparison_scope(
        payload.get("comparison_scope"),
        capture_mode=capture_mode,
        allow_unknown_entitlement=allow_unknown_entitlement,
    )
    exact_fields = (
        "capture_mode",
        "futures_options",
        "risk_free_rate",
        "dividend_yield",
        "market_data_entitlement",
    )
    if any(persisted[field] != derived[field] for field in exact_fields):
        raise ValueError("Persisted GEX comparison scope is inconsistent")
    if raw_strike_count != persisted["strike_count"]:
        raise ValueError("Persisted GEX selected strike count disagrees with comparison scope")
    if (
        not set(derived["strike_ladder"]).issubset(persisted["strike_ladder"])
        or not set(derived["contract_con_ids"]).issubset(persisted["contract_con_ids"])
        or not set(derived["expiries"]).issubset(persisted["expiries"])
        or not {
            (
                identity["expiry"],
                identity["trading_class"],
                identity["exchange"],
                identity["multiplier"],
            )
            for identity in derived["series"]
        }.issubset(
            {
                (
                    identity["expiry"],
                    identity["trading_class"],
                    identity["exchange"],
                    identity["multiplier"],
                )
                for identity in persisted["series"]
            }
        )
    ):
        raise ValueError("Persisted GEX comparison scope does not contain the analysis subset")
    return persisted
