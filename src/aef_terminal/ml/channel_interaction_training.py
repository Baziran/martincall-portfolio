from __future__ import annotations

import csv
import json
import os
import platform
import shutil
import uuid
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import exp, isfinite, log
from pathlib import Path
from types import MappingProxyType
from typing import Any

import numpy as np

from aef_terminal.backtest.labels import channel_moving_line_outcome
from aef_terminal.build_info import build_info, git_worktree_info
from aef_terminal.domain import Bar, BarState
from aef_terminal.ml.channel_interaction import (
    CHANNEL_INTERACTION_ARTIFACT_CONTRACT,
    CHANNEL_INTERACTION_ACTIVE_CONTRACT,
    CHANNEL_INTERACTION_FEATURE_COLUMNS,
    CHANNEL_INTERACTION_FEATURE_SCHEMA_VERSION,
    CHANNEL_INTERACTION_LABELS,
    CHANNEL_INTERACTION_LABELS_BY_PHASE,
    CHANNEL_INTERACTION_LEVEL_SLOT_TIMEFRAME,
    CHANNEL_INTERACTION_PHASES,
    CHANNEL_INTERACTION_SCOPE_CONTRACT,
    CHANNEL_INTERACTION_TARGET_CONTRACT,
    ChannelInteractionInferenceScope,
    FrozenChannelLevel,
    channel_interaction_dataset_root,
    extract_channel_interaction_features,
    normalize_channel_interaction_acceptance_gate,
    normalize_channel_interaction_decision_policy,
    normalize_channel_interaction_build_provenance,
    normalize_channel_interaction_inference_scopes,
    normalize_channel_interaction_phase_temperatures,
    normalize_channel_interaction_target_definition,
    require_channel_interaction_data_path,
    require_channel_interaction_run_id,
    sha256_file,
)
from aef_terminal.research.channel_interaction_journal import (
    validate_channel_interaction_journal_envelope,
)


CHANNEL_INTERACTION_DATASET_CONTRACT = "channel-interaction-dataset-v2"
CHANNEL_INTERACTION_DATASET_FIELDS = (
    "dataset_contract",
    "feature_schema_version",
    "episode_id",
    "provider",
    "instrument_id",
    "route_fingerprint",
    "parent_timeframe",
    "session_key",
    "session_segment",
    "session_calendar",
    "session_timezone",
    "phase",
    "target_contract",
    "level_slot_timeframe",
    "decision_ts",
    "decision_slot",
    "target_start_slot",
    "target_end_slot",
    "outcome_end_ts",
    "frozen_revision",
    "label",
    *CHANNEL_INTERACTION_FEATURE_COLUMNS,
)
SPLIT_NAMES = ("train", "validation", "calibration", "locked_test")


def channel_interaction_build_provenance(
    *,
    xgboost_version: str | None = None,
) -> dict[str, Any]:
    source = git_worktree_info()
    embedded = build_info()
    git_sha = str(source.get("git_sha") or "unknown")
    if git_sha == "unknown":
        git_sha = str(embedded.get("git_sha") or "unknown")
    return {
        "git_sha": git_sha,
        "git_dirty": source.get("git_dirty") is True,
        "build_version": str(embedded.get("version") or "unknown"),
        "python_version": platform.python_version(),
        "numpy_version": str(np.__version__),
        "xgboost_version": xgboost_version,
        "builder_module_sha256": sha256_file(Path(__file__)),
    }


@dataclass(frozen=True, slots=True)
class ChannelInteractionDatasetBuildConfig:
    outcome_horizon_slots: int = 15
    pre_touch_wait_slots: int = 5
    breakout_atr: float = 0.25
    reversal_atr: float = 0.35
    acceptance_atr: float = 0.10
    acceptance_closes: int = 2

    def __post_init__(self) -> None:
        for field_name in (
            "outcome_horizon_slots",
            "pre_touch_wait_slots",
            "acceptance_closes",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{field_name} must be a positive integer")
        for field_name in ("breakout_atr", "reversal_atr", "acceptance_atr"):
            value = float(getattr(self, field_name))
            if not isfinite(value) or value < 0:
                raise ValueError(f"{field_name} must be finite and non-negative")


def channel_interaction_target_definition(
    config: ChannelInteractionDatasetBuildConfig,
) -> Mapping[str, Any]:
    return normalize_channel_interaction_target_definition(
        {
            "target_contract": CHANNEL_INTERACTION_TARGET_CONTRACT,
            "level_slot_timeframe": CHANNEL_INTERACTION_LEVEL_SLOT_TIMEFRAME,
            "by_phase": {
                "pre_touch": {
                    "task": "first_touch_then_outcome",
                    "labels": list(CHANNEL_INTERACTION_LABELS_BY_PHASE["pre_touch"]),
                    "touch_search_interval": (
                        "confirmed_1m_slots_after_decision_through_touch_wait"
                    ),
                    "touch_wait_slots": config.pre_touch_wait_slots,
                    "outcome_interval": (
                        "first_touch_slot_through_touch_slot_plus_horizon_minus_one"
                    ),
                    "outcome_horizon_slots": config.outcome_horizon_slots,
                    "no_touch_label": "no_touch",
                },
                "post_touch": {
                    "task": "rolling_future_continuation",
                    "labels": list(CHANNEL_INTERACTION_LABELS_BY_PHASE["post_touch"]),
                    "outcome_interval": (
                        "decision_slot_plus_one_through_decision_slot_plus_horizon"
                    ),
                    "outcome_horizon_slots": config.outcome_horizon_slots,
                    "known_bars_excluded": True,
                    "no_touch_label_allowed": False,
                },
            },
        }
    )


def _validate_dataset_target_interval(
    row: Mapping[str, Any],
    config: ChannelInteractionDatasetBuildConfig,
) -> None:
    decision_slot = int(row["decision_slot"])
    target_start_slot = int(row["target_start_slot"])
    target_end_slot = int(row["target_end_slot"])
    if row["phase"] == "post_touch":
        if target_start_slot != decision_slot + 1 or row["label"] == "no_touch":
            raise ValueError("post_touch row violates its rolling future target")
        expected_end_slot = decision_slot + config.outcome_horizon_slots
    elif row["label"] == "no_touch":
        if target_start_slot != decision_slot + 1:
            raise ValueError("pre_touch no_touch target start is invalid")
        expected_end_slot = decision_slot + config.pre_touch_wait_slots
    else:
        expected_end_slot = target_start_slot + config.outcome_horizon_slots - 1
    if target_end_slot != expected_end_slot:
        raise ValueError("dataset row target interval disagrees with its build config")


@dataclass(frozen=True, slots=True)
class ParsedJournalObservation:
    record_index: int
    episode_id: str
    provider: str
    instrument_id: str
    route_fingerprint: str
    parent_timeframe: str
    sample_eligible: bool
    slot_schedule_state: str
    session_key: str | None
    session_segment: str
    session_calendar: str
    session_timezone: str
    phase: str
    decision_ts: datetime
    atr_value: float | None
    touch_slot: int | None
    level: FrozenChannelLevel
    bars: tuple[Bar, ...]
    slots: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class LogisticBaseline:
    mean: np.ndarray
    scale: np.ndarray
    weights: np.ndarray
    bias: np.ndarray

    def margins(self, matrix: np.ndarray) -> np.ndarray:
        standardized = (matrix - self.mean) / self.scale
        return standardized @ self.weights + self.bias


def _parse_timestamp(value: object, *, field_name: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a valid ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return parsed.astimezone(UTC)


def _required_text(mapping: Mapping[str, Any], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _parse_frozen_level(payload: object) -> FrozenChannelLevel:
    if not isinstance(payload, Mapping):
        raise ValueError("frozen_level must be an object")
    return FrozenChannelLevel(
        anchor_slot=int(payload["anchor_slot"]),
        anchor_price=float(payload["anchor_price"]),
        slope_per_slot=float(payload["slope_per_slot"]),
        slot_timeframe=_required_text(payload, "slot_timeframe"),
        side=str(payload["side"]),
        frozen_revision=_required_text(payload, "frozen_revision"),
        frozen_at=_parse_timestamp(payload.get("frozen_at"), field_name="frozen_at"),
    )


def _parse_decision_session(
    payload: object,
) -> tuple[str | None, str, str, str]:
    if payload is None:
        return None, "unknown", "unknown", "unknown"
    if not isinstance(payload, Mapping):
        raise ValueError("decision_session must be an object")
    if (
        payload.get("contract") != "provider-session-decision-v1"
        or payload.get("source") != "provider_schedule"
    ):
        raise ValueError("decision_session has an unsupported contract")
    calendar = _required_text(payload, "calendar")
    timezone = _required_text(payload, "timezone")
    state = _required_text(payload, "state")
    if state == "unknown":
        _required_text(payload, "reason_code")
        return None, "unknown", calendar, timezone
    if state != "known":
        raise ValueError("decision_session.state is unsupported")
    segment = _required_text(payload, "segment")
    if segment not in {"liquid", "extended", "continuous"}:
        raise ValueError("decision_session.segment is unsupported")
    return (
        _required_text(payload, "session_key"),
        segment,
        calendar,
        timezone,
    )


def _parse_bar(payload: object, *, fallback_symbol: str) -> Bar:
    if not isinstance(payload, Mapping):
        raise ValueError("micro_window bars must be objects")
    raw_symbol = payload.get("symbol")
    symbol = raw_symbol if isinstance(raw_symbol, str) and raw_symbol else fallback_symbol
    if not symbol:
        raise ValueError("micro_window bar requires provider-qualified routing metadata")
    return Bar(
        symbol=symbol,
        ts=_parse_timestamp(payload.get("ts"), field_name="bar.ts"),
        open=float(payload["open"]),
        high=float(payload["high"]),
        low=float(payload["low"]),
        close=float(payload["close"]),
        volume=float(payload["volume"]),
        timeframe=_required_text(payload, "timeframe"),
        source=_required_text(payload, "source"),
        closed=payload.get("closed") is True,
        state=payload.get("state"),
    )


def parse_channel_interaction_journal_record(
    payload: object,
    *,
    record_index: int,
) -> ParsedJournalObservation:
    if not isinstance(payload, Mapping):
        raise ValueError("journal record must be an object")
    observation = payload.get("observation")
    if not isinstance(observation, Mapping):
        raise ValueError("journal record is missing its typed observation")
    identity = observation.get("identity")
    if not isinstance(identity, Mapping):
        raise ValueError("journal observation is missing exact route identity")
    raw_provider_symbol = identity.get("provider_symbol")
    raw_contract_id = identity.get("provider_contract_id")
    fallback_symbol = (
        raw_provider_symbol
        if isinstance(raw_provider_symbol, str) and raw_provider_symbol
        else raw_contract_id
        if isinstance(raw_contract_id, str) and raw_contract_id
        else ""
    )
    micro_window = observation.get("micro_window")
    if not isinstance(micro_window, Mapping):
        raise ValueError("journal record has no causal micro_window")
    raw_bars = micro_window.get("bars")
    raw_slots = micro_window.get("bar_slots")
    if not isinstance(raw_bars, Sequence) or isinstance(raw_bars, (str, bytes, bytearray)):
        raise ValueError("micro_window.bars must be an array")
    if not isinstance(raw_slots, Sequence) or isinstance(raw_slots, (str, bytes, bytearray)):
        raise ValueError("micro_window.bar_slots must be an array")
    if len(raw_bars) != len(raw_slots):
        raise ValueError("micro_window bars and slots have different lengths")
    bars = tuple(_parse_bar(item, fallback_symbol=fallback_symbol) for item in raw_bars)
    slots = tuple(int(item) for item in raw_slots)
    decision_ts = _parse_timestamp(
        observation.get("decision_ts"),
        field_name="decision_ts",
    )
    if not bars or bars[-1].ts + timedelta(minutes=1) != decision_ts:
        raise ValueError("decision_ts must equal the final confirmed 1m bar close")
    for index, (bar, slot) in enumerate(zip(bars, slots, strict=True)):
        if bar.timeframe != "1m" or not bar.closed or bar.state is not BarState.CONFIRMED:
            raise ValueError("micro_window must contain only confirmed closed 1m bars")
        if bar.ts + timedelta(minutes=1) > decision_ts:
            raise ValueError("micro_window contains a bar unavailable at decision_ts")
        if index and (bar.ts <= bars[index - 1].ts or slot <= slots[index - 1]):
            raise ValueError("micro_window bars and canonical slots must be monotonic")
    raw_atr = observation.get("atr_value")
    atr_value = None if raw_atr is None else float(raw_atr)
    raw_touch_slot = observation.get("touch_slot")
    touch_slot = None if raw_touch_slot is None else int(raw_touch_slot)
    phase = _required_text(observation, "phase")
    if phase not in CHANNEL_INTERACTION_PHASES:
        raise ValueError("research_observation.phase is not supported")
    sample_eligible = observation.get("sample_eligible")
    if not isinstance(sample_eligible, bool):
        raise ValueError("research_observation.sample_eligible must be an exact boolean")
    quality = observation.get("quality")
    if (
        not isinstance(quality, Mapping)
        or quality.get("ok") is not True
        or quality.get("provider_complete") is not True
        or quality.get("pending_slot_count") != 0
        or quality.get("slot_axis_authoritative") is not True
        or quality.get("slot_schedule_state") not in {"continuous", "verified"}
    ):
        raise ValueError("research_observation quality does not prove its canonical slot axis")
    (
        session_key,
        session_segment,
        session_calendar,
        session_timezone,
    ) = _parse_decision_session(observation.get("decision_session"))
    return ParsedJournalObservation(
        record_index=record_index,
        episode_id=_required_text(observation, "episode_id"),
        provider=_required_text(identity, "provider"),
        instrument_id=_required_text(identity, "instrument_id"),
        route_fingerprint=_required_text(identity, "route_fingerprint"),
        parent_timeframe=_required_text(identity, "parent_timeframe"),
        sample_eligible=sample_eligible,
        slot_schedule_state=str(quality["slot_schedule_state"]),
        session_key=session_key,
        session_segment=session_segment,
        session_calendar=session_calendar,
        session_timezone=session_timezone,
        phase=phase,
        decision_ts=decision_ts,
        atr_value=atr_value,
        touch_slot=touch_slot,
        level=_parse_frozen_level(observation.get("frozen_level")),
        bars=bars,
        slots=slots,
    )


def _scope_future_bars(
    observation: ParsedJournalObservation,
    scope_records: Sequence[ParsedJournalObservation],
) -> Mapping[int, Bar]:
    canonical_slot_by_ts = {
        bar.ts: slot for bar, slot in zip(observation.bars, observation.slots, strict=True)
    }
    canonical_ts_by_slot = {
        slot: bar.ts for bar, slot in zip(observation.bars, observation.slots, strict=True)
    }
    by_slot: dict[int, Bar] = {}
    for candidate in sorted(
        scope_records,
        key=lambda item: (item.decision_ts, item.record_index),
    ):
        if candidate.decision_ts <= observation.decision_ts:
            continue

        overlap_offsets = {
            canonical_slot_by_ts[bar.ts] - slot
            for bar, slot in zip(candidate.bars, candidate.slots, strict=True)
            if bar.ts in canonical_slot_by_ts
        }
        if not overlap_offsets:
            continue
        if len(overlap_offsets) != 1:
            raise ValueError("confirmed 1m overlap changed logical offset")
        slot_offset = next(iter(overlap_offsets))

        for bar, raw_slot in zip(candidate.bars, candidate.slots, strict=True):
            slot = raw_slot + slot_offset
            existing_slot = canonical_slot_by_ts.get(bar.ts)
            if existing_slot is not None and existing_slot != slot:
                raise ValueError("confirmed 1m overlap changed logical offset")
            existing_ts = canonical_ts_by_slot.get(slot)
            if existing_ts is not None and existing_ts != bar.ts:
                raise ValueError("canonical 1m slot changed confirmed timestamp")
            canonical_slot_by_ts[bar.ts] = slot
            canonical_ts_by_slot[slot] = bar.ts
            if bar.ts + timedelta(minutes=1) <= observation.decision_ts:
                continue
            # Every candidate already passed the canonical journal admission
            # contract. Provider history may legitimately refine OHLCV after
            # an earlier live snapshot, so the later decision snapshot owns
            # the bar instead of turning a normal revision into dataset loss.
            by_slot[slot] = bar
    ordered = dict(sorted(by_slot.items()))
    previous_bar: Bar | None = None
    for bar in ordered.values():
        if previous_bar is not None and bar.ts <= previous_bar.ts:
            raise ValueError("canonical future slots do not have monotonic timestamps")
        previous_bar = bar
    return MappingProxyType(ordered)


def _first_level_touch(
    slot_bars: Sequence[tuple[int, Bar]],
    level: FrozenChannelLevel,
) -> int | None:
    for slot, bar in slot_bars:
        line_price = level.price_at(slot)
        if float(bar.low) <= line_price <= float(bar.high):
            return slot
    return None


def _required_slot_window(
    future_by_slot: Mapping[int, Bar],
    *,
    start_slot: int,
    end_slot: int,
) -> list[tuple[int, Bar]] | None:
    if end_slot < start_slot:
        raise ValueError("slot window end precedes its start")
    window: list[tuple[int, Bar]] = []
    for slot in range(start_slot, end_slot + 1):
        bar = future_by_slot.get(slot)
        if bar is None:
            return None
        window.append((slot, bar))
    return window


def build_channel_interaction_dataset_rows(
    records: Sequence[object],
    *,
    config: ChannelInteractionDatasetBuildConfig | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    build_config = config or ChannelInteractionDatasetBuildConfig()
    parsed: list[ParsedJournalObservation] = []
    skipped = Counter()
    for index, record in enumerate(records):
        record_id = record.get("record_id") if isinstance(record, Mapping) else None
        record_reference = record_id if isinstance(record_id, str) and record_id else "<missing>"
        try:
            canonical_record = validate_channel_interaction_journal_envelope(record)
            parsed.append(
                parse_channel_interaction_journal_record(
                    canonical_record,
                    record_index=index,
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                "invalid compacted journal record "
                f"record_index={index} record_id={record_reference}: {exc}"
            ) from exc

    scope_records: dict[
        tuple[str, str, str, str],
        list[ParsedJournalObservation],
    ] = defaultdict(list)
    for observation in parsed:
        scope_records[
            (
                observation.provider,
                observation.instrument_id,
                observation.route_fingerprint,
                observation.parent_timeframe,
            )
        ].append(observation)
    for values in scope_records.values():
        values.sort(key=lambda item: (item.decision_ts, item.record_index))

    rows: list[dict[str, Any]] = []
    dedupe: set[tuple[str, str, str, str, str, str, datetime]] = set()
    for observation in sorted(parsed, key=lambda item: (item.decision_ts, item.record_index)):
        if not observation.sample_eligible:
            skipped["sample_ineligible"] += 1
            continue
        dedupe_key = (
            observation.provider,
            observation.instrument_id,
            observation.route_fingerprint,
            observation.parent_timeframe,
            observation.episode_id,
            observation.phase,
            observation.decision_ts,
        )
        if dedupe_key in dedupe:
            skipped["duplicate_decision"] += 1
            continue
        dedupe.add(dedupe_key)
        feature_result = extract_channel_interaction_features(
            observation.bars,
            observation.slots,
            level=observation.level,
            decision_ts=observation.decision_ts,
            phase=observation.phase,
            atr_value=observation.atr_value,
            touch_slot=observation.touch_slot,
        )
        if feature_result.status != "ready":
            skipped[feature_result.reason_code] += 1
            continue
        if (
            observation.atr_value is None
            or not isfinite(observation.atr_value)
            or observation.atr_value <= 0
        ):
            skipped["label_atr_unavailable"] += 1
            continue
        future_by_slot = _scope_future_bars(
            observation,
            scope_records[
                (
                    observation.provider,
                    observation.instrument_id,
                    observation.route_fingerprint,
                    observation.parent_timeframe,
                )
            ],
        )
        if not future_by_slot:
            skipped["outcome_not_mature"] += 1
            continue

        decision_slot = observation.slots[-1]
        if observation.phase == "pre_touch":
            wait_start_slot = decision_slot + 1
            wait_end_slot = decision_slot + build_config.pre_touch_wait_slots
            touch_search = _required_slot_window(
                future_by_slot,
                start_slot=wait_start_slot,
                end_slot=wait_end_slot,
            )
            if touch_search is None:
                skipped[
                    "outcome_not_mature"
                    if max(future_by_slot) < wait_end_slot
                    else "outcome_slot_gap"
                ] += 1
                continue
            first_touch_slot = _first_level_touch(touch_search, observation.level)
            if first_touch_slot is None:
                label = "no_touch"
                target_start_slot = wait_start_slot
                target_end_slot = wait_end_slot
                outcome_end = touch_search[-1][1].ts + timedelta(minutes=1)
            else:
                target_start_slot = first_touch_slot
                target_end_slot = first_touch_slot + build_config.outcome_horizon_slots - 1
                outcome_window = _required_slot_window(
                    future_by_slot,
                    start_slot=target_start_slot,
                    end_slot=target_end_slot,
                )
                if outcome_window is None:
                    skipped[
                        "outcome_not_mature"
                        if max(future_by_slot) < target_end_slot
                        else "outcome_slot_gap"
                    ] += 1
                    continue
                outcome_slots = [slot for slot, _bar in outcome_window]
                outcome_bars = [bar for _slot, bar in outcome_window]
                outcome_end = outcome_bars[-1].ts + timedelta(minutes=1)
                label = channel_moving_line_outcome(
                    outcome_bars,
                    outcome_slots,
                    anchor_slot=observation.level.anchor_slot,
                    anchor_price=observation.level.anchor_price,
                    slope_per_slot=observation.level.slope_per_slot,
                    side=observation.level.side,
                    breakout_distance=build_config.breakout_atr * observation.atr_value,
                    reversal_distance=build_config.reversal_atr * observation.atr_value,
                    acceptance_distance=build_config.acceptance_atr * observation.atr_value,
                    acceptance_closes=build_config.acceptance_closes,
                ).value
        else:
            target_start_slot = decision_slot + 1
            target_end_slot = decision_slot + build_config.outcome_horizon_slots
            outcome_window = _required_slot_window(
                future_by_slot,
                start_slot=target_start_slot,
                end_slot=target_end_slot,
            )
            if outcome_window is None:
                skipped[
                    "outcome_not_mature"
                    if max(future_by_slot) < target_end_slot
                    else "outcome_slot_gap"
                ] += 1
                continue
            outcome_slots = [slot for slot, _bar in outcome_window]
            outcome_bars = [bar for _slot, bar in outcome_window]
            outcome_end = outcome_bars[-1].ts + timedelta(minutes=1)
            label = channel_moving_line_outcome(
                outcome_bars,
                outcome_slots,
                anchor_slot=observation.level.anchor_slot,
                anchor_price=observation.level.anchor_price,
                slope_per_slot=observation.level.slope_per_slot,
                side=observation.level.side,
                breakout_distance=build_config.breakout_atr * observation.atr_value,
                reversal_distance=build_config.reversal_atr * observation.atr_value,
                acceptance_distance=build_config.acceptance_atr * observation.atr_value,
                acceptance_closes=build_config.acceptance_closes,
            ).value

        row: dict[str, Any] = {
            "dataset_contract": CHANNEL_INTERACTION_DATASET_CONTRACT,
            "feature_schema_version": CHANNEL_INTERACTION_FEATURE_SCHEMA_VERSION,
            "episode_id": observation.episode_id,
            "provider": observation.provider,
            "instrument_id": observation.instrument_id,
            "route_fingerprint": observation.route_fingerprint,
            "parent_timeframe": observation.parent_timeframe,
            "session_key": observation.session_key,
            "session_segment": observation.session_segment,
            "session_calendar": observation.session_calendar,
            "session_timezone": observation.session_timezone,
            "phase": observation.phase,
            "target_contract": CHANNEL_INTERACTION_TARGET_CONTRACT,
            "level_slot_timeframe": observation.level.slot_timeframe,
            "decision_ts": observation.decision_ts.isoformat(),
            "decision_slot": decision_slot,
            "target_start_slot": target_start_slot,
            "target_end_slot": target_end_slot,
            "outcome_end_ts": outcome_end.isoformat(),
            "frozen_revision": observation.level.frozen_revision,
            "label": label,
        }
        row.update(feature_result.features)
        rows.append(row)

    report = {
        "parsed_records": len(parsed),
        "input_records": len(records),
        "curated_rows": len(rows),
        "skipped": dict(sorted(skipped.items())),
        "label_counts": dict(sorted(Counter(row["label"] for row in rows).items())),
        "phase_counts": dict(sorted(Counter(row["phase"] for row in rows).items())),
        "session_segment_counts": dict(
            sorted(Counter(row["session_segment"] for row in rows).items())
        ),
        "inference_scopes": [
            {
                "provider": provider,
                "instrument_id": instrument_id,
                "parent_timeframe": parent_timeframe,
            }
            for provider, instrument_id, parent_timeframe in sorted(
                {(row["provider"], row["instrument_id"], row["parent_timeframe"]) for row in rows}
            )
        ],
    }
    return rows, report


def read_jsonl(path: Path) -> list[object]:
    records: list[object] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON on line {line_number}") from exc
    return records


def verify_compacted_journal_records(path: Path) -> dict[str, Any]:
    records_path = require_channel_interaction_data_path(path, kind="dataset")
    source_dir = records_path.parent
    source_manifest_path = source_dir / "manifest.json"
    checksums_path = source_dir / "SHA256SUMS"
    if records_path.name != "records.jsonl":
        raise ValueError("journal input must be a compacted records.jsonl")
    if not source_manifest_path.is_file() or not checksums_path.is_file():
        raise ValueError("journal input is missing its compaction manifest or SHA256SUMS")
    manifest = json.loads(source_manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("journal compaction manifest must be an object")
    if manifest.get("status") != "ready" or manifest.get("immutable") is not True:
        raise ValueError("journal compaction is not an immutable ready dataset")
    if manifest.get("dataset_id") != source_dir.name:
        raise ValueError("journal dataset id does not match its directory")
    checksums: dict[str, str] = {}
    for line in checksums_path.read_text(encoding="utf-8").splitlines():
        digest, separator, relative_path = line.partition("  ")
        if not separator or len(digest) != 64:
            raise ValueError("journal SHA256SUMS is malformed")
        checksums[relative_path] = digest
    if checksums.get("records.jsonl") != sha256_file(records_path):
        raise ValueError("journal records SHA-256 mismatch")
    if checksums.get("manifest.json") != sha256_file(source_manifest_path):
        raise ValueError("journal manifest SHA-256 mismatch")
    counts = manifest.get("counts")
    if not isinstance(counts, Mapping):
        raise ValueError("journal compaction manifest counts are invalid")
    expected_count = counts.get("curated_records")
    actual_count = sum(
        1 for line in records_path.read_text(encoding="utf-8").splitlines() if line.strip()
    )
    if not isinstance(expected_count, int) or expected_count != actual_count:
        raise ValueError("journal record count does not match its manifest")
    return manifest


def write_curated_dataset(
    rows: Sequence[Mapping[str, Any]],
    *,
    raw_path: Path,
    output_path: Path,
    manifest_path: Path,
    config: ChannelInteractionDatasetBuildConfig,
    report: Mapping[str, Any],
) -> dict[str, Any]:
    raw = require_channel_interaction_data_path(raw_path, kind="dataset")
    output = require_channel_interaction_data_path(output_path, kind="dataset")
    manifest_output = require_channel_interaction_data_path(manifest_path, kind="dataset")
    if output.parent != manifest_output.parent or output.name == manifest_output.name:
        raise ValueError("curated table and manifest must be distinct files in one run directory")
    publication_dir = output.parent
    if publication_dir.exists():
        raise FileExistsError(f"training dataset run already exists: {publication_dir}")
    publication_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = publication_dir.parent / (
        f".{publication_dir.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}"
    )
    staging.mkdir()
    try:
        staging_output = staging / output.name
        with staging_output.open("x", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=CHANNEL_INTERACTION_DATASET_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        read_curated_dataset(staging_output)
        for row in rows:
            _validate_dataset_target_interval(row, config)
        inference_scopes = inference_scopes_from_rows(rows)
        normalized_target = channel_interaction_target_definition(config)
        manifest = {
            "dataset_contract": CHANNEL_INTERACTION_DATASET_CONTRACT,
            "feature_schema_version": CHANNEL_INTERACTION_FEATURE_SCHEMA_VERSION,
            "feature_columns": list(CHANNEL_INTERACTION_FEATURE_COLUMNS),
            "labels": list(CHANNEL_INTERACTION_LABELS),
            "phases": list(CHANNEL_INTERACTION_PHASES),
            "target_definition": {
                "target_contract": normalized_target["target_contract"],
                "level_slot_timeframe": normalized_target["level_slot_timeframe"],
                "by_phase": {
                    phase: dict(normalized_target["by_phase"][phase])
                    for phase in CHANNEL_INTERACTION_PHASES
                },
            },
            "inference_scope": {
                "scope_contract": CHANNEL_INTERACTION_SCOPE_CONTRACT,
                "allowed": [scope.as_dict() for scope in inference_scopes],
            },
            "created_at": datetime.now(tz=UTC).isoformat(),
            "immutable": True,
            "build_provenance": channel_interaction_build_provenance(),
            "source_records_file": str(
                raw.relative_to(channel_interaction_dataset_root().resolve(strict=False))
            ),
            "source_records_sha256": sha256_file(raw),
            "source_manifest_file": str(
                (raw.parent / "manifest.json").relative_to(
                    channel_interaction_dataset_root().resolve(strict=False)
                )
            ),
            "source_manifest_sha256": sha256_file(raw.parent / "manifest.json"),
            "curated_file": str(
                output.relative_to(channel_interaction_dataset_root().resolve(strict=False))
            ),
            "curated_sha256": sha256_file(staging_output),
            "row_count": len(rows),
            "build_config": {
                "outcome_horizon_slots": config.outcome_horizon_slots,
                "pre_touch_wait_slots": config.pre_touch_wait_slots,
                "breakout_atr": config.breakout_atr,
                "reversal_atr": config.reversal_atr,
                "acceptance_atr": config.acceptance_atr,
                "acceptance_closes": config.acceptance_closes,
            },
            "report": dict(report),
        }
        (staging / manifest_output.name).write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        staging.rename(publication_dir)
        return manifest
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def read_curated_dataset(path: Path) -> list[dict[str, Any]]:
    source = require_channel_interaction_data_path(path, kind="dataset")
    with source.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != CHANNEL_INTERACTION_DATASET_FIELDS:
            raise ValueError("curated dataset columns do not match the canonical contract")
        rows = list(reader)
    for row in rows:
        if row["dataset_contract"] != CHANNEL_INTERACTION_DATASET_CONTRACT:
            raise ValueError("dataset contract mismatch")
        if int(row["feature_schema_version"]) != CHANNEL_INTERACTION_FEATURE_SCHEMA_VERSION:
            raise ValueError("dataset feature schema mismatch")
        if row["label"] not in CHANNEL_INTERACTION_LABELS:
            raise ValueError("dataset label mismatch")
        if row["phase"] not in CHANNEL_INTERACTION_PHASES:
            raise ValueError("dataset phase mismatch")
        if row["target_contract"] != CHANNEL_INTERACTION_TARGET_CONTRACT:
            raise ValueError("dataset target contract mismatch")
        if row["level_slot_timeframe"] != CHANNEL_INTERACTION_LEVEL_SLOT_TIMEFRAME:
            raise ValueError("dataset level slot timeframe mismatch")
        _required_text(row, "provider")
        _required_text(row, "instrument_id")
        _required_text(row, "route_fingerprint")
        _required_text(row, "parent_timeframe")
        session_segment = _required_text(row, "session_segment")
        if session_segment not in {
            "liquid",
            "extended",
            "continuous",
            "unknown",
        }:
            raise ValueError("dataset session segment mismatch")
        if session_segment != "unknown":
            _required_text(row, "session_key")
        _required_text(row, "session_calendar")
        _required_text(row, "session_timezone")
        decision_ts = _parse_timestamp(row["decision_ts"], field_name="decision_ts")
        outcome_end_ts = _parse_timestamp(
            row["outcome_end_ts"],
            field_name="outcome_end_ts",
        )
        if outcome_end_ts <= decision_ts:
            raise ValueError("dataset outcome must end after its decision")
        decision_slot = int(row["decision_slot"])
        target_start_slot = int(row["target_start_slot"])
        target_end_slot = int(row["target_end_slot"])
        if target_start_slot <= decision_slot or target_end_slot < target_start_slot:
            raise ValueError("dataset target slot interval is invalid")
        if row["phase"] == "post_touch" and (
            target_start_slot != decision_slot + 1 or row["label"] == "no_touch"
        ):
            raise ValueError("post_touch row violates its rolling future target")
        for column in CHANNEL_INTERACTION_FEATURE_COLUMNS:
            value = float(row[column])
            if not isfinite(value):
                raise ValueError("dataset contains non-finite features")
    return rows


def inference_scopes_from_rows(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[ChannelInteractionInferenceScope, ...]:
    scopes = {
        ChannelInteractionInferenceScope(
            provider=_required_text(row, "provider"),
            instrument_id=_required_text(row, "instrument_id"),
            parent_timeframe=_required_text(row, "parent_timeframe"),
        )
        for row in rows
    }
    if not scopes:
        raise ValueError("dataset does not contain an inference scope")
    return tuple(sorted(scopes))


def verify_dataset_manifest(*, dataset_path: Path, manifest_path: Path) -> dict[str, Any]:
    dataset = require_channel_interaction_data_path(dataset_path, kind="dataset")
    manifest_file = require_channel_interaction_data_path(manifest_path, kind="dataset")
    payload = json.loads(manifest_file.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("dataset manifest must be an object")
    if payload.get("immutable") is not True:
        raise ValueError("training dataset manifest must be immutable")
    if payload.get("dataset_contract") != CHANNEL_INTERACTION_DATASET_CONTRACT:
        raise ValueError("dataset manifest contract mismatch")
    if payload.get("feature_columns") != list(CHANNEL_INTERACTION_FEATURE_COLUMNS):
        raise ValueError("dataset manifest feature columns mismatch")
    if payload.get("labels") != list(CHANNEL_INTERACTION_LABELS):
        raise ValueError("dataset manifest labels mismatch")
    if payload.get("phases") != list(CHANNEL_INTERACTION_PHASES):
        raise ValueError("dataset manifest phases mismatch")
    target_definition = normalize_channel_interaction_target_definition(
        payload.get("target_definition")
    )
    build_config = payload.get("build_config")
    if not isinstance(build_config, Mapping) or set(build_config) != {
        "outcome_horizon_slots",
        "pre_touch_wait_slots",
        "breakout_atr",
        "reversal_atr",
        "acceptance_atr",
        "acceptance_closes",
    }:
        raise ValueError("dataset manifest build config is invalid")
    verified_config = ChannelInteractionDatasetBuildConfig(
        outcome_horizon_slots=int(build_config["outcome_horizon_slots"]),
        pre_touch_wait_slots=int(build_config["pre_touch_wait_slots"]),
        breakout_atr=float(build_config["breakout_atr"]),
        reversal_atr=float(build_config["reversal_atr"]),
        acceptance_atr=float(build_config["acceptance_atr"]),
        acceptance_closes=int(build_config["acceptance_closes"]),
    )
    expected_target = channel_interaction_target_definition(verified_config)
    if any(
        dict(target_definition["by_phase"][phase]) != dict(expected_target["by_phase"][phase])
        for phase in CHANNEL_INTERACTION_PHASES
    ):
        raise ValueError("dataset target definition disagrees with build config")
    manifest_scopes = normalize_channel_interaction_inference_scopes(payload.get("inference_scope"))
    normalize_channel_interaction_build_provenance(
        payload.get("build_provenance"),
        require_xgboost=False,
    )
    if manifest_file.parent != dataset.parent:
        raise ValueError("training dataset and manifest must share one immutable run directory")
    expected_relative = str(
        dataset.relative_to(channel_interaction_dataset_root().resolve(strict=False))
    )
    if payload.get("curated_file") != expected_relative:
        raise ValueError("dataset manifest curated path mismatch")
    if payload.get("curated_sha256") != sha256_file(dataset):
        raise ValueError("curated dataset SHA-256 mismatch")
    dataset_rows = read_curated_dataset(dataset)
    for row in dataset_rows:
        _validate_dataset_target_interval(row, verified_config)
    if manifest_scopes != inference_scopes_from_rows(dataset_rows):
        raise ValueError("dataset manifest inference scope mismatch")
    return payload


def chronological_episode_splits(
    rows: Sequence[Mapping[str, Any]],
    *,
    fractions: tuple[float, float, float, float] = (0.55, 0.15, 0.15, 0.15),
    purge_minutes: int = 15,
    embargo_minutes: int = 5,
) -> tuple[dict[str, list[Mapping[str, Any]]], dict[str, Any]]:
    if len(fractions) != 4 or any(value <= 0 for value in fractions):
        raise ValueError("four positive split fractions are required")
    total_fraction = sum(fractions)
    normalized = tuple(value / total_fraction for value in fractions)
    groups: dict[tuple[str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        group_key = (
            _required_text(row, "instrument_id"),
            _required_text(row, "route_fingerprint"),
            _required_text(row, "episode_id"),
        )
        groups[group_key].append(row)
    if len(groups) < 8:
        raise ValueError("at least eight independent episodes are required")

    group_meta: list[tuple[tuple[str, str, str], datetime, datetime]] = []
    for group_key, episode_rows in groups.items():
        starts = [
            _parse_timestamp(row["decision_ts"], field_name="decision_ts") for row in episode_rows
        ]
        ends = [
            _parse_timestamp(row["outcome_end_ts"], field_name="outcome_end_ts")
            for row in episode_rows
        ]
        group_meta.append((group_key, min(starts), max(ends)))
    group_meta.sort(key=lambda item: (item[1], item[0]))

    count = len(group_meta)
    cut_1 = max(1, int(count * normalized[0]))
    cut_2 = max(cut_1 + 1, int(count * (normalized[0] + normalized[1])))
    cut_3 = max(cut_2 + 1, int(count * sum(normalized[:3])))
    cut_3 = min(cut_3, count - 1)
    cut_2 = min(cut_2, cut_3 - 1)
    cut_1 = min(cut_1, cut_2 - 1)
    blocks = [
        group_meta[:cut_1],
        group_meta[cut_1:cut_2],
        group_meta[cut_2:cut_3],
        group_meta[cut_3:],
    ]
    removed = Counter()
    purge = timedelta(minutes=max(purge_minutes, 0))
    embargo = timedelta(minutes=max(embargo_minutes, 0))
    for index in range(3):
        left = blocks[index]
        right = blocks[index + 1]
        if not left or not right:
            raise ValueError("chronological split is empty before purge")
        right_start = min(item[1] for item in right)
        kept_left = [item for item in left if item[2] < right_start - purge]
        removed[f"{SPLIT_NAMES[index]}_purged_episodes"] += len(left) - len(kept_left)
        blocks[index] = kept_left
        left_boundary = max(item[2] for item in left)
        kept_right = [item for item in right if item[1] > left_boundary + embargo]
        removed[f"{SPLIT_NAMES[index + 1]}_embargoed_episodes"] += len(right) - len(kept_right)
        blocks[index + 1] = kept_right

    if any(not block for block in blocks):
        raise ValueError("purge/embargo left an empty chronological split")
    output: dict[str, list[Mapping[str, Any]]] = {}
    intervals: dict[str, dict[str, str]] = {}
    for split_name, block in zip(SPLIT_NAMES, blocks, strict=True):
        group_keys = {item[0] for item in block}
        split_rows = [row for group_key in group_keys for row in groups[group_key]]
        split_rows.sort(
            key=lambda row: (
                _parse_timestamp(row["decision_ts"], field_name="decision_ts"),
                row["instrument_id"],
                row["route_fingerprint"],
                row["episode_id"],
            )
        )
        output[split_name] = split_rows
        intervals[split_name] = {
            "start": min(item[1] for item in block).isoformat(),
            "end": max(item[2] for item in block).isoformat(),
        }
    report = {
        "episode_counts": {
            name: len(
                {
                    (row["instrument_id"], row["route_fingerprint"], row["episode_id"])
                    for row in output[name]
                }
            )
            for name in SPLIT_NAMES
        },
        "row_counts": {name: len(output[name]) for name in SPLIT_NAMES},
        "intervals": intervals,
        "purge_minutes": max(purge_minutes, 0),
        "embargo_minutes": max(embargo_minutes, 0),
        "removed": dict(sorted(removed.items())),
    }
    return output, report


def rows_to_matrix(rows: Sequence[Mapping[str, Any]]) -> tuple[np.ndarray, np.ndarray]:
    if not rows:
        raise ValueError("dataset split is empty")
    matrix = np.asarray(
        [[float(row[column]) for column in CHANNEL_INTERACTION_FEATURE_COLUMNS] for row in rows],
        dtype=np.float64,
    )
    labels = np.asarray(
        [CHANNEL_INTERACTION_LABELS.index(str(row["label"])) for row in rows], dtype=np.int64
    )
    if not np.all(np.isfinite(matrix)):
        raise ValueError("dataset split contains non-finite features")
    return matrix, labels


def _softmax(logits: np.ndarray) -> np.ndarray:
    shifted = logits - np.max(logits, axis=1, keepdims=True)
    exponentials = np.exp(shifted)
    return exponentials / np.sum(exponentials, axis=1, keepdims=True)


def _phase_adjusted_logits(
    logits: np.ndarray,
    phase: str | None,
) -> np.ndarray:
    adjusted = np.asarray(logits, dtype=np.float64)
    if adjusted.ndim != 2 or adjusted.shape[1] != len(CHANNEL_INTERACTION_LABELS):
        raise ValueError("logits do not match the channel interaction label contract")
    if phase is None:
        return adjusted
    if phase not in CHANNEL_INTERACTION_PHASES:
        raise ValueError("phase is not supported")
    adjusted = adjusted.copy()
    for index, label in enumerate(CHANNEL_INTERACTION_LABELS):
        if label not in CHANNEL_INTERACTION_LABELS_BY_PHASE[phase]:
            adjusted[:, index] = -np.inf
    return adjusted


def _negative_log_likelihood(
    logits: np.ndarray,
    labels: np.ndarray,
    temperature: float,
    *,
    phase: str | None = None,
) -> float:
    probabilities = _softmax(_phase_adjusted_logits(logits, phase) / temperature)
    selected = probabilities[np.arange(len(labels)), labels]
    return float(-np.mean(np.log(np.clip(selected, 1e-12, 1.0))))


def fit_positive_temperature(
    logits: np.ndarray,
    labels: np.ndarray,
    *,
    phase: str | None = None,
) -> float:
    if logits.ndim != 2 or logits.shape[0] != labels.shape[0] or not len(labels):
        raise ValueError("calibration logits and labels are incompatible")
    if phase is not None:
        if phase not in CHANNEL_INTERACTION_PHASES:
            raise ValueError("phase is not supported")
        allowed_indexes = {
            CHANNEL_INTERACTION_LABELS.index(label)
            for label in CHANNEL_INTERACTION_LABELS_BY_PHASE[phase]
        }
        if any(int(label) not in allowed_indexes for label in labels):
            raise ValueError("calibration labels do not belong to the requested phase")
    left = log(0.05)
    right = log(20.0)
    ratio = (5.0**0.5 - 1.0) / 2.0
    x1 = right - ratio * (right - left)
    x2 = left + ratio * (right - left)
    for _ in range(80):
        loss_1 = _negative_log_likelihood(logits, labels, exp(x1), phase=phase)
        loss_2 = _negative_log_likelihood(logits, labels, exp(x2), phase=phase)
        if loss_1 <= loss_2:
            right = x2
            x2 = x1
            x1 = right - ratio * (right - left)
        else:
            left = x1
            x1 = x2
            x2 = left + ratio * (right - left)
    temperature = exp((left + right) / 2.0)
    if not isfinite(temperature) or temperature <= 0:
        raise ValueError("temperature calibration failed")
    return float(temperature)


def independent_phase_label_episode_support(
    rows: Sequence[Mapping[str, Any]],
    *,
    phase: str,
    split_name: str,
    minimum_episodes_per_class: int,
) -> dict[str, Any]:
    if phase not in CHANNEL_INTERACTION_PHASES:
        raise ValueError("episode support phase is not supported")
    if split_name not in {"calibration", "locked_test"}:
        raise ValueError("episode support split is not supported")
    if (
        isinstance(minimum_episodes_per_class, bool)
        or not isinstance(minimum_episodes_per_class, int)
        or minimum_episodes_per_class < 1
    ):
        raise ValueError("minimum_episodes_per_class must be positive")
    episodes_by_class: dict[str, set[tuple[str, str, str]]] = {
        label: set() for label in CHANNEL_INTERACTION_LABELS_BY_PHASE[phase]
    }
    for row in rows:
        if row.get("phase") != phase:
            raise ValueError(f"{split_name} rows contain a different phase")
        label = str(row.get("label"))
        if label not in episodes_by_class:
            raise ValueError(f"{split_name} row label is invalid for its phase")
        episodes_by_class[label].add(
            (
                _required_text(row, "instrument_id"),
                _required_text(row, "route_fingerprint"),
                _required_text(row, "episode_id"),
            )
        )
    episode_counts = {
        label: len(episodes_by_class[label]) for label in CHANNEL_INTERACTION_LABELS_BY_PHASE[phase]
    }
    count_requirements = {
        label: episode_counts[label] >= minimum_episodes_per_class
        for label in CHANNEL_INTERACTION_LABELS_BY_PHASE[phase]
    }
    return {
        "passed": all(count_requirements.values()),
        "phase": phase,
        "split": split_name,
        "minimum_independent_episodes_per_class": minimum_episodes_per_class,
        "independent_episodes_per_class": episode_counts,
        "episode_count_requirements": count_requirements,
    }


def multiclass_metrics(
    logits: np.ndarray,
    labels: np.ndarray,
    *,
    temperature: float,
    reliability_bins: int = 10,
    decision_policy: Mapping[str, Any] | None = None,
    phase: str | None = None,
) -> dict[str, Any]:
    adjusted_logits = _phase_adjusted_logits(logits, phase)
    if phase is not None:
        allowed_indexes = {
            CHANNEL_INTERACTION_LABELS.index(label)
            for label in CHANNEL_INTERACTION_LABELS_BY_PHASE[phase]
        }
        if any(int(label) not in allowed_indexes for label in labels):
            raise ValueError("metric labels do not belong to the requested phase")
    probabilities = _softmax(adjusted_logits / temperature)
    predictions = np.argmax(probabilities, axis=1)
    one_hot = np.eye(len(CHANNEL_INTERACTION_LABELS), dtype=np.float64)[labels]
    confidence = np.max(probabilities, axis=1)
    correct = predictions == labels
    bin_rows: list[dict[str, Any]] = []
    ece = 0.0
    for index in range(reliability_bins):
        lower = index / reliability_bins
        upper = (index + 1) / reliability_bins
        mask = (confidence >= lower) & (
            confidence < upper if index + 1 < reliability_bins else confidence <= upper
        )
        count = int(np.sum(mask))
        if not count:
            continue
        bin_confidence = float(np.mean(confidence[mask]))
        bin_accuracy = float(np.mean(correct[mask]))
        ece += count / len(labels) * abs(bin_accuracy - bin_confidence)
        bin_rows.append(
            {
                "lower": lower,
                "upper": upper,
                "count": count,
                "mean_confidence": bin_confidence,
                "accuracy": bin_accuracy,
            }
        )
    recalls: dict[str, float | None] = {}
    for index, label in enumerate(CHANNEL_INTERACTION_LABELS):
        mask = labels == index
        recalls[label] = float(np.mean(predictions[mask] == index)) if np.any(mask) else None
    metrics: dict[str, Any] = {
        "rows": int(len(labels)),
        "accuracy": float(np.mean(correct)),
        "log_loss": _negative_log_likelihood(
            adjusted_logits,
            labels,
            temperature,
        ),
        "brier_multiclass": float(np.mean(np.sum((probabilities - one_hot) ** 2, axis=1))),
        "expected_calibration_error": float(ece),
        "per_class_recall": recalls,
        "reliability": bin_rows,
    }
    if decision_policy is not None:
        normalized_policy = normalize_channel_interaction_decision_policy(decision_policy)
        ordered = np.sort(probabilities, axis=1)
        top2_margin = ordered[:, -1] - ordered[:, -2]
        actionable_indexes = {
            CHANNEL_INTERACTION_LABELS.index("reversal"),
            CHANNEL_INTERACTION_LABELS.index("accepted_breakout"),
        }
        selected = (
            np.isin(predictions, list(actionable_indexes))
            & (confidence >= normalized_policy["min_confidence"])
            & (top2_margin >= normalized_policy["min_top2_margin"])
        )
        selected_rows = int(np.sum(selected))
        directional_precisions: dict[str, float | None] = {}
        for index in sorted(actionable_indexes):
            predicted_mask = selected & (predictions == index)
            directional_precisions[CHANNEL_INTERACTION_LABELS[index]] = (
                float(np.mean(labels[predicted_mask] == index)) if np.any(predicted_mask) else None
            )
        precision_values = tuple(
            value for value in directional_precisions.values() if value is not None
        )
        directional_macro_precision = (
            float(np.mean(np.asarray(precision_values, dtype=np.float64)))
            if len(precision_values) == len(actionable_indexes)
            else None
        )
        metrics["selective"] = {
            "decision_policy": dict(normalized_policy),
            "directional_rows": selected_rows,
            "coverage": selected_rows / len(labels),
            "abstention_rate": 1.0 - selected_rows / len(labels),
            "directional_accuracy": (
                float(np.mean(predictions[selected] == labels[selected])) if selected_rows else None
            ),
            "directional_macro_precision": directional_macro_precision,
            "per_direction_precision": directional_precisions,
        }
    return metrics


def locked_test_acceptance_gate(
    *,
    baseline_metrics: Mapping[str, Any],
    candidate_metrics: Mapping[str, Any],
    calibration_rows: Sequence[Mapping[str, Any]],
    locked_test_rows: Sequence[Mapping[str, Any]],
    phase: str,
    minimum_calibration_episodes_per_class: int,
    minimum_episodes_per_class: int,
    minimum_directional_coverage: float,
    minimum_directional_accuracy: float,
    minimum_directional_macro_precision: float,
) -> dict[str, Any]:
    if phase not in CHANNEL_INTERACTION_PHASES:
        raise ValueError("acceptance gate phase is not supported")
    thresholds = {
        "minimum_directional_coverage": float(minimum_directional_coverage),
        "minimum_directional_accuracy": float(minimum_directional_accuracy),
        "minimum_directional_macro_precision": float(minimum_directional_macro_precision),
    }
    if any(not isfinite(value) or value <= 0 or value > 1 for value in thresholds.values()):
        raise ValueError("selective thresholds must be finite values in (0, 1]")
    calibration_support = independent_phase_label_episode_support(
        calibration_rows,
        phase=phase,
        split_name="calibration",
        minimum_episodes_per_class=minimum_calibration_episodes_per_class,
    )
    locked_test_support = independent_phase_label_episode_support(
        locked_test_rows,
        phase=phase,
        split_name="locked_test",
        minimum_episodes_per_class=minimum_episodes_per_class,
    )
    metric_fields = (
        "log_loss",
        "brier_multiclass",
        "expected_calibration_error",
    )
    metric_requirements: dict[str, bool] = {}
    observed: dict[str, dict[str, float]] = {}
    for field in metric_fields:
        baseline_value = float(baseline_metrics[field])
        candidate_value = float(candidate_metrics[field])
        if not isfinite(baseline_value) or not isfinite(candidate_value):
            raise ValueError("acceptance metrics must be finite")
        metric_requirements[field] = (
            candidate_value < baseline_value
            if field == "log_loss"
            else candidate_value <= baseline_value
        )
        observed[field] = {
            "logistic_baseline": baseline_value,
            "xgboost_candidate": candidate_value,
        }
    selective = candidate_metrics.get("selective")
    if not isinstance(selective, Mapping):
        raise ValueError("candidate metrics are missing selective diagnostics")
    selective_observed: dict[str, float | None] = {}
    selective_requirements: dict[str, bool] = {}
    for metric_name, threshold_name in (
        ("coverage", "minimum_directional_coverage"),
        ("directional_accuracy", "minimum_directional_accuracy"),
        ("directional_macro_precision", "minimum_directional_macro_precision"),
    ):
        raw_value = selective.get(metric_name)
        value = (
            float(raw_value)
            if isinstance(raw_value, (int, float)) and not isinstance(raw_value, bool)
            else None
        )
        if value is not None and not isfinite(value):
            value = None
        selective_observed[metric_name] = value
        selective_requirements[metric_name] = bool(
            value is not None and value > 0 and value >= thresholds[threshold_name]
        )
    passed = (
        calibration_support["passed"]
        and locked_test_support["passed"]
        and all(metric_requirements.values())
        and all(selective_requirements.values())
    )
    return {
        "passed": passed,
        "phase": phase,
        "minimum_calibration_independent_episodes_per_class": (
            minimum_calibration_episodes_per_class
        ),
        "calibration_independent_episodes_per_class": calibration_support[
            "independent_episodes_per_class"
        ],
        "calibration_episode_count_requirements": calibration_support["episode_count_requirements"],
        "minimum_independent_episodes_per_class": minimum_episodes_per_class,
        "locked_test_independent_episodes_per_class": locked_test_support[
            "independent_episodes_per_class"
        ],
        "episode_count_requirements": locked_test_support["episode_count_requirements"],
        "metric_requirements": metric_requirements,
        "selective_thresholds": thresholds,
        "selective_requirements": selective_requirements,
        "selective_observed": selective_observed,
        "observed": observed,
    }


def train_logistic_baseline(
    train_matrix: np.ndarray,
    train_labels: np.ndarray,
    validation_matrix: np.ndarray,
    validation_labels: np.ndarray,
    *,
    epochs: int = 800,
    learning_rate: float = 0.04,
    l2: float = 0.001,
) -> LogisticBaseline:
    mean = np.mean(train_matrix, axis=0)
    scale = np.std(train_matrix, axis=0)
    scale = np.where(scale > 1e-9, scale, 1.0)
    x_train = (train_matrix - mean) / scale
    x_validation = (validation_matrix - mean) / scale
    if np.any(np.bincount(train_labels, minlength=len(CHANNEL_INTERACTION_LABELS)) == 0):
        raise ValueError("training split must contain every channel interaction label")
    weights = np.zeros((train_matrix.shape[1], len(CHANNEL_INTERACTION_LABELS)), dtype=np.float64)
    bias = np.zeros(len(CHANNEL_INTERACTION_LABELS), dtype=np.float64)
    one_hot = np.eye(len(CHANNEL_INTERACTION_LABELS), dtype=np.float64)[train_labels]
    best_weights = weights.copy()
    best_bias = bias.copy()
    best_loss = float("inf")
    stale = 0
    for epoch in range(max(epochs, 1)):
        probabilities = _softmax(x_train @ weights + bias)
        residual = probabilities - one_hot
        gradient_weights = x_train.T @ residual / len(train_labels) + l2 * weights
        gradient_bias = np.mean(residual, axis=0)
        rate = learning_rate / (1.0 + epoch / 400.0)
        weights -= rate * gradient_weights
        bias -= rate * gradient_bias
        validation_loss = _negative_log_likelihood(
            x_validation @ weights + bias,
            validation_labels,
            1.0,
        )
        if validation_loss + 1e-7 < best_loss:
            best_loss = validation_loss
            best_weights = weights.copy()
            best_bias = bias.copy()
            stale = 0
        else:
            stale += 1
            if stale >= 100:
                break
    return LogisticBaseline(mean=mean, scale=scale, weights=best_weights, bias=best_bias)


def train_xgboost_model(
    train_matrix: np.ndarray,
    train_labels: np.ndarray,
    validation_matrix: np.ndarray,
    validation_labels: np.ndarray,
    *,
    seed: int = 17,
    max_rounds: int = 800,
) -> tuple[Any, dict[str, Any]]:
    try:
        import xgboost as xgb
    except (ImportError, OSError) as exc:
        raise RuntimeError("xgboost backend is unavailable; install the ml extra") from exc
    if np.any(np.bincount(train_labels, minlength=len(CHANNEL_INTERACTION_LABELS)) == 0):
        raise ValueError("training split must contain every channel interaction label")
    dtrain = xgb.DMatrix(
        train_matrix,
        label=train_labels,
        feature_names=list(CHANNEL_INTERACTION_FEATURE_COLUMNS),
    )
    dvalidation = xgb.DMatrix(
        validation_matrix,
        label=validation_labels,
        feature_names=list(CHANNEL_INTERACTION_FEATURE_COLUMNS),
    )
    parameters = {
        "objective": "multi:softprob",
        "num_class": len(CHANNEL_INTERACTION_LABELS),
        "eval_metric": "mlogloss",
        "tree_method": "hist",
        "max_depth": 5,
        "min_child_weight": 8.0,
        "eta": 0.035,
        "subsample": 0.80,
        "colsample_bytree": 0.80,
        "lambda": 2.0,
        "alpha": 0.05,
        "seed": seed,
        "nthread": 1,
    }
    booster = xgb.train(
        parameters,
        dtrain,
        num_boost_round=max_rounds,
        evals=[(dtrain, "train"), (dvalidation, "validation")],
        early_stopping_rounds=60,
        verbose_eval=False,
    )
    best_iteration = int(getattr(booster, "best_iteration", max_rounds - 1))
    if best_iteration + 1 < int(booster.num_boosted_rounds()):
        booster = booster[: best_iteration + 1]
    return booster, {
        "parameters": parameters,
        "best_iteration": best_iteration,
        "xgboost_version": str(xgb.__version__),
    }


def xgboost_margins(booster: Any, matrix: np.ndarray) -> np.ndarray:
    raw = booster.inplace_predict(
        np.asarray(matrix, dtype=np.float32),
        predict_type="margin",
        strict_shape=True,
    )
    margins = np.asarray(raw, dtype=np.float64).reshape(
        len(matrix), len(CHANNEL_INTERACTION_LABELS)
    )
    if not np.all(np.isfinite(margins)):
        raise ValueError("XGBoost emitted non-finite margins")
    return margins


def publish_xgboost_artifact(
    booster: Any,
    *,
    output_dir: Path,
    run_id: str,
    phase_temperatures: Mapping[str, Any],
    metrics: Mapping[str, Any],
    training: Mapping[str, Any],
    acceptance_gate: Mapping[str, Any],
    decision_policy: Mapping[str, Any],
    inference_scopes: Sequence[ChannelInteractionInferenceScope],
    target_definition: Mapping[str, Any],
    build_provenance: Mapping[str, Any],
    dataset_manifest_path: Path,
    activate: bool = False,
) -> dict[str, Any]:
    normalized_temperatures = normalize_channel_interaction_phase_temperatures(phase_temperatures)
    gate_fields = {
        "passed",
        "statistical_passed",
        "clean_lineage_required",
        "clean_lineage_passed",
        "dataset_builder_git_dirty",
        "model_builder_git_dirty",
        "by_phase",
    }
    if (
        not isinstance(acceptance_gate, Mapping)
        or set(acceptance_gate) != gate_fields
        or not isinstance(acceptance_gate.get("passed"), bool)
        or not isinstance(acceptance_gate.get("by_phase"), Mapping)
        or set(acceptance_gate["by_phase"]) != set(CHANNEL_INTERACTION_PHASES)
        or any(
            not isinstance(acceptance_gate[field_name], bool)
            for field_name in gate_fields - {"by_phase"}
        )
        or any(
            not isinstance(acceptance_gate["by_phase"][phase], Mapping)
            or acceptance_gate["by_phase"][phase].get("phase") != phase
            or not isinstance(
                acceptance_gate["by_phase"][phase].get("passed"),
                bool,
            )
            for phase in CHANNEL_INTERACTION_PHASES
        )
    ):
        raise ValueError("artifact acceptance gate is not a complete per-phase report")
    if acceptance_gate["passed"] is True:
        normalized_acceptance_gate: Mapping[str, Any] = (
            normalize_channel_interaction_acceptance_gate(acceptance_gate)
        )
    else:
        if activate:
            raise ValueError("a rejected model run cannot become active")
        normalized_acceptance_gate = MappingProxyType(dict(acceptance_gate))
    normalized_policy = normalize_channel_interaction_decision_policy(decision_policy)
    normalized_provenance = normalize_channel_interaction_build_provenance(
        build_provenance,
        require_xgboost=True,
    )
    normalized_scopes = normalize_channel_interaction_inference_scopes(
        {
            "scope_contract": CHANNEL_INTERACTION_SCOPE_CONTRACT,
            "allowed": [scope.as_dict() for scope in inference_scopes],
        }
    )
    if len(normalized_scopes) != 1:
        raise ValueError(
            "channel interaction artifact v1 requires exactly one inference scope; "
            "split a multi-scope dataset before training"
        )
    normalized_target = normalize_channel_interaction_target_definition(target_definition)
    root = require_channel_interaction_data_path(output_dir, kind="model")
    normalized_run_id = require_channel_interaction_run_id(run_id)
    dataset_manifest = require_channel_interaction_data_path(dataset_manifest_path, kind="dataset")
    dataset_manifest_payload = json.loads(dataset_manifest.read_text(encoding="utf-8"))
    if not isinstance(dataset_manifest_payload, Mapping):
        raise ValueError("dataset manifest must be an object")
    dataset_target = normalize_channel_interaction_target_definition(
        dataset_manifest_payload.get("target_definition")
    )
    dataset_build_provenance = normalize_channel_interaction_build_provenance(
        dataset_manifest_payload.get("build_provenance"),
        require_xgboost=False,
    )
    if activate and (normalized_provenance["git_dirty"] or dataset_build_provenance["git_dirty"]):
        raise ValueError("activation requires clean dataset and model build provenance")
    if any(
        dict(dataset_target["by_phase"][phase]) != dict(normalized_target["by_phase"][phase])
        for phase in CHANNEL_INTERACTION_PHASES
    ):
        raise ValueError("artifact target definition disagrees with its dataset manifest")
    dataset_scopes = normalize_channel_interaction_inference_scopes(
        dataset_manifest_payload.get("inference_scope")
    )
    if dataset_scopes != normalized_scopes:
        raise ValueError("artifact inference scope disagrees with its dataset manifest")
    runs_root = root / "runs"
    runs_root.mkdir(parents=True, exist_ok=True)
    run_dir = runs_root / normalized_run_id
    if run_dir.exists():
        raise FileExistsError(f"model run already exists: {run_dir}")
    staging = runs_root / f".{normalized_run_id}.tmp-{os.getpid()}-{uuid.uuid4().hex}"
    staging.mkdir()
    try:
        model_path = staging / "model.ubj"
        booster.save_model(str(model_path))
        model_sha256 = sha256_file(model_path)
        manifest = {
            "artifact_contract": CHANNEL_INTERACTION_ARTIFACT_CONTRACT,
            "backend": "xgboost",
            "feature_schema_version": CHANNEL_INTERACTION_FEATURE_SCHEMA_VERSION,
            "feature_columns": list(CHANNEL_INTERACTION_FEATURE_COLUMNS),
            "labels": list(CHANNEL_INTERACTION_LABELS),
            "phases": list(CHANNEL_INTERACTION_PHASES),
            "model_file": "model.ubj",
            "model_sha256": model_sha256,
            "run_id": normalized_run_id,
            "phase_temperatures": dict(normalized_temperatures),
            "trained_at": datetime.now(tz=UTC).isoformat(),
            "dataset_manifest_file": str(
                dataset_manifest.relative_to(
                    channel_interaction_dataset_root().resolve(strict=False)
                )
            ),
            "dataset_manifest_sha256": sha256_file(dataset_manifest),
            "metrics": dict(metrics),
            "training": dict(training),
            "acceptance_gate": dict(normalized_acceptance_gate),
            "decision_policy": dict(normalized_policy),
            "target_definition": {
                "target_contract": normalized_target["target_contract"],
                "level_slot_timeframe": normalized_target["level_slot_timeframe"],
                "by_phase": {
                    phase: dict(normalized_target["by_phase"][phase])
                    for phase in CHANNEL_INTERACTION_PHASES
                },
            },
            "inference_scope": {
                "scope_contract": CHANNEL_INTERACTION_SCOPE_CONTRACT,
                "allowed": [scope.as_dict() for scope in normalized_scopes],
            },
            "build_provenance": dict(normalized_provenance),
            "dataset_build_provenance": dict(dataset_build_provenance),
        }
        manifest_path = staging / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        staging.rename(run_dir)
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    if activate:
        active_payload = {
            "active_contract": CHANNEL_INTERACTION_ACTIVE_CONTRACT,
            "run_id": normalized_run_id,
            "manifest_sha256": sha256_file(run_dir / "manifest.json"),
            "activated_at": datetime.now(tz=UTC).isoformat(),
        }
        temporary_active = root / f".active.{os.getpid()}.{uuid.uuid4().hex}.json"
        temporary_active.write_text(
            json.dumps(active_payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary_active, root / "active.json")
    return manifest
