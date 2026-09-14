from __future__ import annotations

import asyncio
import threading
from typing import Any

import pytest

import aef_terminal.data.adapters._coinbase.client as coinbase_quotes
from aef_terminal.data.adapters._coinbase.client import CoinbaseQuotePollingRuntime
from aef_terminal.data.instrument_identity import route_fingerprint
from tests.provider_payloads import coinbase_btc_payload


def _instrument(product_id: str) -> dict[str, Any]:
    instrument = coinbase_btc_payload()
    if product_id == "BTC-USD":
        return instrument
    instrument.update(
        {
            "instrument_id": f"coinbase|contract|{product_id}",
            "key": "ETH",
            "instrument_key": "ETH",
            "display": "ETH",
            "name": "Ethereum USD",
            "provider_symbol": product_id,
            "provider_contract_id": product_id,
        }
    )
    instrument["contract_identity"] = {
        **instrument["contract_identity"],
        "provider_contract_id": product_id,
        "product_id": product_id,
    }
    return instrument


def _quote(product: str, price: float) -> dict[str, Any]:
    return {
        "provider": "coinbase",
        "product": product,
        "price": price,
        "message": "",
    }


def test_coinbase_runtime_returns_each_fresh_provider_attempt(monkeypatch) -> None:
    calls: list[tuple[str, float]] = []

    def fetch(product: str, *, timeout: float) -> dict[str, Any]:
        calls.append((product, timeout))
        return _quote(product, float(len(calls)))

    monkeypatch.setattr(coinbase_quotes, "_fetch_coinbase_ticker", fetch)
    instrument = _instrument("BTC-USD")

    async def scenario() -> None:
        runtime = CoinbaseQuotePollingRuntime(timeout=0.25)
        first, first_warning = await runtime.fetch_quotes([instrument])
        second, second_warning = await runtime.fetch_quotes([instrument])
        await runtime.aclose()
        fingerprint = route_fingerprint(instrument)
        assert first[fingerprint]["price"] == 1.0
        assert second[fingerprint]["price"] == 2.0
        assert first_warning == second_warning == ""

    asyncio.run(scenario())
    assert calls == [("BTC-USD", 0.25), ("BTC-USD", 0.25)]


def test_coinbase_runtime_does_not_replay_failed_route(monkeypatch) -> None:
    def fetch(product: str, *, timeout: float) -> dict[str, Any]:
        assert timeout == 1.5
        if product == "ETH-USD":
            raise TimeoutError("provider timeout")
        return _quote(product, 100.0)

    monkeypatch.setattr(coinbase_quotes, "_fetch_coinbase_ticker", fetch)
    btc = _instrument("BTC-USD")
    eth = _instrument("ETH-USD")

    async def scenario() -> None:
        runtime = CoinbaseQuotePollingRuntime()
        quotes, warning = await runtime.fetch_quotes([btc, eth])
        await runtime.aclose()
        assert quotes == {route_fingerprint(btc): _quote("BTC-USD", 100.0)}
        assert warning == "Coinbase quote unavailable for ETH-USD: provider timeout"

    asyncio.run(scenario())


def test_coinbase_runtime_settles_started_http_work_before_cancellation(monkeypatch) -> None:
    started = threading.Event()
    release = threading.Event()

    def fetch(product: str, *, timeout: float) -> dict[str, Any]:
        assert product == "BTC-USD"
        assert timeout == 1.5
        started.set()
        assert release.wait(timeout=2.0)
        return _quote(product, 100.0)

    monkeypatch.setattr(coinbase_quotes, "_fetch_coinbase_ticker", fetch)

    async def scenario() -> None:
        runtime = CoinbaseQuotePollingRuntime()
        task = asyncio.create_task(runtime.fetch_quotes([_instrument("BTC-USD")]))
        assert await asyncio.to_thread(started.wait, 1.0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await runtime.aclose()

    asyncio.run(scenario())
