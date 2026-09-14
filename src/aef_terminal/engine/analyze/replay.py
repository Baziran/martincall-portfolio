from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from aef_terminal.domain import Bar
from aef_terminal.engine.analyze.bars import analyze_bars
from aef_terminal.engine.analyze.inputs import (
    admit_replay_confirmed_bars,
    resolve_analysis_as_of_utc,
)


def _candidate_summary(snapshot: dict[str, Any], *, limit: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in snapshot.get("candidates") or []:
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "name": item.get("name"),
                "direction": item.get("direction"),
                "stage": (item.get("status") or {}).get("stage")
                if isinstance(item.get("status"), dict)
                else None,
                "decision_eligible": (item.get("status") or {}).get("decision_eligible")
                if isinstance(item.get("status"), dict)
                else None,
                "score": item.get("score"),
            }
        )
        if len(rows) >= limit:
            break
    return rows


def replay_analysis_timeline(
    bars: Sequence[Bar],
    *,
    instrument: dict[str, Any],
    min_bars: int = 24,
    step: int = 1,
    indicator_params: dict[str, Any] | None = None,
    candidate_limit: int = 3,
    analyzer: Callable[..., dict[str, Any]] = analyze_bars,
) -> list[dict[str, Any]]:
    """Replay confirmed bars into stable decision timeline rows.

    This helper is intentionally offline-only. It uses the bar under analysis as
    the quality clock so golden tests do not drift with wall-clock time.
    """
    confirmed = admit_replay_confirmed_bars(
        bars,
        instrument=instrument,
        field="replay analysis bars",
    )
    if not confirmed:
        return []
    start = max(int(min_bars), 1)
    stride = max(int(step), 1)
    out: list[dict[str, Any]] = []
    for end in range(start, len(confirmed) + 1, stride):
        window = confirmed[:end]
        latest = window[-1]
        snapshot = analyzer(
            window,
            instrument=instrument,
            indicator_params=indicator_params,
            analysis_as_of_utc=resolve_analysis_as_of_utc(window, None),
        )
        decision = snapshot.get("decision") if isinstance(snapshot.get("decision"), dict) else {}
        preview = (
            (snapshot.get("meta") or {}).get("preview")
            if isinstance(snapshot.get("meta"), dict)
            else {}
        )
        out.append(
            {
                "index": end - 1,
                "ts": latest.ts.isoformat(),
                "close": round(float(latest.close), 4),
                "decision": {
                    "action": decision.get("action"),
                    "direction": decision.get("direction"),
                    "kind": decision.get("kind"),
                    "coherent": decision.get("coherent", True),
                },
                "preview_active": bool(preview.get("active"))
                if isinstance(preview, dict)
                else False,
                "candidates": _candidate_summary(snapshot, limit=max(int(candidate_limit), 0)),
            }
        )
    return out
