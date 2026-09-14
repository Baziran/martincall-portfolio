from __future__ import annotations

from typing import Any

import pytest

from aef_terminal.data.instrument_identity import qualified_instrument_id, route_fingerprint
from tests.provider_payloads import ibkr_future_payload

from aef_terminal.ui.runtime import quote_stream
from aef_terminal.ui.runtime_instruments import lookup_runtime_instrument


def test_lookup_runtime_instrument_watchlist_match(monkeypatch: pytest.MonkeyPatch) -> None:
    watchlist_item = ibkr_future_payload("ES", con_id=12345)
    instrument_id = qualified_instrument_id(watchlist_item)
    watchlist_item["instrument_id"] = instrument_id
    watchlist_item["route_fingerprint"] = route_fingerprint(watchlist_item)
    monkeypatch.setattr(
        quote_stream,
        "select_instruments",
        lambda instrument_ids: (
            [watchlist_item] if tuple(instrument_ids) == (instrument_id,) else []
        ),
    )

    res = lookup_runtime_instrument(instrument_id)
    assert res["key"] == "ES"
    assert res["route_fingerprint"] == route_fingerprint(watchlist_item)


def test_lookup_runtime_instrument_rejects_name_and_missing_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(quote_stream, "select_instruments", lambda _instrument_ids: [])

    with pytest.raises(ValueError, match="instrument_id=ES"):
        lookup_runtime_instrument("ES")
    with pytest.raises(ValueError, match="instrument_id=NON_EXISTENT"):
        lookup_runtime_instrument("NON_EXISTENT")


class StoreWithoutReadSettings:
    def __init__(self, watchlist_data: list[dict[str, Any]]) -> None:
        self.watchlist_data = watchlist_data
        self.read_settings_called = False

    def initialize(self) -> None:
        pass

    def read_watchlist_snapshot(self) -> tuple[list[dict[str, Any]], int]:
        return self.watchlist_data, 0

    def read_settings(self, scope: str) -> dict[str, Any]:
        self.read_settings_called = True
        return {}


def test_selected_instruments_bypasses_read_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    from aef_terminal.ui.client_settings import selected_instruments

    item = ibkr_future_payload("ES", con_id=12345)
    instrument_id = qualified_instrument_id(item)
    mock_store = StoreWithoutReadSettings([item])
    monkeypatch.setattr("aef_terminal.ui.client_settings._postgres_store", lambda: mock_store)

    selected = selected_instruments((instrument_id,))
    assert len(selected) == 1
    assert selected[0]["instrument_id"] == instrument_id
    assert selected[0]["key"] == "ES"
    assert mock_store.read_settings_called is False
