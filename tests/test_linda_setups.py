from datetime import UTC, datetime, timedelta

from aef_terminal.domain import ActionPhase, Bar
from aef_terminal.engine.analyze import attach_indicator_status
from aef_terminal.indicators.defaults import DEFAULT_INDICATOR_SETTINGS
from aef_terminal.indicators.modules.linda_volume.setups import (
    LindaSetupFlags,
    LindaSetupScanContext as _LindaSetupScanContext,
    detect_turtle_soup,
    scan_linda_setups,
    scan_linda_setups_history,
)
from aef_terminal.indicators.modules.linda_volume import (
    LindaVolumeParams,
    linda_volume as _linda_volume,
)
from aef_terminal.indicators.modules.linda_volume.playbook_contract import (
    playbook_promotion_preview,
)
from aef_terminal.features.provider_session import ProviderSessionInterval, ProviderSessionReset
from aef_terminal.runtime.instruments import PROFILES
from tests.provider_payloads import explicit_vwap_session_for_bars


def LindaSetupScanContext(*args, **kwargs):
    kwargs.setdefault(
        "provider_session",
        ProviderSessionReset(
            available=True,
            source="test",
            reason_code="",
            calendar="continuous_24_7",
        ),
    )
    return _LindaSetupScanContext(*args, **kwargs)


def linda_volume(bars, params=None, **kwargs):
    kwargs.setdefault("vwap_session", explicit_vwap_session_for_bars(bars))
    return _linda_volume(bars, profile=PROFILES["ES"], params=params, **kwargs)


def _bar(
    index: int,
    open_: float,
    high: float,
    low: float,
    close: float,
    volume: float = 1200.0,
    *,
    symbol: str = "ES",
) -> Bar:
    return Bar(
        symbol,
        datetime(2026, 1, 2, 14, 0, tzinfo=UTC) + timedelta(minutes=5 * index),
        open_,
        high,
        low,
        close,
        volume,
        "5m",
    )


def test_detect_turtle_soup_flags_false_breakout_high() -> None:
    bars = [
        _bar(i, 100 + i * 0.05, 100.5 + i * 0.05, 99.5 + i * 0.05, 100 + i * 0.05)
        for i in range(24)
    ]
    bars[-1] = _bar(24, 101.0, 103.4, 100.8, 101.2, 2400.0)
    signal = detect_turtle_soup(
        bars,
        length=20,
        atr=1.0,
        bands=DEFAULT_INDICATOR_SETTINGS.score,
    )
    assert signal is not None
    assert signal.code == "TS_DN"
    assert signal.direction == "short"
    assert not hasattr(signal, "tooltip")
    payload = signal.to_dict()
    assert "tooltip" not in payload
    assert "context_lines" not in payload
    assert payload["fact_groups"][0]["kind"] == "linda_evidence"
    evidence = payload["fact_groups"][0]["items"][0]
    assert evidence == {
        "code": "linda_turtle_sweep_rejected",
        "level": 101.6,
        "side": "high",
    }
    assert "content" not in evidence
    assert payload["metrics"]["trigger"]["digits"] == 2
    assert payload["metrics"]["stop"]["digits"] == 2
    assert payload["metrics"]["target"]["digits"] == 2


def test_scan_linda_setups_respects_feature_flags() -> None:
    bars = [
        _bar(i, 100 + i * 0.05, 100.5 + i * 0.05, 99.5 + i * 0.05, 100 + i * 0.05)
        for i in range(24)
    ]
    bars[-1] = _bar(24, 101.0, 103.4, 100.8, 101.2, 2400.0)
    context = LindaSetupScanContext(atr=1.0, adx=28.0, ema20=100.5, ema50=100.0, volume_avg=1200.0)
    enabled = scan_linda_setups(
        bars,
        LindaSetupFlags(
            turtle_soup=True,
            turtle_soup_plus_one=False,
            eighty_twenty=False,
            the_anti=False,
            momentum_pinball=False,
            hv_squeeze=False,
            adx_gapper=False,
        ),
        context=context,
    )
    disabled = scan_linda_setups(
        bars,
        LindaSetupFlags(
            turtle_soup=False,
            turtle_soup_plus_one=False,
            eighty_twenty=False,
            the_anti=False,
            momentum_pinball=False,
            hv_squeeze=False,
            adx_gapper=False,
        ),
        context=context,
    )
    assert any(signal.code.startswith("TS_") for signal in enabled)
    assert not any(signal.code.startswith("TS_") for signal in disabled)


def test_scan_linda_setups_history_marks_prior_bars() -> None:
    bars = [
        _bar(i, 100 + i * 0.05, 100.5 + i * 0.05, 99.5 + i * 0.05, 100 + i * 0.05)
        for i in range(24)
    ]
    bars[-1] = _bar(23, 101.0, 103.4, 100.8, 101.2, 2400.0)
    context = LindaSetupScanContext(atr=1.0, adx=28.0, ema20=100.5, ema50=100.0, volume_avg=1200.0)
    flags = LindaSetupFlags(turtle_soup=True)
    history = scan_linda_setups_history(
        bars,
        flags,
        context_at=lambda _index: context,
        lookback=24,
    )
    assert history
    assert history[-1][0] == len(bars) - 1


def test_linda_playbook_setups_participate_in_decision_context() -> None:
    bars = [
        _bar(i, 100 + i * 0.05, 100.5 + i * 0.05, 99.5 + i * 0.05, 100 + i * 0.05)
        for i in range(24)
    ]
    bars[-1] = _bar(23, 101.0, 103.4, 100.8, 101.2, 2400.0)

    enabled = linda_volume(
        bars,
        params=LindaVolumeParams(enable_turtle_soup=True, indian_min_score=55),
        features={"display_symbol": "ES"},
    )
    disabled = linda_volume(
        bars,
        params=LindaVolumeParams(enable_turtle_soup=False, indian_min_score=55),
        features={"display_symbol": "ES"},
    )
    assert enabled["playbook_setups"]["experimental"] is True
    assert enabled["playbook_setups"]["decision_eligible"] is True
    assert "market_context" not in enabled
    assert "market_context" not in (enabled.get("latest") or {})
    assert "market_context" not in ((enabled.get("latest") or {}).get("table_state") or {})
    assert {"market", "flow", "lock", "strategy"}.isdisjoint(
        (enabled.get("latest") or {}).get("table_state") or {}
    )
    assert disabled["playbook_setups"]["items"] == []
    assert enabled["playbook_setups"]["items"]
    playbook_overlays = [
        item
        for item in enabled.get("overlays") or []
        if str(item.get("role", "")).startswith("linda_setup_")
    ]
    assert playbook_overlays
    assert all("decision_eligible" not in item for item in playbook_overlays)
    assert all("\n" not in str(item.get("tooltip") or "") for item in playbook_overlays)
    assert all(item.get("scenario") and item.get("trigger_event") for item in playbook_overlays)


def test_playbook_setup_includes_promotion_preview() -> None:
    bars = [
        _bar(i, 100 + i * 0.05, 100.5 + i * 0.05, 99.5 + i * 0.05, 100 + i * 0.05)
        for i in range(24)
    ]
    bars[-1] = _bar(23, 101.0, 103.4, 100.8, 101.2, 2400.0)
    context = LindaSetupScanContext(atr=1.0, adx=28.0, ema20=100.5, ema50=100.0, volume_avg=1200.0)
    signals = scan_linda_setups(bars, LindaSetupFlags(turtle_soup=True), context=context)
    assert signals
    payload = signals[0].to_dict()
    assert payload["contract"] == "playbook-decision-v1"
    preview = payload["promotion_preview"]
    assert preview["signal_name"] == "linda_fade"
    assert preview["kind"] == "fade"
    assert payload["action"] in {"WATCH", "ARM", "GO", "CANDIDATE"}
    assert payload.get("theory")
    missing_score = playbook_promotion_preview(
        {"action": "GO", "code": "TS_DN", "direction": "short"}
    )
    assert missing_score["eligible"] is False
    assert missing_score["reject_reasons"][0] == "missing_score"


def test_linda_playbook_promotes_to_shared_decision_candidate() -> None:
    from aef_terminal.domain import ScenarioKind
    from aef_terminal.indicators.modules.linda_volume.candidates import (
        signal_candidates_from_linda_indicator,
    )

    bars = [
        _bar(i, 100 + i * 0.05, 100.5 + i * 0.05, 99.5 + i * 0.05, 100 + i * 0.05)
        for i in range(24)
    ]
    bars[-1] = _bar(23, 101.0, 103.4, 100.8, 101.2, 2400.0)
    result = linda_volume(
        bars,
        params=LindaVolumeParams(enable_turtle_soup=True, indian_min_score=55),
        features={"display_symbol": "ES"},
    )

    candidates = signal_candidates_from_linda_indicator(
        result, source="linda_volume", score_floor=40, atr_value=1.0
    )
    playbook_candidates = [item for item in candidates if item.details.get("playbook")]
    assert playbook_candidates
    candidate = playbook_candidates[0]
    assert candidate.name == "linda_fade"
    assert candidate.kind == ScenarioKind.FADE
    assert candidate.details["code"].startswith("TS_")
    assert candidate.reason_code == candidate.details["code"].lower()
    assert candidate.details["plan_coherent"] is True
    assert candidate.producer_phase in {ActionPhase.ARM, ActionPhase.GO}
    assert result["playbook_candidate_promotion"]["promoted"] is True


def test_playbook_overlay_exposes_action_not_wait_default() -> None:
    from aef_terminal.indicators.modules.linda_volume.setups import setup_overlay

    bars = [
        _bar(i, 100 + i * 0.05, 100.5 + i * 0.05, 99.5 + i * 0.05, 100 + i * 0.05)
        for i in range(24)
    ]
    bars[-1] = _bar(23, 101.0, 103.4, 100.8, 101.2, 2400.0)
    context = LindaSetupScanContext(atr=1.0, adx=28.0, ema20=100.5, ema50=100.0, volume_avg=1200.0)
    signals = scan_linda_setups(bars, LindaSetupFlags(turtle_soup=True), context=context)
    assert signals
    overlay = setup_overlay(signals[0], bars[-1])
    assert overlay["action"] in {"WATCH", "ARM", "GO", "CANDIDATE"}
    assert overlay["action"] != "WAIT"
    assert "lines" not in overlay
    assert "bg" not in overlay
    assert "text" not in overlay
    assert "context_lines" not in overlay
    assert overlay["evidence"]["context"]
    assert overlay["metrics"]["mode"] in {"decision", "visual_test"}
    assert overlay["fact_groups"][0]["items"]
    assert overlay["metrics"]["trigger"]["value"] == signals[0].trigger
    assert overlay["metrics"]["stop"]["value"] == signals[0].stop
    assert overlay["metrics"]["target"]["value"] == signals[0].target
    assert overlay["setup"] == signals[0].code.lower()
    assert overlay["evidence"]["supporting"]
    assert "pro" not in overlay
    assert overlay["setup_summary"] == signals[0].setup_summary
    assert "commentary" not in overlay
    assert "theory" not in overlay
    assert "mode" not in overlay
    assert "experimental" not in overlay
    assert "decision_eligible" not in overlay
    assert overlay["signal"]["theory"] == overlay["candidate_reason"]
    assert overlay["signal"]["mode"] in {"decision", "visual_test"}
    assert overlay["signal"]["decision_eligible"] is True
    assert "read" not in overlay


def test_linda_playbook_overlay_payload_satisfies_runtime_contract() -> None:
    bars = [
        _bar(i, 100 + i * 0.05, 100.5 + i * 0.05, 99.5 + i * 0.05, 100 + i * 0.05)
        for i in range(24)
    ]
    bars[-1] = _bar(23, 101.0, 103.4, 100.8, 101.2, 2400.0)
    result = linda_volume(
        bars,
        params=LindaVolumeParams(enable_turtle_soup=True, indian_min_score=55),
        features={"display_symbol": "ES"},
    )

    attached = attach_indicator_status(
        "linda_volume",
        result,
        input_bars=bars,
        analysis_bar=bars[-1],
        mode="confirmed",
    )

    assert attached["status"]["state_code"] != "error"
    playbook_overlays = [
        item
        for item in attached.get("overlays") or []
        if item.get("overlay_role") == "linda_playbook"
    ]
    assert playbook_overlays
    assert playbook_overlays[0]["evidence"]["context"]
    assert playbook_overlays[0]["metrics"]["mode"] in {"decision", "visual_test"}
    assert "theory" not in playbook_overlays[0]
    assert "mode" not in playbook_overlays[0]
    assert "experimental" not in playbook_overlays[0]
    assert "decision_eligible" not in playbook_overlays[0]
    assert "commentary" not in playbook_overlays[0]
    assert playbook_overlays[0]["signal"]["theory"] == playbook_overlays[0]["candidate_reason"]
    assert playbook_overlays[0]["signal"]["decision_eligible"] is True


def test_intraday_session_engine_builds_first_hour_range() -> None:
    from zoneinfo import ZoneInfo

    from aef_terminal.features.intraday_sessions import first_hour_range, session_context_summary

    ny = ZoneInfo("America/New_York")
    day_bars = [
        Bar("ES", datetime(2026, 1, 2, 9, 35, tzinfo=ny), 100, 101, 99.5, 100.5, 1000, "5m"),
        Bar("ES", datetime(2026, 1, 2, 9, 50, tzinfo=ny), 100.5, 102, 100, 101.5, 1200, "5m"),
        Bar("ES", datetime(2026, 1, 2, 11, 0, tzinfo=ny), 101.5, 103, 101, 102.5, 900, "5m"),
    ]
    provider_session = ProviderSessionReset(
        available=True,
        source="test_provider_schedule",
        reason_code="",
        calendar="provider_schedule",
        intervals=(
            ProviderSessionInterval(
                opens_at=datetime(2026, 1, 2, 9, 30, tzinfo=ny).astimezone(UTC),
                closes_at=datetime(2026, 1, 2, 16, 0, tzinfo=ny).astimezone(UTC),
                session_date=datetime(2026, 1, 2, tzinfo=ny).date(),
            ),
        ),
    )
    high, low = first_hour_range(
        day_bars,
        provider_session=provider_session,
    )
    assert high == 102.0
    assert low == 99.5
    summary = session_context_summary(
        day_bars,
        provider_session=provider_session,
    )
    assert summary["first_hour_high"] == high
    assert summary["in_session"] is True
