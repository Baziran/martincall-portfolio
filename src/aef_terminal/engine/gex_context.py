from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from time import perf_counter
from typing import Any

from aef_terminal.data.gex.contracts import GexCaptureMode, gex_capture_lane
from aef_terminal.data.gex.dynamics import GexDynamicsParams, gex_dynamics
from aef_terminal.domain import Bar


GEX_DYNAMICS_CONTRACT = "gex-dynamics-v1"
GEX_DYNAMICS_VERSION = "3.0-engine"


def build_gex_dynamics_context(
    bars: Sequence[Bar],
    history: Sequence[dict[str, Any]] | None,
    *,
    capture_mode: GexCaptureMode,
) -> dict[str, Any]:
    """Build the engine-owned, non-trading GEX comparison context."""

    _, exact_capture_mode = gex_capture_lane(capture_mode)
    started = perf_counter()
    calculated_at = datetime.now(tz=UTC).isoformat()
    if not bars:
        return {
            "contract": GEX_DYNAMICS_CONTRACT,
            "version": GEX_DYNAMICS_VERSION,
            "latest": None,
            "status": {
                "state": "blocked",
                "reason_code": "no_confirmed_bars",
                "capture_mode": exact_capture_mode,
                "bar_count": 0,
                "history_count": len(history or ()),
                "calculated_at": calculated_at,
                "elapsed_ms": round((perf_counter() - started) * 1000.0, 3),
                "last_error": None,
            },
        }
    try:
        result = gex_dynamics(
            bars,
            history,
            params=GexDynamicsParams(capture_mode=exact_capture_mode),
        )
        latest = result.get("latest")
        if latest is not None and not isinstance(latest, dict):
            raise TypeError("GEX Dynamics latest context must be a typed object or null")
    except Exception as exc:
        return {
            "contract": GEX_DYNAMICS_CONTRACT,
            "version": GEX_DYNAMICS_VERSION,
            "latest": None,
            "status": {
                "state": "error",
                "reason_code": "gex_dynamics_error",
                "capture_mode": exact_capture_mode,
                "bar_count": len(bars),
                "history_count": len(history or ()),
                "calculated_at": calculated_at,
                "elapsed_ms": round((perf_counter() - started) * 1000.0, 3),
                "last_error": str(exc),
            },
        }
    return {
        "contract": GEX_DYNAMICS_CONTRACT,
        "version": GEX_DYNAMICS_VERSION,
        "latest": latest,
        "status": {
            "state": "ready",
            "reason_code": "ok",
            "capture_mode": exact_capture_mode,
            "bar_count": len(bars),
            "history_count": len(history or ()),
            "calculated_at": calculated_at,
            "elapsed_ms": round((perf_counter() - started) * 1000.0, 3),
            "last_error": None,
        },
    }
