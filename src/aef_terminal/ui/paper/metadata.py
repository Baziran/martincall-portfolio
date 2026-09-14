from __future__ import annotations

from typing import Any

from aef_terminal.ui.paper.ranking import paper_signal_source_key


def paper_payload(trade: dict[str, Any]) -> dict[str, Any]:
    return trade.get("payload") if isinstance(trade.get("payload"), dict) else {}


def paper_setup_source_label(trade: dict[str, Any]) -> str:
    payload = paper_payload(trade)
    for value in (payload.get("signal_source_label"), payload.get("primary_label")):
        text = str(value or "").strip()
        if text:
            return text[:80]
    confluence = payload.get("confluence_sources")
    if isinstance(confluence, list):
        labels = [
            str(item.get("label") or item.get("source") or "").strip()
            for item in confluence
            if isinstance(item, dict) and str(item.get("source") or item.get("label") or "").strip()
        ]
        if labels:
            return " | ".join(labels[:3])[:120]
    return paper_setup_key(trade)


def paper_setup_key(trade: dict[str, Any]) -> str:
    payload = paper_payload(trade)
    for value in (
        payload.get("setup"),
        payload.get("code"),
        payload.get("action"),
        trade.get("source"),
    ):
        text = str(value or "").replace("_", " ").strip()
        if text:
            return text[:36]
    return "unknown"


def paper_trade_session(trade: dict[str, Any]) -> str:
    payload = paper_payload(trade)
    provider_session = payload.get("provider_session", trade.get("provider_session"))
    if isinstance(provider_session, dict):
        for key in ("session_id", "session_date", "trade_session_date"):
            value = provider_session.get(key)
            if isinstance(value, str) and value:
                return value
        return "unknown"
    if isinstance(provider_session, str) and provider_session:
        return provider_session
    return "unknown"


def paper_trade_edge_action(trade: dict[str, Any]) -> str:
    payload = paper_payload(trade)
    edge = payload.get("edge") if isinstance(payload.get("edge"), dict) else {}
    rule = payload.get("edge_rule") if isinstance(payload.get("edge_rule"), dict) else {}
    action = str(edge.get("action") or rule.get("action") or "none").strip().lower()
    return action if action in {"block", "review", "prefer"} else "none"


def paper_payload_float(trade: dict[str, Any], *keys: str) -> float | None:
    payload = paper_payload(trade)
    for key in keys:
        try:
            value = payload.get(key)
            if value is not None:
                return float(value)
        except TypeError, ValueError:
            continue
    return None


def paper_trade_enriched(trade: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(trade)
    enriched["signal_source"] = paper_signal_source_key(enriched)
    enriched["setup_source_label"] = paper_setup_source_label(enriched)
    enriched["setup"] = paper_setup_key(enriched)
    return enriched
