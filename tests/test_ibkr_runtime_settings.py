from __future__ import annotations

import os

import pytest

from aef_terminal import config as config_module
from aef_terminal.config import AppConfig
from aef_terminal.runtime import ibkr_settings as runtime_settings
from aef_terminal.settings_contract import IBKR_PORT_SETTING_KEY
from aef_terminal.ui import ibkr_runtime


def _environment_snapshot() -> runtime_settings.IbkrRuntimeSettingsSnapshot:
    return runtime_settings.IbkrRuntimeSettingsSnapshot(
        port=7497,
        gex_port=4002,
        market_data_type=3,
        gex_market_data_type=4,
        generation=0,
        source="environment",
    )


def test_explicit_client_settings_publish_one_atomic_live_generation(monkeypatch) -> None:
    monkeypatch.setattr(runtime_settings, "_SNAPSHOT", _environment_snapshot())
    monkeypatch.setenv("AEF_IBKR_PORT", "1234")
    monkeypatch.setenv("AEF_IBKR_GEX_PORT", "2345")

    payload = ibkr_runtime.apply_ibkr_runtime_settings({IBKR_PORT_SETTING_KEY: "4003"})
    config = AppConfig()

    assert payload == {
        "ibkr_port": 4003,
        "ibkr_gex_port": 4003,
        "ibkr_market_data_type": 1,
        "ibkr_gex_market_data_type": 1,
        "generation": 1,
        "source": "client-settings",
    }
    assert config.ibkr_port == 4003
    assert config.ibkr_gex_port == 4003
    assert config.ibkr_market_data_type == 1
    assert config.ibkr_gex_market_data_type == 1
    assert runtime_settings.ibkr_runtime_settings_snapshot().generation == 1
    assert os.environ["AEF_IBKR_PORT"] == "1234"
    assert os.environ["AEF_IBKR_GEX_PORT"] == "2345"


def test_runtime_read_has_no_storage_or_generation_side_effect(monkeypatch) -> None:
    snapshot = _environment_snapshot()
    monkeypatch.setattr(runtime_settings, "_SNAPSHOT", snapshot)

    payload = ibkr_runtime.apply_ibkr_runtime_settings()

    assert payload == snapshot.payload()
    assert runtime_settings.ibkr_runtime_settings_snapshot() is snapshot


def test_persisted_settings_without_port_still_publish_live_mode(monkeypatch) -> None:
    monkeypatch.setattr(runtime_settings, "_SNAPSHOT", _environment_snapshot())

    payload = ibkr_runtime.apply_ibkr_runtime_settings({})

    assert payload["ibkr_port"] == 7497
    assert payload["ibkr_gex_port"] == 7497
    assert payload["ibkr_market_data_type"] == 1
    assert payload["ibkr_gex_market_data_type"] == 1
    assert payload["generation"] == 1


def test_persisted_settings_initialize_runtime_projection_before_application(
    monkeypatch,
) -> None:
    monkeypatch.setattr(runtime_settings, "_SNAPSHOT", _environment_snapshot())
    persisted = {IBKR_PORT_SETTING_KEY: "4003", "aef:motionPreference": "reduced"}
    mutation_orders = {
        key: {"changed_at_ms": 0, "writer_id": None, "sequence": 0} for key in persisted
    }
    events: list[tuple[str, object]] = []
    monkeypatch.setattr(
        ibkr_runtime,
        "_DEPS",
        ibkr_runtime.IbkrRuntimeDeps(
            load_client_settings_snapshot=lambda: (
                events.append(("load", None)) or (persisted, mutation_orders, 0, 7)
            ),
            publish_client_settings_snapshot=lambda settings, orders, revision: events.append(
                ("publish", (dict(settings), dict(orders), revision))
            ),
            clear_quote_wanted=lambda: None,
        ),
    )

    ibkr_runtime.apply_persisted_ibkr_runtime_settings()

    assert events == [
        ("load", None),
        ("publish", (persisted, mutation_orders, 7)),
    ]
    assert runtime_settings.ibkr_runtime_settings_snapshot().port == 4003


def test_app_config_captures_one_atomic_runtime_generation(monkeypatch) -> None:
    snapshots = (
        runtime_settings.IbkrRuntimeSettingsSnapshot(
            port=1001,
            gex_port=1002,
            market_data_type=1,
            gex_market_data_type=1,
            generation=1,
            source="test-a",
        ),
        runtime_settings.IbkrRuntimeSettingsSnapshot(
            port=2001,
            gex_port=2002,
            market_data_type=2,
            gex_market_data_type=2,
            generation=2,
            source="test-b",
        ),
    )
    reads = 0

    def alternating_snapshot() -> runtime_settings.IbkrRuntimeSettingsSnapshot:
        nonlocal reads
        snapshot = snapshots[reads % len(snapshots)]
        reads += 1
        return snapshot

    monkeypatch.setattr("aef_terminal.config.ibkr_runtime_settings_snapshot", alternating_snapshot)

    config = AppConfig()

    assert reads == 1
    assert (
        config.ibkr_port,
        config.ibkr_gex_port,
        config.ibkr_market_data_type,
        config.ibkr_gex_market_data_type,
    ) == (1001, 1002, 1, 1)


def test_environment_integer_uses_default_only_when_absent(monkeypatch) -> None:
    monkeypatch.delenv("AEF_TEST_INTEGER", raising=False)
    assert (
        runtime_settings._environment_int(
            "AEF_TEST_INTEGER",
            17,
            lower=1,
            upper=20,
        )
        == 17
    )

    for value in ("", "invalid", "0", "21", "1.5"):
        monkeypatch.setenv("AEF_TEST_INTEGER", value)
        with pytest.raises(ValueError, match="AEF_TEST_INTEGER_INVALID"):
            runtime_settings._environment_int(
                "AEF_TEST_INTEGER",
                17,
                lower=1,
                upper=20,
            )


@pytest.mark.parametrize(
    ("value", "expected"),
    (
        ("1", True),
        ("true", True),
        ("YES", True),
        ("on", True),
        ("0", False),
        ("false", False),
        ("NO", False),
        ("off", False),
    ),
)
def test_environment_boolean_accepts_only_explicit_values(
    monkeypatch,
    value: str,
    expected: bool,
) -> None:
    monkeypatch.setenv("AEF_TEST_BOOLEAN", value)

    assert config_module._env_bool("AEF_TEST_BOOLEAN") is expected


@pytest.mark.parametrize("value", ("", "maybe", "2", " true "))
def test_environment_boolean_rejects_ambiguous_values(monkeypatch, value: str) -> None:
    monkeypatch.setenv("AEF_TEST_BOOLEAN", value)

    with pytest.raises(ValueError, match="AEF_TEST_BOOLEAN_INVALID"):
        config_module._env_bool("AEF_TEST_BOOLEAN")
