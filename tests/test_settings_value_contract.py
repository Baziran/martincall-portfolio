from __future__ import annotations

import json

import pytest

from aef_terminal import settings_contract
from aef_terminal.indicators.registry import INDICATOR_REGISTRY
from aef_terminal.indicators.settings_schema import (
    GLOBAL_DEFAULT_FIELDS,
    GLOBAL_DEFAULT_PRESETS,
)


def test_global_indicator_setting_keys_are_built_by_the_settings_contract() -> None:
    assert (
        settings_contract.global_indicator_setting_key("globalAtrLen")
        == "aef:indicator:global:globalAtrLen"
    )
    with pytest.raises(ValueError, match="GLOBAL_INDICATOR_SETTING_KEY_INVALID"):
        settings_contract.global_indicator_setting_key("unknownGlobal")


def test_schema_v18_indicator_settings_have_no_symbol_compatibility_path() -> None:
    instrument_id = "ibkr|contract|756733"
    instrument_key = settings_contract.instrument_indicator_setting_key(
        instrument_id,
        "barRadarVisible",
    )
    legacy_key = "aef:SPY:indicator:regular:barRadarVisible"
    manifest = settings_contract.browser_client_settings_contract_manifest()

    assert settings_contract._indicator_setting_ref(instrument_key) == (
        "instrument",
        "barRadarVisible",
        instrument_id,
        "regular",
    )
    assert settings_contract._indicator_setting_ref(legacy_key) is None
    assert settings_contract.is_current_client_setting(legacy_key) is False
    assert not hasattr(settings_contract, "symbol_indicator_setting_key")
    assert manifest["version"] == 2
    assert set(manifest["indicator_keys"]) == {"global", "instrument"}


def test_ny_range_lines_is_a_current_browser_writable_global_boolean() -> None:
    key = "aef:indicator:global:nyRangeLines"

    assert settings_contract.global_indicator_setting_key("nyRangeLines") == key
    assert settings_contract.is_current_client_setting(key) is True
    assert settings_contract.is_browser_writable_client_setting(key) is True
    assert settings_contract.invalid_client_setting_value_keys({key: "true"}) == []
    assert settings_contract.invalid_client_setting_value_keys({key: "false"}) == []
    assert settings_contract.invalid_client_setting_value_keys({key: True}) == [key]


def test_every_browser_scalar_setting_has_one_strict_string_value_shape() -> None:
    valid_values = {
        "aef:chartViewMode": "classic",
        "aef:cursorMode": "normal",
        "aef:drawingHidden": "false",
        "aef:drawingMagnet": "true",
        "aef:economicCalendar": "true",
        "aef:followLatest": "true",
        "aef:gexSidebarWidth": "248",
        "aef:gexSidebarPlacement": "dock",
        "aef:workspaceDockSide": "left",
        "aef:workspaceDockWidth": "300",
        "aef:healthVisualizer": "candles",
        "aef:ibkrPort": "7497",
        "aef:indicatorLabelStyle": "text",
        "aef:motionPreference": "system",
        "aef:mtfLensAnchor": "bottom-left",
        "aef:mtfLensHeight": "320",
        "aef:mtfLensWidth": "560",
        "aef:paperEdgeGate": "true",
        "aef:paperEntryLabelFrameOffset": "520",
        "aef:paperMinRr": "1.25",
        "aef:paperShowTradesOnChart": "true",
        "aef:paperTelegramFeed": "false",
        "aef:priceShift": "-0.25",
        "aef:priceZoom": "1.25",
        "aef:rightGapBars": "0",
        "aef:rightGapManual": "false",
        "aef:sessionPriceOpacity": "1",
        "aef:sessionVolumeOpacity": "3",
        "aef:showBidAskOnCandle": "false",
        "aef:showCandleGrid": "false",
        "aef:showHorizontalGrid": "true",
        "aef:showVerticalGrid": "true",
        "aef:sideWidth": "360",
        "aef:signalDensity": "focus",
        "aef:signalsRange": "2d",
        "aef:tradeSetupRuntimeLayout": "full",
        "aef:uiShape": "rounded",
        "aef:volumeHeight": "160",
    }

    assert set(valid_values) == set(settings_contract._BROWSER_CLIENT_SCALAR_SETTING_KEYS)
    assert settings_contract.invalid_client_setting_value_keys(valid_values) == []
    assert (
        settings_contract.invalid_client_setting_value_keys({"aef:cursorMode": "informative"}) == []
    )
    for key in valid_values:
        for invalid_value in ({}, [], None, True, 1, float("nan"), "invalid"):
            assert settings_contract.invalid_client_setting_value_keys({key: invalid_value}) == [
                key
            ]
    assert settings_contract.invalid_persisted_client_setting_keys(
        {
            "aef:paperAutoTrading",
            "aef:ES:indicator:regular:optionTargetsLive",
        }
    ) == [
        "aef:ES:indicator:regular:optionTargetsLive",
        "aef:paperAutoTrading",
    ]


@pytest.mark.parametrize(
    ("key", "value"),
    (
        ("aef:ibkrPort", "0"),
        ("aef:ibkrPort", "65536"),
        ("aef:ibkrPort", "07497"),
        ("aef:paperEntryLabelFrameOffset", "520.0"),
        ("aef:paperEntryLabelFrameOffset", "1601"),
        ("aef:priceShift", "-0"),
        ("aef:priceShift", "5.01"),
        ("aef:priceZoom", "0.079"),
        ("aef:priceZoom", "1.0"),
        ("aef:rightGapBars", "721"),
        ("aef:sessionPriceOpacity", "1.5"),
        ("aef:gexSidebarWidth", "153"),
        ("aef:gexSidebarWidth", "421"),
        ("aef:workspaceDockWidth", "219"),
        ("aef:workspaceDockWidth", "641"),
        ("aef:workspaceDockWidth", "300.5"),
        ("aef:workspaceDockSide", "top"),
        ("aef:gexSidebarPlacement", "sidebar"),
        ("aef:mtfLensHeight", "189"),
        ("aef:mtfLensHeight", "1801"),
        ("aef:mtfLensWidth", "319"),
        ("aef:mtfLensWidth", "2401"),
        ("aef:sideWidth", "259"),
        ("aef:volumeHeight", "79"),
    ),
)
def test_browser_scalar_numbers_reject_noncanonical_or_out_of_range_values(
    key: str,
    value: str,
) -> None:
    assert settings_contract.invalid_client_setting_value_keys({key: value}) == [key]


def test_mtf_lens_preferences_use_canonical_size_and_anchor_contracts() -> None:
    valid_values = {
        settings_contract.MTF_LENS_WIDTH_SETTING_KEY: "320",
        settings_contract.MTF_LENS_HEIGHT_SETTING_KEY: "190",
        settings_contract.MTF_LENS_ANCHOR_SETTING_KEY: "bottom-left",
    }
    assert settings_contract.invalid_client_setting_value_keys(valid_values) == []
    assert (
        settings_contract.invalid_client_setting_value_keys(
            {
                settings_contract.MTF_LENS_WIDTH_SETTING_KEY: "2400",
                settings_contract.MTF_LENS_HEIGHT_SETTING_KEY: "1800",
                settings_contract.MTF_LENS_ANCHOR_SETTING_KEY: "top-right",
            }
        )
        == []
    )
    for anchor in settings_contract.MTF_LENS_ANCHORS:
        assert (
            settings_contract.invalid_client_setting_value_keys(
                {settings_contract.MTF_LENS_ANCHOR_SETTING_KEY: anchor}
            )
            == []
        )

    invalid_values = {
        settings_contract.MTF_LENS_WIDTH_SETTING_KEY: "0320",
        settings_contract.MTF_LENS_HEIGHT_SETTING_KEY: "190.0",
        settings_contract.MTF_LENS_ANCHOR_SETTING_KEY: "center",
    }
    assert settings_contract.invalid_client_setting_value_keys(invalid_values) == sorted(
        invalid_values
    )


def test_workspace_scalar_and_route_tuple_values_are_strict() -> None:
    instrument_id = "instrument-a"
    valid_values = {
        "aef:workspace:1:gexFocusMode": "false",
        "aef:workspace:1:instrumentId": instrument_id,
        "aef:workspace:1:sideTab": "instruments",
        "aef:workspace:1:theme": "dark",
        "aef:workspace:1:timeframe": "5m",
        "aef:workspace:1:viewPreset": "trading",
        f'aef:workspace:1:barsVisible:["{instrument_id}","1m"]': "120",
        f'aef:workspace:1:range:["{instrument_id}","1m"]': "31d",
        f'aef:workspace:2:range:["{instrument_id}","5m"]': "6mo",
        f'aef:workspace:3:range:["{instrument_id}","15m"]': "1y",
        f'aef:workspace:4:range:["{instrument_id}","60m"]': "5y",
    }

    def instrument_exists(candidate: str) -> bool:
        return candidate == instrument_id

    assert (
        settings_contract.invalid_client_setting_value_keys(
            valid_values,
            instrument_exists=instrument_exists,
        )
        == []
    )
    for key in valid_values:
        assert settings_contract.invalid_client_setting_value_keys({key: []}) == [key]

    invalid_values = {
        "aef:workspace:1:gexFocusMode": "1",
        "aef:workspace:1:instrumentId": 1,
        "aef:workspace:1:sideTab": "settings",
        "aef:workspace:1:theme": "system",
        "aef:workspace:1:timeframe": "1h",
        "aef:workspace:1:viewPreset": "gex-strike",
        f'aef:workspace:1:barsVisible:["{instrument_id}","5m"]': "23",
        f'aef:workspace:1:range:["{instrument_id}","1m"]': "1y",
    }
    assert settings_contract.invalid_client_setting_value_keys(invalid_values) == sorted(
        invalid_values
    )
    assert (
        settings_contract.is_current_client_setting(
            f'aef:workspace:1:range:["{instrument_id}","2m"]'
        )
        is False
    )


def test_instrument_scalar_settings_reject_fallback_json_and_coercion() -> None:
    instrument_id = "instrument-a"
    risk_prefs = json.dumps(
        {
            "orderType": "limit",
            "qty": 1,
            "useStopLoss": True,
            "stopPoints": 8,
            "useTarget": True,
            "targetPoints": 16,
        },
        separators=(",", ":"),
    )
    indicator_mode_key = f'aef:instrument:["{instrument_id}"]:indicatorMode'
    risk_key = f'aef:instrument:["{instrument_id}"]:paperRiskPrefs'

    assert (
        settings_contract.invalid_client_setting_value_keys(
            {indicator_mode_key: "regular", risk_key: risk_prefs},
            instrument_exists=lambda candidate: candidate == instrument_id,
        )
        == []
    )
    for invalid_value in (
        {},
        "{}",
        risk_prefs.replace('"qty":1', '"qty":"1"'),
        risk_prefs.replace('"qty":1', '"qty":101'),
        risk_prefs.replace('"orderType":"limit"', '"orderType":"fallback"'),
        risk_prefs.replace('"targetPoints":16', '"targetPoints":NaN'),
    ):
        assert settings_contract.invalid_client_setting_value_keys({risk_key: invalid_value}) == [
            risk_key
        ]
    assert settings_contract.invalid_client_setting_value_keys({indicator_mode_key: "advisor"}) == [
        indicator_mode_key
    ]


def test_instrument_paper_auto_trading_setting_is_exact_typed_and_cleanup_scoped() -> None:
    instrument_id = "ibkr|future_root|ES|CME|USD|ES"
    other_instrument_id = "coinbase|contract|BTC-USD"
    key = settings_contract.instrument_paper_auto_trading_setting_key(instrument_id)
    other_key = settings_contract.instrument_paper_auto_trading_setting_key(other_instrument_id)

    assert key == ('aef:instrument:["ibkr|future_root|ES|CME|USD|ES"]:paperAutoTrading')
    assert settings_contract.is_current_client_setting(key) is True
    assert settings_contract.is_browser_writable_client_setting(key) is True
    assert (
        settings_contract.invalid_client_setting_value_keys(
            {key: "true", other_key: "false"},
            instrument_exists=lambda candidate: candidate in {instrument_id, other_instrument_id},
        )
        == []
    )
    for invalid_value in (True, False, 1, 0, "1", "TRUE", "yes", "", None, {}):
        assert settings_contract.invalid_client_setting_value_keys(
            {key: invalid_value},
            instrument_exists=lambda candidate: candidate == instrument_id,
        ) == [key]

    assert settings_contract.client_setting_keys_for_instrument(
        {key: "true", other_key: "false"},
        instrument_id,
    ) == [key]
    assert settings_contract.invalid_client_setting_value_keys(
        {key: "true"},
        instrument_exists=lambda _candidate: False,
    ) == [key]


def test_every_registry_and_manual_indicator_key_has_a_typed_value_contract() -> None:
    instrument_id = "instrument-a"
    valid_values: dict[str, str] = {}
    for spec in INDICATOR_REGISTRY.values():
        for setting_key in (spec.calc_key, spec.visible_key):
            if not setting_key:
                continue
            if spec.settings_scope == "global":
                key = f"aef:indicator:global:{setting_key}"
            else:
                key = f'aef:instrument:["{instrument_id}","gex"]:indicator:{setting_key}'
            valid_values[key] = "true"
        for control in spec.controls:
            if control.scope == "global":
                key = f"aef:indicator:global:{control.storage_key}"
            else:
                key = f'aef:instrument:["{instrument_id}","gex"]:indicator:{control.storage_key}'
            if control.control_type == "toggle":
                value = "true"
            else:
                value = str(control.default)
            valid_values[key] = value

    for setting_key in settings_contract._GLOBAL_INDICATOR_BOOLEAN_KEYS:
        valid_values[f"aef:indicator:global:{setting_key}"] = "true"
    for setting_key in settings_contract._GLOBAL_INDICATOR_COLOR_KEYS:
        valid_values[f"aef:indicator:global:{setting_key}"] = "#12abef"
    for setting_key, options in settings_contract._GLOBAL_INDICATOR_ENUM_SETTING_VALUES.items():
        valid_values[f"aef:indicator:global:{setting_key}"] = sorted(options)[0]
    for field in GLOBAL_DEFAULT_FIELDS:
        valid_values[f"aef:indicator:global:{field.storage_key}"] = str(field.default)
    valid_values["aef:indicator:global:globalDefaultPreset"] = next(iter(GLOBAL_DEFAULT_PRESETS))
    manager_ids = sorted(settings_contract._indicator_value_contract()["manager_ids"])
    order_key = settings_contract._INSTRUMENT_INDICATOR_ORDER_KEY
    valid_values[f'aef:instrument:["{instrument_id}","gex"]:indicator:{order_key}'] = ",".join(
        manager_ids
    )
    for setting_key in settings_contract._INSTRUMENT_INDICATOR_BOOLEAN_KEYS:
        valid_values[f'aef:instrument:["{instrument_id}","gex"]:indicator:{setting_key}'] = "true"
    for (
        setting_key,
        options,
    ) in settings_contract._INSTRUMENT_INDICATOR_ENUM_SETTING_VALUES.items():
        valid_values[f'aef:instrument:["{instrument_id}","gex"]:indicator:{setting_key}'] = sorted(
            options
        )[0]

    covered_keys: dict[str, set[str]] = {"global": set(), "instrument": set()}
    for key in valid_values:
        indicator_ref = settings_contract._indicator_setting_ref(key)
        assert indicator_ref is not None
        covered_keys[indicator_ref[0]].add(indicator_ref[1])
    assert covered_keys == {
        scope: set(keys) for scope, keys in settings_contract._indicator_keys_by_scope().items()
    }
    assert all(settings_contract.is_current_client_setting(key) for key in valid_values)
    assert (
        settings_contract.invalid_client_setting_value_keys(
            valid_values,
            instrument_exists=lambda candidate: candidate == instrument_id,
        )
        == []
    )
    for key in valid_values:
        for invalid_value in ({}, [], float("nan")):
            assert settings_contract.invalid_client_setting_value_keys({key: invalid_value}) == [
                key
            ]


def test_indicator_metadata_enforces_enum_range_step_color_and_partial_order() -> None:
    prefix = 'aef:instrument:["instrument-a","regular"]:indicator:'
    number_key = f"{prefix}impulseFibMarkWidth"
    select_key = f"{prefix}impulseFibStyle"
    color_key = f"{prefix}barRadarTableBg"
    order_key = f"{prefix}indicatorSettingsOrder"
    manager_ids = sorted(settings_contract._indicator_value_contract()["manager_ids"])

    assert settings_contract.invalid_client_setting_value_keys({number_key: "1.25"}) == []
    assert settings_contract.invalid_client_setting_value_keys(
        {order_key: ",".join([*manager_ids, "gex_dynamics", "vsa_volume"])}
    ) == [order_key]
    assert (
        settings_contract.invalid_browser_client_setting_value_keys(
            {order_key: ",".join(manager_ids)}
        )
        == []
    )
    assert settings_contract.invalid_browser_client_setting_value_keys(
        {order_key: ",".join(["option_points", *manager_ids])}
    ) == [order_key]
    partial_order = ",".join(manager_ids[:-1])
    assert settings_contract.invalid_client_setting_value_keys({order_key: partial_order}) == []
    assert (
        settings_contract.invalid_browser_client_setting_value_keys({order_key: partial_order})
        == []
    )
    for key, value in (
        (number_key, "1.3"),
        (number_key, "4.25"),
        (select_key, "fallback"),
        (color_key, "#ABCDEF"),
        (color_key, "#fff"),
        (order_key, ",".join([*manager_ids, "unknown_indicator"])),
        (order_key, ",".join([*manager_ids, manager_ids[0]])),
    ):
        assert settings_contract.invalid_client_setting_value_keys({key: value}) == [key]


def test_instrument_indicator_selector_respects_the_active_mode() -> None:
    instrument_id = "ibkr|contract|756733"
    regular_key = settings_contract.instrument_indicator_setting_key(
        instrument_id,
        "discordSignalsCalcEnabled",
    )
    gex_key = settings_contract.instrument_indicator_setting_key(
        instrument_id,
        "discordSignalsCalcEnabled",
        mode="gex",
    )
    mode_key = settings_contract.instrument_indicator_mode_setting_key(instrument_id)

    assert settings_contract._indicator_setting_ref(gex_key) == (
        "instrument",
        "discordSignalsCalcEnabled",
        instrument_id,
        "gex",
    )
    assert settings_contract.instrument_ids_for_indicator_setting(
        {regular_key: "true", gex_key: "false"},
        "discordSignalsCalcEnabled",
    ) == (instrument_id,)
    assert (
        settings_contract.instrument_ids_for_indicator_setting(
            {mode_key: "gex", regular_key: "true", gex_key: "false"},
            "discordSignalsCalcEnabled",
        )
        == ()
    )


def test_removed_v12_tombstones_are_not_current_v15_settings() -> None:
    key = "aef:indicator:global:vsaVolumeCalcEnabled"
    gex_key = 'aef:instrument:["ibkr|contract|756733","gex"]:indicator:gexDynamicsCalcEnabled'

    assert settings_contract.invalid_persisted_client_setting_keys([key, gex_key]) == sorted(
        [key, gex_key]
    )
    assert settings_contract.invalid_client_setting_value_keys(
        {key: "false", gex_key: "true"}
    ) == sorted([key, gex_key])


def test_unknown_client_key_never_acquires_a_valid_value_shape() -> None:
    assert settings_contract.invalid_client_setting_value_keys(
        {"aef:unknownSetting": "anything"}
    ) == ["aef:unknownSetting"]
