from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from aef_terminal.engine.common import interval_minutes


def alert_cross_hit(previous: float | None, current: float, level: float, direction: str) -> bool:
    if previous is None:
        return False
    crossed_up = previous < level <= current
    crossed_down = previous > level >= current
    if direction == "above":
        return crossed_up
    if direction == "below":
        return crossed_down
    return crossed_up or crossed_down


def server_alert_freeze_ms(timeframe: str) -> int:
    bar_ms = max(interval_minutes(timeframe), 1) * 60_000
    return max(15 * 60_000, 3 * bar_ms)


def alert_hysteresis_width(level: float, current: float) -> float:
    anchor = max(abs(level), abs(current), 1.0)
    return max(0.01, anchor * 0.00002) * 1.2


def alert_freeze_active(
    item: dict[str, Any],
    *,
    current_price: float,
    level: float,
    now_ms: int,
    key: str = "cooldownUntil",
) -> bool:
    until = item.get(key)
    if isinstance(until, bool) or not isinstance(until, int) or until < 0:
        raise ValueError(f"PRICE_ALERT_FIELD_INVALID: {key}")
    if now_ms >= until:
        return False
    fired_level = item.get("lastFiredLevel")
    if fired_level is None:
        fired_level = level
    if isinstance(fired_level, bool) or not isinstance(fired_level, (int, float)):
        raise ValueError("PRICE_ALERT_FIELD_INVALID: lastFiredLevel")
    return abs(current_price - fired_level) <= alert_hysteresis_width(fired_level, current_price)


def alert_monitor_payload(
    alert: dict,
    current_price: float,
    *,
    trigger_event_at: int | None = None,
) -> dict:
    price = alert["price"]
    kind = alert["kind"]
    direction = alert["direction"]
    if direction not in {"cross", "above", "below"}:
        raise ValueError(f"unsupported price alert direction: {direction or '-'}")
    level_source = dict(alert["level_source"])
    gex_label = str(level_source.get("label") or level_source.get("level_kind") or "").strip()
    title = (
        "EMA 233 touch"
        if kind == "ema233_touch"
        else "VSA fuel candle"
        if kind in {"vsa_fuel"}
        else f"GEX {gex_label}"
        if level_source.get("type") == "gex_snapshot" and gex_label
        else "Price alert"
    )
    return {
        "alert_id": alert["id"],
        "alert_type": "price",
        "instrument_id": alert["instrument_id"],
        "route_fingerprint": alert["route_fingerprint"],
        "symbol": alert["symbol"],
        "timeframe": alert["timeframe"],
        "kind": title,
        "label": alert["label"],
        "level_source": level_source,
        "direction": direction,
        "price": price,
        "current_price": current_price,
        "source": "MartinCall backend",
        "ts": datetime.now(tz=UTC).isoformat(),
        "trigger_event_at": trigger_event_at,
        "message": "fired",
    }
