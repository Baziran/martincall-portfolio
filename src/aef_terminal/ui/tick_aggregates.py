from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from math import isfinite
from time import perf_counter
from typing import Any

from aef_terminal.runtime.timeframes import parse_aware_utc_ts


def _exact_tick_integer(
    row: dict[str, Any],
    field: str,
    *,
    nonnegative: bool = False,
) -> int:
    value = row.get(field)
    if type(value) is not int or (nonnegative and value < 0):
        requirement = "non-negative integer" if nonnegative else "integer"
        raise ValueError(f"tick aggregate {field} must be an exact {requirement}")
    return value


def parse_iso_ts(value: str | None) -> datetime | None:
    return parse_aware_utc_ts(value)


def tick_context_payload(
    delta_rows: list[dict[str, Any]],
    profile_rows: list[dict[str, Any]],
    limit: int = 160,
    *,
    price_step: float | None = None,
    levels: list[dict[str, Any]] | None = None,
    include_telemetry: bool = False,
) -> dict[str, Any]:
    cumulative_delta = 0
    delta: list[dict[str, Any]] = []
    for row in sorted(delta_rows, key=lambda item: str(item.get("ts") or "")):
        total_volume = _exact_tick_integer(row, "total_volume", nonnegative=True)
        net_delta = _exact_tick_integer(row, "net_delta")
        trade_count = _exact_tick_integer(row, "trade_count", nonnegative=True)
        cumulative_delta += net_delta
        delta.append(
            {
                "ts": row.get("ts"),
                "total_volume": total_volume,
                "net_delta": net_delta,
                "trade_count": trade_count,
                "cumulative_delta": cumulative_delta,
                "delta_ratio": round(net_delta / total_volume, 4) if total_volume else 0.0,
            }
        )

    profile_candidates = sorted(
        [row for row in profile_rows if _decimal_price(row.get("price")) is not None],
        key=lambda item: int(item["total_volume"]),
        reverse=True,
    )[: max(1, min(int(limit), 500))]
    profile_max = max((int(row["total_volume"]) for row in profile_candidates), default=0)
    profile: list[dict[str, Any]] = []
    for row in sorted(profile_candidates, key=lambda item: Decimal(str(item.get("price")))):
        decimal_price = _decimal_price(row.get("price"))
        if decimal_price is None:
            continue
        total_volume = _exact_tick_integer(row, "total_volume", nonnegative=True)
        net_delta = _exact_tick_integer(row, "net_delta")
        buy_volume = _exact_tick_integer(row, "buy_volume", nonnegative=True)
        sell_volume = _exact_tick_integer(row, "sell_volume", nonnegative=True)
        trade_count = _exact_tick_integer(row, "trade_count", nonnegative=True)
        profile.append(
            {
                "price": float(decimal_price),
                "total_volume": total_volume,
                "buy_volume": buy_volume,
                "sell_volume": sell_volume,
                "net_delta": net_delta,
                "trade_count": trade_count,
                "normalized": round(total_volume / profile_max, 4) if profile_max else 0.0,
                "delta_ratio": round(net_delta / total_volume, 4) if total_volume else 0.0,
            }
        )

    total_volume = sum(int(row["total_volume"]) for row in delta)
    profile_total_volume = sum(int(row["total_volume"]) for row in profile)
    net_delta = sum(int(row["net_delta"]) for row in delta)
    trade_count = sum(int(row["trade_count"]) for row in delta)
    last_delta_ts = max((str(row.get("ts") or "") for row in delta), default="")
    source_state = "tick" if total_volume > 0 or profile_total_volume > 0 else "empty"
    aggregates_started = perf_counter() if include_telemetry else 0.0
    aggregates = tick_profile_aggregates(delta, profile, price_step=price_step, levels=levels)
    stats = {
        "source": source_state,
        "total_volume": total_volume,
        "profile_total_volume": profile_total_volume,
        "net_delta": net_delta,
        "trade_count": trade_count,
        "profile_levels": len(profile),
        "profile_max_volume": profile_max,
        "delta_ratio": round(net_delta / total_volume, 4) if total_volume else 0.0,
        "last_delta_ts": last_delta_ts,
    }
    if include_telemetry:
        stats["aggregates_compute_ms"] = round((perf_counter() - aggregates_started) * 1000.0, 3)
    return {
        "delta": delta,
        "profile": profile,
        "aggregates": aggregates,
        "stats": stats,
    }


def tick_recent_delta_summaries(delta: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    parsed: list[tuple[datetime, dict[str, Any]]] = []
    for row in delta:
        ts = parse_iso_ts(str(row.get("ts") or ""))
        if ts is not None:
            parsed.append((ts, row))
    if not parsed:
        return {}
    parsed.sort(key=lambda item: item[0])
    last_ts = parsed[-1][0]
    out: dict[str, dict[str, Any]] = {}
    for seconds in (15, 30, 60):
        start_ts = last_ts - timedelta(seconds=seconds)
        rows = [row for ts, row in parsed if start_ts <= ts <= last_ts]
        total = sum(int(row["total_volume"]) for row in rows)
        net = sum(int(row["net_delta"]) for row in rows)
        trades = sum(int(row["trade_count"]) for row in rows)
        if total <= 0 and trades <= 0:
            continue
        out[str(seconds)] = {
            "seconds": seconds,
            "startTs": start_ts.isoformat(),
            "lastTs": last_ts.isoformat(),
            "totalVolume": total,
            "netDelta": net,
            "tradeCount": trades,
            "rowCount": len(rows),
            "deltaRatio": round(net / total, 4) if total else 0.0,
        }
    return out


def tick_profile_value_area(
    profile: list[dict[str, Any]], coverage: float = 0.70
) -> dict[str, Any] | None:
    clean = sorted(
        [
            row
            for row in profile
            if _decimal_price(row.get("price")) is not None and int(row["total_volume"]) > 0
        ],
        key=lambda item: Decimal(str(item.get("price"))),
    )
    if not clean:
        return None
    total = sum(int(row["total_volume"]) for row in clean)
    poc_index = max(range(len(clean)), key=lambda index: int(clean[index]["total_volume"]))
    low_index = poc_index
    high_index = poc_index
    covered = int(clean[poc_index]["total_volume"])
    while covered < total * coverage and (low_index > 0 or high_index < len(clean) - 1):
        lower_indices = list(range(max(0, low_index - 2), low_index))
        upper_indices = list(range(high_index + 1, min(len(clean), high_index + 3)))
        lower = (
            sum(int(clean[index]["total_volume"]) for index in lower_indices)
            if lower_indices
            else -1
        )
        upper = (
            sum(int(clean[index]["total_volume"]) for index in upper_indices)
            if upper_indices
            else -1
        )
        if upper >= lower and upper_indices:
            for index in upper_indices:
                high_index = index
                covered += int(clean[index]["total_volume"])
        elif lower_indices:
            for index in reversed(lower_indices):
                low_index = index
                covered += int(clean[index]["total_volume"])
        else:
            break
    return {
        "poc": clean[poc_index],
        "vah": clean[high_index],
        "val": clean[low_index],
        "coverage": round(covered / total, 4) if total else 0.0,
        "total_volume": total,
    }


def _decimal_price(value: Any) -> Decimal | None:
    try:
        price = Decimal(str(value))
    except InvalidOperation, ValueError:
        return None
    if not price.is_finite():
        return None
    try:
        float_price = float(price)
    except OverflowError, ValueError:
        return None
    return price if isfinite(float_price) else None


def _profile_price_step(rows: list[dict[str, Any]]) -> Decimal:
    prices = sorted(
        price for row in rows if (price := _decimal_price(row.get("price"))) is not None
    )
    positive_steps = [
        prices[index] - prices[index - 1]
        for index in range(1, len(prices))
        if prices[index] > prices[index - 1]
    ]
    return min(positive_steps) if positive_steps else Decimal("0.000001")


def _profile_price_key(value: Any, step: Decimal) -> int | None:
    price = _decimal_price(value)
    if price is None or step <= 0:
        return None
    return int((price / step).to_integral_value(rounding=ROUND_HALF_UP))


def tick_profile_imbalance_groups(profile: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = [
        row
        for row in profile
        if _decimal_price(row.get("price")) is not None and int(row["total_volume"]) > 0
    ]
    if len(rows) < 2:
        return []
    step = _profile_price_step(rows)
    by_key = {
        key: row for row in rows if (key := _profile_price_key(row.get("price"), step)) is not None
    }
    items: list[dict[str, Any]] = []
    for row in rows:
        key = _profile_price_key(row.get("price"), step)
        if key is None:
            continue
        buy = max(int(row["buy_volume"]), 0)
        sell = max(int(row["sell_volume"]), 0)
        lower_sell = max(int(by_key.get(key - 1, {}).get("sell_volume") or 0), 0)
        upper_buy = max(int(by_key.get(key + 1, {}).get("buy_volume") or 0), 0)
        if buy >= max(lower_sell, 1) * 3.0:
            items.append(
                {
                    "side": "buy",
                    "label": "B-IMB",
                    "key": key,
                    "dominant": buy,
                    "passive": lower_sell,
                }
            )
        if sell >= max(upper_buy, 1) * 3.0:
            items.append(
                {
                    "side": "sell",
                    "label": "S-IMB",
                    "key": key,
                    "dominant": sell,
                    "passive": upper_buy,
                }
            )
    items.sort(key=lambda item: (str(item["side"]), int(item["key"])))
    groups: list[dict[str, Any]] = []
    for item in items:
        previous = groups[-1] if groups else None
        if (
            previous
            and previous["side"] == item["side"]
            and int(item["key"]) - int(previous["endKey"]) <= 2
        ):
            previous["endKey"] = item["key"]
            previous["count"] += 1
            previous["dominant"] += int(item["dominant"])
            previous["passive"] += int(item["passive"])
        else:
            groups.append(
                {
                    "side": item["side"],
                    "label": item["label"],
                    "startKey": item["key"],
                    "endKey": item["key"],
                    "count": 1,
                    "dominant": int(item["dominant"]),
                    "passive": int(item["passive"]),
                }
            )
    return [
        {
            **group,
            "low": float(Decimal(min(int(group["startKey"]), int(group["endKey"]))) * step),
            "high": float(Decimal(max(int(group["startKey"]), int(group["endKey"]))) * step),
        }
        for group in groups
    ]


def tick_level_delta_summaries(
    profile: list[dict[str, Any]],
    levels: list[dict[str, Any]] | None,
    *,
    price_step: float | None,
) -> list[dict[str, Any]]:
    if not levels or not profile:
        return []
    requested_step = _decimal_price(price_step)
    step = float(requested_step) if requested_step is not None and requested_step > 0 else 0.000001
    clean_levels: list[dict[str, Any]] = []
    seen: set[int] = set()
    for level in levels[:12]:
        decimal_price = _decimal_price(level.get("price"))
        if decimal_price is None:
            continue
        price = float(decimal_price)
        key = round(price / step)
        if key in seen:
            continue
        seen.add(key)
        name = str(level.get("name") or level.get("kind") or "level")
        clean_levels.append({"name": name, "price": price})
    out: list[dict[str, Any]] = []
    for level in clean_levels:
        near = [
            row
            for row in profile
            if (row_price := _decimal_price(row.get("price"))) is not None
            and abs(float(row_price) - level["price"]) <= step * 2.1
        ]
        volume = sum(int(row["total_volume"]) for row in near)
        delta = sum(int(row["net_delta"]) for row in near)
        if volume <= 0:
            continue
        out.append(
            {
                **level,
                "volume": volume,
                "delta": delta,
                "delta_ratio": round(delta / volume, 4) if volume else 0.0,
                "row_count": len(near),
            }
        )
    out.sort(key=lambda item: abs(float(item.get("delta_ratio") or 0.0)), reverse=True)
    return out[:4]


def tick_profile_aggregates(
    delta: list[dict[str, Any]],
    profile: list[dict[str, Any]],
    *,
    price_step: float | None = None,
    levels: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    value_area = tick_profile_value_area(profile)
    high_volume_prices = [
        float(price)
        for row in sorted(profile, key=lambda item: int(item["total_volume"]), reverse=True)
        if (price := _decimal_price(row.get("price"))) is not None
    ][:3]
    return {
        "recent_delta": tick_recent_delta_summaries(delta),
        "value_area": value_area,
        "poc": value_area.get("poc") if isinstance(value_area, dict) else None,
        "high_volume_prices": high_volume_prices,
        "imbalance_groups": tick_profile_imbalance_groups(profile),
        "level_deltas": tick_level_delta_summaries(profile, levels, price_step=price_step),
    }


def parse_tick_context_levels(raw: str | None) -> list[dict[str, Any]]:
    if not raw:
        return []
    try:
        decoded = json.loads(raw)
    except TypeError, ValueError:
        return []
    if not isinstance(decoded, list):
        return []
    levels: list[dict[str, Any]] = []
    for item in decoded[:12]:
        if not isinstance(item, dict):
            continue
        decimal_price = _decimal_price(item.get("price"))
        if decimal_price is None:
            continue
        price = float(decimal_price)
        levels.append(
            {
                "name": str(item.get("name") or item.get("kind") or "level")[:40],
                "price": price,
            }
        )
    return levels
