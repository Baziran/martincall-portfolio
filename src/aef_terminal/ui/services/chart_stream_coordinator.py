from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from logging import Logger
from typing import Any

from aef_terminal.data.provider_contract import InstrumentRoute
from aef_terminal.data.providers import read_recent_provider_bars, route_instrument
from aef_terminal.domain import Bar, BarState
from aef_terminal.engine.data_quality import serialize_data_quality_report
from aef_terminal.engine.serialization import is_gap_placeholder_payload
from aef_terminal.features.provider_session import provider_vwap_session
from aef_terminal.runtime.chart_commits import chart_commits
from aef_terminal.runtime.bar_series import require_ordered_bar_list
from aef_terminal.runtime.derived_timeframes import derive_chart_bars
from aef_terminal.runtime.chart_events import (
    ChartBarsGenerationChanged,
    ChartBarsUpdatedEvent,
    chart_bars_updated_generation,
    live_chart_bars_snapshot,
    require_chart_bars_generation,
    wait_for_chart_bars_stable,
    wait_for_chart_bars_updated,
    wait_for_live_chart_bars,
)
from aef_terminal.runtime.metrics import increment_metric, observe_metric, set_metric
from aef_terminal.runtime.async_tasks import run_physical_executor_call
from aef_terminal.runtime.timeframes import (
    HistoryRangeWindow,
    canonical_bar_source_interval,
    chart_history_range_window,
    interval_bucket,
    interval_seconds,
    is_derived_chart_interval,
)
from aef_terminal.ui.routers.error_payloads import build_stream_status_payload
from aef_terminal.ui.services.chart_history import coalesced_load_confirmed_chart_bars
from aef_terminal.ui.services.chart_stream_messages import (
    CHART_STREAM_CONSUMER_ROLE_PRIMARY,
    CHART_STREAM_CONSUMER_ROLE_SECONDARY_CANDLES,
    ChartStreamConsumerRole,
    chart_bars_payload,
    chart_heartbeat_payload,
    chart_status_payload,
    require_chart_future_axis_payload,
    require_chart_stream_bar_payload,
    require_chart_stream_consumer_role,
)
from aef_terminal.ui.services.chart_stream_payload_tracker import ChartStreamPayloadTracker
from aef_terminal.ui.stream_payload import chart_recovery_tail


CHART_STREAM_LIVE_TAIL_BARS = 64
CHART_STREAM_DB_CHECKPOINT_SECONDS = 30.0
CHART_STREAM_ROUTE_CHECK_SECONDS = 10.0
CHART_STREAM_CONSUMER_QUEUE_SIZE = 16
CHART_STREAM_PROVIDER_RANGE = "5d"
CHART_STREAM_SECONDARY_TAIL_DEFAULT = 300
_CHART_MAINTENANCE_EXECUTOR = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix="chart-maintenance",
)

ChartCheckpointKey = str | tuple[ChartStreamConsumerRole, int]


def require_secondary_chart_tail_bars(value: object | None) -> int:
    if value is None:
        return CHART_STREAM_SECONDARY_TAIL_DEFAULT
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("CHART_STREAM_SECONDARY_TAIL_INVALID")
    if value != CHART_STREAM_SECONDARY_TAIL_DEFAULT:
        raise ValueError(
            "CHART_STREAM_SECONDARY_TAIL_PROFILE_UNSUPPORTED "
            f"tail_bars={value} expected={CHART_STREAM_SECONDARY_TAIL_DEFAULT}"
        )
    return value


def _bar_priority(payload: dict[str, Any]) -> int:
    if is_gap_placeholder_payload(payload):
        return 0
    if payload.get("closed") is True and payload.get("state") == BarState.CONFIRMED.value:
        revision = payload.get("canonical_revision", 0)
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise ValueError("chart stream bar canonical_revision is invalid")
        return 4 if revision > 0 else 3
    if str(payload.get("preview_kind") or "") == "broker_trade":
        return 1
    return 2


def _replace_bar_payload(
    existing: dict[str, Any],
    incoming: dict[str, Any],
) -> dict[str, Any]:
    if is_gap_placeholder_payload(incoming):
        return dict(existing)
    if is_gap_placeholder_payload(existing):
        return dict(incoming)
    same_timestamp = bool(
        existing.get("ts")
        and incoming.get("ts")
        and str(existing.get("ts")) == str(incoming.get("ts"))
    )
    if not same_timestamp:
        return dict(incoming)
    existing_priority = _bar_priority(existing)
    incoming_priority = _bar_priority(incoming)
    if incoming_priority < existing_priority:
        resolved = dict(existing)
        if existing.get("bar_slot") is None and incoming.get("bar_slot") is not None:
            resolved["bar_slot"] = incoming["bar_slot"]
            for payload_field in (
                "bar_slot_authoritative",
                "bar_slot_schedule_state",
            ):
                if payload_field in incoming:
                    resolved[payload_field] = incoming[payload_field]
        if "expected_close" in incoming:
            resolved["expected_close"] = incoming["expected_close"]
        return resolved
    slot = incoming.get("bar_slot")
    if slot is None:
        slot = existing.get("bar_slot")
    resolved = dict(incoming)
    if slot is not None:
        resolved["bar_slot"] = slot
        if incoming.get("bar_slot") is None:
            for field in (
                "bar_slot_authoritative",
                "bar_slot_schedule_state",
            ):
                if field in existing:
                    resolved[field] = existing[field]
    return resolved


def compact_chart_consumer_queue(
    queue: asyncio.Queue[dict[str, Any]],
    payload: dict[str, Any],
) -> None:
    pending: list[dict[str, Any]] = []
    while True:
        try:
            pending.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            break
    pending.append(payload)

    def frame_priority(item: dict[str, Any]) -> int:
        message_type = str(item.get("type") or "")
        if message_type == "chart_status":
            return 6 if item.get("requires_resubscribe") else 5
        if message_type == "chart_bars":
            bars = item.get("bars")
            if not isinstance(bars, list) or any(not isinstance(bar, dict) for bar in bars):
                raise TypeError("chart_bars queue frame must contain a bar list")
            provisional_only = bool(bars) and all(bar.get("authoritative") is False for bar in bars)
            canonical_revision = item.get("canonical_revision")
            if (
                isinstance(canonical_revision, bool)
                or not isinstance(canonical_revision, int)
                or canonical_revision < 0
            ):
                raise ValueError("chart_bars queue canonical_revision is invalid")
            if canonical_revision > 0 and not provisional_only:
                return 4
            return 2 if item.get("recovery") else 3
        return 0 if message_type == "heartbeat" else 1

    limit = queue.maxsize
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("chart stream consumer queue must have a positive bounded size")

    def stream_sequence(item: dict[str, Any]) -> int:
        value = item.get("stream_seq")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("chart stream queue stream_seq is invalid")
        return value

    while len(pending) > limit:
        drop_index = min(
            range(len(pending)),
            key=lambda index: (
                frame_priority(pending[index]),
                stream_sequence(pending[index]),
            ),
        )
        pending.pop(drop_index)
    pending.sort(key=stream_sequence)
    for item in pending:
        queue.put_nowait(item)
    increment_metric("chart_stream_queue_compactions_total")


@dataclass(frozen=True)
class ChartStreamCoordinatorDeps:
    logger: Logger
    store_factory: Callable[[], Any]
    apply_provider_runtime_settings_async: Callable[[], Awaitable[dict[str, Any]]]
    server_sleeping: Callable[[], bool]
    websocket_heartbeat_seconds: float
    stream_bar_payload: Callable[..., dict[str, Any]]
    stream_bar_signature: Callable[[dict[str, Any] | None], tuple]
    parse_iso_ts: Callable[[str | None], datetime | None]
    parse_stream_ts: Callable[[dict[str, Any] | None], datetime | None]
    chart_recovery_snapshot: Callable[
        [list[Bar], datetime | None, str, dict[str, Any], tuple[str, ...]],
        Any,
    ]
    record_chart_execution_snapshot: Callable[..., None]
    chart_stream_poll_seconds: float
    lookup_runtime_instrument: Callable[[str], dict[str, Any]]


@dataclass
class ChartStreamConsumer:
    range_: str
    queue: asyncio.Queue[dict[str, Any]]
    tracker: ChartStreamPayloadTracker
    consumer_role: ChartStreamConsumerRole = CHART_STREAM_CONSUMER_ROLE_PRIMARY
    tail_bars: int = CHART_STREAM_LIVE_TAIL_BARS
    deliver: bool = True
    initial_recovery_pending: bool = True


@dataclass(frozen=True)
class CompactChartRecoverySnapshot:
    payloads: tuple[dict[str, Any], ...]


@dataclass
class ChartStreamCoordinator:
    route: InstrumentRoute
    interval: str
    generation: int
    deps: ChartStreamCoordinatorDeps
    live_tail_bars: int = CHART_STREAM_LIVE_TAIL_BARS
    consumers: dict[asyncio.Queue[dict[str, Any]], ChartStreamConsumer] = field(
        default_factory=dict
    )
    task: asyncio.Task[None] | None = None
    terminal_error_reported: bool = False
    _delivery_seq: int = 0
    _lease_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    _checkpoint_tasks: dict[
        ChartCheckpointKey,
        asyncio.Task[tuple[Any, str, ChartCheckpointKey, int, dict[str, Any], tuple[str, ...]]],
    ] = field(default_factory=dict)
    _checkpoint_followups: set[ChartCheckpointKey] = field(default_factory=set)
    _next_checkpoint_at: dict[ChartCheckpointKey, float] = field(default_factory=dict)
    _stale_tail_repair_from: dict[str, datetime] = field(default_factory=dict)
    _slot_by_ts: dict[str, dict[str, Any]] = field(default_factory=dict)
    _future_axis: dict[str, Any] | None = None
    _future_axis_canonical_revision: int = 0
    _canonical_generation: int = 0
    _live_generation: int = 0
    _expected_live_slot: str | None = None
    _expected_live_close: str | None = None
    _live_tail_refresh_requested: bool = False

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.route.instrument_id, self.route.fingerprint, self.interval)

    @property
    def source_interval(self) -> str:
        return canonical_bar_source_interval(self.interval)

    def start(self) -> None:
        if self.task is None:
            self.task = asyncio.create_task(
                self.run(),
                name=f"chart-coordinator:{self.route.provider}:{self.interval}:g{self.generation}",
            )

    def ensure_live_tail_bars(self, history_bars: int) -> None:
        if isinstance(history_bars, bool) or not isinstance(history_bars, int) or history_bars <= 0:
            raise ValueError("CHART_STREAM_LIVE_TAIL_INVALID")
        requested = max(history_bars, CHART_STREAM_LIVE_TAIL_BARS)
        if requested <= self.live_tail_bars:
            return
        self.live_tail_bars = requested
        if self.task is not None and not self.task.done():
            self._live_tail_refresh_requested = True

    @staticmethod
    def _consumer_checkpoint_key(consumer: ChartStreamConsumer) -> ChartCheckpointKey:
        if not isinstance(consumer, ChartStreamConsumer):
            raise TypeError("chart stream consumer is required")
        role = consumer.consumer_role
        if role == CHART_STREAM_CONSUMER_ROLE_SECONDARY_CANDLES:
            return (
                CHART_STREAM_CONSUMER_ROLE_SECONDARY_CANDLES,
                require_secondary_chart_tail_bars(consumer.tail_bars),
            )
        return consumer.range_

    def _active_checkpoint_keys(self) -> set[ChartCheckpointKey]:
        return {self._consumer_checkpoint_key(consumer) for consumer in self.consumers.values()}

    def _consumers_for_checkpoint(
        self,
        checkpoint_key: ChartCheckpointKey,
    ) -> tuple[ChartStreamConsumer, ...]:
        return tuple(
            consumer
            for consumer in self.consumers.values()
            if self._consumer_checkpoint_key(consumer) == checkpoint_key
        )

    def add_consumer(
        self,
        range_: str,
        since_ts: str,
        *,
        deliver: bool = True,
        live_tail_bars: int = CHART_STREAM_LIVE_TAIL_BARS,
        consumer_role: ChartStreamConsumerRole = CHART_STREAM_CONSUMER_ROLE_PRIMARY,
        tail_bars: int | None = None,
    ) -> ChartStreamConsumer:
        consumer_role = require_chart_stream_consumer_role(consumer_role)
        if not isinstance(range_, str) or not range_ or range_ != range_.strip():
            raise ValueError("CHART_STREAM_RANGE_INVALID")
        if not isinstance(deliver, bool):
            raise TypeError("CHART_STREAM_DELIVER_INVALID")
        if (
            isinstance(live_tail_bars, bool)
            or not isinstance(live_tail_bars, int)
            or live_tail_bars <= 0
        ):
            raise ValueError("CHART_STREAM_LIVE_TAIL_INVALID")
        secondary_consumer = consumer_role == CHART_STREAM_CONSUMER_ROLE_SECONDARY_CANDLES
        initial_ts = None if secondary_consumer else self.deps.parse_iso_ts(since_ts)
        if not secondary_consumer and since_ts and initial_ts is None:
            raise ValueError("CHART_STREAM_SINCE_TIMESTAMP_INVALID")
        bounded_tail_bars = (
            require_secondary_chart_tail_bars(tail_bars)
            if secondary_consumer
            else max(live_tail_bars, CHART_STREAM_LIVE_TAIL_BARS)
        )
        recovery_tail_bars = (
            bounded_tail_bars
            if secondary_consumer
            else chart_recovery_tail(
                initial_ts,
                datetime.now(tz=UTC),
                self.interval,
            )
        )
        requested_live_tail_bars = max(bounded_tail_bars, recovery_tail_bars)
        self.ensure_live_tail_bars(requested_live_tail_bars)
        if (
            not secondary_consumer
            and initial_ts is not None
            and recovery_tail_bars > CHART_STREAM_LIVE_TAIL_BARS
        ):
            repair_from = interval_bucket(
                initial_ts + timedelta(seconds=interval_seconds(self.interval)),
                self.source_interval,
            )
            current_repair_from = self._stale_tail_repair_from.get(str(range_))
            if current_repair_from is None or repair_from < current_repair_from:
                self._stale_tail_repair_from[str(range_)] = repair_from
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(
            maxsize=CHART_STREAM_CONSUMER_QUEUE_SIZE
        )
        consumer = ChartStreamConsumer(
            range_=range_,
            queue=queue,
            tracker=ChartStreamPayloadTracker(
                parse_stream_ts=self.deps.parse_stream_ts,
                stream_bar_signature=self.deps.stream_bar_signature,
                resolve_payload=_replace_bar_payload,
                initial_ts=initial_ts,
            ),
            consumer_role=consumer_role,
            tail_bars=requested_live_tail_bars,
            deliver=deliver,
        )
        self.consumers[queue] = consumer
        checkpoint_key = self._consumer_checkpoint_key(consumer)
        checkpoint_inflight = checkpoint_key in self._checkpoint_tasks
        if not secondary_consumer or not checkpoint_inflight:
            self._next_checkpoint_at[checkpoint_key] = 0.0
        self._schedule_checkpoint(
            checkpoint_key,
            force=(not secondary_consumer or not checkpoint_inflight),
        )
        set_metric(
            "chart_stream_consumers",
            len(self.consumers),
            provider=self.route.provider,
            interval=self.interval,
        )
        return consumer

    def remove_consumer(self, consumer: ChartStreamConsumer) -> int:
        self.consumers.pop(consumer.queue, None)
        previous_live_tail_bars = self.live_tail_bars
        self.live_tail_bars = max(
            (max(item.tail_bars, CHART_STREAM_LIVE_TAIL_BARS) for item in self.consumers.values()),
            default=CHART_STREAM_LIVE_TAIL_BARS,
        )
        if (
            self.live_tail_bars < previous_live_tail_bars
            and self.task is not None
            and not self.task.done()
        ):
            self._live_tail_refresh_requested = True
        active_checkpoint_keys = self._active_checkpoint_keys()
        for checkpoint_key in list(self._next_checkpoint_at):
            if checkpoint_key not in active_checkpoint_keys:
                self._next_checkpoint_at.pop(checkpoint_key, None)
                if isinstance(checkpoint_key, str):
                    self._stale_tail_repair_from.pop(checkpoint_key, None)
                self._checkpoint_followups.discard(checkpoint_key)
        set_metric(
            "chart_stream_consumers",
            len(self.consumers),
            provider=self.route.provider,
            interval=self.interval,
        )
        return len(self.consumers)

    def publish(
        self, payload: dict[str, Any], *, consumer: ChartStreamConsumer | None = None
    ) -> None:
        self._delivery_seq += 1
        base_outgoing = {
            **payload,
            "stream_generation": self.generation,
            "stream_seq": self._delivery_seq,
        }
        targets = (consumer,) if consumer is not None else tuple(self.consumers.values())
        for target in targets:
            if target is None or not target.deliver:
                continue
            outgoing = {
                **base_outgoing,
                "consumer_role": target.consumer_role,
            }
            if outgoing.get("type") in {"chart_status", "heartbeat"}:
                outgoing = {
                    **outgoing,
                    "instrument_id": self.route.instrument_id,
                    "route_fingerprint": self.route.fingerprint,
                    "interval": self.interval,
                    "range": target.range_,
                }
            if target.queue.full():
                compact_chart_consumer_queue(target.queue, outgoing)
            else:
                target.queue.put_nowait(outgoing)
            set_metric(
                "chart_stream_queue_depth",
                target.queue.qsize(),
                provider=self.route.provider,
                interval=self.interval,
            )

    def _required_timestamps_for_range(self, range_: str) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    timestamp
                    for consumer in self.consumers.values()
                    if consumer.range_ == range_
                    for timestamp in consumer.tracker.invalidated_timestamps
                }
            )
        )

    def _required_timestamps_for_checkpoint(
        self,
        checkpoint_key: ChartCheckpointKey,
    ) -> tuple[str, ...]:
        if not isinstance(checkpoint_key, str):
            return ()
        return self._required_timestamps_for_range(checkpoint_key)

    def _schedule_checkpoint(
        self,
        range_: ChartCheckpointKey,
        *,
        force: bool = False,
    ) -> None:
        if range_ in self._checkpoint_tasks:
            if force:
                self._checkpoint_followups.add(range_)
            return
        now = time.monotonic()
        if not force and now < self._next_checkpoint_at.get(range_, 0.0):
            return
        self._next_checkpoint_at[range_] = now + CHART_STREAM_DB_CHECKPOINT_SECONDS
        required_timestamps = self._required_timestamps_for_checkpoint(range_)
        if isinstance(range_, str):
            checkpoint = self._load_checkpoint(range_, required_timestamps)
            profile_name = range_
        else:
            checkpoint = self._load_secondary_checkpoint(range_[1])
            profile_name = f"{range_[0]}:{range_[1]}"
        self._checkpoint_tasks[range_] = asyncio.create_task(
            checkpoint,
            name=f"chart-checkpoint:{self.route.provider}:{self.interval}:{profile_name}",
        )

    async def _load_checkpoint(
        self,
        range_: str,
        required_timestamps: tuple[str, ...] = (),
    ) -> tuple[Any, str, str, int, dict[str, Any], tuple[str, ...]]:
        started = time.perf_counter()
        requested_window = chart_history_range_window(range_, self.interval)
        stale_tail_from = self._stale_tail_repair_from.get(range_)
        parsed_required = tuple(
            sorted(
                {
                    parsed
                    for raw in required_timestamps
                    if (parsed := self.deps.parse_iso_ts(raw)) is not None
                }
            )
        )
        if parsed_required:
            step = timedelta(seconds=interval_seconds(self.interval))
            required_start = interval_bucket(parsed_required[0] - step, self.interval)
            required_end = interval_bucket(parsed_required[-1] + step * 2, self.interval)
            assert requested_window.starts_at is not None
            requested_window = chart_history_range_window(
                range_,
                self.interval,
                window=HistoryRangeWindow(
                    range_key=requested_window.range_key,
                    starts_at=min(requested_window.starts_at, required_start),
                    ends_at=max(requested_window.ends_at, required_end),
                ),
            )
        repair_window: HistoryRangeWindow | None = None
        if stale_tail_from is not None:
            assert requested_window.starts_at is not None
            repair_starts_at = max(requested_window.starts_at, stale_tail_from)
            repair_ends_at = min(
                requested_window.ends_at,
                interval_bucket(datetime.now(tz=UTC), self.source_interval),
            )
            if repair_ends_at > repair_starts_at:
                repair_window = HistoryRangeWindow(
                    range_key=requested_window.range_key,
                    starts_at=repair_starts_at,
                    ends_at=repair_ends_at,
                )
        history = await coalesced_load_confirmed_chart_bars(
            self.route.provider,
            self.route.instrument,
            self.interval,
            range_,
            timeout=0.5,
            store_factory=self.deps.store_factory,
            refresh_provider=not bool(
                self.route.adapter.capabilities.native_chart_stream
                or self.route.adapter.capabilities.chart_tail_polling
            ),
            executor=_CHART_MAINTENANCE_EXECUTOR,
            window=requested_window,
            repair_window=repair_window,
        )
        if repair_window is not None:
            repair = dict(history.coverage.get("repair") or {})
            if repair.get("requested_from") or repair.get("status") in {
                "not_needed",
                "not_supported",
            }:
                if self._stale_tail_repair_from.get(range_) == stale_tail_from:
                    self._stale_tail_repair_from.pop(range_, None)
        bars = history.bars
        warning = history.warning
        snapshot = await run_physical_executor_call(
            _CHART_MAINTENANCE_EXECUTOR,
            self.deps.chart_recovery_snapshot,
            list(bars),
            None,
            self.interval,
            self.route.instrument,
            required_timestamps,
        )
        observe_metric(
            "chart_checkpoint_seconds",
            time.perf_counter() - started,
            provider=self.route.provider,
            interval=self.interval,
            status="ok",
        )
        return (
            snapshot,
            warning,
            range_,
            history.canonical_generation,
            history.coverage,
            required_timestamps,
        )

    def _read_secondary_checkpoint_payloads(
        self,
        tail_bars: int,
    ) -> tuple[dict[str, Any], ...]:
        store = self.deps.store_factory()
        bars = read_recent_provider_bars(
            self.route,
            self.interval,
            store=store,
            limit=tail_bars,
        )
        return tuple(
            self.deps.stream_bar_payload(bar, instrument=self.route.instrument)
            for bar in bars[-tail_bars:]
        )

    async def _load_secondary_checkpoint(
        self,
        tail_bars: int,
    ) -> tuple[
        CompactChartRecoverySnapshot,
        str,
        ChartCheckpointKey,
        int,
        dict[str, Any],
        tuple[str, ...],
    ]:
        started = time.perf_counter()
        bounded_tail_bars = require_secondary_chart_tail_bars(tail_bars)
        load_generation = await wait_for_chart_bars_stable(
            self.interval,
            self.route.fingerprint,
            0.5,
            instrument_id=self.route.instrument_id,
        )
        payloads = await run_physical_executor_call(
            _CHART_MAINTENANCE_EXECUTOR,
            self._read_secondary_checkpoint_payloads,
            bounded_tail_bars,
        )
        observe_metric(
            "chart_checkpoint_seconds",
            time.perf_counter() - started,
            provider=self.route.provider,
            interval=self.interval,
            consumer_role=CHART_STREAM_CONSUMER_ROLE_SECONDARY_CANDLES,
            status="ok",
        )
        return (
            CompactChartRecoverySnapshot(payloads=payloads),
            "",
            (CHART_STREAM_CONSUMER_ROLE_SECONDARY_CANDLES, bounded_tail_bars),
            load_generation,
            {},
            (),
        )

    def _remember_slots(
        self,
        payloads: list[dict[str, Any]],
    ) -> None:
        for payload in payloads:
            require_chart_stream_bar_payload(payload, interval=self.interval)
            ts = payload["ts"]
            slot = payload.get("bar_slot")
            if payload.get("bar_slot_authoritative") is True:
                assert isinstance(slot, int) and not isinstance(slot, bool)
                self._slot_by_ts[ts] = {
                    field: payload[field]
                    for field in (
                        "bar_slot",
                        "bar_slot_authoritative",
                        "bar_slot_schedule_state",
                    )
                    if field in payload
                }
        if len(self._slot_by_ts) > 1024:
            self._slot_by_ts = dict(sorted(self._slot_by_ts.items())[-1024:])

    def _future_axis_order(
        self,
        future_axis: dict[str, Any],
        canonical_revision: int,
    ) -> tuple[int, int, float, int, int, int]:
        require_chart_future_axis_payload(future_axis, interval=self.interval)
        if (
            isinstance(canonical_revision, bool)
            or not isinstance(canonical_revision, int)
            or canonical_revision < 0
        ):
            raise ValueError("chart stream future_axis canonical revision is invalid")
        schedule_revision = future_axis["schedule_revision"]
        raw_anchor = future_axis["anchor_ts"]
        anchor = self.deps.parse_iso_ts(raw_anchor) if raw_anchor is not None else None
        anchor_revision = anchor.timestamp() if anchor is not None else float("-inf")
        schedule_state = future_axis["schedule_state"]
        state_rank = 1 if schedule_state in {"continuous", "verified"} else 0
        slots = future_axis["slots"]
        return (
            canonical_revision,
            schedule_revision,
            anchor_revision,
            state_rank,
            int(future_axis["complete"]),
            len(slots),
        )

    def _ordered_future_axis(
        self,
        future_axis: dict[str, Any] | None,
        canonical_revision: int,
    ) -> dict[str, Any] | None:
        if future_axis is None:
            return None
        future_axis = require_chart_future_axis_payload(future_axis, interval=self.interval)
        if self._future_axis is not None:
            incoming_order = self._future_axis_order(
                future_axis,
                canonical_revision,
            )
            current_order = self._future_axis_order(
                self._future_axis,
                self._future_axis_canonical_revision,
            )
            if (
                incoming_order[0] < current_order[0]
                or incoming_order[1] < current_order[1]
                or (incoming_order[:2] == current_order[:2] and incoming_order <= current_order)
            ):
                return self._future_axis
        self._future_axis = {
            **future_axis,
            "slots": [dict(item) for item in future_axis["slots"]],
        }
        self._future_axis_canonical_revision = canonical_revision
        return self._future_axis

    def _with_known_slot(self, payload: dict[str, Any]) -> dict[str, Any]:
        ts = str(payload.get("ts") or "")
        if payload.get("bar_slot_authoritative") is True or ts not in self._slot_by_ts:
            return payload
        return {**payload, **self._slot_by_ts[ts]}

    def _publish_bar_payloads(
        self,
        payloads: list[dict[str, Any]],
        *,
        source: str,
        recovery: bool = False,
        recovery_complete: bool = False,
        recovery_scope: str = "",
        warning: str = "",
        gap_repair: dict[str, Any] | None = None,
        canonical_revision: int = 0,
        event_reason: str = "",
        chart_data_quality: dict[str, Any] | None = None,
        history_coverage: dict[str, Any] | None = None,
        future_axis: dict[str, Any] | None = None,
        expected_live_slot: str | None = None,
        expected_live_close: str | None = None,
        target_checkpoint: ChartCheckpointKey | None = None,
        secondary_tail_membership: dict[str, Any] | None = None,
    ) -> None:
        if not isinstance(payloads, list):
            raise TypeError("chart stream publication payloads must be a list")
        payloads = [
            require_chart_stream_bar_payload(payload, interval=self.interval)
            for payload in payloads
        ]
        effective_future_axis = self._ordered_future_axis(
            future_axis,
            canonical_revision,
        )
        self._remember_slots(payloads)
        if chart_data_quality and (
            chart_data_quality.get("market_closed") or chart_data_quality.get("session_unknown")
        ):
            self._expected_live_slot = None
            self._expected_live_close = None
        elif expected_live_slot:
            current_expected = self.deps.parse_iso_ts(self._expected_live_slot)
            incoming_expected = self.deps.parse_iso_ts(expected_live_slot)
            if current_expected is None or (
                incoming_expected is not None and incoming_expected > current_expected
            ):
                self._expected_live_slot = expected_live_slot
                self._expected_live_close = expected_live_close
            elif incoming_expected is not None and incoming_expected == current_expected:
                self._expected_live_close = expected_live_close
        effective_expected_live_slot = self._expected_live_slot
        effective_expected_live_close = self._expected_live_close
        for consumer in tuple(self.consumers.values()):
            if not consumer.deliver:
                continue
            if (
                target_checkpoint is not None
                and self._consumer_checkpoint_key(consumer) != target_checkpoint
            ):
                continue
            consumer_role = consumer.consumer_role
            secondary_consumer = consumer_role == CHART_STREAM_CONSUMER_ROLE_SECONDARY_CANDLES
            initial_secondary_recovery = bool(
                recovery and secondary_consumer and consumer.initial_recovery_pending
            )
            if initial_secondary_recovery:
                accepted = [
                    self._with_known_slot(consumer.tracker.resolve(payload)) for payload in payloads
                ]
            else:
                accepted = [
                    self._with_known_slot(consumer.tracker.resolve(payload))
                    for payload in payloads
                    if consumer.tracker.payload_is_new_or_changed(payload)
                ]
            if not accepted and not recovery:
                continue
            effective_recovery_scope = recovery_scope
            initial_recovery = bool(recovery and consumer.initial_recovery_pending)
            if initial_recovery:
                effective_recovery_scope = "initial"
            message = chart_bars_payload(
                source=source,
                instrument_id=self.route.instrument_id,
                route_fingerprint=self.route.fingerprint,
                symbol=str(self.route.instrument.get("display") or self.route.instrument_key),
                provider_symbol=self.route.provider_symbol,
                interval=self.interval,
                range_=consumer.range_,
                bars=accepted,
                warning="" if secondary_consumer else warning,
                gap_repair=None if secondary_consumer else gap_repair,
                recovery=recovery,
                recovery_complete=recovery_complete,
                recovery_scope=effective_recovery_scope,
                stream_generation=self.generation,
                canonical_revision=canonical_revision,
                event_reason=event_reason,
                chart_data_quality=None if secondary_consumer else chart_data_quality,
                history_coverage=None if secondary_consumer else history_coverage,
                future_axis=None if secondary_consumer else effective_future_axis,
                expected_live_slot=effective_expected_live_slot,
                expected_live_close=effective_expected_live_close,
                consumer_role=consumer_role,
                secondary_tail_membership=(
                    secondary_tail_membership if secondary_consumer and recovery else None
                ),
            )
            self.publish(message, consumer=consumer)
            if initial_recovery:
                consumer.initial_recovery_pending = False
            if accepted and not secondary_consumer:
                self.deps.record_chart_execution_snapshot(
                    self.route.instrument_id,
                    self.interval,
                    message["bars"],
                    source,
                    self.route.fingerprint,
                )

    def _handle_provider_bars(self, bars: list[Bar], observed_at: datetime) -> None:
        source_bars = require_ordered_bar_list(
            bars,
            timeframe=self.source_interval,
            field="live chart provider bars",
        )
        display_bars = derive_chart_bars(
            source_bars,
            self.interval,
            as_of=observed_at,
        )
        hot_payloads: list[dict[str, Any]] = []
        confirmed: list[Bar] = []
        for bar in source_bars:
            provider_state = BarState(bar.state)
            if provider_state is BarState.CONFIRMED:
                confirmed.append(bar)
        for bar in display_bars:
            provider_state = BarState(bar.state)
            payload = self.deps.stream_bar_payload(bar, instrument=self.route.instrument)
            hot_payloads.append(
                {
                    **payload,
                    "closed": False
                    if provider_state is BarState.CONFIRMED
                    else payload.get("closed"),
                    "state": (
                        BarState.AWAITING_PROVIDER_CONFIRMATION.value
                        if provider_state is BarState.CONFIRMED
                        else provider_state.value
                    ),
                    "preview_kind": "provider_bar",
                    "preview_ts": observed_at.isoformat(),
                    "provider": self.route.provider,
                    "provider_state": provider_state.value,
                    "commit_pending": provider_state is BarState.CONFIRMED,
                    "authoritative": False,
                }
            )
        if hot_payloads and any(consumer.deliver for consumer in self.consumers.values()):
            self._remember_slots(hot_payloads)
            immediate_payloads = [self._with_known_slot(payload) for payload in hot_payloads]
            self._publish_bar_payloads(
                immediate_payloads,
                source=f"{self.route.provider}:chart-live",
                canonical_revision=self._canonical_generation,
                expected_live_slot=str(immediate_payloads[-1].get("ts") or "") or None,
                expected_live_close=str(immediate_payloads[-1].get("expected_close") or "") or None,
            )
            increment_metric(
                "chart_live_revisions_total", provider=self.route.provider, interval=self.interval
            )
        if confirmed:
            chart_commits.submit(
                self.route.adapter,
                self.route.instrument,
                self.source_interval,
                self.deps.store_factory,
                confirmed,
            )

    def _handle_canonical_events(
        self,
        previous_generation: int,
        next_generation: int,
        events: list[ChartBarsUpdatedEvent],
    ) -> None:
        overflow = bool(
            next_generation > previous_generation
            and (not events or events[0].generation > previous_generation + 1)
        )
        if overflow:
            increment_metric(
                "chart_canonical_event_overflow_total",
                provider=self.route.provider,
                interval=self.interval,
            )
            for range_ in self._active_checkpoint_keys():
                self._schedule_checkpoint(range_, force=True)
        for event in events:
            if (
                event.instrument_id != self.route.instrument_id
                or event.route_fingerprint != self.route.fingerprint
                or event.interval != self.source_interval
            ):
                continue
            invalidated_timestamps = {
                interval_bucket(bar.ts.astimezone(UTC), self.interval).isoformat()
                for bar in event.bars
            }
            for consumer in self.consumers.values():
                consumer.tracker.invalidate_timestamps(invalidated_timestamps)
            if not event.bars:
                for range_ in self._active_checkpoint_keys():
                    self._schedule_checkpoint(range_, force=True)
                continue
            if is_derived_chart_interval(self.interval):
                for range_ in self._active_checkpoint_keys():
                    self._schedule_checkpoint(range_, force=True)
                continue
            session_reset = provider_vwap_session(self.route.instrument, event.bars)
            payloads = []
            for bar in event.bars:
                payload = self.deps.stream_bar_payload(bar, instrument=self.route.instrument)
                authoritative = bool(bar.closed and BarState(bar.state) is BarState.CONFIRMED)
                payloads.append(
                    {
                        **payload,
                        "canonical_revision": event.generation,
                        "authoritative": authoritative,
                        "commit_pending": not authoritative,
                        "session_key": session_reset.key_for_bar(bar)
                        if session_reset.available
                        else None,
                    }
                )
            self._remember_slots(payloads)
            self._publish_bar_payloads(
                [self._with_known_slot(payload) for payload in payloads],
                source=f"{event.provider}:canonical-commit",
                canonical_revision=event.generation,
                event_reason=event.reason,
            )
            for range_ in self._active_checkpoint_keys():
                self._schedule_checkpoint(range_, force=True)

    def _flush_checkpoints(self) -> None:
        for range_, task in list(self._checkpoint_tasks.items()):
            if not task.done():
                continue
            self._checkpoint_tasks.pop(range_, None)
            followup_requested = range_ in self._checkpoint_followups
            self._checkpoint_followups.discard(range_)
            try:
                (
                    snapshot,
                    warning,
                    target_checkpoint,
                    load_generation,
                    history_coverage,
                    required_timestamps,
                ) = task.result()
                require_chart_bars_generation(
                    load_generation,
                    self.interval,
                    self.route.fingerprint,
                    instrument_id=self.route.instrument_id,
                )
            except asyncio.CancelledError:
                if followup_requested and self.consumers:
                    self._schedule_checkpoint(range_, force=True)
                continue
            except ChartBarsGenerationChanged:
                increment_metric(
                    "chart_checkpoint_stale_total",
                    provider=self.route.provider,
                    interval=self.interval,
                )
                # Both an unsettled load and a publication race are transient.
                # A new canonical event may force recovery; otherwise retry at
                # the maintenance cadence, never a self-sustaining hot loop.
                self._next_checkpoint_at[range_] = (
                    time.monotonic() + CHART_STREAM_DB_CHECKPOINT_SECONDS
                )
                continue
            except Exception as exc:
                observe_metric(
                    "chart_checkpoint_seconds",
                    0.0,
                    provider=self.route.provider,
                    interval=self.interval,
                    status="error",
                )
                self.deps.logger.warning(
                    "chart checkpoint failed provider=%s instrument_id=%s interval=%s range=%s: %s",
                    self.route.provider,
                    self.route.instrument_id,
                    self.interval,
                    range_,
                    exc,
                )
                for target_consumer in self._consumers_for_checkpoint(range_):
                    target_role = getattr(
                        target_consumer,
                        "consumer_role",
                        CHART_STREAM_CONSUMER_ROLE_PRIMARY,
                    )
                    self.publish(
                        chart_status_payload(
                            source=f"{self.route.provider}:checkpoint",
                            instrument_id=self.route.instrument_id,
                            route_fingerprint=self.route.fingerprint,
                            symbol=str(
                                self.route.instrument.get("display") or self.route.instrument_key
                            ),
                            provider_symbol=self.route.provider_symbol,
                            interval=self.interval,
                            range_=target_consumer.range_,
                            message=f"Chart checkpoint degraded: {exc}",
                            retry_in_seconds=CHART_STREAM_DB_CHECKPOINT_SECONDS,
                            consumer_role=target_role,
                        ),
                        consumer=target_consumer,
                    )
                if followup_requested:
                    self._schedule_checkpoint(range_, force=True)
                continue
            payloads = list(snapshot.payloads)
            secondary_checkpoint = not isinstance(target_checkpoint, str)
            repair = {} if secondary_checkpoint else dict(history_coverage.get("repair") or {})
            canonical_revision = load_generation
            payloads = [
                {
                    **payload,
                    "canonical_revision": canonical_revision,
                    "authoritative": True,
                    "commit_pending": False,
                }
                if (
                    payload.get("closed") is True
                    and str(payload.get("state") or "") == BarState.CONFIRMED.value
                    and not is_gap_placeholder_payload(payload)
                )
                else payload
                for payload in payloads
            ]
            if secondary_checkpoint:
                assert isinstance(target_checkpoint, tuple)
                secondary_tail_membership = {
                    "limit": target_checkpoint[1],
                    "timestamps": sorted(
                        str(payload.get("ts") or "") for payload in payloads if payload.get("ts")
                    ),
                }
                self._publish_bar_payloads(
                    payloads,
                    source=f"{self.route.provider}:db-checkpoint",
                    recovery=True,
                    recovery_complete=True,
                    recovery_scope="checkpoint",
                    target_checkpoint=target_checkpoint,
                    secondary_tail_membership=secondary_tail_membership,
                    canonical_revision=canonical_revision,
                )
            else:
                self._publish_bar_payloads(
                    payloads,
                    source=f"{self.route.provider}:db-checkpoint",
                    recovery=True,
                    recovery_complete=True,
                    recovery_scope="gap_repair" if required_timestamps else "checkpoint",
                    warning=warning,
                    gap_repair=repair,
                    chart_data_quality=serialize_data_quality_report(snapshot.quality),
                    history_coverage=history_coverage,
                    future_axis=snapshot.future_axis,
                    expected_live_slot=snapshot.expected_live_slot,
                    expected_live_close=snapshot.expected_live_close,
                    target_checkpoint=target_checkpoint,
                    canonical_revision=canonical_revision,
                )
            if followup_requested:
                self._schedule_checkpoint(range_, force=True)

    async def _start_or_poll_live(self) -> list[Bar]:
        requested_tail = self.live_tail_bars
        source_tail = (
            requested_tail * 3 + 2 if is_derived_chart_interval(self.interval) else requested_tail
        )
        bars = await self.route.adapter.async_chart_live_bars(
            self.source_interval,
            CHART_STREAM_PROVIDER_RANGE,
            timeout=max(
                float(self.route.adapter.data_policy.chart_request_timeout_seconds),
                0.1,
            ),
            tail=source_tail,
            instrument=self.route.instrument,
            consumer_id=self._lease_id,
        )
        if self.live_tail_bars <= requested_tail:
            self._live_tail_refresh_requested = False
        return bars

    async def run(self) -> None:
        adapter = self.route.adapter
        live_capable = bool(
            adapter.capabilities.native_chart_stream or adapter.capabilities.chart_tail_polling
        )
        native_live = bool(adapter.capabilities.native_chart_stream)
        self._canonical_generation = chart_bars_updated_generation(
            self.interval,
            self.route.fingerprint,
            instrument_id=self.route.instrument_id,
        )
        live_snapshot = live_chart_bars_snapshot(
            self.interval,
            self.route.fingerprint,
            instrument_id=self.route.instrument_id,
        )
        self._live_generation = live_snapshot.generation if live_snapshot is not None else 0
        live_wait: asyncio.Task[Any] | None = None
        canonical_wait: asyncio.Task[Any] | None = None
        poll_task: asyncio.Task[list[Bar]] | None = None
        route_task: asyncio.Future[dict[str, Any]] | None = None
        next_poll_at = 0.0
        subscription_started = False
        next_route_check_at = time.monotonic() + CHART_STREAM_ROUTE_CHECK_SECONDS
        last_heartbeat_at = 0.0
        try:
            if adapter.capabilities.runtime_settings:
                try:
                    await self.deps.apply_provider_runtime_settings_async()
                except Exception as exc:
                    self.deps.logger.warning(
                        "chart runtime settings failed provider=%s instrument_id=%s interval=%s: %s",
                        self.route.provider,
                        self.route.instrument_id,
                        self.interval,
                        exc,
                    )
                    self.publish(
                        build_stream_status_payload(
                            status_type="chart_status",
                            source=f"{self.route.provider}:runtime-settings",
                            code="CHART_RUNTIME_SETTINGS_ERROR",
                            category="stream",
                            retryable=True,
                            error=exc,
                            retry_in_seconds=self.deps.chart_stream_poll_seconds,
                            instrument_id=self.route.instrument_id,
                            route_fingerprint=self.route.fingerprint,
                            symbol=self.route.instrument_key,
                        )
                    )
            if live_capable:
                try:
                    initial = await self._start_or_poll_live()
                    subscription_started = True
                    self._handle_provider_bars(initial, datetime.now(tz=UTC))
                except Exception as exc:
                    self.deps.logger.warning(
                        "chart live start failed provider=%s instrument_id=%s interval=%s: %s",
                        self.route.provider,
                        self.route.instrument_id,
                        self.interval,
                        exc,
                    )
                    self.publish(
                        build_stream_status_payload(
                            status_type="chart_status",
                            source=f"{self.route.provider}:chart-live",
                            code="CHART_LIVE_START_ERROR",
                            category="stream",
                            retryable=True,
                            error=exc,
                            retry_in_seconds=self.deps.chart_stream_poll_seconds,
                            instrument_id=self.route.instrument_id,
                            route_fingerprint=self.route.fingerprint,
                            symbol=self.route.instrument_key,
                        )
                    )
                next_poll_at = time.monotonic() + max(
                    float(adapter.data_policy.chart_poll_seconds),
                    self.deps.chart_stream_poll_seconds,
                )
            while self.consumers:
                if self.deps.server_sleeping():
                    self.publish(
                        chart_status_payload(
                            source="server:sleep",
                            instrument_id=self.route.instrument_id,
                            route_fingerprint=self.route.fingerprint,
                            symbol=str(
                                self.route.instrument.get("display") or self.route.instrument_key
                            ),
                            provider_symbol=self.route.provider_symbol,
                            interval=self.interval,
                            message="Server sleeping: chart stream paused.",
                            retry_in_seconds=0,
                        )
                    )
                    return
                now = time.monotonic()
                for range_ in self._active_checkpoint_keys():
                    self._schedule_checkpoint(range_)
                if canonical_wait is None:
                    canonical_wait = asyncio.create_task(
                        wait_for_chart_bars_updated(
                            self.interval,
                            self.route.fingerprint,
                            self._canonical_generation,
                            max(self.deps.websocket_heartbeat_seconds, 1.0),
                            instrument_id=self.route.instrument_id,
                        )
                    )
                if native_live and live_wait is None:
                    if subscription_started:
                        live_wait = asyncio.create_task(
                            wait_for_live_chart_bars(
                                self.interval,
                                self.route.fingerprint,
                                self._live_generation,
                                max(self.deps.websocket_heartbeat_seconds, 1.0),
                                instrument_id=self.route.instrument_id,
                            )
                        )
                if (
                    live_capable
                    and poll_task is None
                    and (now >= next_poll_at or self._live_tail_refresh_requested)
                    and (
                        not native_live
                        or not subscription_started
                        or self._live_tail_refresh_requested
                    )
                ):
                    poll_task = asyncio.create_task(self._start_or_poll_live())
                    next_poll_at = now + max(float(adapter.data_policy.chart_poll_seconds), 0.25)
                if route_task is None and now >= next_route_check_at:
                    route_task = asyncio.create_task(
                        run_physical_executor_call(
                            _CHART_MAINTENANCE_EXECUTOR,
                            self.deps.lookup_runtime_instrument,
                            self.route.instrument_id,
                        ),
                        name=(
                            f"chart-route:{self.route.provider}:"
                            f"{self.route.instrument_id}:{self.interval}"
                        ),
                    )
                    next_route_check_at = now + CHART_STREAM_ROUTE_CHECK_SECONDS
                waiters = [
                    task
                    for task in (
                        live_wait,
                        canonical_wait,
                        poll_task,
                        route_task,
                        *self._checkpoint_tasks.values(),
                    )
                    if task is not None
                ]
                if waiters:
                    await asyncio.wait(waiters, timeout=0.5, return_when=asyncio.FIRST_COMPLETED)
                else:
                    await asyncio.sleep(0.05)

                if live_wait is not None and live_wait.done():
                    task = live_wait
                    live_wait = None
                    try:
                        next_generation, event = task.result()
                    except Exception as exc:
                        subscription_started = False
                        next_poll_at = time.monotonic() + self.deps.chart_stream_poll_seconds
                        self.deps.logger.debug(
                            "chart live event wait failed provider=%s instrument_id=%s interval=%s: %s",
                            self.route.provider,
                            self.route.instrument_id,
                            self.interval,
                            exc,
                        )
                        event = None
                        next_generation = self._live_generation
                    if event is not None and next_generation > self._live_generation:
                        self._live_generation = next_generation
                        if event.reason == "stream_closed":
                            subscription_started = False
                            next_poll_at = time.monotonic() + self.deps.chart_stream_poll_seconds
                        elif (
                            event.instrument_id == self.route.instrument_id
                            and event.route_fingerprint == self.route.fingerprint
                            and event.interval == self.source_interval
                        ):
                            self._handle_provider_bars(list(event.bars), event.observed_at)
                    elif (
                        event is None
                        and live_chart_bars_snapshot(
                            self.interval,
                            self.route.fingerprint,
                            instrument_id=self.route.instrument_id,
                        )
                        is None
                    ):
                        subscription_started = False
                        next_poll_at = time.monotonic() + self.deps.chart_stream_poll_seconds
                if poll_task is not None and poll_task.done():
                    task = poll_task
                    poll_task = None
                    try:
                        bars = task.result()
                    except Exception as exc:
                        self.deps.logger.debug(
                            "chart tail poll failed provider=%s instrument_id=%s interval=%s: %s",
                            self.route.provider,
                            self.route.instrument_id,
                            self.interval,
                            exc,
                        )
                    else:
                        subscription_started = True
                        self._handle_provider_bars(bars, datetime.now(tz=UTC))
                if canonical_wait is not None and canonical_wait.done():
                    task = canonical_wait
                    canonical_wait = None
                    previous = self._canonical_generation
                    try:
                        next_generation, events = task.result()
                    except Exception as exc:
                        self.deps.logger.debug(
                            "chart canonical event wait failed provider=%s instrument_id=%s interval=%s: %s",
                            self.route.provider,
                            self.route.instrument_id,
                            self.interval,
                            exc,
                        )
                    else:
                        self._canonical_generation = next_generation
                        self._handle_canonical_events(previous, next_generation, events)
                if route_task is not None and route_task.done():
                    task = route_task
                    route_task = None
                    try:
                        current_route = route_instrument(task.result())
                    except Exception as exc:
                        self.deps.logger.debug(
                            "chart route check failed instrument_id=%s interval=%s: %s",
                            self.route.instrument_id,
                            self.interval,
                            exc,
                        )
                        current_route = self.route
                    if (
                        current_route.instrument_id != self.route.instrument_id
                        or current_route.fingerprint != self.route.fingerprint
                    ):
                        self.publish(
                            {
                                **build_stream_status_payload(
                                    status_type="chart_status",
                                    source="chart:route",
                                    code="CHART_STREAM_ROUTE_CHANGED",
                                    category="routing",
                                    retryable=True,
                                    error="Chart provider contract changed; reconnecting.",
                                    retry_in_seconds=self.deps.chart_stream_poll_seconds,
                                    instrument_id=self.route.instrument_id,
                                    route_fingerprint=self.route.fingerprint,
                                    symbol=current_route.instrument_key,
                                    requires_resubscribe=True,
                                ),
                                "actual_route_fingerprint": current_route.fingerprint,
                            }
                        )
                        return
                self._flush_checkpoints()
                now = time.monotonic()
                if now - last_heartbeat_at >= self.deps.websocket_heartbeat_seconds:
                    for consumer in tuple(self.consumers.values()):
                        self.publish(
                            chart_heartbeat_payload(
                                source=f"{self.route.provider}:chart-idle",
                                instrument_id=self.route.instrument_id,
                                route_fingerprint=self.route.fingerprint,
                                symbol=str(
                                    self.route.instrument.get("display")
                                    or self.route.instrument_key
                                ),
                                provider_symbol=self.route.provider_symbol,
                                interval=self.interval,
                                range_=consumer.range_,
                                consumer_role=getattr(
                                    consumer,
                                    "consumer_role",
                                    CHART_STREAM_CONSUMER_ROLE_PRIMARY,
                                ),
                            ),
                            consumer=consumer,
                        )
                    last_heartbeat_at = now
        finally:
            child_tasks = [
                task
                for task in (
                    live_wait,
                    canonical_wait,
                    poll_task,
                    route_task,
                    *self._checkpoint_tasks.values(),
                )
                if task is not None
            ]
            for task in child_tasks:
                task.cancel()
            if child_tasks:
                await asyncio.gather(*child_tasks, return_exceptions=True)
            self._checkpoint_tasks.clear()
            if native_live:
                try:
                    await adapter.async_cancel_chart_live_bars(
                        self.source_interval,
                        CHART_STREAM_PROVIDER_RANGE,
                        timeout=1.0,
                        instrument=self.route.instrument,
                        consumer_id=self._lease_id,
                    )
                except Exception as exc:
                    self.deps.logger.debug(
                        "chart live cancel failed provider=%s instrument_id=%s interval=%s: %s",
                        self.route.provider,
                        self.route.instrument_id,
                        self.interval,
                        exc,
                    )

    async def stop(self) -> None:
        if self.task is None:
            return
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
