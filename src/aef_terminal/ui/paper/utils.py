from __future__ import annotations

from datetime import datetime
from pathlib import Path

from aef_terminal.runtime.timeframes import parse_aware_utc_ts


def parse_iso_utc(value: object) -> datetime | None:
    return parse_aware_utc_ts(value)


def math_is_finite(value: float) -> bool:
    return value == value and value not in (float("inf"), float("-inf"))


def paper_project_root() -> Path:
    return Path(__file__).resolve().parents[4]
