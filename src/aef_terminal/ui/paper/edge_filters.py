from __future__ import annotations

import csv
import json
import os
import time
from pathlib import Path
from typing import Any

from aef_terminal.data.instrument_identity import require_exact_identity_text
from aef_terminal.features.time_context import utc_daypart
from aef_terminal.ui.paper.constants import (
    EdgeFilterLoader,
    PAPER_EDGE_FILTER_CACHE_SECONDS,
    _LOGGER,
    _PAPER_EDGE_FILTER_CACHE,
)
from aef_terminal.ui.paper.ranking import paper_signal_source_key
from aef_terminal.ui.paper.utils import paper_project_root


def paper_edge_filter_path() -> Path | None:
    env_path = os.environ.get("AEF_PAPER_EDGE_FILTERS")
    if env_path:
        path = Path(env_path).expanduser()
        return path if path.exists() else None
    roots = [
        paper_project_root() / "data" / "backtests" / "nightly",
        paper_project_root().parent / "data" / "backtests" / "nightly",
        paper_project_root() / "data" / "backtests" / "smoke",
        paper_project_root().parent / "data" / "backtests" / "smoke",
    ]
    candidates: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        candidates.extend(root.glob("*/edge_filters.json"))
        candidates.extend(root.glob("*/edge_filters.csv"))
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def paper_normalize_edge_rule(row: dict[str, Any]) -> dict[str, Any] | None:
    action = str(row.get("action") or "").strip().lower()
    if action not in {"block", "review", "prefer"}:
        return None
    try:
        instrument_id = require_exact_identity_text(row.get("instrument_id"), field="instrument_id")
    except ValueError:
        return None
    source = str(row.get("source") or "").replace(" ", "_").strip().lower()
    side = str(row.get("side") or "").strip().lower()
    daypart = str(row.get("utc_daypart") or "").strip().lower()
    if not source or side not in {"long", "short"} or not daypart:
        return None
    return {
        **row,
        "instrument_id": instrument_id,
        "source": source,
        "side": side,
        "utc_daypart": daypart,
        "action": action,
        "edge_key": str(
            row.get("edge_key")
            or json.dumps([instrument_id, source, side, daypart], separators=(",", ":"))
        ),
    }


def paper_load_edge_filters() -> list[dict[str, Any]]:
    now = time.monotonic()
    if (
        now - float(_PAPER_EDGE_FILTER_CACHE.get("loaded_at") or 0.0)
        < PAPER_EDGE_FILTER_CACHE_SECONDS
    ):
        return list(_PAPER_EDGE_FILTER_CACHE.get("rules") or [])
    path = paper_edge_filter_path()
    if path is None:
        _PAPER_EDGE_FILTER_CACHE.update({"loaded_at": now, "path": "", "mtime": 0.0, "rules": []})
        return []
    mtime = path.stat().st_mtime
    if str(path) == str(_PAPER_EDGE_FILTER_CACHE.get("path") or "") and mtime == float(
        _PAPER_EDGE_FILTER_CACHE.get("mtime") or 0.0
    ):
        _PAPER_EDGE_FILTER_CACHE["loaded_at"] = now
        return list(_PAPER_EDGE_FILTER_CACHE.get("rules") or [])
    rows: list[dict[str, Any]] = []
    try:
        if path.suffix.lower() == ".json":
            payload = json.loads(path.read_text())
            raw_rows = payload.get("edge_filters") if isinstance(payload, dict) else payload
            rows = (
                [row for row in raw_rows if isinstance(row, dict)]
                if isinstance(raw_rows, list)
                else []
            )
        else:
            with path.open(newline="") as fh:
                rows = list(csv.DictReader(fh))
    except Exception as exc:
        _LOGGER.warning("paper edge filter load failed from %s: %s", path, exc)
        rows = []
    rules = [rule for row in rows if (rule := paper_normalize_edge_rule(row))]
    _PAPER_EDGE_FILTER_CACHE.update(
        {"loaded_at": now, "path": str(path), "mtime": mtime, "rules": rules}
    )
    return list(rules)


def paper_edge_match(
    instrument_id: str,
    source: str,
    side: str,
    daypart: str,
    edge_filter_loader: EdgeFilterLoader | None = None,
) -> dict[str, Any] | None:
    try:
        exact_instrument_id = require_exact_identity_text(instrument_id, field="instrument_id")
    except ValueError:
        return None
    loader = edge_filter_loader or paper_load_edge_filters
    for rule in loader():
        if (
            rule.get("instrument_id") == exact_instrument_id
            and rule.get("source") == str(source or "").replace(" ", "_").lower()
            and rule.get("side") == str(side or "").lower()
            and rule.get("utc_daypart") == str(daypart or "").lower()
        ):
            return dict(rule)
    return None


def paper_edge_context(
    instrument_id: str,
    raw: dict[str, Any],
    side: str,
    edge_filter_loader: EdgeFilterLoader | None = None,
) -> dict[str, Any]:
    exact_instrument_id = require_exact_identity_text(instrument_id, field="instrument_id")
    source = paper_signal_source_key({"payload": raw, **raw})
    daypart = utc_daypart(raw.get("ts") or raw.get("bar_ts") or raw.get("opened_at"))
    rule = (
        None
        if daypart == "unknown"
        else paper_edge_match(
            exact_instrument_id,
            source,
            side,
            daypart,
            edge_filter_loader=edge_filter_loader,
        )
    )
    return {
        "instrument_id": exact_instrument_id,
        "source": source,
        "side": side,
        "utc_daypart": daypart,
        "rule": rule,
        "action": "block"
        if daypart == "unknown"
        else str(rule.get("action") or "")
        if isinstance(rule, dict)
        else "",
    }
