from __future__ import annotations

import asyncio
import hashlib
import json
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from aef_terminal.config import AppConfig
from aef_terminal.data.providers import route_instrument
from aef_terminal.ui.services.headless_research_capture import (
    HeadlessResearchCaptureDeps,
    HeadlessResearchCaptureRuntime,
    HeadlessResearchCaptureSettings,
    RESEARCH_CAPTURE_INDICATOR_IDS,
)
from aef_terminal.ui.services.market_analysis_store import (
    MARKET_ANALYSIS_EFFECTS_RESEARCH_CAPTURE,
)
from scripts.export_research_capture import (
    export_research_capture,
    verify_research_export,
)
from tests.provider_payloads import ibkr_future_payload


def test_enabled_headless_capture_requires_exact_instrument_ids() -> None:
    with pytest.raises(ValueError, match="requires exact instrument IDs"):
        HeadlessResearchCaptureSettings(enabled=True, instrument_ids=())


def test_headless_capture_settings_load_exact_deployment_environment(monkeypatch) -> None:
    instrument_id = "ibkr|future_root|ES|CME|USD|ES"
    monkeypatch.setenv("AEF_RESEARCH_CAPTURE_ENABLED", "true")
    monkeypatch.setenv("AEF_RESEARCH_CAPTURE_INSTRUMENT_IDS", json.dumps([instrument_id]))
    monkeypatch.setenv("AEF_RESEARCH_CAPTURE_TIMEFRAME", "5m")
    monkeypatch.setenv("AEF_RESEARCH_CAPTURE_RECONCILE_SECONDS", "12")

    settings = HeadlessResearchCaptureSettings.from_config(AppConfig())

    assert settings.enabled is True
    assert settings.instrument_ids == (instrument_id,)
    assert settings.timeframe == "5m"
    assert settings.reconcile_seconds == 12.0


def test_headless_capture_registers_renews_and_releases_research_only_demand() -> None:
    async def scenario() -> tuple[list[dict[str, Any]], list[tuple[str, int]], dict[str, Any]]:
        instrument = ibkr_future_payload("ES", con_id=649180671, local_symbol="ESU6")
        route = route_instrument(instrument)
        registered: list[dict[str, Any]] = []
        released: list[tuple[str, int]] = []
        sleeping = False

        def build_analysis_payload(**kwargs: Any) -> dict[str, Any]:
            exact_route = route_instrument(kwargs["instrument"])
            return {
                **kwargs,
                "route_fingerprint": exact_route.fingerprint,
            }

        async def register_wanted(
            key: str,
            payload: dict[str, Any],
            client_id: str,
            sequence: int,
        ) -> str:
            registered.append(
                {
                    "key": key,
                    "payload": payload,
                    "client_id": client_id,
                    "sequence": sequence,
                }
            )
            return "queued"

        async def renew_client_lease(
            _client_id: str,
            _sequence: int,
            _key: str,
            instrument_id: str,
            route_fingerprint: str,
        ) -> bool:
            return instrument_id == route.instrument_id and route_fingerprint == route.fingerprint

        async def release_client_lease(client_id: str, sequence: int) -> bool:
            released.append((client_id, sequence))
            return True

        runtime = HeadlessResearchCaptureRuntime(
            HeadlessResearchCaptureSettings(
                enabled=True,
                instrument_ids=(route.instrument_id,),
            ),
            HeadlessResearchCaptureDeps(
                lookup_runtime_instrument=lambda instrument_id: (
                    dict(instrument)
                    if instrument_id == route.instrument_id
                    else (_ for _ in ()).throw(ValueError("unexpected instrument"))
                ),
                client_settings_snapshot=lambda: {},
                build_analysis_payload=build_analysis_payload,
                analysis_key=lambda payload: hashlib.sha1(
                    json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
                ).hexdigest()[:20],
                register_wanted=register_wanted,
                renew_client_lease=renew_client_lease,
                release_client_lease=release_client_lease,
                server_sleeping=lambda: sleeping,
            ),
        )

        await runtime.reconcile_once()
        await runtime.reconcile_once()
        sleeping = True
        await runtime.reconcile_once()
        return registered, released, runtime.status()

    registered, released, status = asyncio.run(scenario())

    assert len(registered) == 1
    payload = registered[0]["payload"]
    assert payload["analysis_effects"] == MARKET_ANALYSIS_EFFECTS_RESEARCH_CAPTURE
    assert payload["show_visuals"] is False
    enabled = {
        indicator_id
        for indicator_id, params in payload["indicator_params"].items()
        if isinstance(params, dict) and params.get("enabled") is True
    }
    assert enabled == RESEARCH_CAPTURE_INDICATOR_IDS
    assert status["registrations"] == 1
    assert status["renewals"] == 1
    assert status["releases"] == 1
    assert status["active_leases"] == []
    assert released == [(registered[0]["client_id"], 1)]


def test_headless_capture_cancellation_releases_internal_analysis_lease() -> None:
    async def scenario() -> list[tuple[str, int]]:
        instrument = ibkr_future_payload("ES", con_id=649180672, local_symbol="ESZ6")
        route = route_instrument(instrument)
        registered = asyncio.Event()
        released: list[tuple[str, int]] = []

        async def register_wanted(
            _key: str,
            _payload: dict[str, Any],
            _client_id: str,
            _sequence: int,
        ) -> str:
            registered.set()
            return "queued"

        async def release_client_lease(client_id: str, sequence: int) -> bool:
            released.append((client_id, sequence))
            return True

        runtime = HeadlessResearchCaptureRuntime(
            HeadlessResearchCaptureSettings(
                enabled=True,
                instrument_ids=(route.instrument_id,),
                reconcile_seconds=30.0,
            ),
            HeadlessResearchCaptureDeps(
                lookup_runtime_instrument=lambda _instrument_id: dict(instrument),
                client_settings_snapshot=lambda: {},
                build_analysis_payload=lambda **kwargs: {
                    **kwargs,
                    "route_fingerprint": route.fingerprint,
                },
                analysis_key=lambda _payload: "research-key",
                register_wanted=register_wanted,
                renew_client_lease=lambda *_args: asyncio.sleep(0, result=True),
                release_client_lease=release_client_lease,
                server_sleeping=lambda: False,
            ),
        )
        task = asyncio.create_task(runtime.run())
        await registered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return released

    released = asyncio.run(scenario())

    assert len(released) == 1
    assert released[0][1] == 1


def test_export_research_capture_snapshots_both_immutable_journals(tmp_path: Path) -> None:
    channel = tmp_path / "datasets/channel_interactions/raw/schema-v1/date=2026-08-23/channel.json"
    option = tmp_path / "datasets/option_reversal/raw/schema-v1/date=2026-08-23/option.json"
    channel.parent.mkdir(parents=True)
    option.parent.mkdir(parents=True)
    channel.write_text('{"channel":true}\n', encoding="utf-8")
    option.write_text('{"option":true}\n', encoding="utf-8")

    result = export_research_capture(
        data_root=tmp_path,
        created_at=datetime(2026, 8, 23, 12, 30, tzinfo=UTC),
    )

    archive_path = Path(result["archive_path"])
    manifest_path = Path(result["manifest_path"])
    assert result["record_counts"] == {
        "channel_interactions": 1,
        "option_reversal": 1,
    }
    assert result["records_total"] == 2
    assert hashlib.sha256(archive_path.read_bytes()).hexdigest() == result["archive_sha256"]
    assert archive_path.stat().st_mode & 0o777 == 0o644
    assert manifest_path.stat().st_mode & 0o777 == 0o644
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["archive_sha256"] == result["archive_sha256"]
    with tarfile.open(archive_path, "r:gz") as archive:
        assert archive.getnames() == [
            "datasets/channel_interactions/raw/schema-v1/date=2026-08-23/channel.json",
            "datasets/option_reversal/raw/schema-v1/date=2026-08-23/option.json",
        ]
    verified = verify_research_export(manifest_path)
    assert verified["ok"] is True
    assert verified["record_counts"] == result["record_counts"]
