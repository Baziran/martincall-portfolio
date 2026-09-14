from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from aef_terminal.domain import Bar, Direction
from aef_terminal.runtime.math_utils import float_or_none, round_optional
from aef_terminal.runtime.signal_state import SignalState


def _merge_fact_fields(
    payload: dict[str, Any], fact_fields: Mapping[str, Any] | None
) -> dict[str, Any]:
    if isinstance(fact_fields, Mapping):
        payload.update({str(key): value for key, value in fact_fields.items()})
    return payload


def _parse_roll_ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if value is None:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def projected_end(bar: Bar, bars_forward: int) -> dict[str, Any]:
    """Describe a visual endpoint on the canonical trading-slot axis."""
    if not isinstance(bars_forward, int) or isinstance(bars_forward, bool) or bars_forward < 1:
        raise ValueError("projected overlay endpoint requires a positive bar offset")
    return {
        "end_anchor_ts": bar.ts.isoformat(),
        "end_bar_offset": bars_forward,
    }


def compact_lines(value: str | Sequence[Any], max_len: int = 10) -> list[str]:
    raw = list(value) if isinstance(value, (list, tuple)) else str(value).replace("_", " ").split()
    lines: list[str] = []
    for item in raw:
        text = str(item).strip()
        if not text:
            continue
        if len(text) <= max_len:
            lines.append(text)
            continue
        for index in range(0, len(text), max_len):
            lines.append(text[index : index + max_len])
    return lines[:4] or ["SIG"]


def _normalize_table_cell(cell: Any) -> Any:
    if isinstance(cell, Mapping):
        normalized = {str(key): value for key, value in cell.items()}
        for key in ("text", "label", "prefix", "suffix", "empty", "kind", "separator"):
            if key in normalized and normalized[key] is not None:
                normalized[key] = str(normalized[key])
        if (
            "parts" in normalized
            and isinstance(normalized["parts"], Sequence)
            and not isinstance(normalized["parts"], (str, bytes))
        ):
            normalized["parts"] = [_normalize_table_cell(part) for part in normalized["parts"]]
        return normalized
    return str(cell)


def table(
    *,
    table_id: str,
    title: str = "",
    columns: Sequence[Sequence[Any]] = (),
    model_ref: str | None = None,
    model: Mapping[str, Any] | None = None,
    accent: str | None = None,
    direction: Direction | str | None = None,
    bg: str | None = None,
    text: str | None = None,
    tone: str | None = None,
    role: str | None = None,
    fact_fields: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_columns: list[Any] = []
    for column in columns:
        if isinstance(column, dict):
            cells = column.get("cells", [])
            normalized = dict(column)
            normalized["cells"] = [_normalize_table_cell(cell) for cell in list(cells)[:3]]
            normalized_columns.append(normalized)
        else:
            normalized_columns.append([_normalize_table_cell(cell) for cell in list(column)[:3]])
    if not normalized_columns and not model_ref:
        raise ValueError("overlay table requires columns or a renderer-owned model")
    body: dict[str, Any] = {}
    if title:
        body["title"] = title
    if normalized_columns:
        body["columns"] = normalized_columns
    if model_ref:
        body["model_ref"] = str(model_ref)
    if isinstance(model, Mapping):
        body["model"] = {str(key): value for key, value in model.items()}
    if accent:
        body["accent"] = accent
    if direction:
        body["direction"] = direction.value if isinstance(direction, Direction) else str(direction)
    if bg:
        body["bg"] = bg
    if text:
        body["text"] = text
    if tone:
        body["tone"] = tone
    payload: dict[str, Any] = {"type": "table", "id": table_id, "table": body}
    if role:
        payload["role"] = role
    return _merge_fact_fields(payload, fact_fields)


def label(
    *,
    bar: Bar,
    price: float,
    lines: str | Sequence[Any] | None = None,
    direction: Direction | str = Direction.FLAT,
    bg: str | None = None,
    text: str | None = None,
    tone: str | None = None,
    side: str | None = None,
    opacity: float | None = 1.0,
    deletable: bool = False,
    interactive: bool = True,
    anchor: str | None = None,
    role: str | None = None,
    fact_fields: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if isinstance(direction, Direction):
        dir_value = direction
    else:
        dir_value = (
            Direction.LONG
            if direction == "long"
            else Direction.SHORT
            if direction == "short"
            else Direction.FLAT
        )
    label_side = side or (
        "below"
        if dir_value == Direction.LONG
        else "above"
        if dir_value == Direction.SHORT
        else "center"
    )
    payload = {
        "type": "label",
        "ts": bar.ts.isoformat(),
        "price": round_optional(price),
        "side": label_side,
        "anchor": anchor or label_side,
        "tone": tone
        or (
            "positive"
            if dir_value == Direction.LONG
            else "negative"
            if dir_value == Direction.SHORT
            else "warning"
        ),
        "opacity": opacity,
        "deletable": deletable,
        "interactive": interactive,
        "delete_icon": "left" if deletable else None,
        "direction": dir_value.value,
    }
    if lines:
        payload["lines"] = compact_lines(lines)
    if bg:
        payload["bg"] = bg
    if text:
        payload["text"] = text
    if role:
        payload["role"] = role
    return _merge_fact_fields(payload, fact_fields)


def box(
    *,
    start_ts: str,
    top: float,
    bottom: float,
    end_ts: str | None = None,
    end_anchor_ts: str | None = None,
    end_bar_offset: int | None = None,
    label_text: str | None = None,
    tone: str = "info",
    opacity: float = 0.12,
    interactive: bool = True,
    role: str | None = None,
    direction: Direction | str | None = None,
    fact_fields: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Produce one typed price-zone overlay with an exact or projected endpoint."""
    if bool(end_ts) == bool(end_anchor_ts or end_bar_offset is not None):
        raise ValueError("overlay box endpoint must be exactly one of end_ts or projected end")
    if end_anchor_ts is not None:
        if (
            not isinstance(end_bar_offset, int)
            or isinstance(end_bar_offset, bool)
            or end_bar_offset < 1
        ):
            raise ValueError("projected overlay box requires a positive bar offset")
    elif end_bar_offset is not None:
        raise ValueError("projected overlay box requires an anchor timestamp")
    parsed_top = float_or_none(top)
    parsed_bottom = float_or_none(bottom)
    if parsed_top is None or parsed_bottom is None:
        raise ValueError("overlay box requires finite top and bottom prices")
    upper = round_optional(max(parsed_top, parsed_bottom))
    lower = round_optional(min(parsed_top, parsed_bottom))
    if upper is None or lower is None or upper <= lower:
        raise ValueError("overlay box requires distinct finite top and bottom prices")
    direction_value = direction.value if isinstance(direction, Direction) else str(direction or "")
    payload: dict[str, Any] = {
        "type": "box",
        "start_ts": str(start_ts),
        "top": upper,
        "bottom": lower,
        "tone": str(tone or "info"),
        "opacity": max(0.0, min(float(opacity), 1.0)),
        "interactive": bool(interactive),
    }
    if end_ts:
        payload["end_ts"] = str(end_ts)
    else:
        payload["end_anchor_ts"] = str(end_anchor_ts)
        payload["end_bar_offset"] = int(end_bar_offset)
    if label_text:
        payload["label"] = str(label_text)
    if role:
        payload["role"] = str(role)
    if direction_value:
        payload["direction"] = direction_value
    return _merge_fact_fields(payload, fact_fields)


def futures_roll_event_overlays(
    *,
    bars: Sequence[Bar],
    roll_events: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    bar_list = list(bars)
    if not bar_list or not roll_events:
        return []
    min_price = min(float(bar.low) for bar in bar_list)
    max_price = max(float(bar.high) for bar in bar_list)
    if max_price <= min_price:
        return []
    overlays: list[dict[str, Any]] = []
    for event in roll_events:
        roll_ts = _parse_roll_ts(event.get("roll_ts"))
        if roll_ts is None:
            continue
        anchor = next((bar for bar in bar_list if bar.ts >= roll_ts), None)
        if anchor is None:
            continue
        from_key = str(event.get("from_contract_key") or "")
        to_key = str(event.get("to_contract_key") or "")
        if not from_key or not to_key or from_key == to_key:
            continue
        fact_fields = {
            "scenario": "futures_roll",
            "setup": "provider_managed_roll",
            "trigger_label": "Futures contract roll",
            "metrics": {
                "from_contract_key": from_key,
                "to_contract_key": to_key,
                "roll_policy": str(event.get("roll_policy") or "provider_managed"),
            },
            "notes": "Broker-managed current-contract route change.",
        }
        ts = anchor.ts.isoformat()
        overlays.append(
            {
                "type": "line",
                "start_ts": ts,
                "end_ts": ts,
                "y1": round_optional(min_price),
                "y2": round_optional(max_price),
                "tone": "info",
                "style": "solid",
                "width": 1.25,
                "role": "futures_roll",
                "layer": "foreground",
                "interactive": True,
                "label": "ROLL",
                "label_position": "center",
                "label_side": "above",
                "source": "futures_roll",
                **fact_fields,
            }
        )
        overlays.append(
            {
                "type": "label",
                "ts": ts,
                "price": round_optional(max_price),
                "lines": [from_key],
                "side": "above",
                "anchor": "above",
                "anchor_price_mode": "overlay",
                "x_offset_px": -30,
                "tone": "info",
                "label_style": "text",
                "label_font_size": 8,
                "role": "futures_roll_from",
                "layer": "foreground",
                "interactive": True,
                "source": "futures_roll",
                **fact_fields,
            }
        )
        overlays.append(
            {
                "type": "label",
                "ts": ts,
                "price": round_optional(max_price),
                "lines": [to_key],
                "side": "above",
                "anchor": "above",
                "anchor_price_mode": "overlay",
                "x_offset_px": 30,
                "tone": "info",
                "label_style": "text",
                "label_font_size": 8,
                "role": "futures_roll_to",
                "layer": "foreground",
                "interactive": True,
                "source": "futures_roll",
                **fact_fields,
            }
        )
    return overlays


def line(
    *,
    start: Bar | None = None,
    start_ts: str | None = None,
    price: float,
    label_text: str | None,
    tone: str | None = None,
    end_ts: str | None = None,
    bars_forward: int | None = None,
    width: float | None = 1.25,
    opacity: float = 1.0,
    interactive: bool = True,
    label_handle: bool = False,
    style: str | None = "dotted",
    role: str | None = None,
    label_side: str | None = None,
    label_position: str | None = None,
    label_font_size: float | None = None,
    label_gap_px: float | None = None,
    direction: Direction | str | None = None,
    fact_fields: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if (start is None) == (not start_ts):
        raise ValueError("overlay line requires exactly one start bar or start timestamp")
    anchor_ts = start.ts.isoformat() if start is not None else str(start_ts)
    if isinstance(direction, Direction):
        direction_value = direction.value
    else:
        direction_value = str(direction) if direction else ""
    if bars_forward is not None and end_ts is not None:
        raise ValueError("overlay line endpoint must be exact or projected, not both")
    if bars_forward is None and not end_ts:
        raise ValueError("overlay line endpoint is required")
    resolved_tone = tone or (
        "positive"
        if direction_value == Direction.LONG.value
        else "negative"
        if direction_value == Direction.SHORT.value
        else ""
    )
    if not resolved_tone:
        raise ValueError("overlay line requires a semantic tone")
    payload: dict[str, Any] = {
        "type": "line",
        "start_ts": anchor_ts,
        "y1": round_optional(price),
        "y2": round_optional(price),
        "interactive": interactive,
        "label_handle": label_handle,
    }
    if opacity is not None:
        payload["opacity"] = opacity
    if style:
        payload["style"] = style
    if width is not None:
        payload["width"] = width
    if label_text is not None:
        payload["label"] = label_text
    if resolved_tone:
        payload["tone"] = resolved_tone
    if bars_forward is not None:
        if start is not None:
            payload.update(projected_end(start, bars_forward))
        else:
            if (
                not isinstance(bars_forward, int)
                or isinstance(bars_forward, bool)
                or bars_forward < 1
            ):
                raise ValueError("projected overlay endpoint requires a positive bar offset")
            payload.update({"end_anchor_ts": anchor_ts, "end_bar_offset": bars_forward})
    else:
        payload["end_ts"] = str(end_ts)
    if role:
        payload["role"] = role
    if label_side:
        payload["label_side"] = label_side
    if label_position:
        payload["label_position"] = label_position
    if label_font_size is not None:
        payload["label_font_size"] = float(label_font_size)
    if label_gap_px is not None:
        payload["label_gap_px"] = float(label_gap_px)
    if direction_value:
        payload["direction"] = direction_value
    return _merge_fact_fields(payload, fact_fields)


def bind_signal_trade_plan(
    overlay: dict[str, Any],
    signal: SignalState | dict[str, Any],
    *,
    source: str | None = None,
) -> dict[str, Any]:
    if isinstance(signal, SignalState):
        payload = signal.as_dict()
        plan = signal.trade_plan
    elif isinstance(signal, dict):
        payload = signal
        plan = payload.get("trade_plan")
        plan = plan if isinstance(plan, dict) else None
    else:
        return overlay

    src = source or str(payload.get("source") or overlay.get("source") or "overlay")
    if isinstance(plan, dict):
        overlay["trade_plan"] = plan
    overlay["signal"] = payload
    overlay["signal_overlay"] = True
    overlay["source"] = src
    for field, keys in (
        ("entry", ("entry", "trigger")),
        ("stop", ("stop", "invalidation")),
        ("target", ("target",)),
        ("action", ("action",)),
        ("direction", ("direction",)),
        ("code", ("code",)),
        ("score", ("score",)),
    ):
        if field in overlay and overlay[field] is not None:
            continue
        for key in keys:
            value = payload.get(key)
            if value is not None:
                overlay[field] = value
                break
    return overlay


def signal_label(
    *,
    bar: Bar,
    price: float,
    lines: str | Sequence[Any],
    signal: SignalState,
    bg: str | None = None,
    text: str | None = None,
    side: str | None = None,
    role: str | None = None,
    opacity: float = 1.0,
    fact_fields: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    direction = signal.direction
    item = label(
        bar=bar,
        price=price,
        lines=lines,
        direction=direction,
        bg=bg,
        text=text,
        side=side,
        opacity=opacity,
        role=role or signal.source,
        fact_fields=fact_fields,
    )
    return bind_signal_trade_plan(item, signal)
