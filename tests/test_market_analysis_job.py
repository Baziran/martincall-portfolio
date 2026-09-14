from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any

import pytest

from aef_terminal.data.providers import route_instrument
from aef_terminal.indicators.registry import indicator_manifest
from aef_terminal.runtime.chart_events import (
    ChartBarsGenerationChanged,
    ChartBarsGenerationScope,
    begin_chart_bars_write,
    chart_bars_updated_generation,
    end_chart_bars_write,
)
from aef_terminal.ui.services import (
    market_analysis_compaction,
    market_analysis_job,
    market_snapshot_enrichment,
)
from aef_terminal.ui.services.market_analysis_job import run_market_analysis_job
from tests.provider_payloads import ibkr_future_payload


_PAPER_INSTRUMENT_ID = "ibkr|future_root|ES|CME|USD|ES"
_PAPER_ROUTE_FINGERPRINT = "ibkr|future_root|ES|CME|USD|ES|current:1"


def _analysis_bar(ts: str = "2026-01-01T14:30:00+00:00") -> dict[str, Any]:
    return {
        "symbol": "ES",
        "ts": ts,
        "open": 100.0,
        "high": 101.0,
        "low": 99.0,
        "close": 100.5,
        "volume": 10.0,
        "timeframe": "5m",
        "source": "ibkr",
        "closed": True,
        "state": "confirmed",
    }


def _paper_generation_scopes(generation: int = 0) -> tuple[ChartBarsGenerationScope, ...]:
    return (
        ChartBarsGenerationScope(
            instrument_id=_PAPER_INSTRUMENT_ID,
            route_fingerprint=_PAPER_ROUTE_FINGERPRINT,
            interval="5m",
            generation=generation,
        ),
    )


def _paper_generation_meta(generation: int = 0) -> dict[str, Any]:
    return {
        "analysis_parent_canonical_revision": generation,
        "analysis_confirmed_bar_context_revisions": {},
    }


def test_market_analysis_job_writes_ready_snapshot() -> None:
    async def run() -> None:
        lock = asyncio.Lock()
        tasks: dict[str, asyncio.Task[Any]] = {}
        identities: dict[str, tuple[Any, ...]] = {"job": ("old",)}
        wanted = {"job": {"payload": {"symbol": "ES"}}}
        cache: dict[str, dict[str, Any]] = {}

        def build_snapshot_bytes(key: str, payload: dict[str, Any]) -> bytes:
            snapshot = {
                "bars": [_analysis_bar()],
                "meta": {
                    "analysis_key": key,
                    "analysis_parent_canonical_revision": payload["parent_canonical_generation"],
                    "enriched": True,
                },
            }
            return json.dumps(
                market_analysis_compaction.compact_analysis_snapshot_for_cache(snapshot),
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")

        await run_market_analysis_job(
            "job",
            {
                "source": "ibkr",
                "instrument_id": "ibkr|future_root|ES|CME|USD|ES",
                "route_fingerprint": "ibkr|future_root|ES|CME|USD|ES|current:1",
                "symbol": "ES",
                "interval": "5m",
                "parent_canonical_generation": 0,
                "range": "1d",
                "signal_range": "3d",
                "show_visuals": True,
                "indicator_params": {},
                "market_version": {
                    "latest_ts": "2026-01-01T14:30:00+00:00",
                    "client": "chart-r1",
                },
            },
            lock=lock,
            tasks=tasks,
            identities=identities,
            wanted=wanted,
            cache=cache,
            trim_cache=lambda _now: None,
            on_error=lambda _key, _exc: None,
            auto_paper_trading_enabled=lambda _instrument_id: False,
            build_snapshot_bytes=build_snapshot_bytes,
        )

        assert "job" not in tasks
        assert "job" not in identities
        assert cache["job"]["status"] == "ready"
        assert cache["job"]["instrument_id"] == "ibkr|future_root|ES|CME|USD|ES"
        assert cache["job"]["route_fingerprint"] == "ibkr|future_root|ES|CME|USD|ES|current:1"
        assert cache["job"]["market_version"] == {
            "latest_ts": "2026-01-01T14:30:00+00:00",
            "client": "chart-r1",
        }
        assert cache["job"]["size_bytes"] == len(cache["job"]["snapshot_json"])
        snapshot = json.loads(cache["job"]["snapshot_json"])
        assert snapshot["meta"]["analysis_key"] == "job"
        assert snapshot["meta"]["analysis_parent_canonical_revision"] == 0
        assert snapshot["meta"]["enriched"] is True
        assert snapshot["meta"]["analysis_only"] is True
        assert snapshot["meta"]["analysis_bars_included"] is False
        assert snapshot["meta"]["analysis_bar_count"] == 1
        assert snapshot["meta"]["analysis_latest_ts"] == "2026-01-01T14:30:00+00:00"
        assert len(snapshot["meta"]["analysis_window_hash"]) == 32
        assert "bars" not in snapshot
        assert "snapshot" not in cache["job"]

    asyncio.run(run())


def test_analysis_bar_window_rejects_partial_provisional_and_unordered_rows() -> None:
    bar = _analysis_bar()

    with pytest.raises(TypeError, match="only mappings"):
        market_analysis_compaction.analysis_bar_window([object()])
    with pytest.raises(ValueError, match="display placeholders"):
        market_analysis_compaction.analysis_bar_window([{**bar, "missing": True}])
    with pytest.raises(ValueError, match="only confirmed"):
        market_analysis_compaction.analysis_bar_window(
            [{**bar, "closed": False, "state": "forming"}]
        )
    with pytest.raises(ValueError, match="timestamp-ordered"):
        market_analysis_compaction.analysis_bar_window([bar, dict(bar)])


def test_analysis_bar_window_is_the_exact_compaction_identity() -> None:
    first = _analysis_bar("2026-01-01T14:25:00+00:00")
    latest = _analysis_bar()
    window = market_analysis_compaction.analysis_bar_window([first, latest])
    snapshot = {
        "bars": [first, latest],
        "meta": {"analysis_ts": latest["ts"]},
    }

    compacted = market_analysis_compaction.compact_analysis_snapshot_for_cache(snapshot)

    expected_meta = {
        "analysis_window_hash": window.window_hash,
        "analysis_bar_count": 2,
        "analysis_first_ts": first["ts"],
        "analysis_latest_ts": latest["ts"],
    }
    assert {key: compacted["meta"][key] for key in expected_meta} == expected_meta
    assert len(window.window_hash) == 32
    assert "bars" not in compacted


def test_analysis_compaction_rejects_missing_contract_and_timestamp_disagreement() -> None:
    with pytest.raises(TypeError, match="meta must be a mapping"):
        market_analysis_compaction.compact_analysis_snapshot_for_cache({"bars": []})
    with pytest.raises(ValueError, match="bars are required"):
        market_analysis_compaction.compact_analysis_snapshot_for_cache({"meta": {}})
    with pytest.raises(ValueError, match="timestamp disagrees"):
        market_analysis_compaction.compact_analysis_snapshot_for_cache(
            {
                "bars": [_analysis_bar()],
                "meta": {"analysis_ts": "2026-01-01T14:25:00+00:00"},
            }
        )


def test_channel_master_job_hydrates_all_drawings_at_captured_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        generation = 4
        instrument = ibkr_future_payload("ES", con_id=991017)
        route = route_instrument(instrument)
        drawing = {
            "id": "channel-1",
            "type": "channel",
            "points": [
                {"ts": "2026-07-01T00:00:00Z", "price": 100.0},
                {"ts": "2026-07-01T00:05:00Z", "price": 101.0},
            ],
            "offsetPoint": {
                "anchorTs": "2026-07-01T00:05:00Z",
                "barOffset": 3,
                "price": 110.0,
            },
        }
        normalized = {
            **drawing,
            "anchorResolution": {
                "status": "resolved",
                "canonicalGeneration": generation,
            },
        }
        store_reads: list[tuple[str, str, str]] = []
        normalize_generations: list[int] = []
        built_payloads: list[dict[str, Any]] = []

        class Store:
            def read_drawings(
                self,
                instrument_id: str,
                interval: str,
                *,
                route_fingerprint: str,
            ) -> list[dict[str, Any]]:
                store_reads.append((instrument_id, interval, route_fingerprint))
                return [drawing]

        def normalize(
            _store: Any,
            instrument_id: str,
            interval: str,
            drawings: list[dict[str, Any]],
            **kwargs: Any,
        ) -> list[dict[str, Any]]:
            assert instrument_id == route.instrument_id
            assert interval == "5m"
            assert drawings == [drawing]
            assert kwargs["route_fingerprint"] == route.fingerprint
            normalize_generations.append(generation)
            return [
                {
                    **normalized,
                    "id": f"channel-{index}",
                    "anchorResolution": {
                        **normalized["anchorResolution"],
                        "canonicalGeneration": generation,
                    },
                }
                for index in range(9)
            ]

        def require_generation(
            expected: int,
            *_args: Any,
            **_kwargs: Any,
        ) -> int:
            if expected != generation:
                raise ChartBarsGenerationChanged(
                    expected=expected,
                    observed=generation,
                )
            return generation

        def build_snapshot_bytes(
            _key: str,
            payload: dict[str, Any],
        ) -> bytes:
            built_payloads.append(payload)
            return b"{}"

        monkeypatch.setattr(
            market_analysis_job,
            "chart_bars_updated_generation",
            lambda *_args, **_kwargs: generation,
        )
        monkeypatch.setattr(
            market_analysis_job,
            "require_chart_bars_generation",
            require_generation,
        )
        wanted = {"job": {"payload": {}}}
        cache: dict[str, dict[str, Any]] = {}
        await run_market_analysis_job(
            "job",
            {
                "source": route.provider,
                "instrument_id": route.instrument_id,
                "route_fingerprint": route.fingerprint,
                "interval": "5m",
                "parent_canonical_generation": generation,
                "indicator_params": {
                    "channel_master": {"enabled": True},
                },
                "instrument": instrument,
            },
            lock=asyncio.Lock(),
            tasks={},
            identities={},
            wanted=wanted,
            cache=cache,
            trim_cache=lambda _now: None,
            on_error=lambda _key, _exc: None,
            auto_paper_trading_enabled=lambda _instrument_id: False,
            build_snapshot_bytes=build_snapshot_bytes,
            store_factory=Store,
            normalize_drawing_anchors=normalize,
        )

        assert store_reads == [
            (route.instrument_id, "5m", route.fingerprint),
        ]
        assert normalize_generations == [4]
        assert [
            payload["indicator_params"]["manual_channel_canonical_generation"]
            for payload in built_payloads
        ] == [4]
        assert built_payloads[-1]["indicator_params"]["manual_channels"][0]["offsetPoint"] == {
            "anchorTs": "2026-07-01T00:05:00Z",
            "barOffset": 3,
            "price": 110.0,
        }
        assert [
            channel["id"] for channel in built_payloads[-1]["indicator_params"]["manual_channels"]
        ] == [f"channel-{index}" for index in range(9)]
        assert cache["job"]["status"] == "ready"

    asyncio.run(run())


def test_non_channel_job_strips_untrusted_manual_channel_payload() -> None:
    async def run() -> None:
        built_payloads: list[dict[str, Any]] = []

        await run_market_analysis_job(
            "job",
            {
                "source": "ibkr",
                "instrument_id": "ibkr|future_root|ES|CME|USD|ES",
                "route_fingerprint": ("ibkr|future_root|ES|CME|USD|ES|current:1"),
                "interval": "5m",
                "parent_canonical_generation": 0,
                "indicator_params": {
                    "channel_master": {"enabled": False},
                    "manual_channels": [{"id": "stale"}],
                    "manual_channel_canonical_generation": 99,
                },
            },
            lock=asyncio.Lock(),
            tasks={},
            identities={},
            wanted={"job": {"payload": {}}},
            cache={},
            trim_cache=lambda _now: None,
            on_error=lambda _key, _exc: None,
            auto_paper_trading_enabled=lambda _instrument_id: False,
            build_snapshot_bytes=lambda _key, payload: built_payloads.append(payload) or b"{}",
        )

        assert "manual_channels" not in built_payloads[0]["indicator_params"]
        assert "manual_channel_canonical_generation" not in built_payloads[0]["indicator_params"]

    asyncio.run(run())


def test_non_channel_job_blocks_parent_active_write_before_db_builder() -> None:
    async def run() -> None:
        instrument_id = "test|analysis|active-parent"
        fingerprint = "test|analysis|active-parent|route"
        generation = chart_bars_updated_generation(
            "5m",
            fingerprint,
            instrument_id=instrument_id,
        )
        guard = begin_chart_bars_write(
            generation,
            "5m",
            fingerprint,
            instrument_id=instrument_id,
        )
        built = False
        cache: dict[str, dict[str, Any]] = {}

        def build_snapshot_bytes(
            _key: str,
            _payload: dict[str, Any],
        ) -> bytes:
            nonlocal built
            built = True
            return b"{}"

        try:
            with pytest.raises(
                ChartBarsGenerationChanged,
                match="CHART_BARS_WRITE_IN_PROGRESS",
            ):
                await run_market_analysis_job(
                    "job",
                    {
                        "source": "test",
                        "instrument_id": instrument_id,
                        "route_fingerprint": fingerprint,
                        "interval": "5m",
                        "parent_canonical_generation": generation,
                        "indicator_params": {},
                    },
                    lock=asyncio.Lock(),
                    tasks={},
                    identities={},
                    wanted={"job": {"payload": {}}},
                    cache=cache,
                    trim_cache=lambda _now: None,
                    on_error=lambda _key, _exc: None,
                    auto_paper_trading_enabled=lambda _instrument_id: True,
                    build_snapshot_bytes=build_snapshot_bytes,
                )
        finally:
            end_chart_bars_write(guard)

        assert built is False
        assert cache == {}

    asyncio.run(run())


def test_non_channel_job_rejects_parent_generation_advance_after_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        generation = 4
        built_payloads: list[dict[str, Any]] = []
        paper_checks: list[str] = []
        cache: dict[str, dict[str, Any]] = {}

        def require_generation(
            expected: int,
            *_args: Any,
            **_kwargs: Any,
        ) -> int:
            if expected != generation:
                raise ChartBarsGenerationChanged(
                    expected=expected,
                    observed=generation,
                )
            return generation

        def build_snapshot_bytes(
            _key: str,
            payload: dict[str, Any],
        ) -> bytes:
            nonlocal generation
            built_payloads.append(payload)
            generation = 5
            return b'{"trade_setup":{"action":"GO"}}'

        monkeypatch.setattr(
            market_analysis_job,
            "require_chart_bars_generation",
            require_generation,
        )
        with pytest.raises(ChartBarsGenerationChanged):
            await run_market_analysis_job(
                "job",
                {
                    "source": "test",
                    "instrument_id": "test|analysis|generation-advance",
                    "route_fingerprint": ("test|analysis|generation-advance|route"),
                    "interval": "5m",
                    "parent_canonical_generation": 4,
                    "indicator_params": {},
                },
                lock=asyncio.Lock(),
                tasks={},
                identities={},
                wanted={"job": {"payload": {}}},
                cache=cache,
                trim_cache=lambda _now: None,
                on_error=lambda _key, _exc: None,
                auto_paper_trading_enabled=lambda instrument_id: (
                    paper_checks.append(instrument_id) or True
                ),
                build_snapshot_bytes=build_snapshot_bytes,
            )

        assert len(built_payloads) == 1
        assert paper_checks == []
        assert cache == {}

    asyncio.run(run())


def test_non_channel_job_blocks_active_lower_timeframe_before_db_builder() -> None:
    async def run() -> None:
        instrument_id = "test|analysis|active-context"
        fingerprint = "test|analysis|active-context|route"
        parent_generation = chart_bars_updated_generation(
            "5m",
            fingerprint,
            instrument_id=instrument_id,
        )
        context_generation = chart_bars_updated_generation(
            "1m",
            fingerprint,
            instrument_id=instrument_id,
        )
        guard = begin_chart_bars_write(
            context_generation,
            "1m",
            fingerprint,
            instrument_id=instrument_id,
        )
        built = False

        def build_snapshot_bytes(
            _key: str,
            _payload: dict[str, Any],
        ) -> bytes:
            nonlocal built
            built = True
            return b"{}"

        try:
            with pytest.raises(
                ChartBarsGenerationChanged,
                match="CHART_BARS_WRITE_IN_PROGRESS",
            ):
                await run_market_analysis_job(
                    "job",
                    {
                        "source": "test",
                        "instrument_id": instrument_id,
                        "route_fingerprint": fingerprint,
                        "interval": "5m",
                        "parent_canonical_generation": parent_generation,
                        "confirmed_bar_context_generations": {
                            "1m": context_generation,
                        },
                        "indicator_params": {},
                    },
                    lock=asyncio.Lock(),
                    tasks={},
                    identities={},
                    wanted={"job": {"payload": {}}},
                    cache={},
                    trim_cache=lambda _now: None,
                    on_error=lambda _key, _exc: None,
                    auto_paper_trading_enabled=lambda _instrument_id: False,
                    build_snapshot_bytes=build_snapshot_bytes,
                )
        finally:
            end_chart_bars_write(guard)

        assert built is False

    asyncio.run(run())


def test_market_analysis_job_submits_research_after_causal_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    submitted: list[tuple[str, bytes]] = []

    async def build_in_process(
        _key: str,
        _payload: dict[str, Any],
    ) -> market_analysis_job.MarketAnalysisProcessResult:
        return market_analysis_job.MarketAnalysisProcessResult(
            snapshot_bytes=b"{}",
            research_capture_bytes=b'{"meta":{},"indicators":{}}',
        )

    monkeypatch.setattr(
        market_analysis_job,
        "run_market_analysis_process",
        build_in_process,
    )

    async def run() -> None:
        lock = asyncio.Lock()
        wanted = {"job": {"payload": {}}}
        cache: dict[str, dict[str, Any]] = {}
        await run_market_analysis_job(
            "job",
            {
                "source": "ibkr",
                "instrument_id": "ibkr|future_root|ES|CME|USD|ES",
                "route_fingerprint": ("ibkr|future_root|ES|CME|USD|ES|current:1"),
                "interval": "5m",
                "parent_canonical_generation": 0,
            },
            lock=lock,
            tasks={},
            identities={},
            wanted=wanted,
            cache=cache,
            trim_cache=lambda _now: None,
            on_error=lambda _key, _exc: None,
            auto_paper_trading_enabled=lambda _instrument_id: False,
            submit_research_capture=lambda scope, value: submitted.append((scope, value)),
        )

    asyncio.run(run())

    assert submitted == [
        (
            "ibkr|future_root|ES|CME|USD|ES|ibkr|future_root|ES|CME|USD|ES|current:1|5m",
            b'{"meta":{},"indicators":{}}',
        )
    ]


def test_research_only_analysis_captures_without_paper_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    submitted: list[tuple[str, bytes]] = []
    paper_calls: list[str] = []

    async def build_in_process(
        _key: str,
        _payload: dict[str, Any],
    ) -> market_analysis_job.MarketAnalysisProcessResult:
        return market_analysis_job.MarketAnalysisProcessResult(
            snapshot_bytes=b"{}",
            research_capture_bytes=b'{"meta":{},"indicators":{}}',
        )

    monkeypatch.setattr(
        market_analysis_job,
        "run_market_analysis_process",
        build_in_process,
    )
    monkeypatch.setattr(
        market_analysis_job,
        "_sync_paper_from_analysis_snapshot_bytes",
        lambda *_args, **_kwargs: paper_calls.append("paper"),
    )

    async def run() -> None:
        await run_market_analysis_job(
            "job",
            {
                "source": "ibkr",
                "instrument_id": "ibkr|future_root|ES|CME|USD|ES",
                "route_fingerprint": "ibkr|future_root|ES|CME|USD|ES|current:1",
                "interval": "5m",
                "parent_canonical_generation": 0,
                "analysis_effects": "research_capture",
            },
            lock=asyncio.Lock(),
            tasks={},
            identities={},
            wanted={"job": {"payload": {}}},
            cache={},
            trim_cache=lambda _now: None,
            on_error=lambda _key, _exc: None,
            auto_paper_trading_enabled=lambda _instrument_id: (_ for _ in ()).throw(
                AssertionError("research capture must not evaluate paper permissions")
            ),
            submit_research_capture=lambda scope, value: submitted.append((scope, value)),
        )

    asyncio.run(run())

    assert len(submitted) == 1
    assert paper_calls == []


def test_market_analysis_job_settles_physical_process_before_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        physical_started = asyncio.Event()
        physical_release = asyncio.Event()
        physical_cancelled = False

        async def build_in_process(_key: str, _payload: dict[str, Any]) -> bytes:
            nonlocal physical_cancelled
            physical_started.set()
            try:
                await physical_release.wait()
            except asyncio.CancelledError:
                physical_cancelled = True
                raise
            return market_analysis_job.MarketAnalysisProcessResult(
                snapshot_bytes=b"{}",
            )

        monkeypatch.setattr(
            market_analysis_job,
            "run_market_analysis_process",
            build_in_process,
        )
        lock = asyncio.Lock()
        tasks: dict[str, asyncio.Task[Any]] = {}
        identities: dict[str, tuple[Any, ...]] = {"job": ("old",)}
        wanted = {"job": {"payload": {"symbol": "ES"}}}
        cache: dict[str, dict[str, Any]] = {}
        errors: list[BaseException] = []
        job = asyncio.create_task(
            run_market_analysis_job(
                "job",
                {
                    "source": "ibkr",
                    "instrument_id": "ibkr|future_root|ES|CME|USD|ES",
                    "route_fingerprint": "ibkr|future_root|ES|CME|USD|ES|current:1",
                    "symbol": "ES",
                    "interval": "5m",
                    "parent_canonical_generation": 0,
                    "range": "1d",
                    "signal_range": "3d",
                    "show_visuals": True,
                    "indicator_params": {},
                },
                lock=lock,
                tasks=tasks,
                identities=identities,
                wanted=wanted,
                cache=cache,
                trim_cache=lambda _now: None,
                on_error=lambda _key, exc: errors.append(exc),
                auto_paper_trading_enabled=lambda _instrument_id: False,
            )
        )
        tasks["job"] = job

        await asyncio.wait_for(physical_started.wait(), timeout=0.5)
        async with lock:
            wanted.pop("job")
        job.cancel()
        await asyncio.sleep(0)

        assert not job.done()
        assert tasks["job"] is job
        assert identities == {"job": ("old",)}
        assert cache == {}

        physical_release.set()
        with pytest.raises(asyncio.CancelledError):
            await job

        assert physical_cancelled is False
        assert tasks == {}
        assert identities == {}
        assert cache == {}
        assert errors == []

    asyncio.run(run())


def test_market_analysis_job_auto_syncs_ready_go_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    permission_checks: list[str] = []

    from aef_terminal.ui.paper import sync as paper_sync

    monkeypatch.setattr(
        paper_sync,
        "paper_sync_from_analysis_snapshot",
        lambda snapshot: calls.append(snapshot) or {"ok": True, "opened": 1},
    )

    async def run() -> None:
        lock = asyncio.Lock()
        tasks: dict[str, asyncio.Task[Any]] = {}
        identities: dict[str, tuple[Any, ...]] = {"job": ("old",)}
        wanted = {"job": {"payload": {"symbol": "ES"}}}
        cache: dict[str, dict[str, Any]] = {}

        def build_snapshot_bytes(key: str, _payload: dict[str, Any]) -> bytes:
            snapshot = {
                "bars": [_analysis_bar()],
                "meta": {
                    "analysis_key": key,
                    "instrument_id": "ibkr|future_root|ES|CME|USD|ES",
                    "route_fingerprint": "ibkr|future_root|ES|CME|USD|ES|current:1",
                    "symbol": "ES",
                    "timeframe": "5m",
                    "provider": "ibkr",
                    "provider_contract_id": "1",
                    **_paper_generation_meta(_payload["parent_canonical_generation"]),
                },
                "trade_setup": {
                    "ok": True,
                    "action": "GO",
                    "side": "long",
                    "plan": {"entry": 5000.0, "stop": 4990.0, "target": 5020.0},
                },
            }
            return json.dumps(
                market_analysis_compaction.compact_analysis_snapshot_for_cache(snapshot),
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")

        await run_market_analysis_job(
            "job",
            {
                "source": "ibkr",
                "instrument_id": "ibkr|future_root|ES|CME|USD|ES",
                "route_fingerprint": "ibkr|future_root|ES|CME|USD|ES|current:1",
                "symbol": "ES",
                "interval": "5m",
                "parent_canonical_generation": 0,
                "range": "1d",
                "signal_range": "3d",
                "show_visuals": True,
                "indicator_params": {},
                "instrument": {"session_contract_id": "1"},
            },
            lock=lock,
            tasks=tasks,
            identities=identities,
            wanted=wanted,
            cache=cache,
            trim_cache=lambda _now: None,
            on_error=lambda _key, _exc: None,
            auto_paper_trading_enabled=lambda instrument_id: (
                permission_checks.append(instrument_id) or True
            ),
            build_snapshot_bytes=build_snapshot_bytes,
        )

    asyncio.run(run())

    assert len(calls) == 1
    assert permission_checks == ["ibkr|future_root|ES|CME|USD|ES"]
    assert calls[0]["trade_setup"]["action"] == "GO"
    assert calls[0]["meta"]["analysis_latest_ts"] == "2026-01-01T14:30:00+00:00"


def test_market_analysis_job_runs_paper_sync_off_event_loop() -> None:
    event_loop_thread = threading.get_ident()
    setting_check_threads: list[int] = []

    async def run() -> None:
        lock = asyncio.Lock()
        wanted = {"job": {"payload": {}}}
        snapshot = {
            "meta": {
                "instrument_id": "ibkr|future_root|ES|CME|USD|ES",
                "route_fingerprint": "ibkr|future_root|ES|CME|USD|ES|current:1",
                "timeframe": "5m",
                "provider": "ibkr",
                "provider_contract_id": "1",
                **_paper_generation_meta(),
            },
            "trade_setup": {"action": "GO"},
        }
        await run_market_analysis_job(
            "job",
            {
                "source": "ibkr",
                "instrument_id": "ibkr|future_root|ES|CME|USD|ES",
                "route_fingerprint": "ibkr|future_root|ES|CME|USD|ES|current:1",
                "interval": "5m",
                "parent_canonical_generation": 0,
                "instrument": {"session_contract_id": "1"},
            },
            lock=lock,
            tasks={},
            identities={},
            wanted=wanted,
            cache={},
            trim_cache=lambda _now: None,
            on_error=lambda _key, _exc: None,
            auto_paper_trading_enabled=lambda _instrument_id: (
                setting_check_threads.append(threading.get_ident()) or False
            ),
            build_snapshot_bytes=lambda _key, _payload: json.dumps(
                snapshot,
                separators=(",", ":"),
            ).encode("utf-8"),
        )

    asyncio.run(run())

    assert len(setting_check_threads) == 1
    assert setting_check_threads[0] != event_loop_thread


def test_market_analysis_job_skips_paper_sync_when_demand_is_revoked_after_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paper_sync_calls: list[str] = []
    monkeypatch.setattr(
        market_analysis_job,
        "_sync_paper_from_analysis_snapshot_bytes",
        lambda key, *_args, **_kwargs: paper_sync_calls.append(key),
    )

    async def run() -> None:
        lock = asyncio.Lock()
        wanted = {"job": {"payload": {}}}
        cache: dict[str, dict[str, Any]] = {}

        async def build_snapshot_bytes(
            _key: str,
            _payload: dict[str, Any],
        ) -> bytes:
            async with lock:
                wanted.pop("job", None)
            return b'{"trade_setup":{"action":"GO"}}'

        await run_market_analysis_job(
            "job",
            {
                "source": "ibkr",
                "instrument_id": "ibkr|future_root|ES|CME|USD|ES",
                "route_fingerprint": "ibkr|future_root|ES|CME|USD|ES|current:1",
                "interval": "5m",
                "parent_canonical_generation": 0,
            },
            lock=lock,
            tasks={},
            identities={},
            wanted=wanted,
            cache=cache,
            trim_cache=lambda _now: None,
            on_error=lambda _key, _exc: None,
            auto_paper_trading_enabled=lambda _instrument_id: True,
            build_snapshot_bytes=build_snapshot_bytes,
        )

        assert cache == {}

    asyncio.run(run())

    assert paper_sync_calls == []


def test_market_analysis_job_skips_auto_sync_when_toggle_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    snapshot = {
        "meta": {
            "instrument_id": "ibkr|future_root|ES|CME|USD|ES",
            "route_fingerprint": "ibkr|future_root|ES|CME|USD|ES|current:1",
            "symbol": "ES",
            "timeframe": "5m",
            "provider": "ibkr",
            "provider_contract_id": "1",
            "analysis_latest_ts": "2026-01-01T14:30:00+00:00",
            **_paper_generation_meta(),
        },
        "trade_setup": {"ok": True, "action": "GO", "side": "long"},
    }

    from aef_terminal.ui.paper import sync as paper_sync

    monkeypatch.setattr(
        paper_sync, "paper_sync_from_analysis_snapshot", lambda parsed: calls.append(parsed)
    )

    market_analysis_job._sync_paper_from_analysis_snapshot_bytes(
        "job",
        json.dumps(snapshot, separators=(",", ":")).encode("utf-8"),
        expected_instrument_id="ibkr|future_root|ES|CME|USD|ES",
        expected_route_fingerprint="ibkr|future_root|ES|CME|USD|ES|current:1",
        expected_timeframe="5m",
        expected_provider="ibkr",
        expected_provider_contract_id="1",
        generation_scopes=_paper_generation_scopes(),
        auto_paper_trading_enabled=lambda _instrument_id: False,
    )

    assert calls == []


def test_market_analysis_job_returns_and_logs_typed_auto_execution_outcome(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    snapshot = {
        "meta": {
            "instrument_id": "ibkr|future_root|ES|CME|USD|ES",
            "route_fingerprint": "ibkr|future_root|ES|CME|USD|ES|current:1",
            "symbol": "ES",
            "timeframe": "5m",
            "provider": "ibkr",
            "provider_contract_id": "1",
            **_paper_generation_meta(),
        },
        "trade_setup": {"ok": True, "action": "GO", "side": "long"},
    }
    sync_result = {
        "ok": True,
        "opened": 0,
        "rejected": 1,
        "outcomes": [
            {
                "status": "rejected",
                "code": "PAPER_EXECUTION_SNAPSHOT_STALE",
                "reason": "execution snapshot predates the analysis bar",
                "execution": {
                    "analysis_bar_ts": "2026-01-01T14:30:00+00:00",
                    "ts": "2026-01-01T14:25:00+00:00",
                },
            }
        ],
    }

    from aef_terminal.ui.paper import sync as paper_sync

    monkeypatch.setattr(
        paper_sync,
        "paper_sync_from_analysis_snapshot",
        lambda _parsed: sync_result,
    )

    with caplog.at_level(logging.WARNING):
        result = market_analysis_job._sync_paper_from_analysis_snapshot_bytes(
            "job",
            json.dumps(snapshot, separators=(",", ":")).encode("utf-8"),
            expected_instrument_id="ibkr|future_root|ES|CME|USD|ES",
            expected_route_fingerprint="ibkr|future_root|ES|CME|USD|ES|current:1",
            expected_timeframe="5m",
            expected_provider="ibkr",
            expected_provider_contract_id="1",
            generation_scopes=_paper_generation_scopes(),
            auto_paper_trading_enabled=lambda _instrument_id: True,
        )

    assert result is sync_result
    assert "PAPER_EXECUTION_SNAPSHOT_STALE" in caplog.text
    assert "execution snapshot predates the analysis bar" in caplog.text


def test_market_analysis_auto_paper_effect_holds_canonical_publication_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generation = chart_bars_updated_generation(
        "5m",
        _PAPER_ROUTE_FINGERPRINT,
        instrument_id=_PAPER_INSTRUMENT_ID,
    )
    scopes = _paper_generation_scopes(generation)
    snapshot = {
        "meta": {
            "instrument_id": _PAPER_INSTRUMENT_ID,
            "route_fingerprint": _PAPER_ROUTE_FINGERPRINT,
            "timeframe": "5m",
            "provider": "ibkr",
            "provider_contract_id": "1",
            **_paper_generation_meta(generation),
        },
        "trade_setup": {"action": "GO"},
    }
    started = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []

    from aef_terminal.ui.paper import sync as paper_sync

    def sync_effect(_snapshot: dict[str, Any]) -> dict[str, Any]:
        started.set()
        assert release.wait(1.0)
        return {"ok": True, "outcomes": []}

    monkeypatch.setattr(paper_sync, "paper_sync_from_analysis_snapshot", sync_effect)

    def run_effect() -> None:
        try:
            market_analysis_job._sync_paper_from_analysis_snapshot_bytes(
                "job",
                json.dumps(snapshot, separators=(",", ":")).encode("utf-8"),
                expected_instrument_id=_PAPER_INSTRUMENT_ID,
                expected_route_fingerprint=_PAPER_ROUTE_FINGERPRINT,
                expected_timeframe="5m",
                expected_provider="ibkr",
                expected_provider_contract_id="1",
                generation_scopes=scopes,
                auto_paper_trading_enabled=lambda _instrument_id: True,
            )
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=run_effect)
    worker.start()
    try:
        assert started.wait(1.0)
        with pytest.raises(ChartBarsGenerationChanged) as active:
            begin_chart_bars_write(
                generation,
                "5m",
                _PAPER_ROUTE_FINGERPRINT,
                instrument_id=_PAPER_INSTRUMENT_ID,
            )
        assert active.value.publication_in_progress is True
    finally:
        release.set()
        worker.join(timeout=1.0)

    assert worker.is_alive() is False
    assert errors == []
    guard = begin_chart_bars_write(
        generation,
        "5m",
        _PAPER_ROUTE_FINGERPRINT,
        instrument_id=_PAPER_INSTRUMENT_ID,
    )
    end_chart_bars_write(guard)


def test_market_analysis_paper_rejects_snapshot_generation_mismatch_before_permission() -> None:
    permission_checks: list[str] = []
    snapshot = {
        "meta": {
            "instrument_id": _PAPER_INSTRUMENT_ID,
            "route_fingerprint": _PAPER_ROUTE_FINGERPRINT,
            "timeframe": "5m",
            "provider": "ibkr",
            "provider_contract_id": "1",
            **_paper_generation_meta(1),
        },
        "trade_setup": {"action": "GO"},
    }

    with pytest.raises(ValueError, match="PAPER_ANALYSIS_CANONICAL_GENERATIONS_MISMATCH"):
        market_analysis_job._sync_paper_from_analysis_snapshot_bytes(
            "job",
            json.dumps(snapshot, separators=(",", ":")).encode("utf-8"),
            expected_instrument_id=_PAPER_INSTRUMENT_ID,
            expected_route_fingerprint=_PAPER_ROUTE_FINGERPRINT,
            expected_timeframe="5m",
            expected_provider="ibkr",
            expected_provider_contract_id="1",
            generation_scopes=_paper_generation_scopes(0),
            auto_paper_trading_enabled=lambda instrument_id: (
                permission_checks.append(instrument_id) or True
            ),
        )

    assert permission_checks == []


@pytest.mark.parametrize(
    ("field", "snapshot_value"),
    [
        ("instrument_id", "coinbase|contract|BTC-USD"),
        ("route_fingerprint", "ibkr|future_root|ES|CME|USD|ES|current:2"),
        ("timeframe", "15m"),
        ("provider", "coinbase"),
        ("provider_contract_id", "2"),
    ],
)
def test_market_analysis_job_rejects_snapshot_execution_identity_mismatch_before_permission_check(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    snapshot_value: str,
) -> None:
    paper_sync_calls: list[dict[str, Any]] = []
    permission_checks: list[str] = []
    snapshot = {
        "meta": {
            "instrument_id": "ibkr|future_root|ES|CME|USD|ES",
            "route_fingerprint": "ibkr|future_root|ES|CME|USD|ES|current:1",
            "symbol": "ES",
            "timeframe": "5m",
            "provider": "ibkr",
            "provider_contract_id": "1",
            **_paper_generation_meta(),
        },
        "trade_setup": {"ok": True, "action": "GO", "side": "long"},
    }
    snapshot["meta"][field] = snapshot_value

    from aef_terminal.ui.paper import sync as paper_sync

    monkeypatch.setattr(
        paper_sync,
        "paper_sync_from_analysis_snapshot",
        lambda parsed: paper_sync_calls.append(parsed),
    )

    with pytest.raises(ValueError, match="PAPER_ANALYSIS_EXECUTION_IDENTITY_MISMATCH"):
        market_analysis_job._sync_paper_from_analysis_snapshot_bytes(
            "job",
            json.dumps(snapshot, separators=(",", ":")).encode("utf-8"),
            expected_instrument_id="ibkr|future_root|ES|CME|USD|ES",
            expected_route_fingerprint="ibkr|future_root|ES|CME|USD|ES|current:1",
            expected_timeframe="5m",
            expected_provider="ibkr",
            expected_provider_contract_id="1",
            generation_scopes=_paper_generation_scopes(),
            auto_paper_trading_enabled=lambda instrument_id: (
                permission_checks.append(instrument_id) or True
            ),
        )

    assert permission_checks == []
    assert paper_sync_calls == []


def test_market_analysis_job_requires_producer_owned_session_contract_before_permission_check() -> (
    None
):
    permission_checks: list[str] = []
    snapshot = {
        "meta": {
            "instrument_id": "ibkr|future_root|ES|CME|USD|ES",
            "route_fingerprint": "ibkr|future_root|ES|CME|USD|ES|current:1",
            "symbol": "ES",
            "timeframe": "5m",
            "provider": "ibkr",
            "provider_contract_id": "1",
            **_paper_generation_meta(),
        },
        "trade_setup": {"ok": True, "action": "GO", "side": "long"},
    }

    with pytest.raises(ValueError, match="PAPER_ANALYSIS_EXECUTION_IDENTITY_INVALID"):
        market_analysis_job._sync_paper_from_analysis_snapshot_bytes(
            "job",
            json.dumps(snapshot, separators=(",", ":")).encode("utf-8"),
            expected_instrument_id="ibkr|future_root|ES|CME|USD|ES",
            expected_route_fingerprint="ibkr|future_root|ES|CME|USD|ES|current:1",
            expected_timeframe="5m",
            expected_provider="ibkr",
            expected_provider_contract_id=None,
            generation_scopes=_paper_generation_scopes(),
            auto_paper_trading_enabled=lambda instrument_id: (
                permission_checks.append(instrument_id) or True
            ),
        )

    assert permission_checks == []


def test_market_analysis_job_setting_error_skips_auto_sync_without_failing_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    from aef_terminal.ui.paper import sync as paper_sync

    monkeypatch.setattr(
        paper_sync, "paper_sync_from_analysis_snapshot", lambda parsed: calls.append(parsed)
    )

    async def run() -> None:
        lock = asyncio.Lock()
        tasks: dict[str, asyncio.Task[Any]] = {}
        identities: dict[str, tuple[Any, ...]] = {"job": ("old",)}
        wanted = {"job": {"payload": {"symbol": "ES"}}}
        cache: dict[str, dict[str, Any]] = {}

        def build_snapshot_bytes(_key: str, _payload: dict[str, Any]) -> bytes:
            snapshot = {
                "meta": {
                    "instrument_id": "ibkr|future_root|ES|CME|USD|ES",
                    "route_fingerprint": "ibkr|future_root|ES|CME|USD|ES|current:1",
                    "symbol": "ES",
                    "timeframe": "5m",
                    "provider": "ibkr",
                    "provider_contract_id": "1",
                    **_paper_generation_meta(_payload["parent_canonical_generation"]),
                },
                "trade_setup": {"ok": True, "action": "GO"},
            }
            return json.dumps(snapshot, separators=(",", ":")).encode("utf-8")

        def broken_auto_setting(_instrument_id: str) -> bool:
            raise RuntimeError("settings down")

        await run_market_analysis_job(
            "job",
            {
                "source": "ibkr",
                "instrument_id": "ibkr|future_root|ES|CME|USD|ES",
                "route_fingerprint": "ibkr|future_root|ES|CME|USD|ES|current:1",
                "symbol": "ES",
                "interval": "5m",
                "parent_canonical_generation": 0,
                "range": "1d",
                "signal_range": "3d",
                "show_visuals": True,
                "indicator_params": {},
                "instrument": {"session_contract_id": "1"},
            },
            lock=lock,
            tasks=tasks,
            identities=identities,
            wanted=wanted,
            cache=cache,
            trim_cache=lambda _now: None,
            on_error=lambda _key, _exc: None,
            auto_paper_trading_enabled=broken_auto_setting,
            build_snapshot_bytes=build_snapshot_bytes,
        )

        assert cache["job"]["status"] == "ready"

    asyncio.run(run())

    assert calls == []


def test_market_analysis_cache_compacts_runtime_indicator_payload() -> None:
    snapshot = {
        "bars": [_analysis_bar()],
        "chart_guides": {"meta": {"generation": "analysis-owned"}},
        "vsa_volume": {
            "contract": "vsa-volume-context-v1",
            "version": "3-vsa-breakout-context",
            "breakout_context": {
                "contract": "vsa-breakout-context-v1",
                "mode": "context_only",
                "policy": "spy-research-v1",
                "latest": {
                    "signal_code": "EXH_UP",
                    "short_tier": "a",
                },
                "events": [],
            },
            "series": [
                {
                    "ts": "2026-01-01T14:30:00+00:00",
                    "code": "FUEL",
                    "display_volume": 1200,
                    "display_avg": 800,
                    "breakout_context": {
                        "signal_code": "EXH_UP",
                        "short_tier": "a",
                    },
                    "volume_label": {
                        "text": "FUEL",
                        "orientation": "vertical",
                        "placement": "volume_bar",
                    },
                    "rvol": 1.8,
                    "context_lines": ["heavy text"],
                    "internal_debug": {"rows": list(range(20))},
                }
            ],
            "events": [
                {
                    "ts": f"2026-01-01T14:{minute:02d}:00+00:00",
                    "code": "FUEL",
                    "context_lines": ["keep event"],
                }
                for minute in range(130)
            ],
        },
        "indicators": {
            "obvious_failure": {
                "series": [
                    {
                        "ts": f"2026-01-01T14:{minute % 60:02d}:00+00:00",
                        "score": minute,
                        "details": {"large": list(range(20))},
                    }
                    for minute in range(90)
                ],
                "overlays": [
                    {
                        "type": "label",
                        "ts": "2026-01-01T14:30:00+00:00",
                        "price": 10,
                        "signal_overlay": True,
                        "trade_plan": {"large": list(range(20))},
                    }
                ],
            },
            "impulse_fib": {
                "series": [],
                "events": [],
                "overlays": [
                    {
                        "type": "line",
                        "id": "impulse-pb-0.618-2026-01-01T14:30:00+00:00",
                        "start_ts": "2026-01-01T14:30:00+00:00",
                        "end_ts": "2026-01-01T15:00:00+00:00",
                        "y1": 10.25,
                        "y2": 10.25,
                        "label": "0.618",
                        "style": "dotted",
                        "width": 1.0,
                        "color": "rgba(0,166,118,0.78)",
                        "label_position": "line_right",
                        "label_side": "below",
                        "label_font_size": 7.0,
                        "label_gap_px": 1.0,
                        "scenario": "Impulse pullback",
                        "role": "pullback_zone",
                        "internal_debug": {"drop": True},
                    },
                    {
                        "type": "box",
                        "id": "impulse-pb-zone-2026-01-01T14:30:00+00:00",
                        "start_ts": "2026-01-01T14:30:00+00:00",
                        "end_ts": "2026-01-01T15:00:00+00:00",
                        "top": 10.5,
                        "bottom": 10.0,
                        "bg": "rgba(0,166,118,0.08)",
                        "border": "rgba(0,166,118,0.46)",
                        "style": "dotted",
                        "badge_lines": ["PB-L"],
                    },
                ],
            },
            "trade_setup_engine": {
                "series": [
                    {
                        "ts": "2026-01-01T14:30:00+00:00",
                        "state": "WAIT",
                        "metrics": {"large": list(range(100))},
                    }
                ],
                "events": [
                    {"ts": "2026-01-01T14:30:00+00:00", "state": "WAIT", "internal_debug": True}
                ],
                "latest": {"state": "WAIT"},
            },
        },
        "gex_dynamics": {
            "contract": "gex-dynamics-v1",
            "version": "3.0-engine",
            "latest": {
                "ts": "2026-01-01T14:30:00+00:00",
                "state": "WAIT",
                "comparison_quality": "NEED_HISTORY",
                "context_only": True,
            },
            "status": {
                "state": "ready",
                "reason_code": "ok",
                "capture_mode": "request",
                "bar_count": 610,
                "history_count": 1,
            },
        },
        "meta": {
            "analysis_key": "job",
            "chart_data_quality": {
                "status": "gap",
                "gaps": [
                    {
                        "missing_slots": [
                            f"2026-01-01T14:{minute % 60:02d}:00+00:00" for minute in range(70)
                        ],
                    }
                ],
            },
        },
    }

    compacted = market_analysis_compaction.compact_analysis_snapshot_for_cache(snapshot)

    assert "bars" not in compacted
    assert "chart_guides" not in compacted
    assert compacted["meta"]["analysis_only"] is True
    assert compacted["meta"]["analysis_bars_included"] is False
    vsa_series = compacted["vsa_volume"]["series"]
    assert vsa_series == [
        {
            "ts": "2026-01-01T14:30:00+00:00",
            "display_volume": 1200,
            "display_avg": 800,
            "breakout_context": {
                "signal_code": "EXH_UP",
                "short_tier": "a",
            },
            "rvol": 1.8,
            "code": "FUEL",
        }
    ]
    assert len(compacted["vsa_volume"]["events"]) == 80
    assert compacted["vsa_volume"]["events"][0]["ts"] == "2026-01-01T14:50:00+00:00"
    assert compacted["vsa_volume"]["contract"] == "vsa-volume-context-v1"
    assert compacted["vsa_volume"]["version"] == "3-vsa-breakout-context"
    assert compacted["vsa_volume"]["breakout_context"]["contract"] == "vsa-breakout-context-v1"
    assert "vsa_volume" not in compacted["indicators"]
    assert "series" not in compacted["indicators"]["trade_setup_engine"]
    assert compacted["indicators"]["trade_setup_engine"]["events"] == [
        {"ts": "2026-01-01T14:30:00+00:00", "state": "WAIT"}
    ]
    assert "gex_dynamics" not in compacted["indicators"]
    assert compacted["gex_dynamics"]["contract"] == "gex-dynamics-v1"
    assert compacted["gex_dynamics"]["version"] == "3.0-engine"
    assert compacted["gex_dynamics"]["status"]["capture_mode"] == "request"
    assert "series" not in compacted["gex_dynamics"]
    assert "events" not in compacted["gex_dynamics"]
    assert len(compacted["indicators"]["obvious_failure"]["series"]) == 80
    assert "details" in compacted["indicators"]["obvious_failure"]["series"][0]
    assert compacted["indicators"]["obvious_failure"]["overlays"] == [
        {
            "type": "label",
            "ts": "2026-01-01T14:30:00+00:00",
            "price": 10,
            "signal_overlay": True,
        }
    ]
    impulse_overlays = compacted["indicators"]["impulse_fib"]["overlays"]
    assert impulse_overlays[0] == {
        "type": "line",
        "id": "impulse-pb-0.618-2026-01-01T14:30:00+00:00",
        "start_ts": "2026-01-01T14:30:00+00:00",
        "end_ts": "2026-01-01T15:00:00+00:00",
        "y1": 10.25,
        "y2": 10.25,
        "color": "rgba(0,166,118,0.78)",
        "label": "0.618",
        "scenario": "Impulse pullback",
        "style": "dotted",
        "width": 1.0,
        "label_position": "line_right",
        "label_side": "below",
        "label_font_size": 7.0,
        "label_gap_px": 1.0,
        "role": "pullback_zone",
    }
    assert impulse_overlays[1]["border"] == "rgba(0,166,118,0.46)"
    assert impulse_overlays[1]["style"] == "dotted"
    assert impulse_overlays[1]["badge_lines"] == ["PB-L"]
    assert "internal_debug" not in impulse_overlays[0]
    gap = compacted["meta"]["chart_data_quality"]["gaps"][0]
    assert len(gap["missing_slots"]) == 64
    assert gap["missing_slots_count"] == 70


def test_vsa_response_projection_defaults_to_latest_six_hours() -> None:
    snapshot = {
        "meta": {"analysis_ts": "2026-01-01T14:00:00+00:00"},
        "vsa_volume": {
            "latest": {"ts": "2026-01-01T14:00:00+00:00", "code": "LATEST"},
            "series": [
                {"ts": "2026-01-01T07:59:00+00:00", "code": "OLD"},
                {"ts": "2026-01-01T08:00:00+00:00", "code": "BOUNDARY"},
                {"ts": "2026-01-01T08:01:00+00:00", "code": "RECENT"},
                {"ts": "2026-01-01T14:00:00+00:00", "code": "LATEST"},
            ],
            "events": [
                {"ts": "2026-01-01T07:59:00+00:00", "code": "OLD"},
                {"ts": "2026-01-01T13:00:00+00:00", "code": "RECENT"},
            ],
            "overlays": [
                {"type": "label", "ts": "2026-01-01T07:59:00+00:00"},
                {"type": "label", "ts": "2026-01-01T13:30:00+00:00"},
            ],
            "breakout_context": {
                "events": [
                    {"ts": "2026-01-01T07:59:00+00:00", "signal_code": "OLD"},
                    {"ts": "2026-01-01T13:30:00+00:00", "signal_code": "RECENT"},
                ],
            },
            "status": {"series_count": 4},
        },
    }

    compacted = market_analysis_compaction.compact_runtime_payload_for_response(snapshot)

    assert [row["ts"] for row in compacted["vsa_volume"]["series"]] == [
        "2026-01-01T08:01:00+00:00",
        "2026-01-01T14:00:00+00:00",
    ]
    assert [row["ts"] for row in compacted["vsa_volume"]["events"]] == ["2026-01-01T13:00:00+00:00"]
    assert [row["ts"] for row in compacted["vsa_volume"]["overlays"]] == [
        "2026-01-01T13:30:00+00:00"
    ]
    assert compacted["vsa_volume"]["breakout_context"]["events"] == [
        {"ts": "2026-01-01T13:30:00+00:00", "signal_code": "RECENT"}
    ]
    assert compacted["vsa_volume"]["status"]["series_count"] == 2
    assert compacted["vsa_volume"]["status"]["render_window_seconds"] == 21_600


def test_vsa_analysis_cache_keeps_latest_twelve_hours() -> None:
    snapshot = {
        "bars": [],
        "meta": {},
        "vsa_volume": {
            "latest": {"ts": "2026-01-01T14:00:00+00:00"},
            "series": [
                {"ts": "2026-01-01T01:59:00+00:00"},
                {"ts": "2026-01-01T02:00:00+00:00"},
                {"ts": "2026-01-01T02:01:00+00:00"},
                {"ts": "2026-01-01T14:00:00+00:00"},
            ],
            "events": [],
            "overlays": [],
            "status": {},
        },
    }

    compacted = market_analysis_compaction.compact_analysis_snapshot_for_cache(snapshot)

    assert [row["ts"] for row in compacted["vsa_volume"]["series"]] == [
        "2026-01-01T02:01:00+00:00",
        "2026-01-01T14:00:00+00:00",
    ]
    assert compacted["vsa_volume"]["status"]["render_window_seconds"] == 43_200


def test_market_analysis_response_keeps_engine_gex_context_compact() -> None:
    snapshot = {
        "gex_dynamics": {
            "contract": "gex-dynamics-v1",
            "version": "3.0-engine",
            "latest": {
                "ts": "2026-01-01T14:30:00+00:00",
                "state": "WAIT",
                "comparison_quality": "NEED_HISTORY",
                "context_only": True,
            },
            "status": {
                "state": "ready",
                "reason_code": "ok",
                "capture_mode": "live",
            },
        },
        "indicators": {},
        "meta": {},
    }

    compacted = market_analysis_compaction.compact_runtime_payload_for_response(snapshot)

    context = compacted["gex_dynamics"]
    assert set(context) == {"contract", "version", "latest", "status"}
    assert context["status"]["capture_mode"] == "live"
    assert "series" not in context
    assert "events" not in context
    assert "gex_dynamics" not in compacted["indicators"]


def test_market_analysis_cache_uses_compact_contract_for_overlay_payload() -> None:
    fields = market_analysis_compaction._compact_policy_fields(
        "obvious_failure",
        "overlays",
        {"fields": "compact_contract"},
    )

    assert fields is not None
    assert "type" in fields
    assert "price" in fields
    assert "trade_plan" not in fields
    assert "signal" not in fields


def test_market_analysis_compaction_preserves_active_overlays_before_history_tail() -> None:
    active = {
        "type": "box",
        "id": "active-poi",
        "retention": "active",
        "start_ts": "2026-01-01T14:30:00+00:00",
        "end_ts": "2026-01-01T15:30:00+00:00",
        "top": 101.0,
        "bottom": 99.0,
    }
    history = [
        {
            "type": "label",
            "id": f"history-{index}",
            "retention": "history",
            "ts": "2026-01-01T14:30:00+00:00",
            "price": float(index),
            "label": str(index),
        }
        for index in range(100)
    ]
    snapshot = {
        "meta": {},
        "indicators": {"linda_volume": {"overlays": [active, *history]}},
    }

    compacted = market_analysis_compaction.compact_runtime_payload_for_response(snapshot)
    overlays = compacted["indicators"]["linda_volume"]["overlays"]

    assert len(overlays) == 80
    assert overlays[0]["id"] == "active-poi"
    assert overlays[0]["retention"] == "active"
    assert overlays[1]["id"] == "history-21"
    assert overlays[-1]["id"] == "history-99"
    assert compacted["meta"]["indicator_overlay_compaction"] == {
        "linda_volume": {"dropped": 21, "retained": 80}
    }


def test_market_analysis_compaction_never_discards_active_overlays_to_meet_soft_limit() -> None:
    active = [{"id": f"active-{index}", "retention": "active"} for index in range(81)]

    assert market_analysis_compaction._compact_overlay_tail(active, 80) == active


def test_market_analysis_reports_shared_vsa_overlay_compaction() -> None:
    snapshot = {
        "meta": {},
        "indicators": {},
        "vsa_volume": {
            "overlays": [
                {
                    "type": "label",
                    "id": f"vsa-{index}",
                    "ts": "2026-01-01T14:30:00+00:00",
                    "price": float(index),
                    "code": "VSA",
                }
                for index in range(90)
            ]
        },
    }

    market_analysis_compaction.compact_runtime_payload_for_response(snapshot)

    assert len(snapshot["vsa_volume"]["overlays"]) == 80
    assert snapshot["meta"]["indicator_overlay_compaction"]["vsa_volume"] == {
        "dropped": 10,
        "retained": 80,
    }


def test_market_analysis_compaction_preserves_smc_active_geometry_before_event_history() -> None:
    active = [
        {
            "type": "box",
            "id": f"smc-active-{index}",
            "retention": "active",
            "start_ts": "2026-01-01T14:30:00+00:00",
            "end_ts": "2026-01-01T15:30:00+00:00",
            "top": 101.0 + index,
            "bottom": 99.0 + index,
        }
        for index in range(12)
    ]
    history = [
        {
            "type": "label",
            "id": f"smc-event-{index}",
            "ts": "2026-01-01T14:30:00+00:00",
            "price": 100.0,
            "lines": ["BOS"],
        }
        for index in range(140)
    ]
    snapshot = {
        "meta": {},
        "indicators": {"smc_channels": {"overlays": [*active, *history]}},
    }

    market_analysis_compaction.compact_runtime_payload_for_response(snapshot)
    overlays = snapshot["indicators"]["smc_channels"]["overlays"]

    assert len(overlays) == 120
    assert [item["id"] for item in overlays[:12]] == [item["id"] for item in active]
    assert overlays[12]["id"] == "smc-event-32"
    assert overlays[-1]["id"] == "smc-event-139"


def test_market_analysis_cache_resolves_all_registry_compact_policy_fields() -> None:
    manifest = indicator_manifest()
    policies = market_analysis_compaction._runtime_payload_compact_policies()

    for indicator_id, section_policies in policies.items():
        runtime_contract = manifest[indicator_id]["runtime_payload_contract"]
        compact_contract = manifest[indicator_id]["runtime_payload_compact_contract"]
        for section, policy in section_policies.items():
            fields_ref = policy.get("fields")
            fields = market_analysis_compaction._compact_policy_fields(
                indicator_id, section, policy
            )
            if fields_ref is None:
                assert fields is None
                continue
            assert fields, (indicator_id, section, fields_ref)
            if fields_ref == "runtime_contract":
                assert list(fields) == runtime_contract[section]
            elif fields_ref == "compact_contract":
                assert list(fields) == compact_contract[section]
                assert set(fields) <= set(runtime_contract[section])
            else:
                assert isinstance(fields_ref, (list, tuple)), (indicator_id, section, fields_ref)


def test_market_analysis_cache_preserves_declared_compact_contract_fields() -> None:
    policies = market_analysis_compaction._runtime_payload_compact_policies()
    snapshot = {"bars": [_analysis_bar()], "meta": {}, "indicators": {}}
    expected: dict[tuple[str, str], dict[str, object] | None] = {}

    for indicator_id, section_policies in policies.items():
        indicator: dict[str, object] = {}
        snapshot["indicators"][indicator_id] = indicator
        for section, policy in section_policies.items():
            if policy.get("drop") is True:
                indicator[section] = [{"__internal_debug": "drop"}]
                expected[(indicator_id, section)] = None
                continue
            fields = market_analysis_compaction._compact_policy_fields(
                indicator_id, section, policy
            )
            row = {field: f"{indicator_id}:{section}:{field}" for field in (fields or ())}
            row["__internal_debug"] = "drop"
            indicator[section] = [row]
            expected[(indicator_id, section)] = (
                {field: row[field] for field in (fields or ())} if fields else row
            )

    compacted = market_analysis_compaction.compact_analysis_snapshot_for_cache(snapshot)

    for (indicator_id, section), expected_row in expected.items():
        indicator = compacted["indicators"][indicator_id]
        if expected_row is None:
            assert section not in indicator
            continue
        assert indicator[section] == [expected_row]
        if "__internal_debug" not in expected_row:
            assert "__internal_debug" not in indicator[section][0]


def test_market_analysis_cache_rejects_unknown_compact_policy_field_refs() -> None:
    assert "gex_dynamics" not in market_analysis_compaction._runtime_payload_compact_policies()

    with pytest.raises(ValueError, match="Unknown runtime payload compact fields ref"):
        market_analysis_compaction._compact_policy_fields(
            "obvious_failure", "overlays", {"fields": "contract"}
        )

    with pytest.raises(ValueError, match="Missing compact runtime payload contract"):
        market_analysis_compaction._compact_policy_fields(
            "gex_dynamics", "overlays", {"fields": "compact_contract"}
        )

    with pytest.raises(ValueError, match="Missing runtime payload contract"):
        market_analysis_compaction._compact_policy_fields(
            "obvious_failure", "ghost", {"fields": "runtime_contract"}
        )


def test_market_analysis_job_skips_cache_when_no_longer_wanted() -> None:
    async def run() -> None:
        lock = asyncio.Lock()
        tasks: dict[str, asyncio.Task[Any]] = {}
        identities: dict[str, tuple[Any, ...]] = {}
        wanted: dict[str, dict[str, Any]] = {}
        cache: dict[str, dict[str, Any]] = {}

        await run_market_analysis_job(
            "job",
            {
                "source": "ibkr",
                "instrument_id": "ibkr|future_root|ES|CME|USD|ES",
                "route_fingerprint": "ibkr|future_root|ES|CME|USD|ES|current:1",
                "symbol": "ES",
                "interval": "5m",
                "parent_canonical_generation": 0,
                "range": "1d",
                "signal_range": "3d",
                "show_visuals": True,
                "indicator_params": {},
            },
            lock=lock,
            tasks=tasks,
            identities=identities,
            wanted=wanted,
            cache=cache,
            trim_cache=lambda _now: None,
            on_error=lambda _key, _exc: None,
            auto_paper_trading_enabled=lambda _instrument_id: False,
            build_snapshot_bytes=lambda _key, _payload: b"{}",
        )

        assert cache == {}

    asyncio.run(run())


def test_market_analysis_job_cleanup_survives_error_telemetry_failure() -> None:
    async def run() -> None:
        lock = asyncio.Lock()
        tasks: dict[str, asyncio.Task[Any]] = {}
        identities: dict[str, tuple[Any, ...]] = {"job": ("old",)}
        wanted = {"job": {"payload": {"symbol": "ES"}}}
        cache: dict[str, dict[str, Any]] = {}

        def broken_builder(_key: str, _payload: dict[str, Any]) -> bytes:
            raise RuntimeError("analysis exploded")

        def broken_telemetry(_key: str, _exc: BaseException) -> None:
            raise RuntimeError("telemetry exploded")

        await run_market_analysis_job(
            "job",
            {
                "source": "ibkr",
                "instrument_id": "ibkr|future_root|ES|CME|USD|ES",
                "route_fingerprint": "ibkr|future_root|ES|CME|USD|ES|current:1",
                "symbol": "ES",
                "interval": "5m",
                "parent_canonical_generation": 0,
                "range": "1d",
                "signal_range": "3d",
                "show_visuals": True,
                "indicator_params": {},
            },
            lock=lock,
            tasks=tasks,
            identities=identities,
            wanted=wanted,
            cache=cache,
            trim_cache=lambda _now: None,
            on_error=broken_telemetry,
            auto_paper_trading_enabled=lambda _instrument_id: False,
            build_snapshot_bytes=broken_builder,
        )

        assert "job" not in tasks
        assert "job" not in identities
        assert cache["job"]["status"] == "error"
        assert cache["job"]["message"] == "analysis exploded"

    asyncio.run(run())


def test_market_snapshot_enrichment_isolates_enricher_failures(monkeypatch) -> None:
    def broken_enricher(_snapshot: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("boom")

    def working_enricher(snapshot: dict[str, Any]) -> dict[str, Any]:
        snapshot["ok"] = True
        return snapshot

    monkeypatch.setattr(
        market_snapshot_enrichment,
        "snapshot_enrichers",
        lambda: (broken_enricher, working_enricher),
    )

    snapshot: dict[str, Any] = {}

    assert market_snapshot_enrichment.enrich_market_analysis_snapshot(snapshot) is snapshot
    assert snapshot["ok"] is True
