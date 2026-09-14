from __future__ import annotations

import struct
import zlib
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from aef_terminal.alerts.telegram import TELEGRAM_DISPLAY_TZ
from aef_terminal.data.gex.constants import GEX_CONTEXT_MAX_LEVELS
from aef_terminal.data.gex.contracts import require_gex_market_data_entitlement
from aef_terminal.data.gex.payload_contract import require_gex_levels
from aef_terminal.engine.common import parse_aware_utc_ts
from aef_terminal.runtime.math_utils import float_or_none


def _price_y_projector(
    *,
    min_price: float,
    max_price: float,
    pad_top: float,
    plot_height: float,
) -> Callable[[float], float]:
    price_span = max_price - min_price

    def project(price: float) -> float:
        return pad_top + (max_price - price) / price_span * plot_height

    return project


def gex_market_data_status_text(payload: Mapping[str, Any]) -> str:
    """Format actual GEX entitlement without implying realtime authority."""

    try:
        entitlement = require_gex_market_data_entitlement(
            payload.get("market_data_entitlement"),
            allow_unknown=True,
        )
    except ValueError:
        entitlement = "unknown"
    label = {
        "live": "REALTIME",
        "frozen": "FROZEN",
        "delayed": "DELAYED",
        "delayed_frozen": "DELAYED FROZEN",
        "unknown": "ENTITLEMENT UNKNOWN",
    }[entitlement]
    authority = (
        "DECISION-AUTHORITATIVE"
        if entitlement == "live" and payload.get("decision_authoritative") is True
        else "DISPLAY ONLY"
    )
    return f"{label} · {authority}"


def _gex_num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except TypeError, ValueError:
        return None
    if number != number or number in {float("inf"), float("-inf")}:
        return None
    return number


def _gex_valid_price(value: Any, anchor: float | None = None) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    price = float(value)
    if price != price or price in {float("inf"), float("-inf")} or price <= 0:
        return None
    if anchor and anchor > 0 and (price < anchor * 0.5 or price > anchor * 1.5):
        return None
    return price


def _gex_ts_ms(item: Mapping[str, Any]) -> int | None:
    raw = float_or_none(item.get("timestamp_unix_ms"))
    if raw is not None and raw > 0:
        return int(raw)
    raw_ts = item.get("captured_at")
    if raw_ts is None:
        raw_ts = item.get("timestamp")
    if raw_ts is None:
        raw_ts = item.get("ts")
    parsed = parse_aware_utc_ts(raw_ts)
    return int(parsed.timestamp() * 1000) if parsed is not None else None


def _gex_color_for_kind(kind: Any) -> str:
    if kind == "CALL_WALL":
        return "#ff5a66"
    if kind == "PUT_WALL":
        return "#00d98f"
    if kind == "POS_GAMMA_NODE":
        return "#60a5fa"
    if kind == "NEG_GAMMA_NODE":
        return "#f59e0b"
    return "#ffd166"


def _gex_rgba(hex_color: str, alpha: float) -> str:
    value = hex_color.strip().lstrip("#")
    if len(value) != 6:
        return hex_color
    red = int(value[0:2], 16)
    green = int(value[2:4], 16)
    blue = int(value[4:6], 16)
    return f"rgba({red},{green},{blue},{max(0.0, min(alpha, 1.0)):.3f})"


def _gex_time_label(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000.0, tz=UTC).strftime("%H:%M")


def telegram_capture_header_time(raw_ts: Any) -> str:
    parsed = parse_aware_utc_ts(raw_ts)
    if parsed is None:
        return "-"
    return parsed.astimezone(TELEGRAM_DISPLAY_TZ).strftime("%Y-%m-%d %H:%M %Z")


def _gex_price_label(value: float) -> str:
    return f"{value:.0f}" if abs(value) >= 100 else f"{value:.2f}"


def _gex_compact_value(value: Any) -> str:
    number = _gex_num(value)
    if number is None:
        return ""
    sign = "-" if number < 0 else ""
    abs_value = abs(number)
    if abs_value >= 1_000_000:
        return f"{sign}{abs_value / 1000:,.0f}"
    if abs_value >= 1000:
        return f"{sign}{abs_value:,.0f}"
    if abs_value == 0:
        return "0"
    if abs_value < 0.01:
        return f"{sign}{abs_value:.2e}"
    if abs_value < 10:
        return f"{sign}{abs_value:.2f}"
    if abs_value < 100:
        return f"{sign}{abs_value:.1f}"
    return f"{sign}{abs_value:.0f}"


_FONT_5X7: dict[str, tuple[str, ...]] = {
    " ": ("00000", "00000", "00000", "00000", "00000", "00000", "00000"),
    "-": ("00000", "00000", "00000", "11111", "00000", "00000", "00000"),
    "—": ("00000", "00000", "00000", "11111", "00000", "00000", "00000"),
    "·": ("00000", "00000", "00000", "00100", "00000", "00000", "00000"),
    ".": ("00000", "00000", "00000", "00000", "00000", "01100", "01100"),
    ",": ("00000", "00000", "00000", "00000", "00000", "01100", "01000"),
    ":": ("00000", "01100", "01100", "00000", "01100", "01100", "00000"),
    "/": ("00001", "00010", "00100", "00100", "01000", "10000", "00000"),
    "%": ("11001", "11010", "00010", "00100", "01000", "01011", "10011"),
    "+": ("00000", "00100", "00100", "11111", "00100", "00100", "00000"),
    "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11110", "00001", "00001", "01110", "00001", "00001", "11110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "10000", "11110", "00001", "00001", "11110"),
    "6": ("01110", "10000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00001", "01110"),
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "B": ("11110", "10001", "10001", "11110", "10001", "10001", "11110"),
    "C": ("01110", "10001", "10000", "10000", "10000", "10001", "01110"),
    "D": ("11110", "10001", "10001", "10001", "10001", "10001", "11110"),
    "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
    "F": ("11111", "10000", "10000", "11110", "10000", "10000", "10000"),
    "G": ("01110", "10001", "10000", "10111", "10001", "10001", "01110"),
    "H": ("10001", "10001", "10001", "11111", "10001", "10001", "10001"),
    "I": ("01110", "00100", "00100", "00100", "00100", "00100", "01110"),
    "J": ("00111", "00010", "00010", "00010", "00010", "10010", "01100"),
    "K": ("10001", "10010", "10100", "11000", "10100", "10010", "10001"),
    "L": ("10000", "10000", "10000", "10000", "10000", "10000", "11111"),
    "M": ("10001", "11011", "10101", "10101", "10001", "10001", "10001"),
    "N": ("10001", "11001", "10101", "10011", "10001", "10001", "10001"),
    "O": ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
    "P": ("11110", "10001", "10001", "11110", "10000", "10000", "10000"),
    "Q": ("01110", "10001", "10001", "10001", "10101", "10010", "01101"),
    "R": ("11110", "10001", "10001", "11110", "10100", "10010", "10001"),
    "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
    "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
    "U": ("10001", "10001", "10001", "10001", "10001", "10001", "01110"),
    "V": ("10001", "10001", "10001", "10001", "10001", "01010", "00100"),
    "W": ("10001", "10001", "10001", "10101", "10101", "10101", "01010"),
    "X": ("10001", "10001", "01010", "00100", "01010", "10001", "10001"),
    "Y": ("10001", "10001", "01010", "00100", "00100", "00100", "00100"),
    "Z": ("11111", "00001", "00010", "00100", "01000", "10000", "11111"),
}


def _rgb(value: str) -> tuple[int, int, int]:
    text = str(value or "#ffffff").strip()
    if text.startswith("#") and len(text) == 7:
        return int(text[1:3], 16), int(text[3:5], 16), int(text[5:7], 16)
    return 255, 255, 255


def _blend_color(
    base: tuple[int, int, int], overlay: tuple[int, int, int], alpha: float
) -> tuple[int, int, int]:
    a = max(0.0, min(float(alpha), 1.0))
    return (
        int(base[0] * (1 - a) + overlay[0] * a),
        int(base[1] * (1 - a) + overlay[1] * a),
        int(base[2] * (1 - a) + overlay[2] * a),
    )


class _PngCanvas:
    def __init__(self, width: int, height: int, bg: str = "#10151b") -> None:
        self.width = int(width)
        self.height = int(height)
        self.pixels = bytearray(_rgb(bg) * (self.width * self.height))

    def _offset(self, x: int, y: int) -> int | None:
        if x < 0 or y < 0 or x >= self.width or y >= self.height:
            return None
        return (y * self.width + x) * 3

    def pixel(self, x: float, y: float, color: str, alpha: float = 1.0) -> None:
        offset = self._offset(int(round(x)), int(round(y)))
        if offset is None:
            return
        rgb = _rgb(color)
        if alpha < 1:
            current = (self.pixels[offset], self.pixels[offset + 1], self.pixels[offset + 2])
            rgb = _blend_color(current, rgb, alpha)
        self.pixels[offset : offset + 3] = bytes(rgb)

    def rect(self, x: float, y: float, w: float, h: float, color: str, alpha: float = 1.0) -> None:
        x0 = max(int(round(x)), 0)
        y0 = max(int(round(y)), 0)
        x1 = min(int(round(x + w)), self.width)
        y1 = min(int(round(y + h)), self.height)
        for yy in range(y0, y1):
            for xx in range(x0, x1):
                self.pixel(xx, yy, color, alpha)

    def line(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        color: str,
        width: int = 1,
        alpha: float = 1.0,
        clip: tuple[float, float, float, float] | None = None,
    ) -> None:
        x1i, y1i, x2i, y2i = int(round(x1)), int(round(y1)), int(round(x2)), int(round(y2))
        dx = abs(x2i - x1i)
        dy = -abs(y2i - y1i)
        sx = 1 if x1i < x2i else -1
        sy = 1 if y1i < y2i else -1
        err = dx + dy
        x, y = x1i, y1i
        radius = max(int(width) - 1, 0)
        while True:
            for oy in range(-radius, radius + 1):
                for ox in range(-radius, radius + 1):
                    pixel_x, pixel_y = x + ox, y + oy
                    if clip is not None and not (
                        clip[0] <= pixel_x <= clip[2] and clip[1] <= pixel_y <= clip[3]
                    ):
                        continue
                    self.pixel(pixel_x, pixel_y, color, alpha)
            if x == x2i and y == y2i:
                break
            e2 = 2 * err
            if e2 >= dy:
                err += dy
                x += sx
            if e2 <= dx:
                err += dx
                y += sy

    def circle(self, cx: float, cy: float, r: float, color: str, alpha: float = 1.0) -> None:
        radius = max(int(round(r)), 1)
        cx_i, cy_i = int(round(cx)), int(round(cy))
        rr = radius * radius
        for yy in range(cy_i - radius, cy_i + radius + 1):
            for xx in range(cx_i - radius, cx_i + radius + 1):
                if (xx - cx_i) ** 2 + (yy - cy_i) ** 2 <= rr:
                    self.pixel(xx, yy, color, alpha)

    def text(self, x: float, y: float, text: Any, color: str = "#e6edf3", scale: int = 2) -> None:
        cursor = int(round(x))
        top = int(round(y))
        for char in str(text).upper():
            glyph = _FONT_5X7.get(char, _FONT_5X7.get(" ", ()))
            for row_index, row in enumerate(glyph):
                for col_index, bit in enumerate(row):
                    if bit == "1":
                        self.rect(
                            cursor + col_index * scale, top + row_index * scale, scale, scale, color
                        )
            cursor += 6 * scale

    def png(self) -> bytes:
        def chunk(kind: bytes, data: bytes) -> bytes:
            return (
                struct.pack(">I", len(data))
                + kind
                + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
            )

        raw = bytearray()
        row_bytes = self.width * 3
        for y in range(self.height):
            raw.append(0)
            start = y * row_bytes
            raw.extend(self.pixels[start : start + row_bytes])
        return (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", self.width, self.height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + chunk(b"IEND", b"")
        )


def _gex_history(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Read one selected chart lane without re-bucketing combined history."""

    spot_anchor = _gex_valid_price(payload.get("spot"))
    raw_history = payload.get("history")
    if raw_history is None:
        return []
    if not isinstance(raw_history, list):
        raise ValueError("GEX snapshot history must be a list")
    capture_mode = payload.get("capture_mode")
    if capture_mode not in {"request", "live"}:
        raise ValueError("GEX snapshot history requires an exact selected capture mode")
    try:
        market_data_entitlement = require_gex_market_data_entitlement(
            payload.get("market_data_entitlement"),
            allow_unknown=True,
        )
    except ValueError as exc:
        raise ValueError(
            "GEX snapshot history requires an exact selected market-data entitlement"
        ) from exc
    history: list[dict[str, Any]] = []
    for item in raw_history:
        if not isinstance(item, Mapping):
            raise ValueError("GEX snapshot history rows must be mappings")
        try:
            row_entitlement = require_gex_market_data_entitlement(
                item.get("market_data_entitlement"),
                allow_unknown=True,
            )
        except ValueError as exc:
            raise ValueError(
                "GEX snapshot history rows require an exact market-data entitlement"
            ) from exc
        if item.get("capture_mode") != capture_mode or row_entitlement != market_data_entitlement:
            continue
        ts_ms = item.get("timestamp_unix_ms")
        if type(ts_ms) is not int or ts_ms <= 0:
            raise ValueError("GEX snapshot history requires an exact positive timestamp")
        item_spot = _gex_valid_price(item.get("spot"), spot_anchor)
        if item_spot is None:
            raise ValueError("GEX snapshot history requires an exact positive spot")
        raw_levels = item.get("levels")
        if not isinstance(raw_levels, list):
            raise ValueError("GEX snapshot history levels must be a list")
        canonical_levels = require_gex_levels(
            raw_levels,
            max_levels=GEX_CONTEXT_MAX_LEVELS,
            spot=item_spot,
        )
        levels = [
            {
                "price": level["price"],
                "kind": level["kind"],
                "strength": level["strength"],
            }
            for level in canonical_levels
        ]
        history.append(
            {
                "timestamp_unix_ms": ts_ms,
                "spot": item_spot,
                "levels": levels,
                "source": item.get("source"),
                "capture_mode": capture_mode,
                "market_data_entitlement": row_entitlement,
            }
        )
    return history


def render_gex_interval_png(
    payload: Mapping[str, Any], *, width: int = 1100, height: int = 720
) -> bytes:
    history = _gex_history(payload)
    spot = _gex_valid_price(payload.get("spot"))
    gamma_flip = _gex_valid_price(payload.get("gamma_flip"), spot)
    prices: list[float] = []
    for snap in history:
        if snap.get("spot") is not None:
            prices.append(float(snap["spot"]))
        prices.extend(float(level["price"]) for level in snap.get("levels") or [])
    if spot is not None:
        prices.append(spot)
    if gamma_flip is not None:
        prices.append(gamma_flip)
    canvas = _PngCanvas(width, height, "#10151b")
    canvas.text(18, 18, f"{payload.get('provider_symbol') or 'GEX'} INTERVAL MAP", "#e6edf3", 2)
    canvas.text(
        18,
        42,
        f"{payload.get('status') or 'cache'} {str(payload.get('captured_at') or '-')[:19]}",
        "#94a3b8",
        1,
    )
    canvas.text(
        18,
        56,
        f"MARKET DATA: {gex_market_data_status_text(payload)}",
        "#67e8f9" if payload.get("decision_authoritative") is True else "#f59e0b",
        1,
    )
    if not history or not prices:
        canvas.text(80, 124, "NO GEX HISTORY LOADED", "#94a3b8", 2)
        return canvas.png()
    pad_left, pad_right, pad_top, pad_bottom = 64, 72, 84, 52
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - pad_bottom
    min_p, max_p = min(prices), max(prices)
    span = max(max_p - min_p, abs(max_p) * 0.004, 1.0)
    min_p -= span * 0.08
    max_p += span * 0.08
    min_ts = min(int(row["timestamp_unix_ms"]) for row in history)
    max_ts = max(int(row["timestamp_unix_ms"]) for row in history)
    now_ms = int(datetime.now(tz=UTC).timestamp() * 1000)
    max_ts = max(max_ts, now_ms)
    time_span = max(max_ts - min_ts, 300_000)

    def x_for_ts(ts: int) -> float:
        return pad_left + (ts - min_ts) / time_span * plot_w

    y_for_price = _price_y_projector(
        min_price=min_p,
        max_price=max_p,
        pad_top=pad_top,
        plot_height=plot_h,
    )

    for i in range(13):
        price = min_p + (max_p - min_p) * i / 12
        yy = y_for_price(price)
        canvas.line(pad_left, yy, width - pad_right, yy, "#26303a", 1, 0.65 if i % 2 == 0 else 0.38)
        canvas.text(8, yy - 5, _gex_price_label(price), "#94a3b8", 1)
    for i in range(11):
        ts = int(min_ts + time_span * i / 10)
        xx = x_for_ts(ts)
        canvas.line(xx, pad_top, xx, height - pad_bottom, "#26303a", 1, 0.48)
        canvas.text(xx - 14, height - 22, _gex_time_label(ts), "#94a3b8", 1)
    for snap in history:
        xx = x_for_ts(int(snap["timestamp_unix_ms"]))
        for level in snap.get("levels") or []:
            strength = float(level["strength"])
            canvas.circle(
                xx,
                y_for_price(float(level["price"])),
                2.0 + strength * 7.5,
                _gex_color_for_kind(level.get("kind")),
                0.30 + strength * 0.62,
            )
    spot_points = [
        (x_for_ts(int(snap["timestamp_unix_ms"])), y_for_price(float(snap["spot"])))
        for snap in history
        if snap.get("spot") is not None
    ]
    for left, right in zip(spot_points, spot_points[1:], strict=False):
        canvas.line(left[0], left[1], right[0], right[1], "#60a5fa", 2)
    if spot is not None:
        yy = y_for_price(spot)
        canvas.line(pad_left, yy, width - pad_right, yy, "#60a5fa", 1)
        canvas.rect(width - pad_right + 6, yy - 11, 58, 22, "#60a5fa")
        canvas.text(width - pad_right + 12, yy - 5, _gex_price_label(spot), "#061a2f", 1)
    if gamma_flip is not None:
        yy = y_for_price(gamma_flip)
        for x in range(pad_left, width - pad_right, 14):
            canvas.line(x, yy, min(x + 8, width - pad_right), yy, "#c084fc", 2)
    return canvas.png()


def render_gex_strike_png(
    payload: Mapping[str, Any], *, width: int = 720, height: int = 1100
) -> bytes:
    spot = _gex_valid_price(payload.get("spot"))
    levels = (
        require_gex_levels(
            payload.get("levels") if isinstance(payload.get("levels"), list) else [],
            max_levels=GEX_CONTEXT_MAX_LEVELS,
            spot=spot,
        )
        if spot is not None
        else []
    )
    levels.sort(key=lambda row: row["price"], reverse=True)
    if not levels:
        canvas = _PngCanvas(width, height, "#050607")
        canvas.text(24, 24, "NO GEX STRIKE PROFILE", "#94a3b8", 2)
        canvas.text(
            24,
            50,
            f"MARKET DATA: {gex_market_data_status_text(payload)}",
            "#f59e0b",
            1,
        )
        return canvas.png()
    header_h, footer_h, left_w, right_pad = 112, 52, 78, 12
    row_h = max(28, min(46, int((height - header_h - footer_h) / max(len(levels), 1))))
    ladder_h = row_h * len(levels)
    height = header_h + ladder_h + footer_h
    ladder_w = width - left_w - right_pad
    canvas = _PngCanvas(width, height, "#050607")
    canvas.rect(0, 0, width, header_h, "#0b0b10")
    asset = payload.get("provider_symbol") or "GEX"
    canvas.rect(22, 22, 76, 34, "#06252a", 0.55)
    canvas.text(36, 31, asset, "#67e8f9", 2)
    canvas.text(126, 31, _gex_price_label(spot) if spot else "--", "#e6edf3", 2)
    captured = str(payload.get("captured_at") or "")
    date_label = captured[:10].replace("-", "/") if captured else ""
    control = max(levels, key=lambda row: float(row["abs_gex"]))
    canvas.text(width - 228, 22, "CONTROL NODE", "#94a3b8", 1)
    canvas.text(
        width - 228, 39, f"{date_label[-8:]} {_gex_price_label(control['price'])}", "#e6edf3", 2
    )
    canvas.text(
        22,
        68,
        f"MARKET DATA: {gex_market_data_status_text(payload)}",
        "#67e8f9" if payload.get("decision_authoritative") is True else "#f59e0b",
        1,
    )
    canvas.text(14, header_h - 24, "STRIKE", "#60a5fa", 2)
    canvas.text(left_w + ladder_w / 2 - 40, header_h - 24, date_label[-8:], "#67e8f9", 2)
    net = 0.0
    for i, row in enumerate(levels):
        y = header_h + i * row_h
        value = float(row["net_gex"])
        net += value
        strength = float(row["strength"])
        color = _gex_heat_color(value, strength, include_alpha=False)
        canvas.rect(left_w, y, ladder_w, row_h, color, 0.45 + strength * 0.5)
        canvas.line(left_w, y + row_h - 1, width - right_pad, y + row_h - 1, "#ffffff", 1, 0.12)
        if spot is not None and abs(float(row["price"]) - spot) <= max(0.75, row_h / 40.0):
            canvas.rect(0, y, width, row_h, "#67e8f9", 0.22)
            canvas.rect(20, y + 7, 4, max(10, row_h - 14), "#67e8f9")
        if row["price"] == control["price"]:
            canvas.line(left_w + 4, y + 4, left_w + ladder_w - 4, y + 4, "#ffd166", 3)
            canvas.line(
                left_w + 4, y + row_h - 4, left_w + ladder_w - 4, y + row_h - 4, "#ffd166", 3
            )
            canvas.line(left_w + 4, y + 4, left_w + 4, y + row_h - 4, "#ffd166", 3)
            canvas.line(
                left_w + ladder_w - 4, y + 4, left_w + ladder_w - 4, y + row_h - 4, "#ffd166", 3
            )
        canvas.text(left_w - 54, y + row_h / 2 - 7, _gex_price_label(row["price"]), "#60a5fa", 2)
        canvas.text(width - 130, y + row_h / 2 - 7, _gex_compact_value(value), "#f8fafc", 2)
    footer_y = header_h + ladder_h
    canvas.rect(0, footer_y, width, footer_h, "#121217")
    canvas.line(0, footer_y, width, footer_y, "#26303a", 1)
    canvas.text(28, footer_y + 18, "NET", "#67e8f9", 2)
    canvas.text(left_w + ladder_w / 2 - 60, footer_y + 18, _gex_compact_value(net), "#67e8f9", 2)
    return canvas.png()


def _gex_heat_color(
    value: float,
    strength: float,
    *,
    include_alpha: bool = True,
) -> str:
    alpha = 0.45 + max(0.0, min(strength, 1.0)) * 0.5
    if value > 0:
        base = "#22c55e" if strength > 0.72 else "#b000b5" if strength > 0.42 else "#400040"
    elif value < 0:
        base = "#22d3ee" if strength > 0.72 else "#0f9f9f" if strength > 0.42 else "#08343f"
    else:
        base = "#64748b"
    return _gex_rgba(base, alpha) if include_alpha else base
