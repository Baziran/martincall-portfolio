from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from aef_terminal.data.gex.contracts import GexCaptureMode
from aef_terminal.domain import ActionPhase, Direction, DomainFact, ScenarioDecision, ScenarioKind
from aef_terminal.runtime.instruments import resolve_instrument_profile
from aef_terminal.data.instrument_identity import (
    current_futures_contract_id,
    instrument_is_futures,
    instrument_provider,
    provider_price_increment,
    provider_contract_id,
    qualified_instrument_id,
    route_fingerprint,
)
from aef_terminal.engine.data_quality import snapshot_freshness
from aef_terminal.engine.gex_context import build_gex_dynamics_context
from aef_terminal.engine.vsa_context import empty_vsa_volume_context
from aef_terminal.engine.serialization import (
    serialize_decision,
)
from aef_terminal.engine.trade_setup import (
    TRADE_SETUP_AUTHORITY_CONTRACT,
    blocked_trade_setup_execution_authority,
    build_blocked_trade_setup_card,
)
from aef_terminal.indicators.registry import indicator_ids_for_output_contract

from aef_terminal.engine.analyze.constants import INDICATOR_STATUS_META
from aef_terminal.engine.analyze.indicator_status import (
    attach_indicator_status,
    empty_indicator_result,
)


def empty_market_snapshot(
    instrument: dict[str, str],
    interval: str,
    warning: str,
    *,
    gex_context_active: bool = False,
    gex_capture_mode: GexCaptureMode = "request",
) -> dict[str, Any]:
    if type(gex_context_active) is not bool:
        raise TypeError("GEX context active state must be a boolean")
    empty_indicators = {
        name: attach_indicator_status(
            name,
            empty_indicator_result(name),
            input_bars=[],
            analysis_bar=None,
            mode="confirmed",
            error=ValueError(warning),
        )
        for name in INDICATOR_STATUS_META
    }
    empty_decision = ScenarioDecision(
        kind=ScenarioKind.WAIT,
        direction=Direction.FLAT,
        confidence=0.0,
        action=ActionPhase.BLOCK,
        trigger=None,
        stop=None,
        target=None,
        invalidation=None,
        reasons=[],
        source="data_quality",
        trigger_event=DomainFact("no_history"),
        reason_codes=["no_history"],
    )
    execution_authority = blocked_trade_setup_execution_authority(
        "no_history",
        provider_ids=indicator_ids_for_output_contract(TRADE_SETUP_AUTHORITY_CONTRACT),
    )
    empty_trade_setup = build_blocked_trade_setup_card(
        execution_authority=execution_authority,
        reason_code="no_history",
        watch_event="wait_for_market_history",
        signal_source=empty_decision.source,
    )
    empty_card = dict(empty_trade_setup["action_card"])
    updated_at = datetime.now(tz=UTC)
    instrument_profile = resolve_instrument_profile(instrument)
    snapshot = {
        "meta": {
            "symbol": instrument["display"],
            "provider_symbol": instrument["provider_symbol"],
            "instrument_id": qualified_instrument_id(instrument),
            "route_fingerprint": route_fingerprint(instrument),
            "provider": instrument_provider(instrument),
            "provider_contract_id": current_futures_contract_id(instrument)
            if instrument_is_futures(instrument)
            else provider_contract_id(instrument),
            "timeframe": interval,
            "source": "none",
            "price": None,
            "change": 0.0,
            "change_pct": 0.0,
            "live_bar_closed": True,
            "analysis_ts": None,
            "confirmed_bar_count": 0,
            "chart_bar_count": 0,
            "signal_bar_count": 0,
            "instrument_profile": instrument_profile.as_meta(),
            "price_increment": provider_price_increment(instrument),
            "updated_at": updated_at.isoformat(),
            "freshness": snapshot_freshness(
                timeframe=interval,
                source="none",
                latest_bar_ts=None,
                updated_at=updated_at,
                instrument_profile=instrument_profile,
            ),
            "warning": warning,
        },
        "bars": [],
        "features": {},
        "candidates": [],
        "indicators": empty_indicators,
        "vsa_volume": empty_vsa_volume_context("no_history"),
        "decision": serialize_decision(empty_decision),
        "command": empty_card,
        "trade_setup": empty_trade_setup,
        "levels": [],
    }
    if gex_context_active:
        snapshot["gex_dynamics"] = build_gex_dynamics_context(
            [],
            (),
            capture_mode=gex_capture_mode,
        )
    return snapshot
