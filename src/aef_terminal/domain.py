from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from math import isfinite
from types import MappingProxyType
from typing import Any


DOMAIN_FACT_FORBIDDEN_DISPLAY_KEYS = frozenset(
    {
        "reason",
        "message",
        "label",
        "trigger_label",
        "tooltip",
        "title",
        "do_now",
        "compact_label",
        "color",
        "bg",
        "border",
        "glyph",
    }
)
_DOMAIN_FACT_CODE_CHARACTERS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_.:-")
_DOMAIN_FACT_CODE_START_CHARACTERS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789")


def validate_domain_code(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a canonical string code")
    if not value or value != value.strip() or len(value) > 160:
        raise ValueError(
            f"{field_name} must be a non-empty canonical code of at most 160 characters"
        )
    if (
        value != value.lower()
        or value[0] not in _DOMAIN_FACT_CODE_START_CHARACTERS
        or any(character not in _DOMAIN_FACT_CODE_CHARACTERS for character in value)
    ):
        raise ValueError(f"{field_name} must use lowercase canonical code characters")
    return value


def domain_wire_value(value: Any, *, field_name: str) -> Any:
    return _domain_value(value, field_name=field_name, frozen=False)


def validate_domain_value(value: Any, *, field_name: str) -> None:
    """Use the same domain admission without allocating a discarded projection."""
    _domain_value(value, field_name=field_name, frozen=None)


def _domain_value(value: Any, *, field_name: str, frozen: bool | None) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise ValueError(f"{field_name} must not contain non-finite numbers")
        return value
    if isinstance(value, DomainFact):
        if frozen is None:
            return None
        if frozen:
            # DomainFact already owns recursively validated, immutable values.
            return MappingProxyType({"code": value.code, **value.attributes})
        return value.as_dict()
    if isinstance(value, Mapping):
        out: dict[str, Any] | None = {} if frozen is not None else None
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise TypeError(f"{field_name} mapping keys must be non-empty strings")
            projected = _domain_value(item, field_name=f"{field_name}.{key}", frozen=frozen)
            if out is not None:
                out[key] = projected
        if out is None:
            return None
        return MappingProxyType(out) if frozen else out
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        if frozen is None:
            for index, item in enumerate(value):
                _domain_value(item, field_name=f"{field_name}[{index}]", frozen=None)
            return None
        items = [
            _domain_value(item, field_name=f"{field_name}[{index}]", frozen=frozen)
            for index, item in enumerate(value)
        ]
        return tuple(items) if frozen else items
    raise TypeError(f"{field_name} must contain JSON-safe typed values")


def domain_frozen_value(value: Any, *, field_name: str) -> Any:
    """Validate and recursively freeze one typed domain value.

    ``domain_wire_value`` remains the canonical transport copier.  Domain
    dataclasses use this immutable counterpart so a caller cannot mutate a
    decision after it has entered ranking or serialization.
    """

    return _domain_value(value, field_name=field_name, frozen=True)


def _finite_domain_number(value: object, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field_name} must be a finite number")
    normalized = float(value)
    if not isfinite(normalized):
        raise ValueError(f"{field_name} must be a finite number")
    return normalized


def _finite_optional_domain_number(value: object, *, field_name: str) -> float | None:
    if value is None:
        return None
    return _finite_domain_number(value, field_name=field_name)


def _require_fact_attributes(attributes: Mapping[str, Any]) -> None:
    if any(not isinstance(key, str) or not key for key in attributes):
        raise TypeError("fact attribute keys must be non-empty strings")
    invalid_keys = sorted(DOMAIN_FACT_FORBIDDEN_DISPLAY_KEYS.intersection(attributes))
    if invalid_keys:
        raise ValueError(
            "domain facts must not contain legacy or display fields: " + ", ".join(invalid_keys)
        )
    if "code" in attributes:
        raise ValueError("fact attributes must not redefine code")


def _fact_mapping_fields(value: Mapping[str, Any]) -> tuple[Any, dict[str, Any]]:
    if not isinstance(value, Mapping):
        raise TypeError("domain fact must be a mapping")
    if "code" not in value:
        raise ValueError("domain fact must contain a canonical code")
    return value["code"], {key: item for key, item in value.items() if key != "code"}


@dataclass(frozen=True, slots=True)
class DomainFact:
    """One canonical domain fact crossing producer, engine, and renderer boundaries."""

    code: str
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        code = validate_domain_code(self.code, field_name="fact.code")
        if not isinstance(self.attributes, Mapping):
            raise TypeError("fact attributes must be a mapping")
        attributes = dict(self.attributes)
        _require_fact_attributes(attributes)
        typed_attributes = {
            key: domain_frozen_value(value, field_name=f"fact.{key}")
            for key, value in attributes.items()
        }
        object.__setattr__(self, "code", code)
        object.__setattr__(
            self,
            "attributes",
            MappingProxyType(typed_attributes),
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> DomainFact:
        code, attributes = _fact_mapping_fields(value)
        return cls(code=code, attributes=attributes)

    @staticmethod
    def validate_mapping(value: Mapping[str, Any]) -> None:
        """Validate a fact without constructing or retaining an immutable copy."""
        code, attributes = _fact_mapping_fields(value)
        validate_domain_code(code, field_name="fact.code")
        _require_fact_attributes(attributes)
        for key, item in attributes.items():
            validate_domain_value(item, field_name=f"fact.{key}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            **{
                key: domain_wire_value(value, field_name=f"fact.{key}")
                for key, value in self.attributes.items()
            },
        }


class Direction(str, Enum):
    LONG = "long"
    SHORT = "short"
    FLAT = "flat"


class ActionPhase(str, Enum):
    WAIT = "WAIT"
    WATCH = "WATCH"
    CANDIDATE = "CANDIDATE"
    ARM = "ARM"
    GO = "GO"
    IN = "IN"
    TRAIL = "TRAIL"
    TARGET_HIT = "TARGET_HIT"
    STOP_HIT = "STOP_HIT"
    BLOCK = "BLOCK"


CANDIDATE_ENTRY_PHASES = frozenset(
    {
        ActionPhase.CANDIDATE,
        ActionPhase.WATCH,
        ActionPhase.ARM,
        ActionPhase.GO,
    }
)


def normalize_phase(
    value: object,
    *,
    blocked: bool = False,
    filled: bool = False,
) -> ActionPhase:
    if blocked:
        return ActionPhase.BLOCK
    if isinstance(value, ActionPhase):
        return value
    if isinstance(value, str):
        try:
            return ActionPhase(value)
        except ValueError:
            pass
    if filled:
        return ActionPhase.IN
    return ActionPhase.WAIT


class ScenarioKind(str, Enum):
    FADE = "fade"  # ОТБОЙ / Затухание / Разворот (Mean Reversion)
    TRANSIT = "transit"  # ПРОБОЙ / Продолжение / Импульс (Breakout/Trend)
    WAIT = "wait"


class CandidateFinality(str, Enum):
    """Decision authority carried by one SignalCandidate.

    Only CONFIRMED candidates may enter scenario selection. Provisional,
    retained, and expired values remain typed display/audit context.
    """

    CONFIRMED = "confirmed"
    PROVISIONAL = "provisional"
    RETAINED = "retained"
    EXPIRED = "expired"


class StrategyMode(str, Enum):
    BALANCED = "balanced"
    BREAKOUT = "breakout"
    MEAN_REVERSION = "mean_reversion"


class BarState(str, Enum):
    FORMING = "forming"
    AWAITING_PROVIDER_CONFIRMATION = "awaiting_provider_confirmation"
    CONFIRMED = "confirmed"


class BarProviderRequest(str, Enum):
    HISTORICAL = "historical"
    KEEP_UP_TO_DATE = "keep_up_to_date"
    DETERMINISTIC_AGGREGATION = "deterministic_aggregation"
    CANONICAL_STORAGE = "canonical_storage"


@dataclass(frozen=True, slots=True)
class BarProvenance:
    """Exact provider route that produced or durably owns one Bar."""

    provider: str
    instrument_id: str
    route_fingerprint: str
    request_type: BarProviderRequest | str
    provider_contract_id: str
    provider_contract_type: str
    data_type: str
    source_timeframe: str | None = None

    def __post_init__(self) -> None:
        try:
            request_type = (
                self.request_type
                if isinstance(self.request_type, BarProviderRequest)
                else BarProviderRequest(self.request_type)
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("BAR_PROVENANCE_REQUEST_TYPE_INVALID") from exc
        if not isinstance(self.provider, str) or not self.provider:
            raise ValueError("BAR_PROVENANCE_PROVIDER_INVALID")
        for field_name, value in (
            ("instrument_id", self.instrument_id),
            ("route_fingerprint", self.route_fingerprint),
        ):
            if not isinstance(value, str) or not value:
                raise ValueError(f"BAR_PROVENANCE_{field_name.upper()}_INVALID")
        if (
            not isinstance(self.data_type, str)
            or not self.data_type
            or self.data_type != self.data_type.strip()
        ):
            raise ValueError("BAR_PROVENANCE_DATA_TYPE_INVALID")
        if not isinstance(self.provider_contract_id, str) or (
            not self.provider_contract_id
            and request_type is not BarProviderRequest.CANONICAL_STORAGE
        ):
            raise ValueError("BAR_PROVENANCE_PROVIDER_CONTRACT_ID_INVALID")
        if (
            not isinstance(self.provider_contract_type, str)
            or not self.provider_contract_type
            or self.provider_contract_type != self.provider_contract_type.strip()
        ):
            raise ValueError("BAR_PROVENANCE_PROVIDER_CONTRACT_TYPE_INVALID")
        if (
            self.provider != self.provider.lower()
            or ":" in self.provider
            or not self.provider.isascii()
            or not self.provider[0].isalnum()
            or any(
                character not in "abcdefghijklmnopqrstuvwxyz0123456789_-"
                for character in self.provider
            )
        ):
            raise ValueError("BAR_PROVENANCE_PROVIDER_INVALID")
        if request_type is BarProviderRequest.DETERMINISTIC_AGGREGATION:
            if (
                not isinstance(self.source_timeframe, str)
                or not self.source_timeframe
                or self.source_timeframe != self.source_timeframe.strip()
                or not self.source_timeframe.endswith("m")
                or not self.source_timeframe[:-1].isdigit()
                or int(self.source_timeframe[:-1]) <= 0
                or self.source_timeframe != f"{int(self.source_timeframe[:-1])}m"
            ):
                raise ValueError("BAR_PROVENANCE_AGGREGATION_SOURCE_TIMEFRAME_INVALID")
        elif self.source_timeframe is not None:
            raise ValueError("BAR_PROVENANCE_SOURCE_TIMEFRAME_REQUIRES_AGGREGATION")
        object.__setattr__(self, "request_type", request_type)


class OptionRight(str, Enum):
    CALL = "C"
    PUT = "P"


@dataclass(frozen=True)
class Bar:
    symbol: str
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    timeframe: str = "1m"
    source: str = "unknown"
    closed: bool = True
    state: BarState | str | None = None
    provenance: BarProvenance | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or not self.symbol.strip():
            raise ValueError("BAR_SYMBOL_INVALID")
        if not isinstance(self.timeframe, str) or not self.timeframe.strip():
            raise ValueError("BAR_TIMEFRAME_INVALID")
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("BAR_SOURCE_INVALID")
        if not isinstance(self.closed, bool):
            raise TypeError("BAR_CLOSED_NOT_BOOL")
        if self.provenance is not None and not isinstance(
            self.provenance,
            BarProvenance,
        ):
            raise TypeError("BAR_PROVENANCE_INVALID")
        if (
            not isinstance(self.ts, datetime)
            or self.ts.tzinfo is None
            or self.ts.utcoffset() is None
        ):
            raise ValueError("BAR_TIMESTAMP_NOT_AWARE")
        timestamp = self.ts.astimezone(UTC)
        normalized_values: dict[str, float] = {}
        for field_name, value in (
            ("open", self.open),
            ("high", self.high),
            ("low", self.low),
            ("close", self.close),
            ("volume", self.volume),
        ):
            try:
                normalized_values[field_name] = _finite_domain_number(
                    value,
                    field_name=f"Bar.{field_name}",
                )
            except (TypeError, ValueError) as exc:
                raise ValueError(f"BAR_{field_name.upper()}_NOT_FINITE") from exc
        open_value = normalized_values["open"]
        high_value = normalized_values["high"]
        low_value = normalized_values["low"]
        close_value = normalized_values["close"]
        volume_value = normalized_values["volume"]
        if volume_value < 0:
            raise ValueError("BAR_VOLUME_NEGATIVE")
        if (
            low_value > high_value
            or high_value < max(open_value, close_value)
            or low_value > min(open_value, close_value)
        ):
            raise ValueError("BAR_OHLC_GEOMETRY_INVALID")
        raw_state = self.state
        if raw_state is None:
            normalized = BarState.CONFIRMED if self.closed else BarState.FORMING
        else:
            try:
                normalized = (
                    raw_state if isinstance(raw_state, BarState) else BarState(str(raw_state))
                )
            except ValueError as exc:
                raise ValueError("BAR_STATE_INVALID") from exc
            if self.closed != (normalized is BarState.CONFIRMED):
                raise ValueError(
                    f"BAR_LIFECYCLE_CONTRADICTION closed={self.closed} state={normalized.value}"
                )
        object.__setattr__(self, "ts", timestamp)
        object.__setattr__(self, "open", open_value)
        object.__setattr__(self, "high", high_value)
        object.__setattr__(self, "low", low_value)
        object.__setattr__(self, "close", close_value)
        object.__setattr__(self, "volume", volume_value)
        object.__setattr__(self, "state", normalized)


def bar_revision_signature(
    bar: Bar | Mapping[str, Any],
) -> tuple[Any, float, float, float, float, float, str, str]:
    if isinstance(bar, Mapping):
        raw_ts = bar.get("ts")
        if isinstance(raw_ts, datetime):
            parsed_ts = raw_ts
        elif isinstance(raw_ts, str) and raw_ts:
            try:
                parsed_ts = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("BAR_SIGNATURE_TIMESTAMP_INVALID") from exc
        else:
            raise ValueError("BAR_SIGNATURE_TIMESTAMP_INVALID")
        if parsed_ts.tzinfo is None or parsed_ts.utcoffset() is None:
            raise ValueError("BAR_SIGNATURE_TIMESTAMP_NOT_AWARE")
        values: dict[str, float] = {}
        for field_name in ("open", "high", "low", "close", "volume"):
            if field_name not in bar or bar.get(field_name) is None:
                raise ValueError(f"BAR_SIGNATURE_{field_name.upper()}_MISSING")
            try:
                values[field_name] = _finite_domain_number(
                    bar[field_name],
                    field_name=f"bar_signature.{field_name}",
                )
            except (TypeError, ValueError) as exc:
                raise ValueError(f"BAR_SIGNATURE_{field_name.upper()}_NOT_FINITE") from exc
        if values["volume"] < 0:
            raise ValueError("BAR_SIGNATURE_VOLUME_NEGATIVE")
        if (
            values["low"] > values["high"]
            or values["high"] < max(values["open"], values["close"])
            or values["low"] > min(values["open"], values["close"])
        ):
            raise ValueError("BAR_SIGNATURE_OHLC_GEOMETRY_INVALID")
        raw_closed = bar.get("closed")
        if not isinstance(raw_closed, bool):
            raise TypeError("BAR_SIGNATURE_CLOSED_NOT_BOOL")
        raw_state = bar.get("state")
        if raw_state is None:
            state = BarState.CONFIRMED if raw_closed else BarState.FORMING
        else:
            try:
                state = raw_state if isinstance(raw_state, BarState) else BarState(str(raw_state))
            except ValueError as exc:
                raise ValueError("BAR_SIGNATURE_STATE_INVALID") from exc
        if raw_closed != (state is BarState.CONFIRMED):
            raise ValueError("BAR_SIGNATURE_LIFECYCLE_CONTRADICTION")
        source = bar.get("source")
        if not isinstance(source, str) or not source.strip():
            raise ValueError("BAR_SIGNATURE_SOURCE_INVALID")
        return (
            parsed_ts.astimezone(UTC),
            values["open"],
            values["high"],
            values["low"],
            values["close"],
            values["volume"],
            state.value,
            source,
        )
    return (
        bar.ts,
        float(bar.open),
        float(bar.high),
        float(bar.low),
        float(bar.close),
        float(bar.volume),
        BarState(bar.state).value,
        bar.source,
    )


def bar_ingress_revision_signature(bar: Bar) -> tuple[Any, ...]:
    """Identify one typed producer revision before canonical storage strips provenance."""

    if not isinstance(bar, Bar):
        raise TypeError("BAR_INGRESS_SIGNATURE_REQUIRES_TYPED_BAR")
    provenance = bar.provenance
    provenance_signature = (
        None
        if provenance is None
        else (
            provenance.provider,
            provenance.instrument_id,
            provenance.route_fingerprint,
            provenance.request_type.value,
            provenance.provider_contract_id,
            provenance.provider_contract_type,
            provenance.data_type,
            provenance.source_timeframe,
        )
    )
    return (*bar_revision_signature(bar), provenance_signature)


@dataclass(frozen=True)
class Tick:
    route_fingerprint: str
    ts: datetime
    price: float
    volume: int
    delta_sign: int
    instrument_id: str
    tick_type: str = "last"
    bid: float | None = None
    ask: float | None = None
    exchange: str = ""
    conditions: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not isinstance(self.instrument_id, str) or not self.instrument_id:
            raise ValueError("Tick.instrument_id must be an exact non-empty string")
        if not isinstance(self.route_fingerprint, str) or not self.route_fingerprint:
            raise ValueError("Tick.route_fingerprint must be an exact non-empty string")
        if (
            not isinstance(self.ts, datetime)
            or self.ts.tzinfo is None
            or self.ts.utcoffset() is None
        ):
            raise ValueError("TICK_TIMESTAMP_NOT_AWARE")
        try:
            price = _finite_domain_number(self.price, field_name="Tick.price")
        except (TypeError, ValueError) as exc:
            raise ValueError("TICK_PRICE_NOT_FINITE") from exc
        if type(self.volume) is not int:
            raise TypeError("TICK_VOLUME_NOT_EXACT_INT")
        if self.volume < 0:
            raise ValueError("TICK_VOLUME_NEGATIVE")
        if type(self.delta_sign) is not int or self.delta_sign not in {-1, 0, 1}:
            raise ValueError("TICK_DELTA_SIGN_INVALID")
        if not isinstance(self.tick_type, str) or not self.tick_type.strip():
            raise ValueError("TICK_TYPE_INVALID")
        normalized_quotes: dict[str, float | None] = {}
        for field_name, value in (("bid", self.bid), ("ask", self.ask)):
            if value is None:
                normalized_quotes[field_name] = None
                continue
            try:
                normalized_quotes[field_name] = _finite_domain_number(
                    value,
                    field_name=f"Tick.{field_name}",
                )
            except (TypeError, ValueError) as exc:
                raise ValueError(f"TICK_{field_name.upper()}_NOT_FINITE") from exc
        if not isinstance(self.exchange, str):
            raise TypeError("TICK_EXCHANGE_NOT_STRING")
        if not isinstance(self.conditions, Sequence) or isinstance(
            self.conditions,
            (str, bytes, bytearray),
        ):
            raise TypeError("TICK_CONDITIONS_NOT_SEQUENCE")
        conditions = tuple(self.conditions)
        if any(not isinstance(condition, str) or not condition.strip() for condition in conditions):
            raise ValueError("TICK_CONDITION_INVALID")
        object.__setattr__(self, "ts", self.ts.astimezone(UTC))
        object.__setattr__(self, "price", price)
        object.__setattr__(self, "bid", normalized_quotes["bid"])
        object.__setattr__(self, "ask", normalized_quotes["ask"])
        object.__setattr__(self, "conditions", conditions)


@dataclass(frozen=True)
class SignalCandidate:
    """One rankable signal; producer_phase is its maximum decision authority.

    ``None`` is reserved for raw, score-owned candidates that have no upstream
    action state. A producer-backed candidate must carry its canonical phase.
    """

    name: str
    direction: Direction
    score: float
    level: float | None
    reason: str
    trigger_event: DomainFact
    details: Mapping[str, Any] = field(default_factory=dict)
    kind: ScenarioKind = ScenarioKind.WAIT
    source: str = ""
    role: str = ""
    reason_code: str = ""
    producer_phase: ActionPhase | None = None
    finality: CandidateFinality = field(kw_only=True)

    def __post_init__(self) -> None:
        if not isinstance(self.direction, Direction):
            raise TypeError("SignalCandidate.direction must be a Direction")
        if not isinstance(self.kind, ScenarioKind):
            raise TypeError("SignalCandidate.kind must be a ScenarioKind")
        if self.producer_phase is not None and not isinstance(
            self.producer_phase,
            ActionPhase,
        ):
            raise TypeError("SignalCandidate.producer_phase must be an ActionPhase or None")
        if self.producer_phase is not None and self.producer_phase not in CANDIDATE_ENTRY_PHASES:
            raise ValueError("SignalCandidate.producer_phase must be a candidate entry phase")
        if not isinstance(self.finality, CandidateFinality):
            raise TypeError("SignalCandidate.finality must be a CandidateFinality")
        score = _finite_domain_number(self.score, field_name="SignalCandidate.score")
        level = _finite_optional_domain_number(self.level, field_name="SignalCandidate.level")
        if not isinstance(self.trigger_event, DomainFact):
            raise TypeError("SignalCandidate.trigger_event must be a DomainFact")
        if not isinstance(self.details, Mapping):
            raise TypeError("SignalCandidate.details must be a mapping")
        reason_code = self.reason_code or self.trigger_event.code
        frozen_details = domain_frozen_value(self.details, field_name="SignalCandidate.details")
        if not isinstance(frozen_details, Mapping):
            raise TypeError("SignalCandidate.details must be a mapping")
        object.__setattr__(self, "score", score)
        object.__setattr__(self, "level", level)
        object.__setattr__(self, "details", frozen_details)
        object.__setattr__(
            self,
            "reason_code",
            validate_domain_code(reason_code, field_name="SignalCandidate.reason_code"),
        )

    @property
    def decision_authoritative(self) -> bool:
        return self.finality is CandidateFinality.CONFIRMED


@dataclass(frozen=True)
class ScenarioDecision:
    kind: ScenarioKind
    direction: Direction
    confidence: float
    action: ActionPhase
    trigger: float | None
    stop: float | None
    target: float | None
    invalidation: float | None
    trigger_event: DomainFact
    reasons: tuple[str, ...] = field(default_factory=tuple)
    source: str = ""
    reason_codes: tuple[str, ...] = field(default_factory=tuple)
    metrics: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ScenarioKind):
            raise TypeError("ScenarioDecision.kind must be a ScenarioKind")
        if not isinstance(self.direction, Direction):
            raise TypeError("ScenarioDecision.direction must be a Direction")
        if not isinstance(self.action, ActionPhase):
            raise TypeError("ScenarioDecision.action must be an ActionPhase")
        confidence = _finite_domain_number(
            self.confidence, field_name="ScenarioDecision.confidence"
        )
        trigger = _finite_optional_domain_number(
            self.trigger, field_name="ScenarioDecision.trigger"
        )
        stop = _finite_optional_domain_number(self.stop, field_name="ScenarioDecision.stop")
        target = _finite_optional_domain_number(self.target, field_name="ScenarioDecision.target")
        invalidation = _finite_optional_domain_number(
            self.invalidation,
            field_name="ScenarioDecision.invalidation",
        )
        if not isinstance(self.trigger_event, DomainFact):
            raise TypeError("ScenarioDecision.trigger_event must be a DomainFact")
        if not isinstance(self.reasons, Sequence) or isinstance(
            self.reasons, (str, bytes, bytearray)
        ):
            raise TypeError("ScenarioDecision.reasons must be a sequence of strings")
        reasons = tuple(self.reasons)
        if any(not isinstance(reason, str) for reason in reasons):
            raise TypeError("ScenarioDecision.reasons must be a sequence of strings")
        if not isinstance(self.reason_codes, Sequence) or isinstance(
            self.reason_codes, (str, bytes, bytearray)
        ):
            raise TypeError("ScenarioDecision.reason_codes must be a sequence of canonical codes")
        reason_codes = tuple(
            validate_domain_code(code, field_name="ScenarioDecision.reason_codes")
            for code in self.reason_codes
        )
        if not isinstance(self.metrics, Mapping):
            raise TypeError("ScenarioDecision.metrics must be a mapping")
        frozen_metrics = domain_frozen_value(self.metrics, field_name="ScenarioDecision.metrics")
        if not isinstance(frozen_metrics, Mapping):
            raise TypeError("ScenarioDecision.metrics must be a mapping")
        object.__setattr__(self, "confidence", confidence)
        object.__setattr__(self, "trigger", trigger)
        object.__setattr__(self, "stop", stop)
        object.__setattr__(self, "target", target)
        object.__setattr__(self, "invalidation", invalidation)
        object.__setattr__(self, "reasons", reasons)
        object.__setattr__(self, "reason_codes", reason_codes)
        object.__setattr__(self, "metrics", frozen_metrics)
