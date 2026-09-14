from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal
from math import isfinite
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping, Sequence

from aef_terminal.data.instrument_identity import require_exact_identity_text
from aef_terminal.data.providers import route_instrument
from aef_terminal.domain import Bar
from aef_terminal.engine.analyze import analyze_bars
from aef_terminal.engine.analyze.inputs import (
    admit_replay_confirmed_bars,
    resolve_analysis_as_of_utc,
)
from aef_terminal.engine.execution_intent import trade_plan_projection_from_trade_setup
from aef_terminal.features.time_context import utc_daypart
from aef_terminal.runtime.timeframes import (
    interval_minutes,
    parse_aware_utc_ts,
    timestamp_is_bucket_aligned,
)


@dataclass(frozen=True)
class BacktestTickHistory:
    """One immutable exact-route tick-history input for offline analysis."""

    kind: str
    provider: str
    instrument_id: str
    route_fingerprint: str
    timeframe: str
    rows: tuple[Mapping[str, Any], ...]
    price_step: float | None = None

    def __post_init__(self) -> None:
        if self.kind not in {"bar", "price_profile"}:
            raise ValueError("BACKTEST_TICK_HISTORY_KIND_INVALID")
        for field_name in ("provider", "instrument_id", "route_fingerprint", "timeframe"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value or value != value.strip():
                raise ValueError(f"BACKTEST_TICK_HISTORY_{field_name.upper()}_INVALID")
        require_exact_identity_text(self.instrument_id, field="instrument_id")
        require_exact_identity_text(self.route_fingerprint, field="route_fingerprint")
        if not isinstance(self.rows, tuple) or any(
            not isinstance(row, Mapping) for row in self.rows
        ):
            raise TypeError("BACKTEST_TICK_HISTORY_ROWS_INVALID")
        object.__setattr__(
            self,
            "rows",
            tuple(MappingProxyType(dict(row)) for row in self.rows),
        )
        if self.kind == "bar" and self.price_step is not None:
            raise ValueError("BACKTEST_TICK_BAR_PRICE_STEP_FORBIDDEN")
        if self.kind == "price_profile" and (
            isinstance(self.price_step, bool)
            or not isinstance(self.price_step, int | float)
            or not isfinite(float(self.price_step))
            or float(self.price_step) <= 0
        ):
            raise ValueError("BACKTEST_TICK_PROFILE_PRICE_STEP_INVALID")


def _exact_decimal(value: Any, *, code: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, int | float) or not isfinite(float(value)):
        raise ValueError(code)
    return Decimal(str(value))


def _is_exact_multiple(value: Decimal, step: Decimal) -> bool:
    return value % step == 0


@dataclass(frozen=True)
class BacktestTrade:
    opened_at: str
    closed_at: str | None
    symbol: str
    timeframe: str
    source: str
    setup: str
    side: str
    confidence: float
    entry: float
    stop: float
    target: float
    exit_price: float | None
    exit_reason: str
    pnl_points: float | None
    mfe_points: float
    mae_points: float
    r_multiple: float | None
    hold_bars: int
    utc_daypart: str
    features: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def signal_bar_observed_at(signal_bar: Bar) -> datetime:
    """Return the first deterministic clock at which a confirmed signal bar exists."""

    return resolve_analysis_as_of_utc((signal_bar,), None)


def time_splits(
    bars: Sequence[Bar],
    *,
    instrument: dict[str, Any],
    folds: int = 3,
) -> list[dict[str, Any]]:
    closed = admit_replay_confirmed_bars(
        bars,
        instrument=instrument,
        field="backtest split bars",
    )
    if folds <= 1 or len(closed) < folds * 2:
        return [
            {"fold": 1, "train": None, "test": (closed[0].ts, closed[-1].ts) if closed else None}
        ]
    chunk = max(len(closed) // folds, 1)
    splits: list[dict[str, Any]] = []
    for index in range(folds):
        test_start = index * chunk
        test_end = len(closed) if index == folds - 1 else min((index + 1) * chunk, len(closed))
        if test_start >= test_end:
            continue
        train = (closed[0].ts, closed[test_start - 1].ts) if test_start > 0 else None
        test = (closed[test_start].ts, closed[test_end - 1].ts)
        splits.append({"fold": index + 1, "train": train, "test": test})
    return splits


def _require_tick_ts(value: Any, *, timeframe: str, field: str) -> datetime:
    parsed = parse_aware_utc_ts(value)
    if parsed is None:
        raise ValueError(f"BACKTEST_TICK_TIMESTAMP_INVALID field={field}")
    if not timestamp_is_bucket_aligned(parsed, interval_minutes(timeframe) * 60):
        raise ValueError(
            "BACKTEST_TICK_TIMESTAMP_NOT_ALIGNED "
            f"field={field} timeframe={timeframe} ts={parsed.isoformat()}"
        )
    return parsed


def _admit_tick_history(
    history: BacktestTickHistory | None,
    *,
    instrument: dict[str, Any],
    timeframe: str | None,
    expected_kind: str,
    field: str,
) -> tuple[Mapping[str, Any], ...]:
    if history is None:
        return ()
    if not isinstance(history, BacktestTickHistory):
        raise TypeError(f"{field} must be BacktestTickHistory")
    if history.kind != expected_kind:
        raise ValueError(
            f"BACKTEST_TICK_HISTORY_KIND_MISMATCH field={field} "
            f"expected={expected_kind} actual={history.kind}"
        )
    route = route_instrument(instrument)
    expected_route = (route.provider, route.instrument_id, route.fingerprint)
    actual_route = (history.provider, history.instrument_id, history.route_fingerprint)
    if actual_route != expected_route:
        raise ValueError(
            f"BACKTEST_TICK_HISTORY_ROUTE_MISMATCH field={field} "
            f"expected={expected_route!r} actual={actual_route!r}"
        )
    if timeframe is None or history.timeframe != timeframe:
        raise ValueError(
            f"BACKTEST_TICK_HISTORY_TIMEFRAME_MISMATCH field={field} "
            f"expected={timeframe!r} actual={history.timeframe!r}"
        )
    if history.kind == "price_profile":
        price_increment = route.price_increment
        if price_increment is None:
            raise ValueError("BACKTEST_TICK_PROFILE_ROUTE_INCREMENT_REQUIRED")
        increment = _exact_decimal(
            price_increment,
            code="BACKTEST_TICK_PROFILE_ROUTE_INCREMENT_INVALID",
        )
        price_step = _exact_decimal(
            history.price_step,
            code="BACKTEST_TICK_PROFILE_PRICE_STEP_INVALID",
        )
        if increment <= 0 or price_step <= 0 or not _is_exact_multiple(price_step, increment):
            raise ValueError("BACKTEST_TICK_PROFILE_PRICE_STEP_NOT_ROUTE_ALIGNED")
    return history.rows


def _tick_history_by_ts(
    tick_history: Sequence[Mapping[str, Any]],
    *,
    timeframe: str,
) -> dict[datetime, Mapping[str, Any]]:
    rows: dict[datetime, Mapping[str, Any]] = {}
    for index, row in enumerate(tick_history):
        ts = _require_tick_ts(row.get("ts"), timeframe=timeframe, field=f"bar[{index}].ts")
        if ts in rows:
            raise ValueError(f"BACKTEST_TICK_HISTORY_DUPLICATE_TIMESTAMP ts={ts.isoformat()}")
        total_volume = row.get("total_volume")
        net_delta = row.get("net_delta")
        trade_count = row.get("trade_count")
        if (
            type(total_volume) is not int
            or type(net_delta) is not int
            or type(trade_count) is not int
            or total_volume < 0
            or trade_count < 0
            or abs(net_delta) > total_volume
        ):
            raise ValueError(f"BACKTEST_TICK_BAR_ROW_INVALID index={index}")
        rows[ts] = row
    return rows


def _tick_profile_history_by_ts(
    tick_profile_history: Sequence[Mapping[str, Any]],
    *,
    timeframe: str,
    price_step: float | None,
) -> dict[datetime, list[Mapping[str, Any]]]:
    if not tick_profile_history:
        return {}
    exact_price_step = _exact_decimal(
        price_step,
        code="BACKTEST_TICK_PROFILE_PRICE_STEP_INVALID",
    )
    rows: dict[datetime, list[Mapping[str, Any]]] = {}
    admitted: list[tuple[datetime, Decimal, Mapping[str, Any]]] = []
    seen: set[tuple[datetime, Decimal]] = set()
    for index, row in enumerate(tick_profile_history):
        ts = _require_tick_ts(row.get("ts"), timeframe=timeframe, field=f"profile[{index}].ts")
        raw_price = row.get("price")
        total_volume = row.get("total_volume")
        net_delta = row.get("net_delta")
        trade_count = row.get("trade_count")
        if (
            isinstance(raw_price, bool)
            or not isinstance(raw_price, int | float)
            or not isfinite(float(raw_price))
            or type(total_volume) is not int
            or type(net_delta) is not int
            or type(trade_count) is not int
            or total_volume < 0
            or trade_count < 0
            or abs(net_delta) > total_volume
        ):
            raise ValueError(f"BACKTEST_TICK_PROFILE_ROW_INVALID index={index}")
        price = _exact_decimal(raw_price, code=f"BACKTEST_TICK_PROFILE_ROW_INVALID index={index}")
        if not _is_exact_multiple(price, exact_price_step):
            raise ValueError(f"BACKTEST_TICK_PROFILE_PRICE_NOT_STEP_ALIGNED index={index}")
        identity = (ts, price)
        if identity in seen:
            raise ValueError(
                f"BACKTEST_TICK_PROFILE_DUPLICATE_LEVEL ts={ts.isoformat()} price={price}"
            )
        seen.add(identity)
        admitted.append((ts, price, row))
    for ts, _price, row in sorted(admitted, key=lambda item: (item[0], item[1])):
        rows.setdefault(ts, []).append(row)
    return rows


def _tick_features_for_bar(
    tick_rows: Mapping[datetime, Mapping[str, Any]], bar: Bar
) -> dict[str, Any]:
    row = tick_rows.get(bar.ts)
    if not row:
        return {}
    total_volume = int(row["total_volume"])
    net_delta = int(row["net_delta"])
    trade_count = int(row["trade_count"])
    delta_ratio = net_delta / total_volume if total_volume else 0.0
    return {
        "tick_total_volume": total_volume,
        "tick_net_delta": net_delta,
        "tick_trade_count": trade_count,
        "tick_delta_ratio": round(delta_ratio, 6),
        "tick_history": True,
    }


def _tick_profile_features_for_bar(
    tick_profile_rows: Mapping[datetime, Sequence[Mapping[str, Any]]], bar: Bar
) -> dict[str, Any]:
    rows = tick_profile_rows.get(bar.ts)
    if not rows:
        return {}
    total_volume = 0
    net_delta = 0
    trade_count = 0
    parsed_rows: list[dict[str, Any]] = []
    for row in rows:
        price = float(row["price"])
        volume = int(row["total_volume"])
        delta = int(row["net_delta"])
        trades = int(row["trade_count"])
        total_volume += volume
        net_delta += delta
        trade_count += trades
        parsed_rows.append(
            {"price": price, "total_volume": volume, "net_delta": delta, "trade_count": trades}
        )
    if total_volume == 0:
        return {}
    poc = min(parsed_rows, key=lambda item: (-item["total_volume"], item["price"]))
    delta_ratio = net_delta / total_volume
    return {
        "tick_profile_history": True,
        "tick_profile_levels": len(parsed_rows),
        "tick_profile_total_volume": total_volume,
        "tick_profile_net_delta": net_delta,
        "tick_profile_trade_count": trade_count,
        "tick_profile_delta_ratio": round(delta_ratio, 6),
        "tick_profile_poc": round(float(poc["price"]), 6),
        "tick_profile_poc_volume": int(poc["total_volume"]),
        "tick_profile_poc_delta": int(poc["net_delta"]),
    }


def _normalized_source_allowlist(sources: Iterable[str] | None) -> set[str] | None:
    if not sources:
        return None
    allowed = {str(item).strip().lower() for item in sources if str(item).strip()}
    return allowed or None


def _normalized_edge_blocks(
    values: Iterable[Iterable[str]] | None,
) -> set[tuple[str, str, str]]:
    blocks: set[tuple[str, str, str]] = set()
    for item in values or ():
        if not item:
            continue
        parts = [str(part or "").strip().lower() for part in item]
        if len(parts) < 3:
            continue
        blocks.add((parts[0], parts[1], parts[2]))
    return blocks


def _edge_blocked(plan: dict[str, Any], signal_bar: Bar, blocks: set[tuple[str, str, str]]) -> bool:
    if not blocks:
        return False
    key = (
        str(plan.get("source") or "").strip().lower(),
        str(plan.get("side") or "").strip().lower(),
        utc_daypart(signal_bar.ts),
    )
    return key in blocks


def _normalized_source_set(values: Iterable[str]) -> set[str]:
    return {
        str(value or "").replace(" ", "_").strip().lower()
        for value in values
        if str(value or "").strip()
    }


def _inverted_trade_plan(plan: dict[str, Any]) -> dict[str, Any]:
    side = str(plan.get("side") or "")
    return {
        **plan,
        "side": "short" if side == "long" else "long" if side == "short" else side,
        "stop": float(plan["target"]),
        "target": float(plan["stop"]),
        "inverted": True,
    }


def _widened_trade_plan(
    plan: dict[str, Any],
    *,
    stop_multiplier: float = 1.0,
    target_mode: str = "keep",
) -> dict[str, Any]:
    multiplier = float(stop_multiplier)
    if multiplier <= 0:
        raise ValueError("stop_multiplier must be positive")
    mode = str(target_mode or "keep").strip().lower()
    if mode not in {"keep", "preserve-rr"}:
        raise ValueError("target_mode must be 'keep' or 'preserve-rr'")
    if multiplier == 1.0:
        return plan

    side = str(plan.get("side") or "")
    entry = float(plan["entry"])
    stop = float(plan["stop"])
    target = float(plan["target"])
    risk = abs(entry - stop)
    reward = abs(target - entry)
    if risk <= 0 or reward <= 0:
        return plan

    if side == "long":
        widened_stop = entry - risk * multiplier
        widened_target = target if mode == "keep" else entry + reward * multiplier
    elif side == "short":
        widened_stop = entry + risk * multiplier
        widened_target = target if mode == "keep" else entry - reward * multiplier
    else:
        return plan

    return {
        **plan,
        "stop": widened_stop,
        "target": widened_target,
        "stop_multiplier": multiplier,
        "target_mode": mode,
    }


def _trade_plan_from_snapshot(
    snapshot: dict[str, Any],
    invert_sources: set[str] | None = None,
    *,
    stop_multiplier: float = 1.0,
    target_mode: str = "keep",
) -> dict[str, Any] | None:
    setup = snapshot.get("trade_setup") if isinstance(snapshot.get("trade_setup"), dict) else {}
    projection = trade_plan_projection_from_trade_setup(setup)
    if projection is None:
        return None
    trade_plan = projection.as_backtest_plan()
    if trade_plan["source"] in (invert_sources or set()):
        trade_plan = _inverted_trade_plan(trade_plan)
    trade_plan = _widened_trade_plan(
        trade_plan,
        stop_multiplier=stop_multiplier,
        target_mode=target_mode,
    )
    return trade_plan


def _entry_hit(side: str, bar: Bar, entry: float) -> bool:
    if side == "long":
        return bar.low <= entry <= bar.high
    return bar.low <= entry <= bar.high


def _prepare_fast_entry_bars(
    bars: Sequence[Bar] | None,
    *,
    parent_bars: Sequence[Bar],
    instrument: dict[str, Any],
) -> tuple[list[Bar], str | None]:
    if not bars:
        return [], None
    if not parent_bars:
        raise ValueError("fast_entry_bars require parent bars")
    prepared = admit_replay_confirmed_bars(
        bars,
        instrument=instrument,
        field="backtest fast entry bars",
    )
    if not prepared:
        raise ValueError("fast_entry_bars require confirmed bars")
    timeframe = prepared[0].timeframe
    if any(bar.timeframe != timeframe for bar in prepared):
        raise ValueError("fast_entry_bars must use one timeframe")
    if interval_minutes(timeframe) >= interval_minutes(parent_bars[0].timeframe):
        raise ValueError("fast_entry_bars must be lower timeframe than parent bars")
    return prepared, timeframe


def _fast_execution_window(
    fast_entry_bars: Sequence[Bar],
    *,
    signal_bar: Bar,
    parent_future_bars: Sequence[Bar],
) -> Sequence[Bar]:
    if not fast_entry_bars or not parent_future_bars:
        return parent_future_bars
    starts_at = signal_bar_observed_at(signal_bar)
    ends_at = signal_bar_observed_at(parent_future_bars[-1])
    return [
        bar
        for bar in fast_entry_bars
        if bar.ts >= starts_at and signal_bar_observed_at(bar) <= ends_at
    ]


def _simulate_trade(
    plan: dict[str, Any],
    signal_bar: Bar,
    future_bars: Sequence[Bar],
    *,
    features: dict[str, Any] | None = None,
) -> BacktestTrade | None:
    side = str(plan["side"])
    entry = float(plan["entry"])
    stop = float(plan["stop"])
    target = float(plan["target"])
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    in_position = False
    opened_at = signal_bar.ts
    closed_at: datetime | None = None
    exit_price: float | None = None
    exit_reason = "timeout"
    mfe = 0.0
    mae = 0.0
    hold_bars = 0
    for bar in future_bars:
        if not in_position:
            if not _entry_hit(side, bar, entry):
                continue
            in_position = True
            opened_at = bar.ts
        hold_bars += 1
        if side == "long":
            mfe = max(mfe, float(bar.high) - entry)
            mae = min(mae, float(bar.low) - entry)
            stop_hit = float(bar.low) <= stop
            target_hit = float(bar.high) >= target
        else:
            mfe = max(mfe, entry - float(bar.low))
            mae = min(mae, entry - float(bar.high))
            stop_hit = float(bar.high) >= stop
            target_hit = float(bar.low) <= target
        if stop_hit:
            exit_price = stop
            exit_reason = "stop"
            closed_at = bar.ts
            break
        if target_hit:
            exit_price = target
            exit_reason = "target"
            closed_at = bar.ts
            break
    if not in_position:
        return None
    if exit_price is None and future_bars:
        last = future_bars[-1]
        exit_price = float(last.close)
        closed_at = last.ts
    pnl = None
    r_multiple = None
    if exit_price is not None:
        pnl = exit_price - entry if side == "long" else entry - exit_price
        r_multiple = pnl / risk
    return BacktestTrade(
        opened_at=opened_at.isoformat(),
        closed_at=closed_at.isoformat() if closed_at else None,
        symbol=signal_bar.symbol,
        timeframe=signal_bar.timeframe,
        source=str(plan.get("source") or "trade_setup"),
        setup=str(plan.get("setup") or ""),
        side=side,
        confidence=round(float(plan["confidence"]), 2),
        entry=entry,
        stop=stop,
        target=target,
        exit_price=exit_price,
        exit_reason=exit_reason,
        pnl_points=round(pnl, 4) if pnl is not None else None,
        mfe_points=round(mfe, 4),
        mae_points=round(mae, 4),
        r_multiple=round(r_multiple, 4) if r_multiple is not None else None,
        hold_bars=hold_bars,
        utc_daypart=utc_daypart(opened_at),
        features=dict(features or {}),
    )


def run_backtest(
    bars: Sequence[Bar],
    *,
    instrument: dict[str, Any],
    display_symbol: str | None = None,
    warmup: int = 160,
    horizon: int = 80,
    step: int = 1,
    max_signals: int | None = None,
    indicator_params: dict[str, Any] | None = None,
    invert_sources: Iterable[str] = (),
    edge_blocks: Iterable[Iterable[str]] | None = None,
    stop_multiplier: float = 1.0,
    target_mode: str = "keep",
    analysis_lookback: int | None = None,
    tick_history: BacktestTickHistory | None = None,
    tick_profile_history: BacktestTickHistory | None = None,
    fast_entry_bars: Sequence[Bar] | None = None,
    include_sources: Iterable[str] | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    progress_interval: int = 250,
) -> dict[str, Any]:
    closed = admit_replay_confirmed_bars(
        bars,
        instrument=instrument,
        field="backtest analysis bars",
    )
    prepared_fast_entry_bars, execution_timeframe = _prepare_fast_entry_bars(
        fast_entry_bars,
        parent_bars=closed,
        instrument=instrument,
    )
    analysis_timeframe = closed[0].timeframe if closed else None
    tick_rows = _tick_history_by_ts(
        _admit_tick_history(
            tick_history,
            instrument=instrument,
            timeframe=analysis_timeframe,
            expected_kind="bar",
            field="tick_history",
        ),
        timeframe=analysis_timeframe or "",
    )
    tick_profile_rows = _tick_profile_history_by_ts(
        _admit_tick_history(
            tick_profile_history,
            instrument=instrument,
            timeframe=analysis_timeframe,
            expected_kind="price_profile",
            field="tick_profile_history",
        ),
        timeframe=analysis_timeframe or "",
        price_step=tick_profile_history.price_step if tick_profile_history is not None else None,
    )
    trades: list[BacktestTrade] = []
    seen: set[tuple[str, str, str]] = set()
    invert_source_set = _normalized_source_set(invert_sources)
    edge_block_set = _normalized_edge_blocks(edge_blocks)
    include_source_set = _normalized_source_allowlist(include_sources)
    last_index = max(0, len(closed) - horizon - 1)
    total_steps = max(0, ((last_index - max(warmup, 2)) // max(int(step), 1)) + 1)
    for step_number, index in enumerate(
        range(max(warmup, 2), last_index + 1, max(int(step), 1)), start=1
    ):
        window_start = max(0, index + 1 - int(analysis_lookback)) if analysis_lookback else 0
        window = closed[window_start : index + 1]
        signal_bar = closed[index]
        snapshot = _analyze_backtest_window(
            window,
            signal_bar=signal_bar,
            display_symbol=display_symbol,
            indicator_params=indicator_params,
            instrument=instrument,
        )
        plan = _trade_plan_from_snapshot(
            snapshot,
            invert_source_set,
            stop_multiplier=stop_multiplier,
            target_mode=target_mode,
        )
        if plan is None:
            continue
        if _edge_blocked(plan, signal_bar, edge_block_set):
            continue
        if (
            include_source_set
            and str(plan.get("source") or "").strip().lower() not in include_source_set
        ):
            continue
        key = (str(plan["setup"]), str(plan["side"]), signal_bar.ts.isoformat())
        if key in seen:
            continue
        seen.add(key)
        tick_features = _tick_features_for_bar(tick_rows, signal_bar)
        tick_profile_features = _tick_profile_features_for_bar(tick_profile_rows, signal_bar)
        parent_future_bars = closed[index + 1 : index + 1 + horizon]
        execution_bars = _fast_execution_window(
            prepared_fast_entry_bars,
            signal_bar=signal_bar,
            parent_future_bars=parent_future_bars,
        )
        trade = _simulate_trade(
            plan,
            signal_bar,
            execution_bars,
            features={
                **tick_features,
                **tick_profile_features,
                "risk_points": round(abs(float(plan["entry"]) - float(plan["stop"])), 6),
                "reward_points": round(abs(float(plan["target"]) - float(plan["entry"])), 6),
                "planned_rr": round(
                    abs(float(plan["target"]) - float(plan["entry"]))
                    / max(abs(float(plan["entry"]) - float(plan["stop"])), 1e-9),
                    6,
                ),
                "inverted": bool(plan.get("inverted")),
                "stop_multiplier": float(plan.get("stop_multiplier") or stop_multiplier),
                "target_mode": str(plan.get("target_mode") or target_mode),
                "execution_timeframe": execution_timeframe or signal_bar.timeframe,
            },
        )
        if trade is not None:
            trades.append(trade)
        if max_signals is not None and len(trades) >= max_signals:
            break
        if progress_callback and (
            step_number == 1
            or step_number == total_steps
            or step_number % max(int(progress_interval), 1) == 0
        ):
            progress_callback(
                {
                    "index": index,
                    "step_number": step_number,
                    "total_steps": total_steps,
                    "ts": signal_bar_observed_at(signal_bar).isoformat(),
                    "trades": len(trades),
                }
            )
    return summarize_trades(trades, execution_timeframe=execution_timeframe)


def _variant_feature_payload(
    plan: dict[str, Any], trade_features: dict[str, Any], target_mode: str
) -> dict[str, Any]:
    return {
        **trade_features,
        "risk_points": round(abs(float(plan["entry"]) - float(plan["stop"])), 6),
        "reward_points": round(abs(float(plan["target"]) - float(plan["entry"])), 6),
        "planned_rr": round(
            abs(float(plan["target"]) - float(plan["entry"]))
            / max(abs(float(plan["entry"]) - float(plan["stop"])), 1e-9),
            6,
        ),
        "inverted": bool(plan.get("inverted")),
        "stop_multiplier": float(plan.get("stop_multiplier") or 1.0),
        "target_mode": str(plan.get("target_mode") or target_mode),
    }


def _analyze_backtest_window(
    window: Sequence[Bar],
    *,
    signal_bar: Bar,
    display_symbol: str | None,
    indicator_params: dict[str, Any] | None,
    instrument: dict[str, Any],
) -> dict[str, Any]:
    try:
        return analyze_bars(
            window,
            display_symbol=display_symbol or signal_bar.symbol,
            indicator_params=indicator_params,
            analysis_as_of_utc=signal_bar_observed_at(signal_bar),
            instrument=instrument,
        )
    except Exception as exc:
        raise RuntimeError(
            "BACKTEST_ANALYSIS_FAILED "
            f"signal_ts={signal_bar.ts.isoformat()} "
            f"symbol={signal_bar.symbol} timeframe={signal_bar.timeframe}"
        ) from exc


def run_backtest_variants(
    bars: Sequence[Bar],
    variants: Sequence[dict[str, Any]],
    *,
    instrument: dict[str, Any],
    display_symbol: str | None = None,
    warmup: int = 160,
    horizon: int = 80,
    step: int = 1,
    max_signals: int | None = None,
    indicator_params: dict[str, Any] | None = None,
    analysis_lookback: int | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    progress_interval: int = 250,
    tick_history: BacktestTickHistory | None = None,
    tick_profile_history: BacktestTickHistory | None = None,
    fast_entry_bars: Sequence[Bar] | None = None,
) -> dict[str, dict[str, Any]]:
    closed = admit_replay_confirmed_bars(
        bars,
        instrument=instrument,
        field="backtest analysis bars",
    )
    prepared_fast_entry_bars, execution_timeframe = _prepare_fast_entry_bars(
        fast_entry_bars,
        parent_bars=closed,
        instrument=instrument,
    )
    analysis_timeframe = closed[0].timeframe if closed else None
    tick_rows = _tick_history_by_ts(
        _admit_tick_history(
            tick_history,
            instrument=instrument,
            timeframe=analysis_timeframe,
            expected_kind="bar",
            field="tick_history",
        ),
        timeframe=analysis_timeframe or "",
    )
    tick_profile_rows = _tick_profile_history_by_ts(
        _admit_tick_history(
            tick_profile_history,
            instrument=instrument,
            timeframe=analysis_timeframe,
            expected_kind="price_profile",
            field="tick_profile_history",
        ),
        timeframe=analysis_timeframe or "",
        price_step=tick_profile_history.price_step if tick_profile_history is not None else None,
    )
    last_index = max(0, len(closed) - horizon - 1)
    states: dict[str, dict[str, Any]] = {}
    for variant in variants:
        variant_id = str(variant.get("variant_id") or "")
        if not variant_id:
            continue
        states[variant_id] = {
            "variant": variant,
            "invert_sources": _normalized_source_set(variant.get("invert_sources") or ()),
            "trades": [],
            "seen": set(),
        }
    total_steps = max(0, ((last_index - max(warmup, 2)) // max(int(step), 1)) + 1)
    for step_number, index in enumerate(
        range(max(warmup, 2), last_index + 1, max(int(step), 1)), start=1
    ):
        active_states = [
            state
            for state in states.values()
            if max_signals is None or len(state["trades"]) < max_signals
        ]
        if not active_states:
            break
        window_start = max(0, index + 1 - int(analysis_lookback)) if analysis_lookback else 0
        window = closed[window_start : index + 1]
        signal_bar = closed[index]
        snapshot = _analyze_backtest_window(
            window,
            signal_bar=signal_bar,
            display_symbol=display_symbol,
            indicator_params=indicator_params,
            instrument=instrument,
        )
        for state in active_states:
            variant = state["variant"]
            plan = _trade_plan_from_snapshot(
                snapshot,
                state["invert_sources"],
                stop_multiplier=float(variant.get("stop_multiplier") or 1.0),
                target_mode=str(variant.get("target_mode") or "keep"),
            )
            if plan is None:
                continue
            key = (str(plan["setup"]), str(plan["side"]), signal_bar.ts.isoformat())
            if key in state["seen"]:
                continue
            state["seen"].add(key)
            tick_features = _tick_features_for_bar(tick_rows, signal_bar)
            tick_profile_features = _tick_profile_features_for_bar(tick_profile_rows, signal_bar)
            parent_future_bars = closed[index + 1 : index + 1 + horizon]
            execution_bars = _fast_execution_window(
                prepared_fast_entry_bars,
                signal_bar=signal_bar,
                parent_future_bars=parent_future_bars,
            )
            trade = _simulate_trade(
                plan,
                signal_bar,
                execution_bars,
                features=_variant_feature_payload(
                    plan,
                    {
                        **tick_features,
                        **tick_profile_features,
                        "execution_timeframe": execution_timeframe or signal_bar.timeframe,
                    },
                    str(variant.get("target_mode") or "keep"),
                ),
            )
            if trade is not None:
                state["trades"].append(trade)
        if progress_callback and (
            step_number == 1 or step_number % max(int(progress_interval), 1) == 0
        ):
            progress_callback(
                {
                    "index": index,
                    "step_number": step_number,
                    "total_steps": total_steps,
                    "ts": signal_bar_observed_at(signal_bar).isoformat(),
                    "trades_by_variant": {
                        variant_id: len(state["trades"]) for variant_id, state in states.items()
                    },
                }
            )
    return {
        variant_id: summarize_trades(
            state["trades"],
            execution_timeframe=execution_timeframe,
        )
        for variant_id, state in states.items()
    }


def _bucket_summary(items: Sequence[BacktestTrade]) -> dict[str, Any]:
    closed = [item for item in items if item.pnl_points is not None]
    pnl = [float(item.pnl_points) for item in closed if item.pnl_points is not None]
    r_values = [float(item.r_multiple) for item in closed if item.r_multiple is not None]
    wins = [value for value in pnl if value > 0]
    losses = [value for value in pnl if value < 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    return {
        "trades": len(items),
        "closed": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(closed), 4) if closed else None,
        "net_points": round(sum(pnl), 4),
        "avg_points": round(sum(pnl) / len(closed), 4) if closed else None,
        "expectancy_r": round(sum(r_values) / len(r_values), 4) if r_values else None,
        "profit_factor": round(gross_win / gross_loss, 4) if gross_loss > 0 else None,
    }


def _grouped(trades: Iterable[BacktestTrade], key: str) -> list[dict[str, Any]]:
    groups: dict[str, list[BacktestTrade]] = defaultdict(list)
    for trade in trades:
        groups[str(getattr(trade, key) or "unknown")].append(trade)
    rows = [{"key": name, **_bucket_summary(items)} for name, items in groups.items()]
    return sorted(
        rows, key=lambda row: (int(row["closed"]), float(row["net_points"])), reverse=True
    )


def summarize_trades(
    trades: Sequence[BacktestTrade],
    *,
    execution_timeframe: str | None = None,
) -> dict[str, Any]:
    model = {
        "kind": "independent_signal_outcomes",
        "entry": (
            "next_confirmed_lower_timeframe_bar_touch"
            if execution_timeframe
            else "next_confirmed_bar_touch"
        ),
        "same_bar_priority": "stop_before_target",
        "timeout": "horizon_last_close",
        "costs": "excluded",
    }
    if execution_timeframe:
        model["execution_timeframe"] = execution_timeframe
    return {
        "model": model,
        "summary": _bucket_summary(trades),
        "by_utc_daypart": _grouped(trades, "utc_daypart"),
        "by_source": _grouped(trades, "source"),
        "by_setup": _grouped(trades, "setup"),
        "by_confidence": _confidence_buckets(trades),
        "trades": [trade.as_dict() for trade in trades],
    }


def _confidence_buckets(trades: Sequence[BacktestTrade]) -> list[dict[str, Any]]:
    groups: dict[str, list[BacktestTrade]] = defaultdict(list)
    for trade in trades:
        confidence = float(trade.confidence)
        if confidence >= 85:
            key = "85+"
        elif confidence >= 70:
            key = "70-84"
        elif confidence >= 55:
            key = "55-69"
        else:
            key = "<55"
        groups[key].append(trade)
    return [{"key": key, **_bucket_summary(items)} for key, items in sorted(groups.items())]
