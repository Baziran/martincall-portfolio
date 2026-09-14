from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aef_terminal.domain import domain_frozen_value
from aef_terminal.ml.channel_interaction import (
    CHANNEL_INTERACTION_LEVEL_SLOT_TIMEFRAME,
)
from aef_terminal.research.channel_interaction_journal import (
    ChannelInteractionJournal,
    ChannelInteractionJournalConflict,
    ChannelInteractionJournalError,
    compact_channel_interaction_journal,
)


def _wire_bar(ts: datetime, close: float) -> dict[str, object]:
    return {
        "ts": ts.isoformat(),
        "open": close - 0.25,
        "high": close + 0.5,
        "low": close - 0.5,
        "close": close,
        "volume": 100.0,
        "timeframe": "1m",
        "source": "ibkr:historical",
        "closed": True,
        "state": "confirmed",
        "session_key": "2026-08-03",
        "provenance": {
            "provider": "ibkr",
            "instrument_id": "ibkr|future_root|ES",
            "route_fingerprint": "ibkr|contract|700001",
            "request_type": "canonical_storage",
            "provider_contract_id": "",
            "provider_contract_type": "CANONICAL_STORAGE",
            "data_type": "canonical_ohlcv",
            "source_timeframe": None,
        },
    }


def _observation() -> dict[str, object]:
    first = datetime(2026, 8, 3, 14, 30, tzinfo=UTC)
    bars = [
        _wire_bar(first, 99.0),
        _wire_bar(first + timedelta(minutes=1), 99.25),
        _wire_bar(first + timedelta(minutes=2), 99.5),
    ]
    return {
        "schema_version": 1,
        "episode_id": "episode-20260803-1432-lower-edge",
        "sample_eligible": True,
        "sample_reason_code": "sample_ready",
        "phase": "pre_touch",
        "decision_ts": (first + timedelta(minutes=3)).isoformat(),
        "atr_value": 2.5,
        "touch_slot": None,
        "frozen_level": {
            "anchor_slot": 100,
            "anchor_price": 100.0,
            "slope_per_slot": 0.0,
            "slot_timeframe": CHANNEL_INTERACTION_LEVEL_SLOT_TIMEFRAME,
            "side": "resistance",
            "frozen_revision": "drawing-sha256-revision",
            "frozen_at": (first - timedelta(minutes=30)).isoformat(),
        },
        "micro_window": {
            "bars": bars,
            "bar_slots": [100, 101, 102],
        },
        "decision": {
            "phase": "pre_touch",
            "as_of_ts": (first + timedelta(minutes=3)).isoformat(),
            "timeframe": "1m",
            "confirmed": True,
            "slot": 102,
        },
        "level": {
            "channel_id": "channel-a",
            "drawing_revision": "drawing-sha256-revision",
            "level": 0.0,
            "role": "lower_edge",
            "reference_slot": 100,
            "reference_price": 100.0,
            "slope_per_slot": 0.0,
            "offset": 4.0,
            "distance_atr": 0.2,
            "touch": False,
        },
        "micro_bar": bars[-1],
        "feature_schema_version": 1,
        "features": {
            "distance_close_atr": -0.2,
            "touch_missing": 1.0,
            "optional_tick_delta": None,
        },
        "quality": {
            "ok": True,
            "provider_complete": True,
            "pending_slot_count": 0,
            "slot_axis_authoritative": True,
            "slot_schedule_state": "verified",
        },
    }


def _snapshot(observation: dict[str, object] | None = None) -> dict[str, object]:
    payload = _observation() if observation is None else observation
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
            "channel_master": {
                "research_observations": domain_frozen_value(
                    [payload],
                    field_name="research_observations",
                )
            }
        },
    }


def test_journal_uses_external_partition_and_is_duplicate_idempotent(tmp_path: Path) -> None:
    journal = ChannelInteractionJournal(data_root=tmp_path)
    snapshot = _snapshot()

    (first,) = journal.record_snapshot(snapshot)
    (duplicate,) = journal.record_snapshot_bytes(
        json.dumps(snapshot, default=dict, sort_keys=True).encode("utf-8")
    )

    assert first.status == "written"
    assert duplicate.status == "duplicate"
    assert duplicate.record_id == first.record_id
    assert first.path == (
        tmp_path
        / "datasets"
        / "channel_interactions"
        / "raw"
        / "schema-v1"
        / "date=2026-08-03"
        / f"{first.record_id}.json"
    )
    assert list(first.path.parent.glob("*.json")) == [first.path]
    envelope = json.loads(first.path.read_text(encoding="utf-8"))
    assert envelope["record_id"] == first.record_id
    assert envelope["observation"]["identity"] == {
        "instrument_id": "ibkr|future_root|ES",
        "parent_timeframe": "5m",
        "provider": "ibkr",
        "provider_contract_id": "700001",
        "provider_symbol": "ESU6",
        "route_fingerprint": "ibkr|contract|700001",
    }
    assert envelope["observation"]["micro_window"]["bar_slots"] == [100, 101, 102]
    assert (
        envelope["observation"]["frozen_level"]["slot_timeframe"]
        == CHANNEL_INTERACTION_LEVEL_SLOT_TIMEFRAME
    )


def test_journal_validates_then_writes_all_level_observations(
    tmp_path: Path,
) -> None:
    first = _observation()
    second = copy.deepcopy(first)
    second["episode_id"] = "episode-20260803-1432-upper-edge"
    second["level"]["channel_id"] = "channel-b"
    snapshot = _snapshot()
    snapshot["indicators"]["channel_master"]["research_observations"] = domain_frozen_value(
        [first, second],
        field_name="research_observations",
    )

    results = ChannelInteractionJournal(data_root=tmp_path).record_snapshot(snapshot)

    assert len(results) == 2
    assert {result.status for result in results} == {"written"}
    assert len({result.record_id for result in results}) == 2
    assert all(result.path.is_file() for result in results)


@pytest.mark.parametrize(
    ("mutation", "match"),
    (
        (
            lambda observation: observation["micro_window"]["bars"][-1].update(
                {"state": "forming", "closed": False}
            ),
            "state=confirmed",
        ),
        (
            lambda observation: observation["micro_window"].update({"bar_slots": [100, 102, 101]}),
            "strictly increasing",
        ),
        (
            lambda observation: observation.update({"decision_ts": "2026-08-03T14:32:30+00:00"}),
            "closes after decision_ts",
        ),
        (
            lambda observation: observation["frozen_level"].update({"slot_timeframe": "5m"}),
            "frozen_level is invalid",
        ),
    ),
)
def test_journal_rejects_unconfirmed_or_noncausal_micro_context(
    tmp_path: Path,
    mutation,
    match: str,
) -> None:
    observation = _observation()
    mutation(observation)

    with pytest.raises(ChannelInteractionJournalError, match=match):
        ChannelInteractionJournal(data_root=tmp_path).record_snapshot(_snapshot(observation))


def test_journal_rejects_missing_exact_snapshot_identity(tmp_path: Path) -> None:
    snapshot = _snapshot()
    snapshot["meta"]["instrument_id"] = ""

    with pytest.raises(ValueError, match="instrument_id"):
        ChannelInteractionJournal(data_root=tmp_path).record_snapshot(snapshot)


def test_journal_rejects_micro_bar_route_provenance_mismatch(
    tmp_path: Path,
) -> None:
    observation = _observation()
    observation["micro_window"]["bars"][0]["provenance"]["route_fingerprint"] = (
        "ibkr|contract|wrong"
    )

    with pytest.raises(ChannelInteractionJournalError, match="snapshot identity"):
        ChannelInteractionJournal(data_root=tmp_path).record_snapshot(_snapshot(observation))


def test_post_touch_requires_intersection_with_frozen_level(tmp_path: Path) -> None:
    observation = _observation()
    observation["phase"] = "post_touch"
    observation["touch_slot"] = 100
    observation["decision"]["phase"] = "post_touch"

    with pytest.raises(ChannelInteractionJournalError, match="does not intersect"):
        ChannelInteractionJournal(data_root=tmp_path).record_snapshot(_snapshot(observation))


def test_compaction_excludes_conflicts_and_surfaces_truncation(tmp_path: Path) -> None:
    journal = ChannelInteractionJournal(data_root=tmp_path)
    (first,) = journal.record_snapshot(_snapshot())
    conflicting = _observation()
    conflicting["features"]["distance_close_atr"] = -0.1

    with pytest.raises(ChannelInteractionJournalConflict) as raised:
        journal.record_snapshot(_snapshot(conflicting))
    assert raised.value.conflict_path.is_file()

    truncated_path = first.path.parent / "truncated.json"
    truncated_path.write_bytes(b'{"journal_schema_version":1')
    manifest = compact_channel_interaction_journal(
        "es-20260803-v1",
        data_root=tmp_path,
    )

    output = journal.dataset_root / "curated" / "es-20260803-v1"
    issues = [
        json.loads(line)
        for line in (output / "issues.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert manifest["status"] == "needs_review"
    assert manifest["counts"] == {
        "curated_records": 0,
        "issues": 2,
        "conflicting_record_ids": 1,
    }
    assert {row["issue_code"] for row in issues} == {
        "conflicting_record",
        "truncated_record",
    }
    assert (output / "records.jsonl").read_text(encoding="utf-8") == ""
    assert (output / "manifest.json").is_file()
    assert (output / "source_inventory.jsonl").is_file()
    checksum_lines = (output / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    assert any(line.endswith("  manifest.json") for line in checksum_lines)
    assert any(line.endswith("  records.jsonl") for line in checksum_lines)
    with pytest.raises(FileExistsError):
        compact_channel_interaction_journal("es-20260803-v1", data_root=tmp_path)


def test_compaction_publishes_ready_immutable_dataset(tmp_path: Path) -> None:
    journal = ChannelInteractionJournal(data_root=tmp_path)
    (result,) = journal.record_snapshot(_snapshot())

    manifest = compact_channel_interaction_journal(
        "es-20260803-ready-v1",
        data_root=tmp_path,
    )

    output = journal.dataset_root / "curated" / "es-20260803-ready-v1"
    record = json.loads((output / "records.jsonl").read_text(encoding="utf-8"))
    assert manifest["status"] == "ready"
    assert manifest["counts"]["curated_records"] == 1
    assert manifest["counts"]["issues"] == 0
    assert record["record_id"] == result.record_id


def test_same_natural_record_key_preserves_conflicting_variant(tmp_path: Path) -> None:
    journal = ChannelInteractionJournal(data_root=tmp_path)
    original = _observation()
    (first,) = journal.record_snapshot(_snapshot(original))
    variant = copy.deepcopy(original)
    variant["quality"] = {**original["quality"], "revision": 2}

    with pytest.raises(ChannelInteractionJournalConflict) as raised:
        journal.record_snapshot(_snapshot(variant))

    assert raised.value.record_id == first.record_id
    assert raised.value.conflict_path.name.startswith(f"{first.record_id}.conflict.")
    assert raised.value.conflict_path.read_bytes().endswith(b"\n")


def test_prospective_capture_freezes_first_causal_observation(tmp_path: Path) -> None:
    journal = ChannelInteractionJournal(data_root=tmp_path)
    original = _observation()
    (first,) = journal.record_prospective_snapshot(_snapshot(original))
    revised = copy.deepcopy(original)
    revised["micro_bar"]["volume"] = 250.0
    revised["micro_window"]["bars"][-1]["volume"] = 250.0

    (duplicate,) = journal.record_prospective_snapshot(_snapshot(revised))

    assert duplicate.status == "duplicate"
    assert duplicate.record_id == first.record_id
    assert duplicate.content_sha256 == first.content_sha256
    assert list(first.path.parent.glob("*.conflict.*.json")) == []
    envelope = json.loads(first.path.read_text(encoding="utf-8"))
    assert envelope["observation"]["micro_bar"]["volume"] == 100.0
    assert envelope["observation"]["micro_window"]["bars"][-1]["volume"] == 100.0


def test_analysis_process_captures_internal_observation_before_compaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aef_terminal import config as config_module
    from aef_terminal.engine.snapshot import builder as builder_module
    from aef_terminal.ui.services import market_analysis_process

    events: list[str] = []
    builder_calls: list[dict[str, object]] = []
    snapshot = {
        "meta": {},
        "indicators": {
            "channel_master": {
                "research_observations": [{"private": True}],
            },
            "option_reversal": {
                "research_observation": {"private": "option"},
            },
        },
    }

    class _Config:
        database_url = ""

    def compact(value):
        assert events == ["enrich"]
        value["indicators"]["channel_master"].pop("research_observations")
        value["indicators"]["option_reversal"].pop("research_observation")
        events.append("compact")
        return value

    def build_snapshot(**kwargs):
        builder_calls.append(kwargs)
        return snapshot

    monkeypatch.setattr(config_module, "AppConfig", _Config)
    monkeypatch.setattr(
        builder_module,
        "build_market_snapshot_from_db",
        build_snapshot,
    )
    monkeypatch.setattr(
        market_analysis_process,
        "enrich_market_analysis_snapshot",
        lambda _value: events.append("enrich"),
    )
    monkeypatch.setattr(
        market_analysis_process,
        "compact_analysis_snapshot_for_cache",
        compact,
    )

    process_result = market_analysis_process.build_market_analysis_process_result(
        "analysis-key",
        {
            "source": "ibkr",
            "interval": "5m",
            "range": "5d",
            "signal_range": "1d",
            "show_visuals": True,
            "indicator_params": {},
            "parent_canonical_generation": 7,
            "confirmed_bar_context_generations": {"1m": 3},
        },
    )
    result = json.loads(process_result.snapshot_bytes)
    research_capture = json.loads(process_result.research_capture_bytes)

    assert events == ["enrich", "compact"]
    assert builder_calls[0]["range_"] == "1d"
    assert builder_calls[0]["signal_range_"] == "1d"
    assert builder_calls[0]["include_chart_projection"] is False
    assert result["meta"]["analysis_parent_canonical_revision"] == 7
    assert result["meta"]["analysis_confirmed_bar_context_revisions"] == {"1m": 3}
    assert "research_observations" not in result["indicators"]["channel_master"]
    assert "research_observation" not in result["indicators"]["option_reversal"]
    assert research_capture["indicators"] == {
        "channel_master": {"research_observations": [{"private": True}]},
        "option_reversal": {"research_observation": {"private": "option"}},
    }


def test_analysis_capture_uses_provider_schedule_for_session_segment() -> None:
    from aef_terminal.ui.services import market_analysis_process

    snapshot = {
        "meta": {
            "instrument_id": "ibkr|future_root|ES",
            "route_fingerprint": "ibkr|contract|700001",
            "provider": "ibkr",
            "provider_contract_id": "700001",
            "timeframe": "5m",
        },
        "indicators": {
            "channel_master": {
                "research_observations": [
                    {"decision_ts": "2026-08-03T15:00:00+00:00"},
                    {"decision_ts": "2026-08-03T21:30:00+00:00"},
                ]
            }
        },
    }
    instrument = {
        "session": {
            "calendar": "CME",
            "timezone": "America/Chicago",
            "trading_intervals": [
                {
                    "status": "open",
                    "session_date": "2026-08-03",
                    "opens_at": "2026-08-03T00:00:00+00:00",
                    "closes_at": "2026-08-03T23:00:00+00:00",
                }
            ],
            "liquid_intervals": [
                {
                    "status": "open",
                    "session_date": "2026-08-03",
                    "opens_at": "2026-08-03T14:30:00+00:00",
                    "closes_at": "2026-08-03T21:00:00+00:00",
                }
            ],
        }
    }

    capture = market_analysis_process._market_analysis_research_capture(
        snapshot,
        instrument=instrument,
    )

    observations = capture["indicators"]["channel_master"]["research_observations"]
    assert observations[0]["decision_session"]["segment"] == "liquid"
    assert observations[1]["decision_session"]["segment"] == "extended"
    assert observations[0]["decision_session"]["session_key"] == ("2026-08-03")
