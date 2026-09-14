from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum, IntEnum
from math import isfinite
from typing import Any, Protocol, runtime_checkable

from aef_terminal.domain import (
    Bar,
    BarProviderRequest,
    BarState,
    bar_revision_signature,
)
from aef_terminal.runtime.timeframes import (
    HistoryRangeWindow,
    PERSISTED_BAR_INTERVALS,
    interval_seconds,
    require_aware_utc_datetime,
)


class OptionUniverseUnavailableError(RuntimeError):
    """An exact provider route currently has no usable option universe."""

    def __init__(
        self,
        message: str,
        *,
        reason: str = "OPTION_UNIVERSE_UNAVAILABLE",
        diagnostics: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        reason_value = str(reason or "").strip()
        if not reason_value:
            raise ValueError("OPTION_UNIVERSE_REASON_REQUIRED")
        self.reason = reason_value
        self.diagnostics = dict(diagnostics or {})


OPTION_EXPIRY_MODES = frozenset({"hybrid", "0dte", "1dte"})


def require_option_expiry_mode(value: object) -> str:
    if not isinstance(value, str) or value not in OPTION_EXPIRY_MODES:
        raise ValueError("OPTION_EXPIRY_MODE_INVALID")
    return value


class ProviderSessionScope(str, Enum):
    UNKNOWN = "unknown"
    TRADING = "trading"
    CONTINUOUS = "continuous"
    INSTRUMENT = "instrument"


class ProviderScheduleState(str, Enum):
    UNKNOWN = "unknown"
    CONTINUOUS = "continuous"
    VERIFIED = "verified"

    @property
    def authoritative(self) -> bool:
        return self in {
            ProviderScheduleState.CONTINUOUS,
            ProviderScheduleState.VERIFIED,
        }


@dataclass(frozen=True)
class CanonicalBarCommitReceipt:
    """Atomic canonical-storage outcome for one logically ordered bar batch."""

    revision_sequence: int
    written: int = 0
    acknowledged: tuple[Bar, ...] = ()
    superseded: tuple[Bar, ...] = ()
    rejected: tuple[Bar, ...] = ()
    deleted: int = 0

    def __post_init__(self) -> None:
        if int(self.revision_sequence) <= 0:
            raise ValueError("revision_sequence must be positive")
        if int(self.written) < 0:
            raise ValueError("written must not be negative")
        if isinstance(self.deleted, bool) or not isinstance(self.deleted, int) or self.deleted < 0:
            raise ValueError("deleted must be a non-negative integer")


def canonical_bar_commit_receipt(
    submitted: Sequence[Bar],
    persisted: Sequence[tuple[Bar, int]],
    *,
    revision_sequence: int,
    written: int,
    rejected: Sequence[Bar] = (),
    deleted: int = 0,
    timeframe_normalizer: Callable[[str], str] | None = None,
) -> CanonicalBarCommitReceipt:
    """Classify an atomic readback without inferring revision order from OHLCV."""

    sequence = int(revision_sequence)
    normalize_timeframe = timeframe_normalizer or (lambda value: value)
    persisted_by_slot = {
        (normalize_timeframe(bar.timeframe), bar.ts.astimezone(UTC)): (
            bar,
            int(stored_sequence),
        )
        for bar, stored_sequence in persisted
    }
    acknowledged: list[Bar] = []
    superseded: list[Bar] = []
    for bar in submitted:
        slot = (normalize_timeframe(bar.timeframe), bar.ts.astimezone(UTC))
        current = persisted_by_slot.get(slot)
        if current is None:
            raise RuntimeError(
                "CANONICAL_BAR_COMMIT_NOT_DURABLE "
                f"timeframe={bar.timeframe} ts={bar.ts.astimezone(UTC).isoformat()}"
            )
        persisted_bar, persisted_sequence = current
        if bar_revision_signature(persisted_bar) == bar_revision_signature(bar):
            acknowledged.append(bar)
            continue
        if persisted_sequence > sequence:
            superseded.append(bar)
            continue
        raise RuntimeError(
            "CANONICAL_BAR_REVISION_ORDER_VIOLATION "
            f"submitted_sequence={sequence} persisted_sequence={persisted_sequence} "
            f"timeframe={bar.timeframe} ts={bar.ts.astimezone(UTC).isoformat()}"
        )
    return CanonicalBarCommitReceipt(
        revision_sequence=sequence,
        written=max(int(written), 0),
        acknowledged=tuple(acknowledged),
        superseded=tuple(superseded),
        rejected=tuple(rejected),
        deleted=deleted,
    )


@dataclass(frozen=True)
class ProviderCapabilities:
    chart_stream: bool = False
    native_chart_stream: bool = False
    chart_tail_polling: bool = False
    live_quote_stream: bool = False
    live_quote_polling: bool = False
    quote_trade_events: bool = False
    gap_repair: bool = False
    exact_history_snapshot_authority: bool = False
    trading_hours: bool = False
    runtime_settings: bool = False
    instrument_search: bool = False
    instrument_binding: bool = False
    canonical_futures_history: bool = False
    gex: bool = False
    options: bool = False
    read_only: bool = True

    def as_dict(self) -> dict[str, bool]:
        return {
            "chart_stream": self.chart_stream,
            "native_chart_stream": self.native_chart_stream,
            "chart_tail_polling": self.chart_tail_polling,
            "live_quote_stream": self.live_quote_stream,
            "live_quote_polling": self.live_quote_polling,
            "quote_trade_events": self.quote_trade_events,
            "gap_repair": self.gap_repair,
            "exact_history_snapshot_authority": (self.exact_history_snapshot_authority),
            "trading_hours": self.trading_hours,
            "runtime_settings": self.runtime_settings,
            "instrument_search": self.instrument_search,
            "instrument_binding": self.instrument_binding,
            "canonical_futures_history": self.canonical_futures_history,
            "gex": self.gex,
            "options": self.options,
            "read_only": self.read_only,
        }


@dataclass(frozen=True)
class ProviderDataPolicy:
    tail_delay_grace_seconds: float = 0.0
    cache_ttl_seconds: int = 20
    authoritative_ohlcv: bool = True
    sparse_sessions: bool = False
    chart_poll_seconds: float = 30.0
    chart_request_timeout_seconds: float = 1.5

    def as_dict(self) -> dict[str, float | int | bool]:
        return {
            "tail_delay_grace_seconds": self.tail_delay_grace_seconds,
            "cache_ttl_seconds": self.cache_ttl_seconds,
            "authoritative_ohlcv": self.authoritative_ohlcv,
            "sparse_sessions": self.sparse_sessions,
            "chart_poll_seconds": self.chart_poll_seconds,
            "chart_request_timeout_seconds": self.chart_request_timeout_seconds,
        }


def _exact_history_text(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be an exact non-empty string")
    return value


class HistoryRequestUnsupportedError(ValueError):
    """A provider-owned exact route cannot satisfy authoritative history."""

    def __init__(self, *, provider: str, code: str) -> None:
        self.provider = _exact_history_text(provider, field="provider")
        self.code = _exact_history_text(code, field="code")
        super().__init__(self.code)


class HistoryRepairPriority(IntEnum):
    FOREGROUND = 0
    BACKGROUND = 1


class ProviderHistoryTerminal(str, Enum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    ROLLING_COMPLETE = "rolling_complete"
    ROLLING_PARTIAL = "rolling_partial"
    MALFORMED = "malformed"


class HistoryContractResolution(str, Enum):
    EXACT = "exact"
    PROVIDER_RESPONSE = "provider_response"


class HistoryRepairAdmissionStatus(str, Enum):
    SCHEDULED = "scheduled"
    RUNNING = "running"
    QUEUED = "queued"
    AWAITING_GENERATION_ADVANCE = "awaiting_generation_advance"
    THROTTLED = "throttled"
    QUEUE_FULL = "queue_full"


class HistoryRepairOutcomeStatus(str, Enum):
    COMMITTED = "committed"
    NO_CHANGE = "no_change"
    INCOMPLETE = "incomplete"
    MALFORMED = "malformed"
    STALE_GENERATION = "stale_generation"
    STALE_ROUTE = "stale_route"
    FETCH_FAILED = "fetch_failed"
    STORAGE_FAILED = "storage_failed"
    COMMIT_FAILED = "commit_failed"
    COMMIT_QUEUE_FULL = "commit_queue_full"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"


@dataclass(frozen=True, slots=True)
class HistoryRequestAdmissionIdentity:
    request_contract_version: int
    admission_contract_version: int
    request_mode: str
    request_type: BarProviderRequest
    provider_source: str
    provider_contract_id: str
    provider_contract_type: str
    data_type: str
    contract_resolution: HistoryContractResolution = HistoryContractResolution.EXACT

    def __post_init__(self) -> None:
        for field in ("request_contract_version", "admission_contract_version"):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{field} must be a positive integer")
        try:
            request_type = BarProviderRequest(self.request_type)
        except (TypeError, ValueError) as exc:
            raise ValueError("history request_type must be canonical") from exc
        if request_type is not BarProviderRequest.HISTORICAL:
            raise ValueError("history request_type must be historical")
        object.__setattr__(self, "request_type", request_type)
        try:
            contract_resolution = HistoryContractResolution(self.contract_resolution)
        except (TypeError, ValueError) as exc:
            raise ValueError("history contract_resolution must be canonical") from exc
        object.__setattr__(self, "contract_resolution", contract_resolution)
        for field in ("request_mode", "provider_source", "provider_contract_type", "data_type"):
            object.__setattr__(
                self,
                field,
                _exact_history_text(getattr(self, field), field=field),
            )
        if contract_resolution is HistoryContractResolution.EXACT:
            object.__setattr__(
                self,
                "provider_contract_id",
                _exact_history_text(self.provider_contract_id, field="provider_contract_id"),
            )
        elif self.provider_contract_id != "" or self.provider_contract_type != "CONTFUT":
            raise ValueError(
                "provider-response history identity requires unresolved CONTFUT contract"
            )
        if self.data_type != "TRADES":
            raise ValueError("history data_type must be TRADES")


@dataclass(frozen=True, slots=True)
class HistoryRepairIntent:
    """One provider-bounded chunk of one exact closed UTC repair envelope."""

    provider: str
    instrument_id: str
    route_fingerprint: str
    timeframe: str
    starts_at: datetime
    ends_at: datetime
    request_identity: HistoryRequestAdmissionIdentity
    canonical_generation: int
    priority: HistoryRepairPriority
    cooldown_seconds: float
    target_starts_at: datetime | None = None
    target_ends_at: datetime | None = None

    def __post_init__(self) -> None:
        for field in ("provider", "instrument_id", "route_fingerprint"):
            object.__setattr__(
                self,
                field,
                _exact_history_text(getattr(self, field), field=field),
            )
        timeframe = _exact_history_text(self.timeframe, field="timeframe")
        if timeframe not in PERSISTED_BAR_INTERVALS:
            raise ValueError("history repair timeframe must be canonical")
        object.__setattr__(self, "timeframe", timeframe)
        starts_at = require_aware_utc_datetime(self.starts_at, field="starts_at")
        ends_at = require_aware_utc_datetime(self.ends_at, field="ends_at")
        step_seconds = interval_seconds(timeframe)
        if (
            starts_at.microsecond
            or ends_at.microsecond
            or int(starts_at.timestamp()) % step_seconds
            or int(ends_at.timestamp()) % step_seconds
        ):
            raise ValueError("history repair range must be timeframe-aligned")
        if ends_at <= starts_at:
            raise ValueError("history repair range must be non-empty")
        if ends_at > datetime.now(tz=UTC):
            raise ValueError("history repair range must be fully elapsed")
        object.__setattr__(self, "starts_at", starts_at)
        object.__setattr__(self, "ends_at", ends_at)
        target_starts_at = require_aware_utc_datetime(
            self.target_starts_at if self.target_starts_at is not None else starts_at,
            field="target_starts_at",
        )
        target_ends_at = require_aware_utc_datetime(
            self.target_ends_at if self.target_ends_at is not None else ends_at,
            field="target_ends_at",
        )
        if (
            target_starts_at.microsecond
            or target_ends_at.microsecond
            or int(target_starts_at.timestamp()) % step_seconds
            or int(target_ends_at.timestamp()) % step_seconds
        ):
            raise ValueError("history repair target range must be timeframe-aligned")
        if (
            target_ends_at <= target_starts_at
            or target_ends_at > datetime.now(tz=UTC)
            or starts_at < target_starts_at
            or ends_at > target_ends_at
        ):
            raise ValueError("history repair chunk must be inside one fully elapsed target range")
        object.__setattr__(self, "target_starts_at", target_starts_at)
        object.__setattr__(self, "target_ends_at", target_ends_at)
        if not isinstance(self.request_identity, HistoryRequestAdmissionIdentity):
            raise TypeError("history repair request_identity must be typed")
        generation = self.canonical_generation
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            raise ValueError("canonical_generation must be a non-negative integer")
        try:
            priority = HistoryRepairPriority(self.priority)
        except (TypeError, ValueError) as exc:
            raise ValueError("history repair priority must be canonical") from exc
        object.__setattr__(self, "priority", priority)
        cooldown = self.cooldown_seconds
        if (
            isinstance(cooldown, bool)
            or not isinstance(cooldown, (int, float))
            or not isfinite(float(cooldown))
            or float(cooldown) < 0
        ):
            raise ValueError("history repair cooldown must be finite and non-negative")
        object.__setattr__(self, "cooldown_seconds", float(cooldown))


@dataclass(frozen=True, slots=True)
class HistoryRangeCompletion:
    starts_at: datetime
    ends_at: datetime
    response_count: int
    completed_at: datetime
    provider_source: str
    request_limit: int | None = None
    provider_limit: int | None = None

    def __post_init__(self) -> None:
        starts_at = require_aware_utc_datetime(self.starts_at, field="starts_at")
        ends_at = require_aware_utc_datetime(self.ends_at, field="ends_at")
        completed_at = require_aware_utc_datetime(
            self.completed_at,
            field="completed_at",
        )
        if ends_at <= starts_at or ends_at > completed_at:
            raise ValueError("history completion range must be fully elapsed")
        object.__setattr__(self, "starts_at", starts_at)
        object.__setattr__(self, "ends_at", ends_at)
        object.__setattr__(self, "completed_at", completed_at)
        object.__setattr__(
            self,
            "provider_source",
            _exact_history_text(self.provider_source, field="provider_source"),
        )
        if (
            isinstance(self.response_count, bool)
            or not isinstance(self.response_count, int)
            or self.response_count < 0
        ):
            raise ValueError("history completion response_count must be non-negative")
        for field in ("request_limit", "provider_limit"):
            value = getattr(self, field)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value <= 0
            ):
                raise ValueError(f"history completion {field} must be positive")
        if self.request_limit is not None and self.response_count >= self.request_limit:
            raise ValueError("history completion request limit is saturated")
        if self.provider_limit is not None and self.response_count >= self.provider_limit:
            raise ValueError("history completion provider limit is saturated")


@dataclass(frozen=True, slots=True)
class ProviderHistoryFetchResult:
    intent: HistoryRepairIntent
    terminal: ProviderHistoryTerminal
    authoritative_bars: tuple[Bar, ...]
    completions: tuple[HistoryRangeCompletion, ...] = ()
    error_code: str | None = None
    resolved_provider_contract_id: str | None = None
    response_starts_at: datetime | None = None
    response_ends_at: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.intent, HistoryRepairIntent):
            raise TypeError("provider history result intent must be typed")
        try:
            terminal = ProviderHistoryTerminal(self.terminal)
        except (TypeError, ValueError) as exc:
            raise ValueError("provider history terminal must be canonical") from exc
        object.__setattr__(self, "terminal", terminal)
        raw_bars = tuple(self.authoritative_bars)
        if any(not isinstance(bar, Bar) for bar in raw_bars):
            raise TypeError("provider history authoritative_bars must contain Bar values")
        bars = tuple(sorted(raw_bars, key=lambda bar: bar.ts))
        if len({bar.ts.astimezone(UTC) for bar in bars}) != len(bars):
            raise ValueError("provider history bars must have unique timestamps")
        identity = self.intent.request_identity
        rolling = identity.contract_resolution is HistoryContractResolution.PROVIDER_RESPONSE
        resolved_contract_id = self.resolved_provider_contract_id
        if rolling and terminal is not ProviderHistoryTerminal.MALFORMED:
            resolved_contract_id = _exact_history_text(
                resolved_contract_id,
                field="resolved_provider_contract_id",
            )
            object.__setattr__(
                self,
                "resolved_provider_contract_id",
                resolved_contract_id,
            )
        elif not rolling and resolved_contract_id is not None:
            raise ValueError("exact provider history cannot carry a resolved response contract")
        for bar in bars:
            provenance = bar.provenance
            if (
                BarState(bar.state) is not BarState.CONFIRMED
                or not bar.closed
                or bar.timeframe != self.intent.timeframe
                or not self.intent.starts_at <= bar.ts.astimezone(UTC) < self.intent.ends_at
                or provenance is None
                or provenance.provider != self.intent.provider
                or provenance.instrument_id != self.intent.instrument_id
                or provenance.route_fingerprint != self.intent.route_fingerprint
                or provenance.request_type is not identity.request_type
                or provenance.provider_contract_id
                != (resolved_contract_id if rolling else identity.provider_contract_id)
                or provenance.provider_contract_type != identity.provider_contract_type
                or provenance.data_type != identity.data_type
            ):
                raise ValueError("provider history bar contract mismatch")
        object.__setattr__(self, "authoritative_bars", bars)
        raw_completions = tuple(self.completions)
        if any(not isinstance(item, HistoryRangeCompletion) for item in raw_completions):
            raise TypeError("provider history completions must be typed")
        completions = tuple(
            sorted(raw_completions, key=lambda item: (item.starts_at, item.ends_at))
        )
        object.__setattr__(self, "completions", completions)
        response_starts_at = self.response_starts_at
        response_ends_at = self.response_ends_at
        if (response_starts_at is None) != (response_ends_at is None):
            raise ValueError("provider history response bounds must be paired")
        if response_starts_at is not None and response_ends_at is not None:
            response_starts_at = require_aware_utc_datetime(
                response_starts_at,
                field="response_starts_at",
            )
            response_ends_at = require_aware_utc_datetime(
                response_ends_at,
                field="response_ends_at",
            )
            if response_ends_at <= response_starts_at:
                raise ValueError("provider history response bounds must be non-empty")
            object.__setattr__(self, "response_starts_at", response_starts_at)
            object.__setattr__(self, "response_ends_at", response_ends_at)
        if rolling:
            if completions:
                raise ValueError("rolling provider history cannot carry exact completions")
            if terminal is ProviderHistoryTerminal.MALFORMED:
                if bars:
                    raise ValueError("malformed provider history cannot carry bars")
                if not isinstance(self.error_code, str) or not self.error_code:
                    raise ValueError("malformed provider history requires an error code")
                return
            if terminal not in {
                ProviderHistoryTerminal.ROLLING_COMPLETE,
                ProviderHistoryTerminal.ROLLING_PARTIAL,
            }:
                raise ValueError("provider-response history requires a rolling terminal")
            if bars and response_starts_at is None:
                raise ValueError("rolling provider bars require response bounds")
            if terminal is ProviderHistoryTerminal.ROLLING_COMPLETE:
                if (
                    response_starts_at is None
                    or response_starts_at > self.intent.starts_at
                    or response_ends_at is None
                    or response_ends_at < self.intent.ends_at
                    or self.error_code is not None
                ):
                    raise ValueError("complete rolling history must contain the target range")
            elif not isinstance(self.error_code, str) or not self.error_code:
                raise ValueError("partial rolling history requires an error code")
            return
        if response_starts_at is not None or response_ends_at is not None:
            raise ValueError("exact provider history cannot carry rolling response bounds")
        if terminal is ProviderHistoryTerminal.COMPLETE:
            if not completions:
                raise ValueError("complete provider history requires range completions")
            cursor = self.intent.starts_at
            admitted_count = 0
            step_seconds = interval_seconds(self.intent.timeframe)
            for completion in completions:
                if (
                    completion.starts_at != cursor
                    or completion.ends_at > self.intent.ends_at
                    or completion.provider_source != identity.provider_source
                    or completion.starts_at.microsecond
                    or completion.ends_at.microsecond
                    or int(completion.starts_at.timestamp()) % step_seconds
                    or int(completion.ends_at.timestamp()) % step_seconds
                ):
                    raise ValueError("provider history completion range mismatch")
                chunk_count = sum(
                    completion.starts_at <= bar.ts < completion.ends_at for bar in bars
                )
                if chunk_count != completion.response_count:
                    raise ValueError("provider history completion cardinality mismatch")
                admitted_count += chunk_count
                cursor = completion.ends_at
            if cursor != self.intent.ends_at or admitted_count != len(bars):
                raise ValueError("provider history completion coverage is incomplete")
            if self.error_code is not None:
                raise ValueError("complete provider history cannot carry an error")
            return
        if completions:
            raise ValueError("non-complete provider history cannot carry completions")
        if terminal is ProviderHistoryTerminal.MALFORMED:
            if bars:
                raise ValueError("malformed provider history cannot carry bars")
            if not isinstance(self.error_code, str) or not self.error_code:
                raise ValueError("malformed provider history requires an error code")


@dataclass(frozen=True, slots=True)
class HistoryRepairOutcome:
    status: HistoryRepairOutcomeStatus
    intent: HistoryRepairIntent
    terminal: ProviderHistoryTerminal | None = None
    written_bars: int = 0
    deleted_bars: int = 0
    coverage_inserted: int = 0
    coverage_expanded: bool = False
    generation: int | None = None
    error_code: str | None = None
    response_starts_at: datetime | None = None
    response_ends_at: datetime | None = None

    def __post_init__(self) -> None:
        try:
            status = HistoryRepairOutcomeStatus(self.status)
        except (TypeError, ValueError) as exc:
            raise ValueError("history repair outcome status must be canonical") from exc
        object.__setattr__(self, "status", status)
        if not isinstance(self.intent, HistoryRepairIntent):
            raise TypeError("history repair outcome intent must be typed")
        if self.terminal is not None:
            object.__setattr__(
                self,
                "terminal",
                ProviderHistoryTerminal(self.terminal),
            )
        for field in ("written_bars", "deleted_bars", "coverage_inserted"):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"history repair outcome {field} must be non-negative")
        if not isinstance(self.coverage_expanded, bool):
            raise TypeError("history repair outcome coverage_expanded must be bool")
        if self.generation is not None and (
            isinstance(self.generation, bool)
            or not isinstance(self.generation, int)
            or self.generation < 0
        ):
            raise ValueError("history repair outcome generation must be non-negative")
        if (self.response_starts_at is None) != (self.response_ends_at is None):
            raise ValueError("history repair response bounds must be paired")
        if self.response_starts_at is not None and self.response_ends_at is not None:
            for field in ("response_starts_at", "response_ends_at"):
                object.__setattr__(
                    self,
                    field,
                    require_aware_utc_datetime(getattr(self, field), field=field),
                )
            if self.response_ends_at <= self.response_starts_at:
                raise ValueError("history repair response bounds must be non-empty")

    @property
    def remaining_range(self) -> tuple[datetime, datetime] | None:
        """Unsettled target envelope, including a failed or partial current chunk.

        This is repair progress, not proof of absent provider bars. In particular
        a complete rolling fetch still cannot create absence-authoritative coverage.
        """
        chunk_complete = self.status in {
            HistoryRepairOutcomeStatus.COMMITTED,
            HistoryRepairOutcomeStatus.NO_CHANGE,
        } and self.terminal in {
            ProviderHistoryTerminal.COMPLETE,
            ProviderHistoryTerminal.ROLLING_COMPLETE,
        }
        end = self.intent.starts_at if chunk_complete else self.intent.ends_at
        start = self.intent.target_starts_at
        return (start, end) if start < end else None


@dataclass(frozen=True, slots=True)
class HistoryRepairAdmission:
    status: HistoryRepairAdmissionStatus
    intent: HistoryRepairIntent
    newly_admitted: bool = False
    completion_subscribed: bool = False
    retry_after_seconds: float = 0.0

    def __post_init__(self) -> None:
        try:
            status = HistoryRepairAdmissionStatus(self.status)
        except (TypeError, ValueError) as exc:
            raise ValueError("history repair admission status must be canonical") from exc
        object.__setattr__(self, "status", status)
        if not isinstance(self.intent, HistoryRepairIntent):
            raise TypeError("history repair admission intent must be typed")
        if not isinstance(self.newly_admitted, bool):
            raise TypeError("history repair admission newly_admitted must be bool")
        if not isinstance(self.completion_subscribed, bool):
            raise TypeError("history repair admission completion_subscribed must be bool")
        retry_after = self.retry_after_seconds
        if (
            isinstance(retry_after, bool)
            or not isinstance(retry_after, (int, float))
            or not isfinite(float(retry_after))
            or float(retry_after) < 0
        ):
            raise ValueError("history repair admission retry_after_seconds must be non-negative")
        object.__setattr__(self, "retry_after_seconds", float(retry_after))


class CanonicalHistoryStorageKind(str, Enum):
    FUTURES_CONTINUOUS = "futures_continuous"
    FUTURES_CONTRACT = "futures_contract"


@dataclass(frozen=True)
class CanonicalHistoryRoute:
    series_type: str = ""
    roll_policy: str = ""
    storage_kind: CanonicalHistoryStorageKind = CanonicalHistoryStorageKind.FUTURES_CONTINUOUS
    contract_key: str = ""

    def __post_init__(self) -> None:
        try:
            storage_kind = CanonicalHistoryStorageKind(self.storage_kind)
        except (TypeError, ValueError) as exc:
            raise ValueError("Canonical history storage kind must be typed") from exc
        object.__setattr__(self, "storage_kind", storage_kind)
        fields = {
            "series_type": self.series_type,
            "roll_policy": self.roll_policy,
            "contract_key": self.contract_key,
        }
        if any(not isinstance(value, str) or value != value.strip() for value in fields.values()):
            raise ValueError("Canonical history route fields must be exact strings")
        if storage_kind is CanonicalHistoryStorageKind.FUTURES_CONTINUOUS:
            if not self.series_type or not self.roll_policy or self.contract_key:
                raise ValueError("Continuous futures history requires series_type/roll_policy only")
            return
        if not self.contract_key or self.series_type or self.roll_policy:
            raise ValueError("Exact futures history requires contract_key only")

    @classmethod
    def exact_futures_contract(cls, contract_key: str) -> CanonicalHistoryRoute:
        return cls(
            storage_kind=CanonicalHistoryStorageKind.FUTURES_CONTRACT,
            contract_key=contract_key,
        )


@dataclass(frozen=True)
class ProviderBarBucket:
    starts_at: datetime
    closes_at: datetime


@dataclass(frozen=True)
class ProviderManifest:
    key: str
    name: str
    db_providers: tuple[str, ...]
    capabilities: ProviderCapabilities
    data_policy: ProviderDataPolicy
    source_prefixes: tuple[str, ...] = ()
    session_scope: ProviderSessionScope = ProviderSessionScope.UNKNOWN
    latency: str = ""
    status: str = ""
    requires: tuple[str, ...] = ()
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "latency": self.latency,
            "status": self.status,
            "requires": list(self.requires),
            "note": self.note,
            "db_providers": list(self.db_providers),
            "source_prefixes": list(self.source_prefixes),
            "session_scope": self.session_scope.value,
            "capabilities": self.capabilities.as_dict(),
            "data_policy": self.data_policy.as_dict(),
        }


@runtime_checkable
class ProviderAdapter(Protocol):
    manifest: ProviderManifest

    @property
    def key(self) -> str: ...

    @property
    def db_providers(self) -> tuple[str, ...]: ...

    @property
    def capabilities(self) -> ProviderCapabilities: ...

    @property
    def data_policy(self) -> ProviderDataPolicy: ...

    def catalog_entry(self) -> dict[str, Any]: ...

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
    ) -> tuple[list[Bar], str]: ...

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
    ) -> tuple[list[Bar], str]: ...

    def search_instruments(self, query: str, limit: int = 20) -> list[dict[str, Any]]: ...

    def bind_instrument(self, provider_contract_id: str) -> dict[str, Any] | None: ...

    def bind_future_root(
        self,
        root: str,
        binding: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None: ...

    def resolve_current_contract(self, instrument: dict[str, Any]) -> dict[str, Any]: ...

    def session_contract_id(self, instrument: dict[str, Any]) -> str: ...

    def bar_data_type(self, instrument: dict[str, Any]) -> str: ...

    def continuous_session(self, instrument: dict[str, Any]) -> bool: ...

    def history_session_scope(self, instrument: dict[str, Any]) -> str: ...

    def provider_bar_bucket(
        self,
        instrument: dict[str, Any],
        ts: datetime,
        interval: str,
        *,
        session_open: datetime | None = None,
        session_close: datetime | None = None,
    ) -> ProviderBarBucket | None: ...

    def provider_session_bar_buckets(
        self,
        instrument: dict[str, Any],
        interval: str,
        *,
        session_open: datetime,
        session_close: datetime,
    ) -> tuple[ProviderBarBucket, ...]: ...

    def canonical_history_route(
        self, instrument: dict[str, Any]
    ) -> CanonicalHistoryRoute | None: ...

    def commit_continuous_history_result(
        self,
        store: Any,
        bars: Sequence[Bar],
        *,
        instrument: dict[str, Any],
        revision_sequence: int,
    ) -> CanonicalBarCommitReceipt: ...

    def quote_close_base(
        self, instrument: dict[str, Any], quote: dict[str, Any]
    ) -> float | None: ...

    def read_quote_trade_events(
        self,
        after_sequence: int,
        route_identities: tuple[tuple[str, str], ...],
    ) -> tuple[int, list[dict[str, Any]], bool]: ...


@runtime_checkable
class HistoryRepairProvider(Protocol):
    def history_request_identity(
        self,
        instrument: dict[str, Any],
    ) -> HistoryRequestAdmissionIdentity: ...

    def history_request_max_span(
        self,
        instrument: dict[str, Any],
        timeframe: str,
    ) -> timedelta: ...

    async def async_fetch_history(
        self,
        intent: HistoryRepairIntent,
        timeout: float,
        *,
        instrument: dict[str, Any],
    ) -> ProviderHistoryFetchResult: ...


@runtime_checkable
class ChartLiveProvider(Protocol):
    async def async_chart_live_bars(self, *args: Any, **kwargs: Any) -> list[Bar]: ...


@runtime_checkable
class ChartCommitProvider(Protocol):
    async def async_commit_chart_bars(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> CanonicalBarCommitReceipt: ...


@runtime_checkable
class NativeChartLiveProvider(ChartLiveProvider, Protocol):
    def cached_chart_live_bars(self, *args: Any, **kwargs: Any) -> list[Bar]: ...

    async def async_cancel_chart_live_bars(self, *args: Any, **kwargs: Any) -> bool: ...


@runtime_checkable
class QuoteStreamProvider(Protocol):
    def quote_subscription_generation(self) -> str: ...

    async def async_sync_quote_subscriptions(
        self,
        instruments: list[dict[str, Any]],
        **kwargs: Any,
    ) -> dict[str, int]: ...

    def cached_quotes(
        self,
        routes: Sequence[InstrumentRoute],
        *,
        after_sequence: int | None = None,
    ) -> QuoteSnapshotRead: ...


@dataclass(frozen=True)
class QuoteSnapshotRead:
    """Provider snapshot read; reset the cursor whenever the selected routes change.

    An unchanged read carries no rows, not an empty authoritative snapshot.
    The sequence belongs to the provider owner, never to a consumer cache.
    """

    sequence: int
    quotes: dict[str, dict[str, Any]]
    changed: bool = True

    def __post_init__(self) -> None:
        if type(self.sequence) is not int or self.sequence < 0:
            raise ValueError("QUOTE_SNAPSHOT_SEQUENCE_INVALID")
        if type(self.changed) is not bool or not isinstance(self.quotes, dict):
            raise ValueError("QUOTE_SNAPSHOT_READ_INVALID")
        if not self.changed and self.quotes:
            raise ValueError("QUOTE_SNAPSHOT_UNCHANGED_MUST_BE_EMPTY")


@runtime_checkable
class QuotePollingRuntime(Protocol):
    async def fetch_quotes(
        self,
        instruments: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], str]: ...

    async def aclose(self) -> None: ...


@runtime_checkable
class QuotePollingProvider(Protocol):
    def create_quote_polling_runtime(self) -> QuotePollingRuntime: ...


@runtime_checkable
class TradingScheduleProvider(Protocol):
    async def fetch_trading_schedule(
        self,
        instrument: dict[str, Any],
        *,
        timeout: float = 6.0,
        starts_at: datetime | None = None,
        ends_at: datetime | None = None,
    ) -> dict[str, Any]: ...


@runtime_checkable
class GexProvider(Protocol):
    async def async_load_gex(self, instrument: dict[str, Any], **kwargs: Any) -> dict[str, Any]: ...

    async def async_load_live_gex(
        self, instrument: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]: ...

    async def async_stop_live_gex(self, instrument: dict[str, Any]) -> dict[str, Any]: ...

    def cached_gex(self, instrument: dict[str, Any], **kwargs: Any) -> dict[str, Any]: ...

    def gex_request_config(self, instrument: dict[str, Any], **kwargs: Any) -> Any: ...

    def gex_request_for_job(self, instrument: dict[str, Any], job: Any, **kwargs: Any) -> Any: ...


@runtime_checkable
class OptionsProvider(Protocol):
    async def async_load_option_board(
        self,
        instrument: dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]: ...

    async def async_stop_option_board(
        self,
        instrument: dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]: ...

    def option_contract_universe(
        self,
        instrument: dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]: ...

    def option_target_price(self, instrument: dict[str, Any], **kwargs: Any) -> dict[str, Any]: ...

    def live_option_quote(
        self,
        instrument: dict[str, Any],
        contract: dict[str, Any],
        *,
        consumer_id: str = "option-point",
    ) -> dict[str, Any]: ...

    def cancel_option_quote(
        self,
        instrument: dict[str, Any],
        contract: dict[str, Any],
        *,
        consumer_id: str = "option-point",
    ) -> dict[str, Any]: ...

    def reconcile_option_quote_consumers(
        self,
        active_consumer_ids: set[str],
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class InstrumentRoute:
    instrument: dict[str, Any]
    adapter: ProviderAdapter
    provider: str
    provider_symbol: str
    instrument_id: str
    instrument_key: str
    fingerprint: str
    price_increment: float | None


@dataclass(frozen=True)
class FuturesRouteTransition:
    """Provider-owned route facts for one current-futures rollover."""

    provider: str
    instrument_id: str
    previous_route_fingerprint: str
    next_route_fingerprint: str
    previous_session_contract_id: str
    next_session_contract_id: str
