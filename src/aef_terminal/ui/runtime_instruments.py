from __future__ import annotations

from typing import Any

from aef_terminal.data.instrument_identity import require_exact_identity_text


def lookup_runtime_instrument(instrument_id: str) -> dict[str, Any]:
    from aef_terminal.ui.runtime.quote_stream import quote_stream

    text = require_exact_identity_text(instrument_id, field="instrument_id")
    selected = quote_stream.select_instruments((text,))
    if len(selected) == 1:
        return dict(selected[0])
    raise ValueError(f"Instrument is not in the runtime route snapshot: instrument_id={text}")
