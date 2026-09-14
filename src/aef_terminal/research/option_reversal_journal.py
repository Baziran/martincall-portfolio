"""Durable raw capture for prospective Option Reversal research.

The indicator owns the causal observation and this module owns strict
validation, exact snapshot identity attachment, and append-only publication.
Capture runs in the isolated persistent market-analysis worker before the private
observation is removed from the cache/browser payload.

Artifacts live below ``AppConfig.data_root / datasets / option_reversal``.
There is no repository-local or application-database fallback.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from aef_terminal.data.gex.option_target_contract import (
    OPTION_TARGET_CURRENT_QUOTE_PRICE_SOURCES,
)
from aef_terminal.data.instrument_identity import require_exact_identity_text
from aef_terminal.indicators.modules.option_reversal import (
    OPTION_REVERSAL_RESEARCH_FEATURE_FIELDS,
    OPTION_REVERSAL_RESEARCH_OBSERVATION_SCHEMA_VERSION,
)
from aef_terminal.research.journal_utils import (
    canonical_json_bytes,
    dataset_root,
    publish_create_once,
)


OPTION_REVERSAL_OBSERVATION_SCHEMA_VERSION = OPTION_REVERSAL_RESEARCH_OBSERVATION_SCHEMA_VERSION
OPTION_REVERSAL_JOURNAL_SCHEMA_VERSION = 2

_DATASET_RELATIVE_ROOT = Path("datasets") / "option_reversal"
_RAW_SCHEMA_RELATIVE_ROOT = Path("raw") / f"schema-v{OPTION_REVERSAL_OBSERVATION_SCHEMA_VERSION}"
_INDICATOR_ID = "option_reversal"
_MAX_RAW_RECORD_BYTES = 64 * 1024
_ACTIONS = frozenset({"BUY_CALL", "BUY_PUT", "BUY_OPTION", "WATCH", "WAIT", "SELL_TAKE"})
_FEATURE_FIELDS = frozenset(OPTION_REVERSAL_RESEARCH_FEATURE_FIELDS)
_PARAMETER_FIELDS = frozenset(
    {
        "near_atr",
        "compression_ratio",
        "compression_neutral_ratio",
        "reaction_min_atr",
        "premium_lag_max_change",
        "wide_spread_ratio",
    }
)


class OptionReversalJournalError(ValueError):
    """The snapshot observation violates the raw capture contract."""


class OptionReversalJournalConflict(OptionReversalJournalError):
    """One exact quote observation was emitted with different content."""

    def __init__(self, record_id: str, conflict_path: Path) -> None:
        super().__init__(
            f"option reversal record {record_id} has conflicting content; "
            f"preserved at {conflict_path}"
        )
        self.record_id = record_id
        self.conflict_path = conflict_path


@dataclass(frozen=True, slots=True)
class OptionReversalJournalWriteResult:
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


def _aware_utc(value: object, *, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise OptionReversalJournalError(f"{field} must be a non-empty ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OptionReversalJournalError(f"{field} must be a valid ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise OptionReversalJournalError(f"{field} must be timezone-aware")
    return parsed.astimezone(UTC)


def _optional_aware_utc(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    return _aware_utc(value, field=field).isoformat()


def _mapping(value: object, *, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise OptionReversalJournalError(f"{field} must be a typed mapping")
    return value


def _exact_keys(
    value: Mapping[str, Any],
    expected: frozenset[str],
    *,
    field: str,
) -> None:
    actual = frozenset(str(key) for key in value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise OptionReversalJournalError(
            f"{field} keys differ from schema; missing={missing} extra={extra}"
        )


def _finite_float(value: object, *, field: str) -> float:
    if type(value) is not float or not math.isfinite(value):
        raise OptionReversalJournalError(f"{field} must be a finite float")
    return value


def _optional_finite_float(value: object, *, field: str) -> float | None:
    return None if value is None else _finite_float(value, field=field)


def _strict_bool(value: object, *, field: str) -> bool:
    if type(value) is not bool:
        raise OptionReversalJournalError(f"{field} must be a boolean")
    return value


def _exact_text(value: object, *, field: str) -> str:
    try:
        text = require_exact_identity_text(value, field=field)
    except (TypeError, ValueError) as exc:
        raise OptionReversalJournalError(f"{field} must be exact text") from exc
    if text != text.strip():
        raise OptionReversalJournalError(f"{field} must not have outer whitespace")
    return text


def _snapshot_observation(
    snapshot: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    indicators = snapshot.get("indicators")
    if not isinstance(indicators, Mapping):
        return None
    indicator = indicators.get(_INDICATOR_ID)
    if not isinstance(indicator, Mapping):
        return None
    observation = indicator.get("research_observation")
    if observation is None:
        return None
    return _mapping(observation, field="research_observation")


def _snapshot_identity(snapshot: Mapping[str, Any]) -> dict[str, str]:
    meta = _mapping(snapshot.get("meta"), field="snapshot.meta")
    identity = {
        "instrument_id": _exact_text(meta.get("instrument_id"), field="instrument_id"),
        "route_fingerprint": _exact_text(meta.get("route_fingerprint"), field="route_fingerprint"),
        "provider": _exact_text(meta.get("provider"), field="provider"),
        "provider_contract_id": _exact_text(
            meta.get("provider_contract_id"), field="provider_contract_id"
        ),
        "parent_timeframe": _exact_text(meta.get("timeframe"), field="parent_timeframe"),
    }
    provider_symbol = meta.get("provider_symbol")
    if provider_symbol is not None:
        identity["provider_symbol"] = _exact_text(provider_symbol, field="provider_symbol")
    return identity


def _normalize_observation(
    observation: Mapping[str, Any],
    *,
    identity: Mapping[str, Any],
) -> _ValidatedObservation:
    expected_observation_keys = frozenset(
        {
            "schema_version",
            "decision_ts",
            "action_taken",
            "trigger_code",
            "price_source",
            "contract",
            "sample",
            "features",
            "feature_availability",
            "parameters",
        }
    )
    _exact_keys(
        observation,
        expected_observation_keys,
        field="research_observation",
    )
    if observation.get("schema_version") != OPTION_REVERSAL_OBSERVATION_SCHEMA_VERSION:
        raise OptionReversalJournalError(
            "research_observation.schema_version must equal "
            f"{OPTION_REVERSAL_OBSERVATION_SCHEMA_VERSION}"
        )
    if any(key in observation for key in ("identity", "symbol", "data_symbol")):
        raise OptionReversalJournalError(
            "indicator observation must not carry route identity or symbol aliases"
        )

    exact_identity = {
        "instrument_id": _exact_text(identity.get("instrument_id"), field="instrument_id"),
        "route_fingerprint": _exact_text(
            identity.get("route_fingerprint"), field="route_fingerprint"
        ),
        "provider": _exact_text(identity.get("provider"), field="provider"),
        "provider_contract_id": _exact_text(
            identity.get("provider_contract_id"), field="provider_contract_id"
        ),
        "parent_timeframe": _exact_text(identity.get("parent_timeframe"), field="parent_timeframe"),
    }
    provider_symbol = identity.get("provider_symbol")
    if provider_symbol is not None:
        exact_identity["provider_symbol"] = _exact_text(provider_symbol, field="provider_symbol")

    decision_at = _aware_utc(observation.get("decision_ts"), field="decision_ts")
    action = _exact_text(observation.get("action_taken"), field="action_taken")
    if action not in _ACTIONS:
        raise OptionReversalJournalError("action_taken is unsupported")
    trigger_code = _exact_text(observation.get("trigger_code"), field="trigger_code")
    price_source = _exact_text(observation.get("price_source"), field="price_source")
    if price_source not in OPTION_TARGET_CURRENT_QUOTE_PRICE_SOURCES:
        raise OptionReversalJournalError("price_source is not a current quote source")

    contract = _mapping(observation.get("contract"), field="contract")
    _exact_keys(
        contract,
        frozenset(
            {
                "option_target_id",
                "option_contract_id",
                "sec_type",
                "right",
                "expiry",
                "strike",
                "target_delta",
                "estimated_greeks",
            }
        ),
        field="contract",
    )
    exact_contract: dict[str, Any] = {
        "option_target_id": _exact_text(
            contract.get("option_target_id"), field="contract.option_target_id"
        ),
        "option_contract_id": _exact_text(
            contract.get("option_contract_id"),
            field="contract.option_contract_id",
        ),
        "sec_type": _exact_text(contract.get("sec_type"), field="contract.sec_type"),
        "right": _exact_text(contract.get("right"), field="contract.right"),
        "expiry": _exact_text(contract.get("expiry"), field="contract.expiry"),
    }
    if exact_contract["sec_type"] not in {"OPT", "FOP"}:
        raise OptionReversalJournalError("contract.sec_type is unsupported")
    if exact_contract["right"] not in {"CALL", "PUT", "OPTION"}:
        raise OptionReversalJournalError("contract.right is unsupported")
    if len(exact_contract["expiry"]) != 8 or not exact_contract["expiry"].isdigit():
        raise OptionReversalJournalError("contract.expiry must be YYYYMMDD")
    strike = contract.get("strike")
    if strike is None or _finite_float(strike, field="contract.strike") <= 0:
        raise OptionReversalJournalError("contract.strike must be positive")
    exact_contract["strike"] = strike
    target_delta = contract.get("target_delta")
    if target_delta is not None:
        target_delta = _finite_float(target_delta, field="contract.target_delta")
        if not -1.0 <= target_delta <= 1.0:
            raise OptionReversalJournalError("contract.target_delta must be within [-1, 1]")
    exact_contract["target_delta"] = target_delta
    estimated_greeks = contract.get("estimated_greeks")
    if estimated_greeks is not None:
        estimated_greeks = _strict_bool(estimated_greeks, field="contract.estimated_greeks")
    exact_contract["estimated_greeks"] = estimated_greeks

    sample = _mapping(observation.get("sample"), field="sample")
    _exact_keys(
        sample,
        frozenset(
            {
                "observation_ts",
                "option_quote_ts",
                "option_quote_time_basis",
                "underlying_quote_ts",
                "underlying_quote_time_basis",
                "previous_option_quote_ts",
                "previous_underlying_quote_ts",
                "compression_sample_at",
            }
        ),
        field="sample",
    )
    exact_sample = {
        "observation_ts": _aware_utc(
            sample.get("observation_ts"), field="sample.observation_ts"
        ).isoformat(),
        "option_quote_ts": _aware_utc(
            sample.get("option_quote_ts"), field="sample.option_quote_ts"
        ).isoformat(),
        "option_quote_time_basis": _exact_text(
            sample.get("option_quote_time_basis"),
            field="sample.option_quote_time_basis",
        ),
        "underlying_quote_ts": _aware_utc(
            sample.get("underlying_quote_ts"),
            field="sample.underlying_quote_ts",
        ).isoformat(),
        "underlying_quote_time_basis": _exact_text(
            sample.get("underlying_quote_time_basis"),
            field="sample.underlying_quote_time_basis",
        ),
        "previous_option_quote_ts": _optional_aware_utc(
            sample.get("previous_option_quote_ts"),
            field="sample.previous_option_quote_ts",
        ),
        "previous_underlying_quote_ts": _optional_aware_utc(
            sample.get("previous_underlying_quote_ts"),
            field="sample.previous_underlying_quote_ts",
        ),
        "compression_sample_at": _optional_aware_utc(
            sample.get("compression_sample_at"),
            field="sample.compression_sample_at",
        ),
    }
    for field in ("option_quote_time_basis", "underlying_quote_time_basis"):
        if exact_sample[field] not in {"provider_event", "client_receive"}:
            raise OptionReversalJournalError(f"sample.{field} is unsupported")

    features = _mapping(observation.get("features"), field="features")
    _exact_keys(features, _FEATURE_FIELDS, field="features")
    optional_feature_fields = {
        "previous_option",
        "previous_underlying",
        "premium_change_ratio",
    }
    exact_features = {
        field: (
            _optional_finite_float(features.get(field), field=f"features.{field}")
            if field in optional_feature_fields
            else _finite_float(features.get(field), field=f"features.{field}")
        )
        for field in OPTION_REVERSAL_RESEARCH_FEATURE_FIELDS
    }
    for field in (
        "compression",
        "lag_score",
        "near_ratio",
        "spread_ratio",
        "option_price",
        "reference_price",
        "atr",
        "bid",
        "ask",
        "compression_score",
        "reaction_score",
        "premium_lag_score",
        "near_score",
        "edge_score",
        "raw_score",
        "score",
    ):
        if exact_features[field] < 0:
            raise OptionReversalJournalError(f"features.{field} must be non-negative")
    for field in ("option_price", "reference_price", "atr"):
        if exact_features[field] <= 0:
            raise OptionReversalJournalError(f"features.{field} must be positive")
    if exact_contract["sec_type"] == "OPT":
        for field in ("underlying_price", "target_price"):
            if exact_features[field] <= 0:
                raise OptionReversalJournalError(f"features.{field} must be positive for OPT")
    previous_option = exact_features["previous_option"]
    previous_underlying = exact_features["previous_underlying"]
    premium_change_ratio = exact_features["premium_change_ratio"]
    if previous_option is not None and previous_option <= 0:
        raise OptionReversalJournalError("features.previous_option must be positive")
    if (
        exact_contract["sec_type"] == "OPT"
        and previous_underlying is not None
        and previous_underlying <= 0
    ):
        raise OptionReversalJournalError("features.previous_underlying must be positive for OPT")
    previous_pair_available = previous_option is not None and previous_underlying is not None
    if (previous_option is None) is not (previous_underlying is None):
        raise OptionReversalJournalError("previous option/underlying features must be atomic")
    if (premium_change_ratio is not None) is not previous_pair_available:
        raise OptionReversalJournalError(
            "premium_change_ratio availability must match the previous pair"
        )

    availability = _mapping(
        observation.get("feature_availability"),
        field="feature_availability",
    )
    _exact_keys(
        availability,
        frozenset({"previous_pair", "bid_ask", "target_delta"}),
        field="feature_availability",
    )
    exact_availability = {
        field: _strict_bool(availability.get(field), field=f"feature_availability.{field}")
        for field in ("previous_pair", "bid_ask", "target_delta")
    }
    expected_availability = {
        "previous_pair": previous_pair_available,
        "bid_ask": exact_features["bid"] > 0 and exact_features["ask"] > 0,
        "target_delta": target_delta is not None,
    }
    if exact_availability != expected_availability:
        raise OptionReversalJournalError(
            "feature_availability disagrees with captured feature values"
        )
    if exact_availability["bid_ask"] and exact_features["ask"] < exact_features["bid"]:
        raise OptionReversalJournalError("features.ask must not be below features.bid")
    if exact_availability["previous_pair"] and (
        exact_sample["previous_option_quote_ts"] is None
        or exact_sample["previous_underlying_quote_ts"] is None
    ):
        raise OptionReversalJournalError(
            "available previous prices require their exact quote timestamps"
        )

    parameters = _mapping(observation.get("parameters"), field="parameters")
    _exact_keys(parameters, _PARAMETER_FIELDS, field="parameters")
    exact_parameters = {
        field: _finite_float(parameters.get(field), field=f"parameters.{field}")
        for field in _PARAMETER_FIELDS
    }
    if any(value <= 0 for value in exact_parameters.values()):
        raise OptionReversalJournalError("all parameters must be positive")
    if exact_parameters["compression_neutral_ratio"] <= exact_parameters["compression_ratio"]:
        raise OptionReversalJournalError("compression_neutral_ratio must exceed compression_ratio")

    normalized = {
        "schema_version": OPTION_REVERSAL_OBSERVATION_SCHEMA_VERSION,
        "identity": exact_identity,
        "decision_ts": decision_at.isoformat(),
        "action_taken": action,
        "trigger_code": trigger_code,
        "price_source": price_source,
        "contract": exact_contract,
        "sample": exact_sample,
        "features": exact_features,
        "feature_availability": exact_availability,
        "parameters": exact_parameters,
    }
    natural_key = {
        "schema_version": OPTION_REVERSAL_OBSERVATION_SCHEMA_VERSION,
        "instrument_id": exact_identity["instrument_id"],
        "route_fingerprint": exact_identity["route_fingerprint"],
        "provider": exact_identity["provider"],
        "provider_contract_id": exact_identity["provider_contract_id"],
        "parent_timeframe": exact_identity["parent_timeframe"],
        "option_target_id": exact_contract["option_target_id"],
        "option_contract_id": exact_contract["option_contract_id"],
        "decision_ts": normalized["decision_ts"],
        "observation_ts": exact_sample["observation_ts"],
        "option_quote_ts": exact_sample["option_quote_ts"],
        "underlying_quote_ts": exact_sample["underlying_quote_ts"],
        "action_taken": action,
        "parameters": exact_parameters,
    }
    record_id = hashlib.sha256(canonical_json_bytes(natural_key)).hexdigest()
    content_sha256 = hashlib.sha256(canonical_json_bytes(normalized)).hexdigest()
    return _ValidatedObservation(
        value=normalized,
        decision_at=decision_at,
        record_id=record_id,
        content_sha256=content_sha256,
    )


def _record_envelope(validated: _ValidatedObservation) -> dict[str, Any]:
    return {
        "journal_schema_version": OPTION_REVERSAL_JOURNAL_SCHEMA_VERSION,
        "record_id": validated.record_id,
        "content_sha256": validated.content_sha256,
        "observation": validated.value,
    }


class OptionReversalJournal:
    """Validate and publish exact prospective Option Reversal observations."""

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
    ) -> OptionReversalJournalWriteResult | None:
        if not isinstance(snapshot_bytes, bytes):
            raise TypeError("snapshot_bytes must be exact bytes")
        try:
            snapshot = json.loads(snapshot_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OptionReversalJournalError("snapshot_bytes must contain one JSON object") from exc
        if not isinstance(snapshot, Mapping):
            raise OptionReversalJournalError("snapshot_bytes must contain one JSON object")
        return self.record_snapshot(snapshot)

    def record_snapshot(
        self,
        snapshot: Mapping[str, Any],
    ) -> OptionReversalJournalWriteResult | None:
        if not isinstance(snapshot, Mapping):
            raise TypeError("snapshot must be a typed mapping")
        observation = _snapshot_observation(snapshot)
        if observation is None:
            return None
        validated = _normalize_observation(
            observation,
            identity=_snapshot_identity(snapshot),
        )
        return self._record_validated(validated, preserve_first=False)

    def record_prospective_snapshot(
        self,
        snapshot: Mapping[str, Any],
    ) -> OptionReversalJournalWriteResult | None:
        """Freeze the first validated observation for one live sample identity."""
        if not isinstance(snapshot, Mapping):
            raise TypeError("snapshot must be a typed mapping")
        observation = _snapshot_observation(snapshot)
        if observation is None:
            return None
        validated = _normalize_observation(
            observation,
            identity=_snapshot_identity(snapshot),
        )
        return self._record_validated(validated, preserve_first=True)

    def _record_validated(
        self,
        validated: _ValidatedObservation,
        *,
        preserve_first: bool,
    ) -> OptionReversalJournalWriteResult:
        envelope = _record_envelope(validated)
        data = canonical_json_bytes(envelope)
        if len(data) > _MAX_RAW_RECORD_BYTES:
            raise OptionReversalJournalError(
                f"journal record exceeds {_MAX_RAW_RECORD_BYTES} bytes"
            )

        date_partition = f"date={validated.decision_at.date().isoformat()}"
        raw_dir = self._dataset_root / _RAW_SCHEMA_RELATIVE_ROOT / date_partition
        target = raw_dir / f"{validated.record_id}.json"
        publication = publish_create_once(target, data)
        if publication != "conflict":
            return OptionReversalJournalWriteResult(
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
            raise OptionReversalJournalError(
                f"conflict artifact is itself inconsistent: {conflict_path}"
            )
        raise OptionReversalJournalConflict(validated.record_id, conflict_path)


def _validated_envelope(value: object) -> tuple[dict[str, Any], _ValidatedObservation]:
    envelope = _mapping(value, field="journal envelope")
    _exact_keys(
        envelope,
        frozenset(
            {
                "journal_schema_version",
                "record_id",
                "content_sha256",
                "observation",
            }
        ),
        field="journal envelope",
    )
    if envelope.get("journal_schema_version") != OPTION_REVERSAL_JOURNAL_SCHEMA_VERSION:
        raise OptionReversalJournalError("journal envelope has an unsupported schema version")
    observation = _mapping(
        envelope.get("observation"),
        field="journal observation",
    )
    identity = _mapping(
        observation.get("identity"),
        field="journal observation.identity",
    )
    indicator_observation = dict(observation)
    indicator_observation.pop("identity", None)
    validated = _normalize_observation(
        indicator_observation,
        identity=identity,
    )
    if envelope.get("record_id") != validated.record_id:
        raise OptionReversalJournalError(
            "journal envelope record_id does not match its observation"
        )
    if envelope.get("content_sha256") != validated.content_sha256:
        raise OptionReversalJournalError(
            "journal envelope content_sha256 does not match its observation"
        )
    canonical = _record_envelope(validated)
    return canonical, validated


def _existing_write_result(
    path: Path,
    *,
    expected_record_id: str,
) -> OptionReversalJournalWriteResult:
    try:
        data = path.read_bytes()
        decoded = json.loads(data)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OptionReversalJournalError(
            f"existing prospective journal record is unreadable: {path}"
        ) from exc
    canonical, validated = _validated_envelope(decoded)
    if validated.record_id != expected_record_id:
        raise OptionReversalJournalError(
            "existing prospective journal record has a different identity"
        )
    if data != canonical_json_bytes(canonical):
        raise OptionReversalJournalError("existing prospective journal record is not canonical")
    return OptionReversalJournalWriteResult(
        record_id=validated.record_id,
        content_sha256=validated.content_sha256,
        status="duplicate",
        path=path,
    )
