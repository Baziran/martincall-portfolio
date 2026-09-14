from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from enum import IntEnum
from types import SimpleNamespace

import pytest

from aef_terminal import config as config_module
from aef_terminal.config import AppConfig
from aef_terminal.data.adapters._tinvest import qualification
from aef_terminal.data.adapters._tinvest.qualification import (
    TInvestInstrumentBinding,
    TInvestQualificationError,
    bind_tinvest_payload_sync,
    open_tinvest_services,
    qualify_tinvest_instrument,
    qualify_tinvest_search_items,
    search_tinvest_payloads_sync,
    tinvest_binding_payload,
    tinvest_candidate_payload,
)
from aef_terminal.data.instrument_identity import (
    provider_symbol,
    require_provider_identity,
    route_fingerprint,
)
from aef_terminal.data.providers import get_provider, provider_catalog, storage_provider_keys


class InstrumentKind(IntEnum):
    INSTRUMENT_TYPE_UNSPECIFIED = 0
    INSTRUMENT_TYPE_SHARE = 2
    INSTRUMENT_TYPE_FUTURES = 5


def _instrument(
    uid: str,
    *,
    ticker: str = "SBER",
    kind: InstrumentKind = InstrumentKind.INSTRUMENT_TYPE_SHARE,
) -> SimpleNamespace:
    return SimpleNamespace(
        uid=uid,
        position_uid=f"position:{uid}",
        figi=f"figi:{uid}",
        ticker=ticker,
        class_code="TQBR" if kind is InstrumentKind.INSTRUMENT_TYPE_SHARE else "SPBFUT",
        instrument_type="share" if kind is InstrumentKind.INSTRUMENT_TYPE_SHARE else "futures",
        instrument_kind=kind,
        name=f"Instrument {uid}",
        api_trade_available_flag=True,
        for_qual_investor_flag=False,
        weekend_flag=False,
        lot=10,
        first_1min_candle_date=datetime(2018, 1, 1, tzinfo=UTC),
        first_1day_candle_date=datetime(2000, 1, 1, tzinfo=UTC),
    )


def test_tinvest_candidate_preserves_exact_provider_identity() -> None:
    candidate = qualify_tinvest_instrument(_instrument("Uid-MixedCase"))

    assert candidate.provider == "tinvest"
    assert candidate.instrument_uid == "Uid-MixedCase"
    assert candidate.provider_contract_id == "Uid-MixedCase"
    assert candidate.position_uid == "position:Uid-MixedCase"
    assert candidate.figi == "figi:Uid-MixedCase"
    assert candidate.asset_class == "stock"


def test_tinvest_search_deduplicates_only_identical_exact_uids() -> None:
    first = _instrument("uid-a", ticker="SAME")
    second = _instrument("uid-b", ticker="SAME")

    candidates = qualify_tinvest_search_items([first, first, second])

    assert [item.instrument_uid for item in candidates] == ["uid-a", "uid-b"]

    conflicting = _instrument("uid-a", ticker="DIFFERENT")
    with pytest.raises(TInvestQualificationError, match="TINVEST_DUPLICATE_UID_CONFLICT"):
        qualify_tinvest_search_items([first, conflicting])


def test_tinvest_unknown_typed_kind_fails_closed() -> None:
    item = _instrument("uid-unknown")
    item.instrument_kind = InstrumentKind.INSTRUMENT_TYPE_UNSPECIFIED

    with pytest.raises(TInvestQualificationError, match="TINVEST_INSTRUMENT_KIND_UNSUPPORTED"):
        qualify_tinvest_instrument(item)


def test_tinvest_search_normalizes_query_but_not_selected_identity(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class Instruments:
        def find_instrument(self, request: object) -> object:
            captured["request"] = request
            return SimpleNamespace(instruments=[_instrument(" Exact UID ")])

    class Client:
        def __enter__(self) -> object:
            return SimpleNamespace(instruments=Instruments())

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(
        qualification,
        "_find_request",
        lambda query: SimpleNamespace(query=query),
    )
    monkeypatch.setattr(qualification, "_sync_client", lambda _token: Client())

    result = search_tinvest_payloads_sync("secret-token", "  sber  ")

    assert captured["request"].query == "sber"
    assert result[0]["provider_contract_id"] == " Exact UID "


def test_tinvest_search_ranks_exact_ticker_before_provider_substring_results(
    monkeypatch,
) -> None:
    bonds = [_instrument(f"bond-{index}", ticker=f"RU000A{index:05d}") for index in range(25)]
    exact_share = _instrument("exact-sber-uid", ticker="SBER")

    class Instruments:
        def find_instrument(self, _request: object) -> object:
            return SimpleNamespace(instruments=[*bonds, exact_share])

    class Client:
        def __enter__(self) -> object:
            return SimpleNamespace(instruments=Instruments())

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(qualification, "_find_request", lambda query: query)
    monkeypatch.setattr(qualification, "_sync_client", lambda _token: Client())

    result = search_tinvest_payloads_sync(
        "secret-token",
        "sber",
        max_results=20,
    )

    assert len(result) == 20
    assert result[0]["provider_contract_id"] == "exact-sber-uid"


def test_tinvest_bind_uses_only_exact_uid_and_rejects_mismatch(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class Instruments:
        def __init__(self, returned_uid: str) -> None:
            self.returned_uid = returned_uid

        def get_instrument_by(self, request: object) -> object:
            captured["request"] = request
            return SimpleNamespace(instrument=_instrument(self.returned_uid))

    returned_uid = " Uid-MixedCase "

    class Client:
        def __enter__(self) -> object:
            return SimpleNamespace(instruments=Instruments(returned_uid))

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(
        qualification,
        "tinvest_uid_request",
        lambda uid: SimpleNamespace(instrument_uid=uid),
    )
    monkeypatch.setattr(qualification, "_sync_client", lambda _token: Client())
    exact_uid = " Uid-MixedCase "
    binding = bind_tinvest_payload_sync("secret-token", exact_uid)

    assert captured["request"].instrument_uid == exact_uid
    assert binding["provider_contract_id"] == exact_uid
    assert binding["contract_identity"]["identity_scope"] == "contract"

    returned_uid = "different-uid"
    with pytest.raises(TInvestQualificationError, match="TINVEST_BIND_UID_MISMATCH"):
        bind_tinvest_payload_sync("secret-token", exact_uid)


def test_tinvest_future_binding_is_typed_as_exact_contract(monkeypatch) -> None:
    class Instruments:
        def get_instrument_by(self, _request: object) -> object:
            return SimpleNamespace(
                instrument=_instrument(
                    "future-uid",
                    ticker="SiU6",
                    kind=InstrumentKind.INSTRUMENT_TYPE_FUTURES,
                )
            )

    class Client:
        def __enter__(self) -> object:
            return SimpleNamespace(instruments=Instruments())

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(qualification, "tinvest_uid_request", lambda uid: uid)
    monkeypatch.setattr(qualification, "_sync_client", lambda _token: Client())
    binding = bind_tinvest_payload_sync("secret-token", "future-uid")

    assert binding["asset_class"] == "future"
    assert binding["contract_identity"]["identity_scope"] == "contract"
    assert "root" not in binding["contract_identity"]


def test_tinvest_candidate_payload_is_one_canonical_exact_contract() -> None:
    candidate = qualify_tinvest_instrument(_instrument(" Exact UID "))

    payload = tinvest_candidate_payload(candidate)

    assert require_provider_identity(payload, provider="tinvest") is payload
    assert payload["instrument_id"] == "tinvest|contract| Exact UID "
    assert payload["provider_contract_id"] == " Exact UID "
    assert provider_symbol(payload, "tinvest") == " Exact UID "
    assert route_fingerprint(payload) == payload["instrument_id"]
    assert payload["instrument_key"] == "SBER"
    assert payload["contract_identity"] == {
        "provider": "tinvest",
        "asset_class": "stock",
        "identity_scope": "contract",
        "provider_contract_id": " Exact UID ",
        "instrument_uid": " Exact UID ",
        "position_uid": "position: Exact UID ",
        "figi": "figi: Exact UID ",
        "ticker": "SBER",
        "class_code": "TQBR",
        "instrument_type": "share",
        "instrument_kind": "INSTRUMENT_TYPE_SHARE",
        "api_trade_available": True,
        "for_qualified_investor": False,
        "weekend_trading_available": False,
        "lot": 10,
        "first_1m_candle_at": "2018-01-01T00:00:00+00:00",
        "first_1d_candle_at": "2000-01-01T00:00:00+00:00",
    }


def test_tinvest_future_binding_payload_never_claims_root_or_continuous_identity() -> None:
    candidate = qualify_tinvest_instrument(
        _instrument(
            "future-uid",
            ticker="SiU6",
            kind=InstrumentKind.INSTRUMENT_TYPE_FUTURES,
        )
    )

    payload = tinvest_binding_payload(TInvestInstrumentBinding(candidate=candidate))

    assert require_provider_identity(payload, provider="tinvest") is payload
    assert payload["instrument_id"] == "tinvest|contract|future-uid"
    assert payload["provider_symbol"] == "future-uid"
    assert payload["contract_identity"]["identity_scope"] == "contract"
    assert payload["contract_identity"]["asset_class"] == "future"
    assert "root" not in payload["contract_identity"]
    assert "current_contract" not in payload["contract_identity"]
    assert "continuous_series" not in payload


def test_sync_reference_boundary_uses_official_client_without_asyncio_run(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {
        "clients": [],
        "queries": [],
        "uids": [],
        "exits": 0,
    }

    class Instruments:
        def find_instrument(self, request: object) -> object:
            captured["queries"].append(request.query)
            return SimpleNamespace(instruments=[_instrument("uid-a")])

        def get_instrument_by(self, request: object) -> object:
            captured["uids"].append(request.instrument_uid)
            return SimpleNamespace(instrument=_instrument(request.instrument_uid))

    class Client:
        def __enter__(self) -> object:
            return SimpleNamespace(instruments=Instruments())

        def __exit__(self, *_args: object) -> None:
            captured["exits"] = int(captured["exits"]) + 1

    def create_client(*, token: str, **kwargs: object) -> Client:
        captured["clients"].append({"token": token, **kwargs})
        return Client()

    monkeypatch.setattr(qualification, "_sdk_client", lambda: create_client)
    monkeypatch.setattr(
        qualification,
        "_find_request",
        lambda query: SimpleNamespace(query=query),
    )
    monkeypatch.setattr(
        qualification,
        "tinvest_uid_request",
        lambda uid: SimpleNamespace(instrument_uid=uid),
    )
    monkeypatch.setattr(
        asyncio,
        "run",
        lambda *_args, **_kwargs: pytest.fail("sync qualification used asyncio.run"),
    )

    matches = search_tinvest_payloads_sync(
        "secret-token",
        "  sber  ",
        max_results=20,
    )
    rebound = bind_tinvest_payload_sync("secret-token", " Exact UID ")

    assert captured["queries"] == ["sber"]
    assert captured["uids"] == [" Exact UID "]
    assert captured["exits"] == 2
    assert captured["clients"] == [
        {
            "token": "secret-token",
            "instrument_methods_to_cache": (),
            "warmup_cache_methods": (),
        },
        {
            "token": "secret-token",
            "instrument_methods_to_cache": (),
            "warmup_cache_methods": (),
        },
    ]
    assert matches[0]["instrument_id"] == "tinvest|contract|uid-a"
    assert rebound["instrument_id"] == "tinvest|contract| Exact UID "


def test_tinvest_official_client_disables_hidden_sdk_caches(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class Client:
        async def __aenter__(self) -> object:
            return SimpleNamespace(instruments=object())

        async def __aexit__(self, *_args: object) -> None:
            return None

    def create_client(*, token: str, **kwargs: object) -> Client:
        captured["token"] = token
        captured.update(kwargs)
        return Client()

    monkeypatch.setattr(qualification, "_sdk_async_client", lambda: create_client)

    async def use_services() -> None:
        async with open_tinvest_services("secret-token") as services:
            assert services.instruments is not None

    asyncio.run(use_services())

    assert captured == {
        "token": "secret-token",
        "instrument_methods_to_cache": (),
        "warmup_cache_methods": (),
    }


def test_tinvest_token_uses_secret_owner_and_is_excluded_from_repr(
    monkeypatch,
    tmp_path,
) -> None:
    secret_file = tmp_path / "runtime.env"
    secret_file.write_text("AEF_TINVEST_TOKEN=secret-from-file\n", encoding="utf-8")
    monkeypatch.delenv("AEF_TINVEST_TOKEN", raising=False)
    monkeypatch.setenv("AEF_SECRET_FILE", str(secret_file))
    monkeypatch.setattr(config_module, "_SECRET_FILE_CACHE", None)

    config = AppConfig()

    assert config.tinvest_token == "secret-from-file"
    assert "secret-from-file" not in repr(config)

    monkeypatch.setenv("AEF_TINVEST_TOKEN", "env-wins")
    assert AppConfig().tinvest_token == "env-wins"


def test_tinvest_provider_registration_is_manifest_derived_for_v18() -> None:
    assert "tinvest" in {item["key"] for item in provider_catalog()}
    assert "tinvest" in storage_provider_keys()
    provider = get_provider("tinvest")
    assert provider.capabilities.gap_repair is True
    assert provider.capabilities.chart_tail_polling is True
    assert provider.capabilities.read_only is True
