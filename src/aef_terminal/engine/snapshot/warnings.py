from __future__ import annotations

from typing import Any


def attach_provider_warning(snapshot: dict[str, Any], provider_warning: str) -> None:
    if not provider_warning:
        return
    existing = snapshot["meta"].get("warning", "")
    snapshot["meta"]["warning"] = (
        f"{existing}; {provider_warning}" if existing else provider_warning
    )
