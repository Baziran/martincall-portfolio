from __future__ import annotations

import logging
from typing import Any
from aef_terminal.indicators.registry import indicator_manifest

STRUCTURE_MAX_BARS = 1800
MAX_SNAPSHOT_BARS = 18000  # Предел для стабильности браузера
_LOGGER = logging.getLogger(__name__)

INDICATOR_STATUS_META: dict[str, dict[str, Any]] = indicator_manifest()
