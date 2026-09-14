from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from aef_terminal.data.instrument_identity import require_exact_identity_text
from aef_terminal.data.providers import route_instrument
from aef_terminal.paper_contract import require_paper_contract_identity
from aef_terminal.ui.paper.config import paper_config as build_paper_config
from aef_terminal.ui.paper.constants import PAPER_MIN_RR
from aef_terminal.ui.paper.execution import screen_execution_price_snapshot
from aef_terminal.ui.paper.option_market import paper_option_execution_snapshot
from aef_terminal.ui.paper.signals import paper_trade_from_signal as build_paper_trade_from_signal
from aef_terminal.ui.runtime_instruments import lookup_runtime_instrument


@dataclass(frozen=True)
class PaperRuntimeDeps:
    client_settings_snapshot: Callable[[], dict[str, Any]]
    edge_filter_loader: Callable[[], list[dict[str, Any]]]


_DEPS: PaperRuntimeDeps | None = None


def configure_paper_runtime_deps(deps: PaperRuntimeDeps) -> None:
    global _DEPS
    _DEPS = deps


def _deps() -> PaperRuntimeDeps:
    if _DEPS is None:
        raise RuntimeError("paper runtime dependencies are not configured")
    return _DEPS


def _client_settings_snapshot() -> dict[str, Any]:
    return _deps().client_settings_snapshot()


def _edge_filter_loader() -> list[dict[str, Any]]:
    return _deps().edge_filter_loader()


def _paper_boolean_setting(settings: dict[str, Any], key: str, *, default: bool) -> bool:
    raw = settings.get(key)
    if raw is None:
        return default
    if raw == "true":
        return True
    if raw == "false":
        return False
    raise ValueError(f"PAPER_SETTING_INVALID: {key}")


def paper_config() -> dict[str, Any]:
    settings = _client_settings_snapshot()
    return build_paper_config(
        {
            "min_rr": settings.get("aef:paperMinRr", PAPER_MIN_RR),
            "edge_gate": _paper_boolean_setting(
                settings,
                "aef:paperEdgeGate",
                default=True,
            ),
        }
    )


def paper_trade_from_signal(
    symbol: str,
    timeframe: str,
    raw: object,
    *,
    instrument_id: str,
) -> dict | None:
    return build_paper_trade_from_signal(
        symbol,
        timeframe,
        raw,
        config=paper_config(),
        edge_filter_loader=_edge_filter_loader,
        instrument_id=instrument_id,
    )


def paper_order_price_snapshot(order: dict[str, Any]) -> dict[str, Any] | None:
    try:
        instrument_id = require_exact_identity_text(
            order.get("instrument_id"), field="instrument_id"
        )
        route_fingerprint = require_exact_identity_text(
            order.get("route_fingerprint"), field="route_fingerprint"
        )
        contract_id = require_exact_identity_text(
            order.get("provider_contract_id"), field="provider_contract_id"
        )
    except ValueError:
        return None
    provider = str(order.get("provider") or "").strip().lower()
    timeframe = str(order.get("timeframe") or "").strip()
    if (
        not instrument_id
        or not route_fingerprint
        or not provider
        or not contract_id
        or not timeframe
    ):
        return None
    try:
        paper_contract = require_paper_contract_identity(order)
    except ValueError:
        return None
    if paper_contract.scope_kind == "option":
        return paper_option_execution_snapshot(order)
    try:
        instrument = lookup_runtime_instrument(instrument_id)
        route = route_instrument(instrument)
    except Exception:
        return None
    if (
        route.fingerprint != route_fingerprint
        or route.provider != provider
        or route.adapter.session_contract_id(route.instrument) != contract_id
    ):
        return None
    return screen_execution_price_snapshot(instrument_id, timeframe, route_fingerprint)
