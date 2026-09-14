"""Paper execution sync — Trade Center is the only entry authority.

Flow:
  indicators (signals for chart/state)
    -> engine decision + trade_setup card
    -> paper_sync_from_trade_setup (this module)
    -> PostgreSQL positions/orders (journal)
    -> chart markers from journal only

Entry plans are frozen at open. Opposite GO may flip the net position when the
new signal source outranks the open position owner.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from aef_terminal.data.instrument_identity import require_exact_identity_text
from aef_terminal.engine.common import parse_aware_utc_ts
from aef_terminal.engine.execution_intent import execution_intent_from_trade_setup
from aef_terminal.ui.paper.positions import (
    paper_open_signal_position,
)


@dataclass(frozen=True)
class PaperSyncDeps:
    store_factory: Callable[[], Any]
    paper_config: Callable[[], dict[str, Any]]
    paper_order_execution_transition: Callable[..., dict[str, Any]]
    paper_order_price_snapshot: Callable[
        [dict[str, Any]],
        dict[str, Any] | None,
    ]


_DEPS: PaperSyncDeps | None = None


def configure_paper_sync_deps(deps: PaperSyncDeps) -> None:
    global _DEPS
    _DEPS = deps


def _deps() -> PaperSyncDeps:
    if _DEPS is None:
        raise RuntimeError("paper sync dependencies are not configured")
    return _DEPS


def paper_sync_from_analysis_snapshot(
    snapshot: dict[str, Any],
    *,
    queue_paper_trade_telegram: Callable[[str, dict[str, Any]], None] | None = None,
) -> dict[str, Any] | None:
    if not isinstance(snapshot, dict):
        return None
    meta = snapshot.get("meta") if isinstance(snapshot.get("meta"), dict) else {}
    symbol = str(meta.get("symbol") or "")
    try:
        instrument_id = require_exact_identity_text(
            meta.get("instrument_id"), field="instrument_id"
        )
        route_fingerprint = require_exact_identity_text(
            meta.get("route_fingerprint"), field="route_fingerprint"
        )
        provider_contract_id = require_exact_identity_text(
            meta.get("provider_contract_id"), field="provider_contract_id"
        )
    except ValueError:
        return None
    provider = str(meta.get("provider") or "").strip().lower()
    timeframe = str(meta.get("timeframe") or "").strip()
    if not symbol or not provider or not timeframe:
        return None
    trade_setup = (
        snapshot.get("trade_setup") if isinstance(snapshot.get("trade_setup"), dict) else {}
    )
    bar_ts = meta.get("analysis_latest_ts")
    analysis_generation = meta.get("analysis_window_hash")
    analysis_bar_count = meta.get("analysis_bar_count")
    if (
        not isinstance(bar_ts, str)
        or parse_aware_utc_ts(bar_ts) is None
        or not isinstance(analysis_generation, str)
        or len(analysis_generation) != 32
        or any(character not in "0123456789abcdef" for character in analysis_generation)
        or isinstance(analysis_bar_count, bool)
        or not isinstance(analysis_bar_count, int)
        or analysis_bar_count <= 0
    ):
        return None

    deps = _deps()
    store = deps.store_factory()
    if store is None:
        return None
    return paper_sync_from_trade_setup(
        store,
        symbol=symbol,
        instrument_id=instrument_id,
        route_fingerprint=route_fingerprint,
        provider=provider,
        provider_contract_id=provider_contract_id,
        timeframe=timeframe,
        trade_setup=trade_setup,
        bar_ts=bar_ts,
        analysis_generation=analysis_generation,
        config=deps.paper_config(),
        paper_order_execution_transition=deps.paper_order_execution_transition,
        paper_order_price_snapshot=deps.paper_order_price_snapshot,
        queue_paper_trade_telegram=queue_paper_trade_telegram,
    )


def paper_sync_from_trade_setup(
    store: Any,
    *,
    symbol: str,
    instrument_id: str,
    route_fingerprint: str,
    provider: str,
    provider_contract_id: str,
    timeframe: str,
    trade_setup: dict[str, Any] | None,
    bar_ts: str,
    analysis_generation: str,
    config: dict[str, Any],
    paper_order_execution_transition: Any,
    paper_order_price_snapshot: Any,
    edge_filter_loader: Any = None,
    queue_paper_trade_telegram: Callable[[str, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    from aef_terminal.ui.paper.notify import paper_notify_fill_result

    opened = 0
    rejected = 0
    flipped = 0
    skipped = 0
    outcomes: list[dict[str, Any]] = []
    setup = trade_setup if isinstance(trade_setup, dict) else {}
    setup_go = str(setup.get("action") or "").upper() == "GO"
    analysis_bar_dt = parse_aware_utc_ts(bar_ts)
    valid_generation = analysis_bar_dt is not None
    exact_analysis_generation = str(analysis_generation or "").strip()
    valid_generation = valid_generation and bool(exact_analysis_generation)
    canonical_bar_ts = (
        analysis_bar_dt.isoformat() if analysis_bar_dt is not None else str(bar_ts or "")
    )
    signal = (
        execution_intent_from_trade_setup(
            setup,
            bar_ts=canonical_bar_ts,
            analysis_generation=exact_analysis_generation,
        )
        if valid_generation
        else None
    )
    if signal is not None:
        result = paper_open_signal_position(
            store,
            symbol=symbol,
            instrument_id=instrument_id,
            route_fingerprint=route_fingerprint,
            provider=provider,
            provider_contract_id=provider_contract_id,
            timeframe=timeframe,
            raw=signal,
            config=config,
            price_snapshot=paper_order_price_snapshot(
                {
                    "symbol": symbol,
                    "instrument_id": instrument_id,
                    "route_fingerprint": route_fingerprint,
                    "provider": provider,
                    "provider_contract_id": provider_contract_id,
                    "timeframe": timeframe,
                    "side": signal.get("side"),
                    "entry": signal.get("entry"),
                }
            ),
            paper_order_execution_transition=paper_order_execution_transition,
            edge_filter_loader=edge_filter_loader,
        )
        result_order = result.get("order") if isinstance(result.get("order"), dict) else {}
        result_execution = (
            result.get("execution") if isinstance(result.get("execution"), dict) else {}
        )
        if result.get("skipped"):
            skipped += 1
            outcomes.append(
                {
                    "status": "skipped",
                    "code": str(result.get("code") or "PAPER_EXECUTION_SKIPPED"),
                    "reason": str(result.get("reason") or "paper execution skipped"),
                    "order_id": str(result_order.get("id") or ""),
                    "execution": dict(result_execution),
                }
            )
        elif result.get("flipped") and result.get("opened"):
            flipped += 1
            opened += 1
            outcomes.append(
                {
                    "status": "filled",
                    "code": "PAPER_POSITION_FLIPPED",
                    "reason": "",
                    "order_id": str(result_order.get("id") or ""),
                    "execution": dict(result_execution),
                }
            )
            paper_notify_fill_result(
                result,
                close_reason="ranked_flip",
                queue_telegram=queue_paper_trade_telegram,
            )
        elif result.get("opened"):
            opened += 1
            outcomes.append(
                {
                    "status": "filled",
                    "code": "PAPER_POSITION_OPENED",
                    "reason": "",
                    "order_id": str(result_order.get("id") or ""),
                    "execution": dict(result_execution),
                }
            )
            paper_notify_fill_result(
                result,
                close_reason="automatic_position_close",
                queue_telegram=queue_paper_trade_telegram,
            )
        else:
            rejected += 1
            outcomes.append(
                {
                    "status": "rejected",
                    "code": str(result.get("code") or "PAPER_EXECUTION_REJECTED"),
                    "reason": str(result.get("reason") or "paper execution rejected"),
                    "order_id": str(result_order.get("id") or ""),
                    "execution": dict(result_execution),
                }
            )
    elif setup_go:
        rejected += 1
        outcomes.append(
            {
                "status": "rejected",
                "code": (
                    "PAPER_SIGNAL_INTENT_INVALID"
                    if valid_generation
                    else "PAPER_SIGNAL_GENERATION_INVALID"
                ),
                "reason": (
                    "trade setup GO is not executable"
                    if valid_generation
                    else "trade setup analysis generation is unavailable"
                ),
                "order_id": "",
                "execution": {
                    "analysis_bar_ts": canonical_bar_ts,
                    "analysis_generation": exact_analysis_generation,
                },
            }
        )
    return {
        "ok": True,
        "opened": opened,
        "rejected": rejected,
        "flipped": flipped,
        "updated": 0,
        "closed": 0,
        "skipped": skipped,
        "execution": "positions",
        "outcomes": outcomes,
    }
