from __future__ import annotations

from typing import Any

from aef_terminal.data.instrument_identity import require_exact_identity_text


def lock_provider_instrument_lifecycle_on_cursor(
    cur: Any,
    *,
    provider: str,
    instrument_id: str,
) -> tuple[str, str]:
    """Serialize one provider-owned instrument lifecycle transition."""

    exact_provider = require_exact_identity_text(provider, field="provider")
    exact_instrument_id = require_exact_identity_text(
        instrument_id,
        field="instrument_id",
    )
    cur.execute(
        "SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))",
        (exact_provider, exact_instrument_id),
    )
    return exact_provider, exact_instrument_id
