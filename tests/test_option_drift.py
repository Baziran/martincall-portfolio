from __future__ import annotations

import asyncio
import logging
import subprocess
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from aef_terminal.indicators.modules.option_drift import INDICATOR_MODULE
from aef_terminal.indicators.modules.option_drift.calculation import calculate_option_drift
from aef_terminal.indicators.modules.option_drift import service as option_drift_service
from aef_terminal.indicators.modules.option_drift import calculation as option_drift_calculation
from aef_terminal.indicators.registry import indicator_ids_for_shared_context
from aef_terminal.indicators.service_contract import IndicatorServiceContext


_INSTRUMENT_ID = "ibkr:conid:123"
_ROUTE_FINGERPRINT = "route:test:123"


def _contract(
    *,
    con_id: int,
    right: str,
    volume: float,
    price: float,
) -> dict[str, Any]:
    return {
        "con_id": con_id,
        "expiry": "20260821",
        "trading_class": "SPXW",
        "exchange": "SMART",
        "multiplier": 100.0,
        "strike": 6500.0,
        "right": right,
        "volume": volume,
        "reference_option_price": price,
        "reference_option_price_source": "bid_ask_mid",
        "delta": 0.50 if right == "C" else -0.50,
        "bid": price - 0.05,
        "ask": price + 0.05,
    }


def _snapshot(
    captured_at: datetime,
    *,
    call_volume: float,
    put_volume: float,
    call_price: float,
    put_price: float,
    scope_id: int = 1,
    spot: float = 6500.0,
    capture_mode: str = "request",
) -> dict[str, Any]:
    call_id = scope_id * 10 + 1
    put_id = scope_id * 10 + 2
    source = "gex:ibkr-live" if capture_mode == "live" else "gex:ibkr"
    return {
        "captured_at": captured_at.isoformat(),
        "instrument_id": _INSTRUMENT_ID,
        "route_fingerprint": _ROUTE_FINGERPRINT,
        "source": source,
        "capture_mode": capture_mode,
        "spot": spot,
        "market_data_entitlement": "live",
        "comparison_scope": {
            "capture_mode": capture_mode,
            "strike_count": 1,
            "strike_ladder": [6500.0],
            "contract_con_ids": [call_id, put_id],
            "expiries": ["20260821"],
            "futures_options": False,
            "series": [
                {
                    "expiry": "20260821",
                    "trading_class": "SPXW",
                    "exchange": "SMART",
                    "multiplier": 100.0,
                }
            ],
            "risk_free_rate": 0.04,
            "dividend_yield": 0.0,
            "market_data_entitlement": "live",
        },
        "raw": {
            "contracts": [
                _contract(
                    con_id=call_id,
                    right="C",
                    volume=call_volume,
                    price=call_price,
                ),
                _contract(
                    con_id=put_id,
                    right="P",
                    volume=put_volume,
                    price=put_price,
                ),
            ]
        },
    }


def test_option_drift_projects_call_control_without_publishing_decision_facts(monkeypatch) -> None:
    start = datetime(2026, 8, 19, 14, 0, tzinfo=UTC)
    snapshots = [
        _snapshot(
            start + timedelta(minutes=index * 5),
            call_volume=index * 10.0,
            put_volume=0.0,
            call_price=1.0 + index * 0.2,
            put_price=1.0,
        )
        for index in range(4)
    ]

    indexed: list[str] = []
    contracts = option_drift_calculation._contracts

    def indexed_contracts(snapshot):
        indexed.append(snapshot["captured_at"])
        return contracts(snapshot)

    monkeypatch.setattr(option_drift_calculation, "_contracts", indexed_contracts)
    result = calculate_option_drift(snapshots, now=start + timedelta(minutes=16))

    assert result["state_code"] == "CALL_CONTROL"
    assert result["advisory_only"] is True
    assert result["decision_eligible"] is False
    assert result["metrics"]["call_drift"] > 0
    assert result["metrics"]["put_drift"] == 0
    assert result["quality"]["accepted_intervals"] == 3
    assert "signal" not in result
    assert "candidate" not in result
    assert indexed == [snapshot["captured_at"] for snapshot in snapshots]


def test_option_drift_detects_repeated_call_put_crossing_as_conflict() -> None:
    start = datetime(2026, 8, 19, 14, 0, tzinfo=UTC)
    snapshots = [
        _snapshot(start, call_volume=0, put_volume=0, call_price=1.0, put_price=1.0),
        _snapshot(
            start + timedelta(minutes=5),
            call_volume=10,
            put_volume=0,
            call_price=1.2,
            put_price=1.0,
        ),
        _snapshot(
            start + timedelta(minutes=10),
            call_volume=10,
            put_volume=30,
            call_price=1.2,
            put_price=1.3,
        ),
        _snapshot(
            start + timedelta(minutes=15),
            call_volume=50,
            put_volume=30,
            call_price=1.5,
            put_price=1.3,
        ),
        _snapshot(
            start + timedelta(minutes=20),
            call_volume=50,
            put_volume=70,
            call_price=1.5,
            put_price=1.6,
        ),
    ]

    result = calculate_option_drift(snapshots, now=start + timedelta(minutes=21))

    assert result["state_code"] == "CONFLICT"
    assert result["metrics"]["crossings"] >= 2
    assert result["metrics"]["chop_score"] >= 70
    assert result["quality"]["accepted_intervals"] == 4


def test_option_drift_resets_at_exact_universe_change_and_marks_stale_tail() -> None:
    start = datetime(2026, 8, 19, 14, 0, tzinfo=UTC)
    snapshots = [
        _snapshot(start, call_volume=0, put_volume=0, call_price=1.0, put_price=1.0),
        _snapshot(
            start + timedelta(minutes=5),
            call_volume=10,
            put_volume=0,
            call_price=1.2,
            put_price=1.0,
        ),
        _snapshot(
            start + timedelta(minutes=10),
            call_volume=20,
            put_volume=0,
            call_price=1.4,
            put_price=1.0,
            scope_id=2,
        ),
        _snapshot(
            start + timedelta(minutes=15),
            call_volume=30,
            put_volume=0,
            call_price=1.6,
            put_price=1.0,
            scope_id=2,
        ),
    ]

    result = calculate_option_drift(snapshots, now=start + timedelta(minutes=31))

    assert result["state_code"] == "STALE"
    assert result["quality"]["accepted_intervals"] == 1
    assert result["quality"]["rejected_intervals"] == 1
    assert len(result["series"]) == 1


def test_option_drift_excludes_one_small_counter_correction_without_resetting() -> None:
    start = datetime(2026, 8, 19, 14, 0, tzinfo=UTC)
    snapshots = [
        _snapshot(start, call_volume=0, put_volume=100, call_price=1.0, put_price=1.0),
        _snapshot(
            start + timedelta(minutes=5),
            call_volume=10,
            put_volume=100,
            call_price=1.2,
            put_price=1.0,
        ),
        _snapshot(
            start + timedelta(minutes=10),
            call_volume=20,
            put_volume=100,
            call_price=1.4,
            put_price=1.0,
        ),
        _snapshot(
            start + timedelta(minutes=15),
            call_volume=30,
            put_volume=99,
            call_price=1.6,
            put_price=1.0,
        ),
    ]

    result = calculate_option_drift(snapshots, now=start + timedelta(minutes=16))

    assert result["state_code"] == "CALL_CONTROL"
    assert result["quality"]["accepted_intervals"] == 3
    assert result["quality"]["rejected_intervals"] == 0
    assert result["quality"]["corrected_volume"] == 1.0
    assert result["quality"]["corrected_contract_observations"] == 1
    assert result["metrics"]["counter_volume_coverage"] > 0.99


def test_option_drift_still_resets_on_broad_counter_reset() -> None:
    start = datetime(2026, 8, 19, 14, 0, tzinfo=UTC)
    snapshots = [
        _snapshot(start, call_volume=0, put_volume=0, call_price=1.0, put_price=1.0),
        _snapshot(
            start + timedelta(minutes=5),
            call_volume=10,
            put_volume=10,
            call_price=1.2,
            put_price=1.2,
        ),
        _snapshot(
            start + timedelta(minutes=10),
            call_volume=20,
            put_volume=20,
            call_price=1.4,
            put_price=1.4,
        ),
        _snapshot(
            start + timedelta(minutes=15),
            call_volume=0,
            put_volume=0,
            call_price=1.4,
            put_price=1.4,
        ),
    ]

    result = calculate_option_drift(snapshots, now=start + timedelta(minutes=16))

    assert result["state_code"] == "BASELINING"
    assert result["quality"]["accepted_intervals"] == 0
    assert result["quality"]["rejected_intervals"] == 1
    assert result["series"] == []


def test_option_drift_live_minute_lane_closes_each_bucket_with_its_latest_frame(
    monkeypatch,
) -> None:
    start = datetime(2026, 8, 19, 14, 0, 5, tzinfo=UTC)
    frames = [
        _snapshot(
            start,
            call_volume=0,
            put_volume=0,
            call_price=1.0,
            put_price=1.0,
            capture_mode="live",
        ),
        _snapshot(
            start + timedelta(seconds=45),
            call_volume=5,
            put_volume=0,
            call_price=1.1,
            put_price=1.0,
            capture_mode="live",
        ),
        _snapshot(
            start + timedelta(minutes=1),
            call_volume=10,
            put_volume=0,
            call_price=1.2,
            put_price=1.0,
            capture_mode="live",
        ),
        _snapshot(
            start + timedelta(minutes=2),
            call_volume=20,
            put_volume=0,
            call_price=1.4,
            put_price=1.0,
            capture_mode="live",
        ),
    ]
    monkeypatch.setattr(
        option_drift_service,
        "_require_option_drift_snapshot_payload",
        lambda payload: dict(payload),
    )
    option_drift_service._clear_live_minute_lanes()
    generation = option_drift_service._SERVICE_GENERATION

    assert (
        option_drift_service._record_live_minute_frame(
            frames[0],
            generation=generation,
            instrument_id=_INSTRUMENT_ID,
            route_fingerprint=_ROUTE_FINGERPRINT,
        )
        == []
    )
    assert (
        option_drift_service._record_live_minute_frame(
            frames[1],
            generation=generation,
            instrument_id=_INSTRUMENT_ID,
            route_fingerprint=_ROUTE_FINGERPRINT,
        )
        == []
    )
    first_closed = option_drift_service._record_live_minute_frame(
        frames[2],
        generation=generation,
        instrument_id=_INSTRUMENT_ID,
        route_fingerprint=_ROUTE_FINGERPRINT,
    )
    two_closed = option_drift_service._record_live_minute_frame(
        frames[3],
        generation=generation,
        instrument_id=_INSTRUMENT_ID,
        route_fingerprint=_ROUTE_FINGERPRINT,
    )

    assert [payload["captured_at"] for payload in first_closed] == [frames[1]["captured_at"]]
    assert [payload["captured_at"] for payload in two_closed] == [
        frames[1]["captured_at"],
        frames[2]["captured_at"],
    ]


def test_option_drift_package_is_ui_only_and_has_no_decision_context_edge() -> None:
    spec = INDICATOR_MODULE.spec

    assert spec.pipeline_stage == "ui"
    assert spec.module_type == "ui-only"
    assert spec.candidate_promoter == "none"
    assert spec.paper_tradable is False
    assert spec.default_calc is False
    assert spec.shared_context_refs == ()
    assert spec.depends_on == ()
    assert spec.required_dependencies == ()
    assert spec.optional_context == ()
    assert INDICATOR_MODULE.adapter_ref == ""
    assert "option_drift" not in indicator_ids_for_shared_context("option_flow")
    capture_mode = next(control for control in spec.controls if control.key == "captureMode")
    assert capture_mode.default == "live"
    assert capture_mode.options == ("live", "request")

    client_source = Path("src/aef_terminal/indicators/modules/option_drift/client.js").read_text(
        encoding="utf-8"
    )
    assert '!indicatorCalcForId("option_drift")' in client_source
    assert "countsAsSignal: false" in client_source
    assert "effectiveGexContextMode" not in client_source
    assert 'arrow: "↑"' in client_source
    assert 'arrow: "↓"' in client_source
    assert "context_lines" in client_source
    assert "Arrows are market bias, not value movement" in client_source
    assert "CALL sign map: + means bullish call strength" in client_source
    assert "PUT sign map: + means bearish put strength" in client_source
    assert "BALANCE sign map: + means bullish call dominance" in client_source
    assert "not change since the previous refresh" in client_source
    assert "CONF ${Math.round" in client_source
    assert "C${Math.round" not in client_source
    assert "const intervalLabel = `#${Number" in client_source
    assert "accepted comparison-interval counter" in client_source
    assert "ACT ${optionDriftActivityMoney" in client_source
    assert "SHARE ${(Number(metrics.balance_ratio" in client_source
    assert "↔ beside CHOP is a two-sided/conflict glyph" in client_source
    assert " Δ" not in client_source
    assert "optionDriftTheorySections" in client_source
    assert "THEORY · ${String(kind" in client_source
    assert "Остаток = изменение цены опциона" in client_source
    assert "live_minute_active" in client_source


def test_option_drift_table_explains_current_direction_and_unambiguous_counters(
    tmp_path: Path,
) -> None:
    script = tmp_path / "option-drift-directional-tooltips.js"
    script.write_text(
        """
const assert = require("node:assert/strict");
const fs = require("node:fs");

(async () => {
  const instrumentId = "ibkr|future_root|ES|CME|USD|ES";
  const routeFingerprint = `${instrumentId}|current:123`;
  const state = {
    instrumentId,
    snapshot: { meta: { instrument_id: instrumentId, route_fingerprint: routeFingerprint } },
    indicators: { optionDrift: { captureMode: "live", tablePosition: "bottom" } },
  };
  let lifecycleSync = null;
  let overlayContribution = null;
  const exactIdentityText = value => String(value || "");
  const instrumentRouteFingerprint = () => routeFingerprint;
  const indicatorCalcForId = () => true;
  const indicatorVisibleForId = () => true;
  const fetchJson = async () => ({
    ok: true,
    advisory_only: true,
    decision_eligible: false,
    instrument_id: instrumentId,
    route_fingerprint: routeFingerprint,
    capture_mode: "live",
    state_code: "CALL_CONTROL",
    revision: "directional-test",
    metrics: {
      call_drift: 65000,
      put_drift: 21000,
      balance: 44000,
      call_gross_premium: 42000,
      put_gross_premium: 119000,
      balance_ratio: 0.27,
      chop_score: 34,
      confidence: 0.64,
      crossings: 1,
      price_efficiency: 0.58,
      two_sided_ratio: 0.71,
    },
    quality: { accepted_intervals: 4, live_minute_active: true, live_minute_samples: 3 },
  });
  const bumpChartRenderVersion = () => {};
  const renderCharts = () => {};
  const requestErrorMessage = error => String(error);
  const registerIndicatorRuntimeStateRef = () => {};
  const registerIndicatorLifecycleSync = (_id, callback) => { lifecycleSync = callback; };
  const registerIndicatorProcessEffect = () => {};
  const registerIndicatorControlEffect = () => {};
  const registerIndicatorPanelRenderer = () => {};
  const registerIndicatorOverlayContribution = (_id, contribution) => {
    overlayContribution = contribution;
  };

  const source = fs.readFileSync(
    "src/aef_terminal/indicators/modules/option_drift/client.js",
    "utf8",
  );
  eval(source);
  await lifecycleSync({ force: true, snapshot: state.snapshot });
  const [overlay] = overlayContribution.collect(state.snapshot);
  const [status, call, put, balance, chop] = overlay.table.columns;

  assert.deepEqual(status.cells, ["STATE", "↑ CALL", "CONF 64%"]);
  assert.deepEqual(call.cells, ["CALL", "↑ +$65K", "ACT $42K"]);
  assert.deepEqual(put.cells, ["PUT", "↓ +$21K", "ACT $119K"]);
  assert.deepEqual(balance.cells, ["BALANCE", "↑ +$44K", "SHARE 27%"]);
  assert.deepEqual(chop.cells, ["CHOP", "↔ 34%", "#4 · 1m"]);
  assert.ok(call.context_lines.some(line => line.includes("NOW: ↑ BULLISH")));
  assert.ok(put.context_lines.some(line => line.includes("NOW: ↓ BEARISH")));
  assert.ok(balance.context_lines.some(line => line.includes("BALANCE sign map")));
  assert.ok(chop.context_lines.some(line => line.includes("LOW CHOP")));
  assert.ok(chop.context_lines.some(line => line.includes("accepted comparison-interval counter")));
  assert.ok(overlay.table.context_lines.some(line => line.includes("Arrows are market bias")));
  assert.equal(status.tooltip_sections[0].title, "THEORY · STATE");
  assert.ok(status.tooltip_sections[0].lines.some(line => line.text.includes("CONF измеряет")));
  assert.equal(call.tooltip_sections[0].title, "THEORY · CALL");
  assert.ok(call.tooltip_sections[0].lines.some(line => line.text.includes("+CALL → ↑ bullish")));
  assert.equal(put.tooltip_sections[0].title, "THEORY · PUT");
  assert.ok(put.tooltip_sections[0].lines.some(line => line.text.includes("+PUT → ↓ bearish")));
  assert.equal(balance.tooltip_sections[0].title, "THEORY · BALANCE");
  assert.ok(balance.tooltip_sections[0].lines.some(line => line.text.includes("BALANCE = CALL DRIFT")));
  assert.equal(chop.tooltip_sections[0].title, "THEORY · CHOP");
  assert.ok(chop.tooltip_sections[0].lines.some(line => line.text.includes("#N — число")));
  assert.equal(overlay.table.tooltip_sections[0].title, "THEORY · TABLE");
  assert.ok(!JSON.stringify(overlay).includes(" Δ"));
})().catch(error => {
  console.error(error);
  process.exit(1);
});
""",
        encoding="utf-8",
    )
    result = subprocess.run(["node", str(script)], capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr


def test_option_drift_service_reads_one_exact_gex_lane_on_demand(monkeypatch) -> None:
    start = datetime(2026, 8, 19, 14, 0, tzinfo=UTC)
    snapshots = [
        _snapshot(
            start + timedelta(minutes=index * 5),
            call_volume=index * 10.0,
            put_volume=0,
            call_price=1.0 + index * 0.2,
            put_price=1.0,
        )
        for index in range(4)
    ]
    observed: dict[str, Any] = {}

    class Store:
        def read_gex_snapshots(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            observed["args"] = args
            observed["kwargs"] = kwargs
            return [
                {
                    "captured_at": payload["captured_at"],
                    "source": payload["source"],
                    "instrument_id": payload["instrument_id"],
                    "route_fingerprint": payload["route_fingerprint"],
                    "payload": payload,
                }
                for payload in snapshots
            ]

    async def direct_thread_call(function: Any, *args: Any, **kwargs: Any) -> Any:
        return function(*args, **kwargs)

    monkeypatch.setattr(option_drift_service, "run_physical_thread_call", direct_thread_call)
    monkeypatch.setattr(
        option_drift_service,
        "_require_option_drift_snapshot_payload",
        lambda payload: dict(payload),
    )
    contribution = option_drift_service.configure_service(
        IndicatorServiceContext(
            logger=logging.getLogger("test.option_drift"),
            store_factory=Store,
            server_sleeping=lambda: False,
            client_settings_snapshot=dict,
        )
    )

    payload = asyncio.run(
        option_drift_service.option_drift_payload(
            instrument_id=_INSTRUMENT_ID,
            route_fingerprint=_ROUTE_FINGERPRINT,
            capture_mode="request",
            now=start + timedelta(minutes=16),
        )
    )

    assert contribution.tasks == ()
    assert observed["kwargs"] == {
        "limit": 256,
        "source": "gex:ibkr",
        "sources": None,
        "include_raw": True,
    }
    assert payload["state_code"] == "CALL_CONTROL"
    assert payload["source"] == "gex:ibkr"
    assert payload["advisory_only"] is True
    assert payload["decision_eligible"] is False
    assert payload["revision"]


def test_option_drift_projection_runs_off_loop_and_settles_before_cancellation(monkeypatch) -> None:
    release = threading.Event()
    completed = threading.Event()

    class Store:
        def read_gex_snapshots(self, *_args, **_kwargs):
            return []

    option_drift_service.configure_service(
        IndicatorServiceContext(
            logger=logging.getLogger("test.option_drift"),
            store_factory=Store,
            server_sleeping=lambda: False,
            client_settings_snapshot=dict,
        )
    )

    async def run():
        started = asyncio.Event()
        loop = asyncio.get_running_loop()
        owner_thread = threading.get_ident()

        def calculate(snapshots, *, now):
            assert threading.get_ident() != owner_thread
            loop.call_soon_threadsafe(started.set)
            assert release.wait(5)
            completed.set()
            return calculate_option_drift(snapshots, now=now)

        monkeypatch.setattr(option_drift_service, "calculate_option_drift", calculate)
        task = asyncio.create_task(
            option_drift_service.option_drift_payload(
                instrument_id=_INSTRUMENT_ID,
                route_fingerprint=_ROUTE_FINGERPRINT,
                capture_mode="request",
            )
        )
        await asyncio.wait_for(started.wait(), timeout=2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    try:
        asyncio.run(run())
    finally:
        release.set()
    assert completed.is_set()


def test_option_drift_worker_cannot_restore_a_retired_minute_lane(monkeypatch) -> None:
    release = threading.Event()
    frame = _snapshot(
        datetime(2026, 8, 19, 14, 0, tzinfo=UTC),
        call_volume=1,
        put_volume=1,
        call_price=1,
        put_price=1,
        capture_mode="live",
    )
    monkeypatch.setattr(option_drift_service, "live_gex_frame_snapshot", lambda *_: frame)
    monkeypatch.setattr(
        option_drift_service,
        "_require_option_drift_snapshot_payload",
        lambda payload: dict(payload),
    )

    async def run():
        started = asyncio.Event()
        loop = asyncio.get_running_loop()

        class Store:
            def read_gex_snapshots(self, *_args, **_kwargs):
                loop.call_soon_threadsafe(started.set)
                assert release.wait(5)
                return []

        option_drift_service.configure_service(
            IndicatorServiceContext(
                logger=logging.getLogger("test.option_drift"),
                store_factory=Store,
                server_sleeping=lambda: False,
                client_settings_snapshot=dict,
            )
        )
        task = asyncio.create_task(
            option_drift_service.option_drift_payload(
                instrument_id=_INSTRUMENT_ID,
                route_fingerprint=_ROUTE_FINGERPRINT,
                capture_mode="live",
                now=datetime(2026, 8, 19, 14, 1, tzinfo=UTC),
            )
        )
        await asyncio.wait_for(started.wait(), timeout=2)
        await option_drift_service._shutdown_service()
        release.set()
        with pytest.raises(option_drift_service.OptionDriftServiceError) as rejected:
            await task
        assert rejected.value.code == "OPTION_DRIFT_SERVICE_CHANGED"
        assert not option_drift_service._LIVE_MINUTE_LANES

    try:
        asyncio.run(run())
    finally:
        release.set()


def test_option_drift_service_adds_only_closed_live_minute_frames(monkeypatch) -> None:
    start = datetime(2026, 8, 19, 14, 0, 10, tzinfo=UTC)
    persisted = [
        _snapshot(
            start - timedelta(minutes=10 - index * 5),
            call_volume=0,
            put_volume=0,
            call_price=1.0,
            put_price=1.0,
            capture_mode="live",
        )
        for index in range(2)
    ]
    frames = [
        _snapshot(
            start + timedelta(minutes=index),
            call_volume=index * 10.0,
            put_volume=0,
            call_price=1.0 + index * 0.2,
            put_price=1.0,
            capture_mode="live",
        )
        for index in range(4)
    ]
    current_frame = {"index": 0}

    class Store:
        def read_gex_snapshots(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            return [
                {
                    "captured_at": payload["captured_at"],
                    "source": payload["source"],
                    "instrument_id": payload["instrument_id"],
                    "route_fingerprint": payload["route_fingerprint"],
                    "payload": payload,
                }
                for payload in persisted
            ]

    async def direct_thread_call(function: Any, *args: Any, **kwargs: Any) -> Any:
        return function(*args, **kwargs)

    def current_live_frame(_instrument_id: str, _route_fingerprint: str) -> dict[str, Any]:
        return frames[current_frame["index"]]

    monkeypatch.setattr(option_drift_service, "run_physical_thread_call", direct_thread_call)
    monkeypatch.setattr(option_drift_service, "live_gex_frame_snapshot", current_live_frame)
    monkeypatch.setattr(
        option_drift_service,
        "_require_option_drift_snapshot_payload",
        lambda payload: dict(payload),
    )
    option_drift_service.configure_service(
        IndicatorServiceContext(
            logger=logging.getLogger("test.option_drift.live"),
            store_factory=Store,
            server_sleeping=lambda: False,
            client_settings_snapshot=dict,
        )
    )

    results = []
    for index, frame in enumerate(frames):
        current_frame["index"] = index
        results.append(
            asyncio.run(
                option_drift_service.option_drift_payload(
                    instrument_id=_INSTRUMENT_ID,
                    route_fingerprint=_ROUTE_FINGERPRINT,
                    capture_mode="live",
                    now=datetime.fromisoformat(frame["captured_at"]) + timedelta(seconds=1),
                )
            )
        )

    assert [result["quality"]["live_minute_samples"] for result in results] == [0, 1, 2, 3]
    assert results[-1]["quality"]["live_minute_active"] is True
    assert results[-1]["quality"]["target_interval_seconds"] == 60
    assert results[-1]["quality"]["accepted_intervals"] >= 3
    assert results[-1]["series"][-1]["ts"] == frames[-2]["captured_at"]


def test_concurrent_option_drift_clients_share_one_ordered_minute_lane(monkeypatch) -> None:
    start = datetime(2026, 8, 19, 14, 0, 10, tzinfo=UTC)
    frames = [
        _snapshot(
            start + timedelta(minutes=index),
            call_volume=index * 10.0,
            put_volume=0,
            call_price=1.0 + index * 0.2,
            put_price=1.0,
            capture_mode="live",
        )
        for index in range(4)
    ]
    current_frame = {"index": 0}
    readers = threading.Barrier(2)

    class Store:
        def read_gex_snapshots(self, *_args, **_kwargs):
            readers.wait(timeout=5)
            return []

    monkeypatch.setattr(
        option_drift_service,
        "live_gex_frame_snapshot",
        lambda *_: frames[current_frame["index"]],
    )
    monkeypatch.setattr(
        option_drift_service,
        "_require_option_drift_snapshot_payload",
        lambda payload: dict(payload),
    )
    option_drift_service.configure_service(
        IndicatorServiceContext(
            logger=logging.getLogger("test.option_drift.concurrent"),
            store_factory=Store,
            server_sleeping=lambda: False,
            client_settings_snapshot=dict,
        )
    )

    async def run():
        for index, frame in enumerate(frames):
            current_frame["index"] = index

            async def request():
                return await option_drift_service.option_drift_payload(
                    instrument_id=_INSTRUMENT_ID,
                    route_fingerprint=_ROUTE_FINGERPRINT,
                    capture_mode="live",
                    now=datetime.fromisoformat(frame["captured_at"]) + timedelta(seconds=1),
                )

            first, second = await asyncio.gather(request(), request())
            assert first == second
            assert first["quality"]["live_minute_samples"] == index
        return first

    result = asyncio.run(run())
    assert result["series"][-1]["ts"] == frames[-2]["captured_at"]
    assert result["quality"]["accepted_intervals"] == 2
