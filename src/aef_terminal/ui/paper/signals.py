from __future__ import annotations

import hashlib
from typing import Any

from aef_terminal.domain import Direction, DomainFact
from aef_terminal.runtime.math_utils import exact_finite_number_or_none
from aef_terminal.signals.direction import direction_from_any
from aef_terminal.signals.trade_plan import (
    coherent_trade_plan,
    execution_plan_rejection,
    reward_risk,
)
from aef_terminal.ui.paper.config import paper_config
from aef_terminal.ui.paper.constants import (
    EdgeFilterLoader,
    PAPER_ALLOWED_SETUPS,
    PAPER_ALLOWED_SOURCES,
)
from aef_terminal.ui.paper.edge_filters import paper_edge_context
from aef_terminal.ui.paper.utils import parse_iso_utc


def paper_execution_plan_rejection(
    side: str,
    *,
    fill_price: float | None,
    stop: float | None,
    target: float | None,
    config: dict[str, Any],
) -> DomainFact | None:
    config = paper_config(config)
    direction = direction_from_any(side)
    if direction == Direction.FLAT:
        return DomainFact("flat_direction")
    return execution_plan_rejection(
        direction,
        fill_price=fill_price,
        stop=stop,
        target=target,
        min_rr=config["min_rr"],
    )


def paper_normalized_setup(raw: dict[str, Any], plan: dict[str, Any]) -> str:
    setup = str(raw.get("setup") or plan.get("kind") or "")
    return setup if setup in {"fade", "transit"} else ""


def paper_signal_rejection(
    raw: dict[str, Any],
    plan: dict[str, Any],
) -> str:
    source = str(raw.get("source") or "").strip().lower()
    if source not in PAPER_ALLOWED_SOURCES:
        return f"source {source or 'unknown'} is not paper-enabled"
    if source == "trade_setup":
        setup = paper_normalized_setup(raw, plan)
        if setup not in PAPER_ALLOWED_SETUPS:
            return f"setup {setup or 'unknown'} is not paper-enabled"
    action = str(raw.get("action") or "").strip().upper()
    if action != "GO":
        return f"action {action or 'unknown'} is not GO"
    return ""


def paper_trade_from_signal(
    symbol: str,
    timeframe: str,
    raw: object,
    config: dict[str, Any],
    edge_filter_loader: EdgeFilterLoader | None = None,
    *,
    instrument_id: str,
) -> dict | None:
    if not isinstance(raw, dict):
        return None
    config = paper_config(config)
    plan = raw.get("plan") if isinstance(raw.get("plan"), dict) else {}
    if not plan and isinstance(raw.get("trade_plan"), dict):
        plan = raw.get("trade_plan") or {}
    if paper_signal_rejection(raw, plan):
        return None
    side = str(raw.get("side") or raw.get("direction") or "").lower()
    if side not in {"long", "short"}:
        return None
    edge = paper_edge_context(instrument_id, raw, side, edge_filter_loader=edge_filter_loader)
    entry_raw = (
        raw.get("entry")
        if raw.get("entry") is not None
        else raw.get("trigger")
        if raw.get("trigger") is not None
        else plan.get("entry")
        if plan.get("entry") is not None
        else plan.get("trigger")
    )
    stop_raw = raw.get("stop") if raw.get("stop") is not None else plan.get("stop")
    target_raw = raw.get("target") if raw.get("target") is not None else plan.get("target")
    entry = exact_finite_number_or_none(entry_raw)
    stop = exact_finite_number_or_none(stop_raw)
    target = exact_finite_number_or_none(target_raw)
    if entry is None or stop is None or target is None:
        return None
    if side == "long" and not coherent_trade_plan(Direction.LONG, entry, stop, target):
        return None
    if side == "short" and not coherent_trade_plan(Direction.SHORT, entry, stop, target):
        return None
    risk = abs(entry - stop)
    reward = abs(target - entry)
    rr = (
        reward_risk(Direction.LONG if side == "long" else Direction.SHORT, entry, stop, target)
        or 0.0
    )
    if risk <= 0 or reward <= 0:
        return None
    if rr < config["min_rr"]:
        return None
    if risk > max(abs(entry) * 0.20, reward * 25.0):
        return None
    source = str(raw.get("source") or "indicator")
    setup = paper_normalized_setup(raw, plan)
    opened_at = parse_iso_utc(raw.get("ts") or raw.get("bar_ts"))
    if opened_at is None:
        return None
    signal_ts = opened_at.isoformat()
    analysis_generation = str(raw.get("analysis_generation") or "").strip()
    seed = "|".join(
        [
            instrument_id,
            timeframe,
            source,
            side,
            setup,
            signal_ts,
            analysis_generation,
            repr(entry),
            repr(stop),
            repr(target),
            str(raw.get("code") or raw.get("action") or ""),
        ]
    )
    trade_id = "pt-" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:24]
    return {
        "id": trade_id,
        "symbol": symbol,
        "instrument_id": instrument_id,
        "timeframe": timeframe,
        "source": source,
        "side": side,
        "qty": 1.0,
        "entry": entry,
        "stop": stop,
        "target": target,
        "opened_at": opened_at,
        "payload": {
            **dict(raw),
            "setup": setup,
            "rr": round(rr, 4),
            "paper_min_rr": config["min_rr"],
            "edge": {key: value for key, value in edge.items() if key != "rule"},
            "edge_rule": edge.get("rule"),
        },
    }
