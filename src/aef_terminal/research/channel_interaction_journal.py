"""Prospective, append-only capture for Channel Master ML observations.

The live indicator owns the typed observation.  This module owns validation,
exact snapshot identity attachment, durable raw capture, and offline compaction.
It deliberately has no application-storage dependency. Capture runs inside the
isolated persistent analysis worker before its internal observation is removed from the
cache/browser response.

All artifacts are below ``AppConfig.data_root / datasets / channel_interactions``.
There is no repository-local fallback.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shutil
import uuid
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from aef_terminal.data.instrument_identity import require_exact_identity_text
from aef_terminal.data.providers import provider_key_for_source
from aef_terminal.domain import (
    BarProvenance,
    BarState,
    bar_revision_signature,
    domain_frozen_value,
    domain_wire_value,
)
from aef_terminal.runtime.math_utils import exact_finite_number_or_none
from aef_terminal.research.journal_utils import (
    canonical_json_bytes,
    dataset_root,
    fsync_directory,
    publish_create_once,
)
from aef_terminal.ml.channel_interaction import (
    CHANNEL_INTERACTION_FEATURE_SCHEMA_VERSION,
    CHANNEL_INTERACTION_PHASES,
    FrozenChannelLevel,
)


CHANNEL_INTERACTION_OBSERVATION_SCHEMA_VERSION = 1
CHANNEL_INTERACTION_JOURNAL_SCHEMA_VERSION = 1
CHANNEL_INTERACTION_DATASET_SCHEMA_VERSION = 1

_DATASET_RELATIVE_ROOT = Path("datasets") / "channel_interactions"
_RAW_SCHEMA_RELATIVE_ROOT = Path("raw") / "schema-v1"
_CURATED_RELATIVE_ROOT = Path("curated")
_CHANNEL_INDICATOR_ID = "channel_master"
_RAW_DATE_RE = re.compile(r"date=(\d{4}-\d{2}-\d{2})\Z")
_DATASET_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,95}\Z")
_HEX_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_MAX_MICRO_WINDOW_BARS = 256
_MAX_SNAPSHOT_OBSERVATIONS = 512
_MAX_RAW_RECORD_BYTES = 4 * 1024 * 1024


class ChannelInteractionJournalError(ValueError):
    """The snapshot observation violates the research-capture contract."""


class ChannelInteractionJournalConflict(ChannelInteractionJournalError):
    """One natural observation identity was emitted with different content."""

    def __init__(self, record_id: str, conflict_path: Path) -> None:
        super().__init__(
            f"channel interaction record {record_id} has conflicting content; "
            f"preserved at {conflict_path}"
        )
        self.record_id = record_id
        self.conflict_path = conflict_path


@dataclass(frozen=True, slots=True)
class JournalWriteResult:
    record_id: str
    content_sha256: str
    status: Literal["written", "duplicate"]
    path: Path


@dataclass(frozen=True, slots=True)
class _ValidatedObservation:
    value: Mapping[str, Any]
    decision_at: datetime
    record_id: str
    content_sha256: str


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _aware_utc(value: object, *, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ChannelInteractionJournalError(f"{field} must be a non-empty ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ChannelInteractionJournalError(f"{field} must be a valid ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ChannelInteractionJournalError(f"{field} must be timezone-aware")
    return parsed.astimezone(UTC)


def _exact_int(value: object, *, field: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ChannelInteractionJournalError(
            f"{field} must be an exact integer greater than or equal to {minimum}"
        )
    return value


def _finite_number(value: object, *, field: str) -> float:
    number = exact_finite_number_or_none(value)
    if number is None:
        raise ChannelInteractionJournalError(f"{field} must be an exact finite number")
    return number


def _required_mapping(value: object, *, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ChannelInteractionJournalError(f"{field} must be a typed mapping")
    if any(not isinstance(key, str) or not key for key in value):
        raise ChannelInteractionJournalError(f"{field} keys must be non-empty strings")
    return value


def _validate_features(value: object) -> dict[str, float | int | None]:
    features = _required_mapping(value, field="features")
    if not features:
        raise ChannelInteractionJournalError("features must not be empty")
    normalized: dict[str, float | int | None] = {}
    for key, item in features.items():
        if item is None:
            normalized[key] = None
            continue
        _finite_number(item, field=f"features.{key}")
        normalized[key] = item
    return normalized


def _validated_wire_bar(
    value: object,
    *,
    field: str,
    decision_at: datetime,
    identity: Mapping[str, Any],
) -> tuple[
    dict[str, Any],
    tuple[datetime, float, float, float, float, float, str, str],
]:
    raw = _required_mapping(value, field=field)
    try:
        source_provider = provider_key_for_source(
            require_exact_identity_text(raw.get("source"), field=f"{field}.source")
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ChannelInteractionJournalError(
            f"{field}.source does not identify a supported provider"
        ) from exc
    expected_provider = require_exact_identity_text(identity.get("provider"), field="provider")
    if source_provider != expected_provider:
        raise ChannelInteractionJournalError(
            f"{field}.source provider disagrees with snapshot identity"
        )
    provenance_raw = _required_mapping(raw.get("provenance"), field=f"{field}.provenance")
    try:
        provenance = BarProvenance(
            provider=require_exact_identity_text(
                provenance_raw.get("provider"), field=f"{field}.provenance.provider"
            ),
            instrument_id=require_exact_identity_text(
                provenance_raw.get("instrument_id"),
                field=f"{field}.provenance.instrument_id",
            ),
            route_fingerprint=require_exact_identity_text(
                provenance_raw.get("route_fingerprint"),
                field=f"{field}.provenance.route_fingerprint",
            ),
            request_type=require_exact_identity_text(
                provenance_raw.get("request_type"),
                field=f"{field}.provenance.request_type",
            ),
            provider_contract_id=str(provenance_raw.get("provider_contract_id") or ""),
            provider_contract_type=require_exact_identity_text(
                provenance_raw.get("provider_contract_type"),
                field=f"{field}.provenance.provider_contract_type",
            ),
            data_type=require_exact_identity_text(
                provenance_raw.get("data_type"),
                field=f"{field}.provenance.data_type",
            ),
            source_timeframe=(
                require_exact_identity_text(
                    provenance_raw.get("source_timeframe"),
                    field=f"{field}.provenance.source_timeframe",
                )
                if provenance_raw.get("source_timeframe") is not None
                else None
            ),
        )
    except (TypeError, ValueError) as exc:
        raise ChannelInteractionJournalError(f"{field}.provenance is invalid") from exc
    if (
        provenance.provider != expected_provider
        or provenance.instrument_id != identity.get("instrument_id")
        or provenance.route_fingerprint != identity.get("route_fingerprint")
    ):
        raise ChannelInteractionJournalError(f"{field}.provenance disagrees with snapshot identity")
    if raw.get("timeframe") != "1m":
        raise ChannelInteractionJournalError(f"{field}.timeframe must equal 1m")
    if raw.get("closed") is not True or raw.get("state") != BarState.CONFIRMED.value:
        raise ChannelInteractionJournalError(
            f"{field} must explicitly carry closed=true and state=confirmed"
        )
    if (
        raw.get("missing") is True
        or raw.get("data_gap") is True
        or raw.get("fill_forward") is True
        or raw.get("authoritative") is False
        or raw.get("preview_kind") is not None
    ):
        raise ChannelInteractionJournalError(
            f"{field} must not be a provisional or display-only bar"
        )
    try:
        signature = bar_revision_signature(raw)
    except (TypeError, ValueError) as exc:
        raise ChannelInteractionJournalError(f"{field} is not a valid confirmed Bar") from exc
    timestamp, open_price, high, low, close, volume, state, source = signature
    if state != BarState.CONFIRMED.value:
        raise ChannelInteractionJournalError(f"{field} must be provider-confirmed")
    if timestamp + timedelta(minutes=1) > decision_at:
        raise ChannelInteractionJournalError(f"{field} closes after decision_ts")
    typed = domain_wire_value(raw, field_name=field)
    if not isinstance(typed, dict):
        raise ChannelInteractionJournalError(f"{field} must serialize to a mapping")
    typed.update(
        {
            "ts": timestamp.isoformat(),
            "open": open_price,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "timeframe": "1m",
            "source": source,
            "closed": True,
            "state": BarState.CONFIRMED.value,
            "provenance": {
                "provider": provenance.provider,
                "instrument_id": provenance.instrument_id,
                "route_fingerprint": provenance.route_fingerprint,
                "request_type": provenance.request_type.value,
                "provider_contract_id": provenance.provider_contract_id,
                "provider_contract_type": provenance.provider_contract_type,
                "data_type": provenance.data_type,
                "source_timeframe": provenance.source_timeframe,
            },
        }
    )
    return typed, signature


def _same_bar_facts(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    fields = (
        "ts",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "timeframe",
        "source",
        "closed",
        "state",
    )
    return all(left.get(field) == right.get(field) for field in fields)


def _snapshot_observations(
    snapshot: Mapping[str, Any],
) -> tuple[Mapping[str, Any], ...]:
    indicators = snapshot.get("indicators")
    if not isinstance(indicators, Mapping):
        return ()
    channel = indicators.get(_CHANNEL_INDICATOR_ID)
    if not isinstance(channel, Mapping):
        return ()
    raw_observations = channel.get("research_observations")
    if raw_observations is None:
        return ()
    if isinstance(raw_observations, (str, bytes, bytearray)) or not isinstance(
        raw_observations, Sequence
    ):
        raise ChannelInteractionJournalError("research_observations must be a typed sequence")
    if len(raw_observations) > _MAX_SNAPSHOT_OBSERVATIONS:
        raise ChannelInteractionJournalError(
            f"research_observations cannot exceed {_MAX_SNAPSHOT_OBSERVATIONS} rows"
        )
    return tuple(
        _required_mapping(
            observation,
            field=f"research_observations[{index}]",
        )
        for index, observation in enumerate(raw_observations)
    )


def _snapshot_identity(snapshot: Mapping[str, Any]) -> dict[str, str]:
    meta = _required_mapping(snapshot.get("meta"), field="snapshot.meta")
    identity = {
        "instrument_id": require_exact_identity_text(
            meta.get("instrument_id"), field="instrument_id"
        ),
        "route_fingerprint": require_exact_identity_text(
            meta.get("route_fingerprint"), field="route_fingerprint"
        ),
        "provider": require_exact_identity_text(meta.get("provider"), field="provider"),
        "provider_contract_id": require_exact_identity_text(
            meta.get("provider_contract_id"), field="provider_contract_id"
        ),
        "parent_timeframe": require_exact_identity_text(
            meta.get("timeframe"), field="parent_timeframe"
        ),
    }
    provider_symbol = meta.get("provider_symbol")
    if provider_symbol is not None:
        identity["provider_symbol"] = require_exact_identity_text(
            provider_symbol, field="provider_symbol"
        )
    return identity


def _normalize_observation(
    observation: Mapping[str, Any],
    *,
    identity: Mapping[str, Any],
) -> _ValidatedObservation:
    if observation.get("schema_version") != CHANNEL_INTERACTION_OBSERVATION_SCHEMA_VERSION:
        raise ChannelInteractionJournalError("research_observation.schema_version must equal 1")
    if "identity" in observation:
        raise ChannelInteractionJournalError(
            "indicator research_observation must not carry route identity; snapshot.meta owns it"
        )
    if "symbol" in observation or "data_symbol" in observation:
        raise ChannelInteractionJournalError(
            "research_observation must not carry generic symbol aliases"
        )
    sample_eligible = observation.get("sample_eligible")
    if not isinstance(sample_eligible, bool):
        raise ChannelInteractionJournalError(
            "research_observation.sample_eligible must be a boolean"
        )
    sample_reason_code = require_exact_identity_text(
        observation.get("sample_reason_code"), field="sample_reason_code"
    )

    exact_identity = {
        "instrument_id": require_exact_identity_text(
            identity.get("instrument_id"), field="instrument_id"
        ),
        "route_fingerprint": require_exact_identity_text(
            identity.get("route_fingerprint"), field="route_fingerprint"
        ),
        "provider": require_exact_identity_text(identity.get("provider"), field="provider"),
        "provider_contract_id": require_exact_identity_text(
            identity.get("provider_contract_id"), field="provider_contract_id"
        ),
        "parent_timeframe": require_exact_identity_text(
            identity.get("parent_timeframe"), field="parent_timeframe"
        ),
    }
    provider_symbol = identity.get("provider_symbol")
    if provider_symbol is not None:
        exact_identity["provider_symbol"] = require_exact_identity_text(
            provider_symbol, field="provider_symbol"
        )

    episode_id = require_exact_identity_text(observation.get("episode_id"), field="episode_id")
    phase = require_exact_identity_text(observation.get("phase"), field="phase")
    if phase not in CHANNEL_INTERACTION_PHASES:
        raise ChannelInteractionJournalError("phase must be pre_touch or post_touch")
    decision_at = _aware_utc(observation.get("decision_ts"), field="decision_ts")
    atr_value = observation.get("atr_value")
    if atr_value is not None and _finite_number(atr_value, field="atr_value") <= 0:
        raise ChannelInteractionJournalError("atr_value must be positive or null")
    if "touch_slot" not in observation:
        raise ChannelInteractionJournalError("touch_slot is required")
    touch_slot = observation.get("touch_slot")
    if phase == "pre_touch":
        if touch_slot is not None:
            raise ChannelInteractionJournalError("pre_touch observations must have touch_slot=null")
    else:
        touch_slot = _exact_int(touch_slot, field="touch_slot")

    frozen_raw = _required_mapping(observation.get("frozen_level"), field="frozen_level")
    try:
        frozen_level = FrozenChannelLevel(
            anchor_slot=_exact_int(frozen_raw.get("anchor_slot"), field="frozen_level.anchor_slot"),
            anchor_price=_finite_number(
                frozen_raw.get("anchor_price"), field="frozen_level.anchor_price"
            ),
            slope_per_slot=_finite_number(
                frozen_raw.get("slope_per_slot"), field="frozen_level.slope_per_slot"
            ),
            slot_timeframe=require_exact_identity_text(
                frozen_raw.get("slot_timeframe"),
                field="frozen_level.slot_timeframe",
            ),
            side=require_exact_identity_text(frozen_raw.get("side"), field="frozen_level.side"),
            frozen_revision=require_exact_identity_text(
                frozen_raw.get("frozen_revision"), field="frozen_level.frozen_revision"
            ),
            frozen_at=_aware_utc(frozen_raw.get("frozen_at"), field="frozen_level.frozen_at"),
        )
    except (TypeError, ValueError) as exc:
        raise ChannelInteractionJournalError("frozen_level is invalid") from exc
    if frozen_level.frozen_at > decision_at:
        raise ChannelInteractionJournalError("frozen_level must not come from the future")

    micro_window = _required_mapping(observation.get("micro_window"), field="micro_window")
    raw_bars = micro_window.get("bars")
    raw_slots = micro_window.get("bar_slots")
    if (
        isinstance(raw_bars, (str, bytes, bytearray))
        or not isinstance(raw_bars, Sequence)
        or isinstance(raw_slots, (str, bytes, bytearray))
        or not isinstance(raw_slots, Sequence)
    ):
        raise ChannelInteractionJournalError(
            "micro_window.bars and micro_window.bar_slots must be typed sequences"
        )
    if not raw_bars or len(raw_bars) != len(raw_slots):
        raise ChannelInteractionJournalError(
            "micro_window must contain equally sized non-empty bars and bar_slots"
        )
    if len(raw_bars) > _MAX_MICRO_WINDOW_BARS:
        raise ChannelInteractionJournalError(
            f"micro_window cannot exceed {_MAX_MICRO_WINDOW_BARS} bars"
        )
    normalized_bars: list[dict[str, Any]] = []
    bar_signatures: list[tuple[datetime, float, float, float, float, float, str, str]] = []
    slots: list[int] = []
    for index, (raw_bar, raw_slot) in enumerate(zip(raw_bars, raw_slots, strict=True)):
        slot = _exact_int(raw_slot, field=f"micro_window.bar_slots[{index}]")
        normalized_bar, signature = _validated_wire_bar(
            raw_bar,
            field=f"micro_window.bars[{index}]",
            decision_at=decision_at,
            identity=exact_identity,
        )
        carried_slot = normalized_bar.get("bar_slot")
        if carried_slot is not None and carried_slot != slot:
            raise ChannelInteractionJournalError(
                f"micro_window.bars[{index}].bar_slot disagrees with bar_slots"
            )
        if bar_signatures and signature[0] <= bar_signatures[-1][0]:
            raise ChannelInteractionJournalError(
                "micro_window bar timestamps must be strictly increasing"
            )
        if slots and slot <= slots[-1]:
            raise ChannelInteractionJournalError(
                "micro_window bar slots must be strictly increasing"
            )
        normalized_bars.append(normalized_bar)
        bar_signatures.append(signature)
        slots.append(slot)

    if phase == "post_touch":
        try:
            touch_index = slots.index(int(touch_slot))
        except ValueError as exc:
            raise ChannelInteractionJournalError(
                "post_touch touch_slot must be present in micro_window.bar_slots"
            ) from exc
        touch_bar = bar_signatures[touch_index]
        touch_price = frozen_level.price_at(slots[touch_index])
        if not (touch_bar[3] <= touch_price <= touch_bar[2]):
            raise ChannelInteractionJournalError(
                "post_touch touch_slot bar does not intersect frozen_level"
            )

    redundant_decision = observation.get("decision")
    if redundant_decision is not None:
        decision = _required_mapping(redundant_decision, field="decision")
        if (
            decision.get("phase") != phase
            or decision.get("timeframe") != "1m"
            or decision.get("confirmed") is not True
            or _exact_int(decision.get("slot"), field="decision.slot") != slots[-1]
            or _aware_utc(decision.get("as_of_ts"), field="decision.as_of_ts") != decision_at
        ):
            raise ChannelInteractionJournalError(
                "redundant decision fields disagree with canonical observation fields"
            )

    redundant_level = observation.get("level")
    if redundant_level is not None:
        level = _required_mapping(redundant_level, field="level")
        comparisons = (
            ("reference_slot", frozen_level.anchor_slot),
            ("reference_price", frozen_level.anchor_price),
            ("slope_per_slot", frozen_level.slope_per_slot),
            ("drawing_revision", frozen_level.frozen_revision),
        )
        for field, expected in comparisons:
            if field in level and level.get(field) != expected:
                raise ChannelInteractionJournalError(
                    f"redundant level.{field} disagrees with frozen_level"
                )
        if "touch" in level:
            level_touch = level.get("touch")
            if not isinstance(level_touch, bool):
                raise ChannelInteractionJournalError("redundant level.touch must be a boolean")
            if level_touch != (touch_slot is not None):
                raise ChannelInteractionJournalError(
                    "redundant level.touch disagrees with touch_slot"
                )

    redundant_micro_bar = observation.get("micro_bar")
    if redundant_micro_bar is not None:
        normalized_micro_bar, _bar = _validated_wire_bar(
            redundant_micro_bar,
            field="micro_bar",
            decision_at=decision_at,
            identity=exact_identity,
        )
        if not _same_bar_facts(normalized_micro_bar, normalized_bars[-1]):
            raise ChannelInteractionJournalError("micro_bar must equal the final micro_window bar")

    features = observation.get("features")
    if features is not None:
        features = _validate_features(features)
        feature_schema = observation.get(
            "feature_schema_version",
            CHANNEL_INTERACTION_FEATURE_SCHEMA_VERSION,
        )
        if feature_schema != CHANNEL_INTERACTION_FEATURE_SCHEMA_VERSION:
            raise ChannelInteractionJournalError("feature_schema_version is unsupported")
    if sample_eligible and features is None:
        raise ChannelInteractionJournalError(
            "eligible research observations must carry canonical features"
        )
    if not sample_eligible and features is not None:
        raise ChannelInteractionJournalError(
            "ineligible research observations must not carry decision features"
        )
    quality = _required_mapping(observation.get("quality"), field="quality")
    if (
        quality.get("ok") is not True
        or quality.get("provider_complete") is not True
        or quality.get("pending_slot_count") != 0
        or quality.get("slot_axis_authoritative") is not True
        or quality.get("slot_schedule_state") not in {"continuous", "verified"}
    ):
        raise ChannelInteractionJournalError(
            "quality must prove one complete authoritative confirmed 1m context"
        )
    decision_session = observation.get("decision_session")
    if decision_session is not None:
        session = _required_mapping(
            decision_session,
            field="decision_session",
        )
        if (
            session.get("contract") != "provider-session-decision-v1"
            or session.get("source") != "provider_schedule"
            or session.get("state") not in {"known", "unknown"}
        ):
            raise ChannelInteractionJournalError(
                "decision_session must carry the provider schedule contract"
            )
        require_exact_identity_text(
            session.get("calendar"),
            field="decision_session.calendar",
        )
        require_exact_identity_text(
            session.get("timezone"),
            field="decision_session.timezone",
        )
        if session.get("state") == "known":
            require_exact_identity_text(
                session.get("session_key"),
                field="decision_session.session_key",
            )
            if session.get("segment") not in {
                "liquid",
                "extended",
                "continuous",
            }:
                raise ChannelInteractionJournalError(
                    "known decision_session has an unsupported segment"
                )
        else:
            require_exact_identity_text(
                session.get("reason_code"),
                field="decision_session.reason_code",
            )
    if "model" in observation:
        raise ChannelInteractionJournalError(
            "advisory model diagnostics do not belong to the training journal"
        )

    typed = domain_wire_value(observation, field_name="research_observation")
    if not isinstance(typed, dict):
        raise ChannelInteractionJournalError("research_observation must serialize to a mapping")
    typed["episode_id"] = episode_id
    typed["sample_eligible"] = sample_eligible
    typed["sample_reason_code"] = sample_reason_code
    typed["phase"] = phase
    typed["decision_ts"] = decision_at.isoformat()
    typed["atr_value"] = atr_value
    typed["touch_slot"] = touch_slot
    typed["frozen_level"] = frozen_level.as_dict()
    typed["micro_window"] = {
        "bars": normalized_bars,
        "bar_slots": slots,
    }
    if redundant_decision is not None:
        typed["decision"] = {
            **dict(typed["decision"]),
            "phase": phase,
            "as_of_ts": decision_at.isoformat(),
            "timeframe": "1m",
            "confirmed": True,
            "slot": slots[-1],
        }
    if redundant_micro_bar is not None:
        typed["micro_bar"] = dict(normalized_bars[-1])
    if features is not None:
        typed["features"] = features
        typed["feature_schema_version"] = CHANNEL_INTERACTION_FEATURE_SCHEMA_VERSION
    typed["identity"] = exact_identity
    frozen = domain_frozen_value(typed, field_name="channel_interaction_observation")
    if not isinstance(frozen, Mapping):
        raise ChannelInteractionJournalError("normalized observation must be a mapping")
    wire = domain_wire_value(frozen, field_name="channel_interaction_observation")
    if not isinstance(wire, dict):
        raise ChannelInteractionJournalError("normalized observation must serialize to a mapping")

    natural_key = {
        "schema_version": CHANNEL_INTERACTION_OBSERVATION_SCHEMA_VERSION,
        "instrument_id": exact_identity["instrument_id"],
        "route_fingerprint": exact_identity["route_fingerprint"],
        "provider_contract_id": exact_identity["provider_contract_id"],
        "parent_timeframe": exact_identity["parent_timeframe"],
        "episode_id": episode_id,
        "decision_ts": decision_at.isoformat(),
        "phase": phase,
    }
    record_id = _sha256_bytes(canonical_json_bytes(natural_key))
    content_sha256 = _sha256_bytes(canonical_json_bytes(wire))
    return _ValidatedObservation(
        value=frozen,
        decision_at=decision_at,
        record_id=record_id,
        content_sha256=content_sha256,
    )


def _record_envelope(validated: _ValidatedObservation) -> dict[str, Any]:
    return {
        "journal_schema_version": CHANNEL_INTERACTION_JOURNAL_SCHEMA_VERSION,
        "record_id": validated.record_id,
        "content_sha256": validated.content_sha256,
        "observation": domain_wire_value(
            validated.value,
            field_name="channel_interaction_observation",
        ),
    }


class ChannelInteractionJournal:
    """Validate and atomically persist prospective Channel Master observations."""

    def __init__(self, *, data_root: Path | None = None) -> None:
        self._dataset_root = dataset_root(
            data_root=data_root,
            relative_root=_DATASET_RELATIVE_ROOT,
        )

    @property
    def dataset_root(self) -> Path:
        return self._dataset_root

    def record_snapshot_bytes(
        self,
        snapshot_bytes: bytes,
    ) -> tuple[JournalWriteResult, ...]:
        if not isinstance(snapshot_bytes, bytes):
            raise TypeError("snapshot_bytes must be exact bytes")
        try:
            snapshot = json.loads(snapshot_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ChannelInteractionJournalError(
                "snapshot_bytes must contain one JSON object"
            ) from exc
        if not isinstance(snapshot, Mapping):
            raise ChannelInteractionJournalError("snapshot_bytes must contain one JSON object")
        return self.record_snapshot(snapshot)

    def _validated_snapshot_observations(
        self,
        snapshot: Mapping[str, Any],
    ) -> tuple[_ValidatedObservation, ...]:
        observations = _snapshot_observations(snapshot)
        if not observations:
            return ()
        identity = _snapshot_identity(snapshot)
        validated: list[_ValidatedObservation] = []
        by_record_id: dict[str, _ValidatedObservation] = {}
        for observation in observations:
            item = _normalize_observation(
                observation,
                identity=identity,
            )
            existing = by_record_id.get(item.record_id)
            if existing is not None:
                if existing.content_sha256 != item.content_sha256:
                    raise ChannelInteractionJournalError(
                        "one research capture contains conflicting content "
                        f"for record {item.record_id}"
                    )
                continue
            by_record_id[item.record_id] = item
            validated.append(item)
        return tuple(validated)

    def record_snapshot(
        self,
        snapshot: Mapping[str, Any],
    ) -> tuple[JournalWriteResult, ...]:
        if not isinstance(snapshot, Mapping):
            raise TypeError("snapshot must be a typed mapping")
        return tuple(
            self._record_validated(item, preserve_first=False)
            for item in self._validated_snapshot_observations(snapshot)
        )

    def record_prospective_snapshot(
        self,
        snapshot: Mapping[str, Any],
    ) -> tuple[JournalWriteResult, ...]:
        """Freeze first validated rows for all live level decisions."""
        if not isinstance(snapshot, Mapping):
            raise TypeError("snapshot must be a typed mapping")
        return tuple(
            self._record_validated(item, preserve_first=True)
            for item in self._validated_snapshot_observations(snapshot)
        )

    def record_observation(
        self,
        observation: Mapping[str, Any],
        *,
        identity: Mapping[str, Any],
    ) -> JournalWriteResult:
        if not isinstance(observation, Mapping):
            raise TypeError("observation must be a typed mapping")
        if not isinstance(identity, Mapping):
            raise TypeError("identity must be a typed mapping")
        validated = _normalize_observation(observation, identity=identity)
        return self._record_validated(validated, preserve_first=False)

    def _record_validated(
        self,
        validated: _ValidatedObservation,
        *,
        preserve_first: bool,
    ) -> JournalWriteResult:
        envelope = _record_envelope(validated)
        data = canonical_json_bytes(envelope)
        if len(data) > _MAX_RAW_RECORD_BYTES:
            raise ChannelInteractionJournalError(
                f"journal record exceeds {_MAX_RAW_RECORD_BYTES} bytes"
            )
        date_partition = f"date={validated.decision_at.date().isoformat()}"
        raw_dir = self._dataset_root / _RAW_SCHEMA_RELATIVE_ROOT / date_partition
        target = raw_dir / f"{validated.record_id}.json"
        publication = publish_create_once(target, data)
        if publication != "conflict":
            return JournalWriteResult(
                record_id=validated.record_id,
                content_sha256=validated.content_sha256,
                status="written" if publication == "created" else "duplicate",
                path=target,
            )
        if preserve_first:
            return _existing_write_result(
                target,
                expected_record_id=validated.record_id,
            )

        conflict_path = raw_dir / (
            f"{validated.record_id}.conflict.{validated.content_sha256}.json"
        )
        conflict_publication = publish_create_once(conflict_path, data)
        if conflict_publication == "conflict":
            raise ChannelInteractionJournalError(
                f"conflict artifact is itself inconsistent: {conflict_path}"
            )
        raise ChannelInteractionJournalConflict(validated.record_id, conflict_path)


def _validated_envelope(value: object) -> tuple[dict[str, Any], _ValidatedObservation]:
    envelope = _required_mapping(value, field="journal envelope")
    if envelope.get("journal_schema_version") != CHANNEL_INTERACTION_JOURNAL_SCHEMA_VERSION:
        raise ChannelInteractionJournalError("journal envelope has an unsupported schema version")
    record_id = envelope.get("record_id")
    content_sha256 = envelope.get("content_sha256")
    if not isinstance(record_id, str) or _HEX_SHA256_RE.fullmatch(record_id) is None:
        raise ChannelInteractionJournalError("journal envelope record_id is invalid")
    if not isinstance(content_sha256, str) or _HEX_SHA256_RE.fullmatch(content_sha256) is None:
        raise ChannelInteractionJournalError("journal envelope content_sha256 is invalid")
    observation = _required_mapping(envelope.get("observation"), field="journal observation")
    identity = _required_mapping(observation.get("identity"), field="journal observation.identity")
    indicator_observation = dict(observation)
    indicator_observation.pop("identity", None)
    validated = _normalize_observation(indicator_observation, identity=identity)
    if validated.record_id != record_id:
        raise ChannelInteractionJournalError(
            "journal envelope record_id does not match its observation"
        )
    if validated.content_sha256 != content_sha256:
        raise ChannelInteractionJournalError(
            "journal envelope content_sha256 does not match its observation"
        )
    canonical = _record_envelope(validated)
    return canonical, validated


def _existing_write_result(
    path: Path,
    *,
    expected_record_id: str,
) -> JournalWriteResult:
    try:
        data = path.read_bytes()
        decoded = json.loads(data)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ChannelInteractionJournalError(
            f"existing prospective journal record is unreadable: {path}"
        ) from exc
    canonical, validated = _validated_envelope(decoded)
    if validated.record_id != expected_record_id:
        raise ChannelInteractionJournalError(
            "existing prospective journal record has a different identity"
        )
    if data != canonical_json_bytes(canonical):
        raise ChannelInteractionJournalError("existing prospective journal record is not canonical")
    return JournalWriteResult(
        record_id=validated.record_id,
        content_sha256=validated.content_sha256,
        status="duplicate",
        path=path,
    )


def validate_channel_interaction_journal_envelope(value: object) -> dict[str, Any]:
    """Return the canonical envelope after the complete journal contract validates."""
    canonical, _validated = _validated_envelope(value)
    return canonical


def _raw_relative_path(raw_root: Path, path: Path) -> str:
    return str(path.relative_to(raw_root.parent.parent))


def _issue(
    code: str,
    *,
    path: str | None = None,
    detail: str | None = None,
    record_id: str | None = None,
    content_sha256s: list[str] | None = None,
    paths: list[str] | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {"issue_code": code}
    if path is not None:
        row["path"] = path
    if detail is not None:
        row["detail"] = detail
    if record_id is not None:
        row["record_id"] = record_id
    if content_sha256s is not None:
        row["content_sha256s"] = content_sha256s
    if paths is not None:
        row["paths"] = paths
    return row


def _write_jsonl(path: Path, rows: list[Mapping[str, Any]]) -> None:
    with path.open("xb") as handle:
        for row in rows:
            handle.write(canonical_json_bytes(row))
        handle.flush()
        os.fsync(handle.fileno())


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    with path.open("xb") as handle:
        handle.write(
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                indent=2,
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )
        handle.flush()
        os.fsync(handle.fileno())


def _write_sha256sums(output_dir: Path) -> None:
    checksum_path = output_dir / "SHA256SUMS"
    paths = sorted(
        path for path in output_dir.rglob("*") if path.is_file() and path != checksum_path
    )
    with checksum_path.open("x", encoding="utf-8") as handle:
        for path in paths:
            handle.write(f"{_sha256_path(path)}  {path.relative_to(output_dir)}\n")
        handle.flush()
        os.fsync(handle.fileno())


def compact_channel_interaction_journal(
    dataset_id: str,
    *,
    data_root: Path | None = None,
) -> dict[str, Any]:
    """Publish one immutable curated snapshot of all currently visible raw files.

    Invalid, truncated, misplaced, and conflicting records are excluded from
    ``records.jsonl`` and retained as explicit rows in ``issues.jsonl``.  Raw
    files are never changed or deleted.
    """
    if not isinstance(dataset_id, str) or _DATASET_ID_RE.fullmatch(dataset_id) is None:
        raise ValueError(
            "dataset_id must use 1-96 lowercase letters, digits, dots, underscores, or hyphens"
        )
    resolved_dataset_root = dataset_root(
        data_root=data_root,
        relative_root=_DATASET_RELATIVE_ROOT,
    )
    raw_root = resolved_dataset_root / _RAW_SCHEMA_RELATIVE_ROOT
    curated_root = resolved_dataset_root / _CURATED_RELATIVE_ROOT
    output_dir = curated_root / dataset_id
    if output_dir.exists():
        raise FileExistsError(f"curated dataset already exists: {output_dir}")
    if not raw_root.is_dir():
        raise FileNotFoundError(f"channel interaction raw journal does not exist: {raw_root}")

    source_paths = sorted(
        path for path in raw_root.rglob("*") if path.is_file() and not path.name.startswith(".")
    )
    if not source_paths:
        raise ChannelInteractionJournalError("channel interaction raw journal is empty")

    issues: list[dict[str, Any]] = []
    inventory: list[dict[str, Any]] = []
    variants: dict[str, dict[str, tuple[dict[str, Any], list[str]]]] = defaultdict(dict)
    for path in source_paths:
        relative = _raw_relative_path(raw_root, path)
        if path.is_symlink():
            inventory.append(
                {
                    "path": relative,
                    "size_bytes": path.lstat().st_size,
                    "sha256": None,
                    "status": "forbidden_symlink",
                }
            )
            issues.append(_issue("forbidden_symlink", path=relative))
            continue
        size_bytes = path.stat().st_size
        if size_bytes > _MAX_RAW_RECORD_BYTES:
            inventory.append(
                {
                    "path": relative,
                    "size_bytes": size_bytes,
                    "sha256": None,
                    "status": "oversized_record",
                }
            )
            issues.append(_issue("oversized_record", path=relative))
            continue
        raw = path.read_bytes()
        inventory_row: dict[str, Any] = {
            "path": relative,
            "size_bytes": len(raw),
            "sha256": _sha256_bytes(raw),
        }
        inventory.append(inventory_row)
        date_match = _RAW_DATE_RE.fullmatch(path.parent.name)
        if path.parent.parent != raw_root or date_match is None:
            inventory_row["status"] = "invalid_path"
            issues.append(_issue("invalid_raw_path", path=relative))
            continue
        if not raw.endswith(b"\n"):
            inventory_row["status"] = "truncated"
            issues.append(_issue("truncated_record", path=relative))
            continue
        lines = raw.splitlines()
        if len(lines) != 1 or not lines[0].strip():
            inventory_row["status"] = "invalid_record_shape"
            issues.append(
                _issue(
                    "invalid_record_shape",
                    path=relative,
                    detail="atomic raw files must contain exactly one JSON object",
                )
            )
            continue
        try:
            decoded = json.loads(lines[0])
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            inventory_row["status"] = "invalid_json"
            issues.append(_issue("invalid_json", path=relative, detail=str(exc)))
            continue
        try:
            envelope, validated = _validated_envelope(decoded)
        except (TypeError, ValueError) as exc:
            inventory_row["status"] = "invalid_envelope"
            issues.append(_issue("invalid_envelope", path=relative, detail=str(exc)))
            continue
        expected_date = validated.decision_at.date().isoformat()
        if date_match.group(1) != expected_date:
            inventory_row["status"] = "partition_mismatch"
            issues.append(
                _issue(
                    "partition_mismatch",
                    path=relative,
                    detail=f"expected date={expected_date}",
                    record_id=validated.record_id,
                )
            )
            continue
        canonical_name = f"{validated.record_id}.json"
        conflict_name = f"{validated.record_id}.conflict.{validated.content_sha256}.json"
        if path.name not in {canonical_name, conflict_name}:
            inventory_row["status"] = "filename_mismatch"
            issues.append(
                _issue(
                    "filename_mismatch",
                    path=relative,
                    record_id=validated.record_id,
                )
            )
            continue
        inventory_row["status"] = "parsed"
        by_digest = variants[validated.record_id]
        if validated.content_sha256 in by_digest:
            by_digest[validated.content_sha256][1].append(relative)
        else:
            by_digest[validated.content_sha256] = (envelope, [relative])

    records: list[dict[str, Any]] = []
    conflicting_record_ids = 0
    for record_id in sorted(variants):
        by_digest = variants[record_id]
        if len(by_digest) > 1:
            conflicting_record_ids += 1
            issues.append(
                _issue(
                    "conflicting_record",
                    record_id=record_id,
                    content_sha256s=sorted(by_digest),
                    paths=sorted(
                        path
                        for _envelope, variant_paths in by_digest.values()
                        for path in variant_paths
                    ),
                )
            )
            continue
        envelope, _paths = next(iter(by_digest.values()))
        records.append(envelope)

    issues.sort(
        key=lambda row: (
            str(row.get("record_id") or ""),
            str(row.get("path") or ""),
            str(row["issue_code"]),
        )
    )
    inventory.sort(key=lambda row: str(row["path"]))
    curated_root.mkdir(parents=True, exist_ok=True)
    staging = curated_root / f".{dataset_id}.tmp-{os.getpid()}-{uuid.uuid4().hex}"
    staging.mkdir()
    try:
        _write_jsonl(staging / "records.jsonl", records)
        _write_jsonl(staging / "issues.jsonl", issues)
        _write_jsonl(staging / "source_inventory.jsonl", inventory)
        manifest = {
            "dataset_schema_version": CHANNEL_INTERACTION_DATASET_SCHEMA_VERSION,
            "observation_schema_version": CHANNEL_INTERACTION_OBSERVATION_SCHEMA_VERSION,
            "journal_schema_version": CHANNEL_INTERACTION_JOURNAL_SCHEMA_VERSION,
            "dataset_id": dataset_id,
            "created_at": datetime.now(tz=UTC).isoformat(),
            "immutable": True,
            "source": {
                "relative_root": str(_RAW_SCHEMA_RELATIVE_ROOT),
                "visible_file_count": len(source_paths),
            },
            "counts": {
                "curated_records": len(records),
                "issues": len(issues),
                "conflicting_record_ids": conflicting_record_ids,
            },
            "status": "needs_review" if issues else "ready",
            "files": (
                "records.jsonl",
                "issues.jsonl",
                "source_inventory.jsonl",
                "manifest.json",
                "SHA256SUMS",
            ),
        }
        _write_json(staging / "manifest.json", manifest)
        _write_sha256sums(staging)
        fsync_directory(staging)
        try:
            staging.rename(output_dir)
        except OSError as exc:
            if exc.errno in {errno.EEXIST, errno.ENOTEMPTY} or output_dir.exists():
                raise FileExistsError(f"curated dataset already exists: {output_dir}") from exc
            raise
        fsync_directory(curated_root)
        return manifest
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        raise
