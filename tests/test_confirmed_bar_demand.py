from __future__ import annotations

import asyncio
from types import SimpleNamespace

import aef_terminal.ui.runtime.confirmed_bar_demand as demand_module
from aef_terminal.data.providers import route_instrument
from aef_terminal.ui.runtime.confirmed_bar_demand import (
    ConfirmedBarDemandRuntime,
)
from tests.provider_payloads import ibkr_stock_payload


def _payload(*, interval: str = "5m", rounded: bool, w5: bool) -> dict:
    instrument = ibkr_stock_payload("SPY")
    route = route_instrument(instrument)
    return {
        "source": route.provider,
        "instrument_id": route.instrument_id,
        "route_fingerprint": route.fingerprint,
        "interval": interval,
        "indicator_params": {
            "rounded_reversal": {"enabled": rounded},
            "w5_structure": {"enabled": w5},
        },
        "instrument": instrument,
    }


def test_confirmed_bar_demand_coalesces_enabled_indicator_consumers() -> None:
    desired = ConfirmedBarDemandRuntime._desired((_payload(rounded=True, w5=True),))

    assert len(desired) == 1
    demand = next(iter(desired.values()))
    assert demand.timeframe == "1m"
    assert demand.history_bars == 256
    assert demand.consumers == ("rounded_reversal", "w5_structure")


def test_confirmed_bar_demand_coalesces_all_intrabar_preview_consumers() -> None:
    payload = _payload(rounded=True, w5=True)
    payload["indicator_params"].update(
        {
            "absorption_trap": {"enabled": True},
            "breakout_accumulation": {"enabled": True},
            "linda_volume": {"enabled": True},
            "market_spotlight": {"enabled": True},
            "obvious_failure": {"enabled": True},
            "trade_setup_engine": {"enabled": True},
        }
    )

    desired = ConfirmedBarDemandRuntime._desired((payload,))

    assert len(desired) == 1
    demand = next(iter(desired.values()))
    assert demand.history_bars == 256
    assert demand.consumers == (
        "absorption_trap",
        "breakout_accumulation",
        "linda_volume",
        "market_spotlight",
        "obvious_failure",
        "rounded_reversal",
        "trade_setup_engine",
        "w5_structure",
    )


def test_confirmed_bar_demand_is_absent_when_calc_is_off_or_parent_is_1m() -> None:
    assert ConfirmedBarDemandRuntime._desired((_payload(rounded=False, w5=False),)) == {}
    assert (
        ConfirmedBarDemandRuntime._desired((_payload(interval="1m", rounded=True, w5=True),)) == {}
    )


def test_research_capture_retains_parent_and_declared_lower_context() -> None:
    payload = _payload(interval="5m", rounded=False, w5=False)
    payload["analysis_effects"] = "research_capture"
    payload["indicator_params"]["option_reversal"] = {"enabled": True}

    desired = ConfirmedBarDemandRuntime._desired((payload,))

    assert {demand.timeframe for demand in desired.values()} == {"1m", "5m"}
    parent = next(demand for demand in desired.values() if demand.timeframe == "5m")
    lower = next(demand for demand in desired.values() if demand.timeframe == "1m")
    assert parent.history_bars == 512
    assert parent.consumers == ("research_capture",)
    assert lower.history_bars == 256
    assert lower.consumers == ("option_reversal",)


def test_confirmed_bar_demand_forwards_exact_canonical_generation(
    monkeypatch,
) -> None:
    async def run() -> None:
        calls: list[tuple[str, str, int, float, str]] = []
        delivered: list[tuple[str, str, str, int]] = []
        callback_seen = asyncio.Event()
        block = asyncio.Event()

        async def wait_for_generation(
            timeframe: str,
            route_fingerprint: str,
            seen_generation: int,
            timeout: float,
            *,
            instrument_id: str,
        ) -> tuple[int, list]:
            calls.append(
                (
                    timeframe,
                    route_fingerprint,
                    seen_generation,
                    timeout,
                    instrument_id,
                )
            )
            if len(calls) == 1:
                return 7, []
            await block.wait()
            return 7, []

        async def on_generation(
            instrument_id: str,
            route_fingerprint: str,
            timeframe: str,
            generation: int,
        ) -> None:
            delivered.append(
                (
                    instrument_id,
                    route_fingerprint,
                    timeframe,
                    generation,
                )
            )
            callback_seen.set()

        monkeypatch.setattr(
            demand_module,
            "wait_for_chart_bars_updated",
            wait_for_generation,
        )
        runtime = ConfirmedBarDemandRuntime()
        runtime.configure(SimpleNamespace(), on_generation=on_generation)
        lease = SimpleNamespace(last_generation=6)
        key = (
            "ibkr|contract|756733",
            "ibkr|contract|756733|route",
            "1m",
        )
        task = asyncio.create_task(runtime._watch_canonical_generations(key, lease))
        await callback_seen.wait()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        assert calls[0] == (
            "1m",
            "ibkr|contract|756733|route",
            6,
            60.0,
            "ibkr|contract|756733",
        )
        assert delivered == [
            (
                "ibkr|contract|756733",
                "ibkr|contract|756733|route",
                "1m",
                7,
            )
        ]
        assert lease.last_generation == 7

    asyncio.run(run())


def test_confirmed_bar_generation_retries_before_advancing_cursor(
    monkeypatch,
) -> None:
    async def run() -> None:
        wait_calls: list[int] = []
        delivered: list[int] = []
        cursor_at_delivery: list[int] = []
        delivered_successfully = asyncio.Event()
        second_wait_started = asyncio.Event()
        block = asyncio.Event()
        lease = SimpleNamespace(last_generation=6)

        async def wait_for_generation(
            _timeframe: str,
            _route_fingerprint: str,
            seen_generation: int,
            _timeout: float,
            *,
            instrument_id: str,
        ) -> tuple[int, list]:
            assert instrument_id == "ibkr|contract|756734"
            wait_calls.append(seen_generation)
            if len(wait_calls) == 1:
                return 7, []
            second_wait_started.set()
            await block.wait()
            return 7, []

        async def on_generation(
            _instrument_id: str,
            _route_fingerprint: str,
            _timeframe: str,
            generation: int,
        ) -> None:
            delivered.append(generation)
            cursor_at_delivery.append(lease.last_generation)
            if len(delivered) == 1:
                raise RuntimeError("transient callback failure")
            delivered_successfully.set()

        monkeypatch.setattr(
            demand_module,
            "wait_for_chart_bars_updated",
            wait_for_generation,
        )
        monkeypatch.setattr(
            demand_module,
            "CONFIRMED_BAR_CALLBACK_RETRY_INITIAL_SECONDS",
            0.01,
        )
        runtime = ConfirmedBarDemandRuntime()
        runtime.configure(SimpleNamespace(), on_generation=on_generation)
        key = (
            "ibkr|contract|756734",
            "ibkr|contract|756734|route",
            "1m",
        )
        task = asyncio.create_task(runtime._watch_canonical_generations(key, lease))
        await asyncio.wait_for(
            delivered_successfully.wait(),
            timeout=0.5,
        )
        await asyncio.wait_for(second_wait_started.wait(), timeout=0.5)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        assert delivered == [7, 7]
        assert cursor_at_delivery == [6, 6]
        assert wait_calls == [6, 7]
        assert lease.last_generation == 7

    asyncio.run(run())
