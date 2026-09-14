from __future__ import annotations

from typing import Any

from aef_terminal.config import AppConfig

_APP_CFG = AppConfig()
QUOTE_STREAM_SECONDS = max(float(_APP_CFG.quote_stream_seconds), 0.05)
QUOTE_SNAPSHOT_SECONDS = max(float(_APP_CFG.quote_snapshot_seconds), 1.0)
QUOTE_STREAM_CLEANUP_GRACE_SECONDS = max(float(_APP_CFG.quote_stream_cleanup_grace_seconds), 0.1)
WS_HEARTBEAT_SECONDS = max(float(_APP_CFG.ws_heartbeat_seconds), 5.0)
HOST_SLEEP_CHECK_SECONDS = max(float(_APP_CFG.host_sleep_check_seconds), 5.0)
HOST_SLEEP_GAP_SECONDS = max(float(_APP_CFG.host_sleep_gap_seconds), 15.0)
CHART_STREAM_RECOVERY_TAIL = max(int(_APP_CFG.chart_stream_recovery_tail), 24)
CHART_STREAM_MAX_RECOVERY_TAIL = max(
    int(_APP_CFG.chart_stream_max_recovery_tail),
    CHART_STREAM_RECOVERY_TAIL,
)
CHART_STREAM_POLL_SECONDS = max(float(_APP_CFG.chart_stream_poll_seconds), 0.5)


def load_runtime_constants(cfg: AppConfig | None = None) -> dict[str, Any]:
    source = cfg or AppConfig()
    recovery_tail = max(int(source.chart_stream_recovery_tail), 24)
    return {
        "QUOTE_STREAM_SECONDS": max(float(source.quote_stream_seconds), 0.05),
        "QUOTE_SNAPSHOT_SECONDS": max(float(source.quote_snapshot_seconds), 1.0),
        "QUOTE_STREAM_CLEANUP_GRACE_SECONDS": max(
            float(source.quote_stream_cleanup_grace_seconds), 0.1
        ),
        "WS_HEARTBEAT_SECONDS": max(float(source.ws_heartbeat_seconds), 5.0),
        "HOST_SLEEP_CHECK_SECONDS": max(float(source.host_sleep_check_seconds), 5.0),
        "HOST_SLEEP_GAP_SECONDS": max(float(source.host_sleep_gap_seconds), 15.0),
        "CHART_STREAM_RECOVERY_TAIL": recovery_tail,
        "CHART_STREAM_MAX_RECOVERY_TAIL": max(
            int(source.chart_stream_max_recovery_tail), recovery_tail
        ),
        "CHART_STREAM_POLL_SECONDS": max(float(source.chart_stream_poll_seconds), 0.5),
    }
