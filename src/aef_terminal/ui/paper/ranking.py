from __future__ import annotations

from typing import Any

from aef_terminal.engine.sentiment_weights import resolve_candidate_weight

PAPER_META_SOURCES = frozenset({"trade_setup", "trade setup", "decision", "level_cluster"})


def paper_signal_source_key(raw: dict[str, Any]) -> str:
    payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else {}
    for source_payload in (raw, payload):
        for key in (
            "signal_source",
            "indicator_source",
            "setup_source",
            "primary_source",
            "indicator",
            "module",
        ):
            value = str(source_payload.get(key) or "").replace(" ", "_").strip().lower()
            if value and value not in PAPER_META_SOURCES:
                return value[:48]
        confluence = source_payload.get("confluence_sources")
        if isinstance(confluence, list):
            for item in confluence:
                if not isinstance(item, dict):
                    continue
                value = str(item.get("source") or "").replace(" ", "_").strip().lower()
                if value and value not in PAPER_META_SOURCES:
                    return value[:48]
    source = str(raw.get("source") or "").replace(" ", "_").strip().lower()
    if source and source not in PAPER_META_SOURCES:
        return source[:48]
    return ""


def paper_position_source_key(position: dict[str, Any]) -> str:
    payload = position.get("payload") if isinstance(position.get("payload"), dict) else {}
    return paper_signal_source_key(payload)


def paper_signal_reliability_rank(source: str, *, profile_key: str | None = None) -> float:
    key = str(source or "").strip().lower()
    if not key or key in PAPER_META_SOURCES:
        return 0.0
    return float(resolve_candidate_weight(key, profile_key=profile_key))


def paper_flip_rank_allows(
    *,
    current_source: str,
    new_source: str,
    profile_key: str | None = None,
) -> bool:
    current_rank = paper_signal_reliability_rank(current_source, profile_key=profile_key)
    new_rank = paper_signal_reliability_rank(new_source, profile_key=profile_key)
    return new_rank > current_rank
