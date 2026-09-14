from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from aef_terminal.indicators.modules.option_reversal import (
    OPTION_REVERSAL_RESEARCH_FEATURE_FIELDS,
)
from aef_terminal.research.option_reversal_journal import (
    OptionReversalJournal,
    OptionReversalJournalConflict,
    OptionReversalJournalError,
)
from aef_terminal.ui.services.market_analysis_compaction import (
    compact_analysis_snapshot_for_cache,
)


def _observation() -> dict[str, object]:
    features = {
        "compression": 0.79,
        "lag_score": 0.0,
        "favorable_move_atr": 0.0,
        "near_ratio": 0.0,
        "spread_ratio": 0.025,
        "option_price": 7.9,
        "reference_price": 10.0,
        "previous_option": None,
        "previous_underlying": None,
        "atr": 1.0,
        "underlying_price": 101.0,
        "target_price": 101.0,
        "bid": 7.8,
        "ask": 8.0,
        "compression_score": 1.0,
        "reaction_score": 0.0,
        "premium_lag_score": 0.75,
        "premium_change_ratio": None,
        "near_score": 1.0,
        "edge_score": 1.0,
        "raw_score": 99.0,
        "score": 99.0,
    }
    assert set(features) == set(OPTION_REVERSAL_RESEARCH_FEATURE_FIELDS)
    return {
        "schema_version": 2,
        "decision_ts": "2026-08-03T14:30:00+00:00",
        "action_taken": "BUY_CALL",
        "trigger_code": "premium_compression_entry_candidate",
        "price_source": "bid_ask_mid",
        "contract": {
            "option_target_id": "point-1",
            "option_contract_id": "800001",
            "sec_type": "OPT",
            "right": "CALL",
            "expiry": "20260803",
            "strike": 5000.0,
            "target_delta": 0.24,
            "estimated_greeks": True,
        },
        "sample": {
            "observation_ts": "2026-08-03T14:30:10+00:00",
            "option_quote_ts": "2026-08-03T14:30:09+00:00",
            "option_quote_time_basis": "provider_event",
            "underlying_quote_ts": "2026-08-03T14:30:10+00:00",
            "underlying_quote_time_basis": "provider_event",
            "previous_option_quote_ts": None,
            "previous_underlying_quote_ts": None,
            "compression_sample_at": "2026-08-03T14:30:10+00:00",
        },
        "features": features,
        "feature_availability": {
            "previous_pair": False,
            "bid_ask": True,
            "target_delta": True,
        },
        "parameters": {
            "near_atr": 0.45,
            "compression_ratio": 0.82,
            "compression_neutral_ratio": 1.06,
            "reaction_min_atr": 0.12,
            "premium_lag_max_change": 0.03,
            "wide_spread_ratio": 0.35,
        },
    }


def _snapshot(observation: dict[str, object] | None = None) -> dict[str, object]:
    return {
        "meta": {
            "instrument_id": "ibkr|future_root|ES",
            "route_fingerprint": "ibkr|contract|700001",
            "provider": "ibkr",
            "provider_contract_id": "700001",
            "provider_symbol": "ESU6",
            "timeframe": "5m",
        },
        "indicators": {
            "option_reversal": {
                "research_observation": observation or _observation(),
            }
        },
    }


def test_journal_writes_external_partition_and_deduplicates(tmp_path: Path) -> None:
    journal = OptionReversalJournal(data_root=tmp_path)
    snapshot = _snapshot()

    first = journal.record_snapshot(snapshot)
    duplicate = journal.record_snapshot_bytes(json.dumps(snapshot, sort_keys=True).encode("utf-8"))

    assert first is not None
    assert duplicate is not None
    assert first.status == "written"
    assert duplicate.status == "duplicate"
    assert duplicate.record_id == first.record_id
    assert first.path == (
        tmp_path
        / "datasets"
        / "option_reversal"
        / "raw"
        / "schema-v2"
        / "date=2026-08-03"
        / f"{first.record_id}.json"
    )
    envelope = json.loads(first.path.read_text(encoding="utf-8"))
    assert envelope["observation"]["identity"] == {
        "instrument_id": "ibkr|future_root|ES",
        "parent_timeframe": "5m",
        "provider": "ibkr",
        "provider_contract_id": "700001",
        "provider_symbol": "ESU6",
        "route_fingerprint": "ibkr|contract|700001",
    }
    assert envelope["observation"]["contract"]["option_target_id"] == "point-1"


def test_distinct_option_points_and_quote_samples_have_distinct_ids(
    tmp_path: Path,
) -> None:
    journal = OptionReversalJournal(data_root=tmp_path)
    base = _observation()
    first = journal.record_snapshot(_snapshot(base))
    assert first is not None

    second_point = copy.deepcopy(base)
    second_point["contract"]["option_target_id"] = "point-2"
    second_point["features"]["target_price"] = 102.0
    second = journal.record_snapshot(_snapshot(second_point))

    next_quote = copy.deepcopy(base)
    next_quote["sample"]["observation_ts"] = "2026-08-03T14:30:11+00:00"
    next_quote["sample"]["option_quote_ts"] = "2026-08-03T14:30:11+00:00"
    next_quote["features"]["option_price"] = 7.95
    third = journal.record_snapshot(_snapshot(next_quote))

    changed_parameters = copy.deepcopy(base)
    changed_parameters["parameters"]["near_atr"] = 0.5
    fourth = journal.record_snapshot(_snapshot(changed_parameters))

    assert second is not None
    assert third is not None
    assert fourth is not None
    assert len({first.record_id, second.record_id, third.record_id, fourth.record_id}) == 4


def test_journal_preserves_signed_fop_underlying_prices(tmp_path: Path) -> None:
    observation = _observation()
    observation["contract"]["sec_type"] = "FOP"
    observation["features"]["underlying_price"] = -10.0
    observation["features"]["target_price"] = -10.25

    written = OptionReversalJournal(data_root=tmp_path).record_snapshot(_snapshot(observation))

    assert written is not None
    payload = json.loads(written.path.read_text(encoding="utf-8"))["observation"]
    assert payload["features"]["underlying_price"] == -10.0
    assert payload["features"]["target_price"] == -10.25


@pytest.mark.parametrize(
    ("mutate", "match"),
    (
        (
            lambda value: value["features"].__setitem__("compression", float("nan")),
            "features.compression must be a finite float",
        ),
        (
            lambda value: value["features"].__setitem__("atr", 1),
            "features.atr must be a finite float",
        ),
        (
            lambda value: value["contract"].__setitem__("symbol", "ES"),
            "contract keys differ from schema",
        ),
    ),
)
def test_journal_rejects_dirty_training_values(mutate, match: str) -> None:
    observation = _observation()
    mutate(observation)

    with pytest.raises(OptionReversalJournalError, match=match):
        OptionReversalJournal(data_root=Path("unused")).record_snapshot(_snapshot(observation))


def test_same_natural_key_preserves_conflicting_content(tmp_path: Path) -> None:
    journal = OptionReversalJournal(data_root=tmp_path)
    original = _observation()
    first = journal.record_snapshot(_snapshot(original))
    assert first is not None
    changed = copy.deepcopy(original)
    changed["features"]["score"] = 98.0

    with pytest.raises(OptionReversalJournalConflict) as raised:
        journal.record_snapshot(_snapshot(changed))

    assert raised.value.record_id == first.record_id
    assert raised.value.conflict_path.name.startswith(f"{first.record_id}.conflict.")
    assert raised.value.conflict_path.read_bytes().endswith(b"\n")


def test_prospective_capture_freezes_first_causal_sample(tmp_path: Path) -> None:
    journal = OptionReversalJournal(data_root=tmp_path)
    original = _observation()
    first = journal.record_prospective_snapshot(_snapshot(original))
    assert first is not None
    revised = copy.deepcopy(original)
    revised["features"]["score"] = 98.0

    duplicate = journal.record_prospective_snapshot(_snapshot(revised))

    assert duplicate is not None
    assert duplicate.status == "duplicate"
    assert duplicate.record_id == first.record_id
    assert duplicate.content_sha256 == first.content_sha256
    assert list(first.path.parent.glob("*.conflict.*.json")) == []
    envelope = json.loads(first.path.read_text(encoding="utf-8"))
    assert envelope["observation"]["features"]["score"] == 99.0


def test_journal_ignores_snapshots_without_observation(tmp_path: Path) -> None:
    snapshot = _snapshot()
    snapshot["indicators"]["option_reversal"].pop("research_observation")

    assert OptionReversalJournal(data_root=tmp_path).record_snapshot(snapshot) is None
    assert list(tmp_path.iterdir()) == []


def test_private_observation_is_removed_from_cached_snapshot() -> None:
    snapshot = _snapshot()
    snapshot["bars"] = []

    compacted = compact_analysis_snapshot_for_cache(snapshot)

    assert "research_observation" not in compacted["indicators"]["option_reversal"]
