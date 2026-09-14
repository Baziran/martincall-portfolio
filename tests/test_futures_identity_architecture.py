from __future__ import annotations

from aef_terminal.data.ibkr.contracts import _current_future_contract
from aef_terminal.data.instrument_identity import route_fingerprint
from tests.provider_payloads import ibkr_future_payload


def test_ibkr_current_future_uses_persisted_lifecycle_contract_without_broker_calls() -> None:
    instrument = ibkr_future_payload("ES")

    class FakeIb:
        def qualifyContracts(self, _contract):
            raise AssertionError("live consumers must not qualify futures contracts")

    contract = _current_future_contract(FakeIb(), instrument=instrument)

    assert contract.secType == "FUT"
    assert contract.localSymbol == "ESU6"
    assert contract.conId == 11004968


def test_futures_route_cache_fingerprint_changes_with_persisted_current_contract() -> None:
    first = ibkr_future_payload("ES", con_id=1, local_symbol="ESU6")
    second = ibkr_future_payload("ES", con_id=2, local_symbol="ESZ6")

    assert route_fingerprint(first) != route_fingerprint(second)


def test_static_symbol_registry_and_sync_repair_client_paths_are_absent() -> None:
    import aef_terminal.data.ibkr.contracts as contracts

    assert not hasattr(contracts, "fetch_historical_bars_once")
    assert not hasattr(contracts, "ibkr_repair_client_id_candidates")
