from __future__ import annotations

import asyncio
import json
import socket
import struct
import threading
from collections.abc import Mapping
from datetime import UTC, date, datetime, tzinfo
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from pytest import MonkeyPatch
from starlette.testclient import TestClient

from aef_terminal.indicators.domain_facts import validate_indicator_fact_fields
from aef_terminal.indicators.modules.discord_signals import INDICATOR_MODULE
from aef_terminal.indicators.modules.discord_signals.contracts import (
    DiscordFeedMessage,
    DiscordSignalAction,
    DiscordSignalCue,
    DiscordSignalParseStatus,
    DiscordSignalProfile,
    require_discord_snowflakes,
)
from aef_terminal.indicators.modules.discord_signals.companion import (
    DiscordIpcConnection,
    DiscordRpcCompanion,
    DiscordRpcCompanionConfig,
    TerminalBridgePublisher,
)
from aef_terminal.indicators.modules.discord_signals.rpc_bridge import (
    DISCORD_RPC_BRIDGE_CONTRACT,
    DiscordSignalFeed,
    DiscordSignalFeedConfig,
)
from aef_terminal.indicators.modules.discord_signals import rpc_bridge as discord_rpc_bridge
from aef_terminal.indicators.modules.discord_signals.lifecycle import (
    project_discord_signal_lifecycle,
)
from aef_terminal.indicators.modules.discord_signals.parser import (
    parse_discord_feed,
    parse_discord_message,
)
from aef_terminal.indicators.modules.discord_signals.service import (
    configure_service,
    create_router,
    discord_signal_payload,
    discord_signal_live_payload,
)
from aef_terminal.indicators.modules.discord_signals import service as discord_service
from aef_terminal.indicators.registry import indicator_manifest
from aef_terminal.settings_contract import instrument_ids_for_indicator_setting


TEST_PROFILE = DiscordSignalProfile(
    source_id="gaa_test",
    author_id="test:gaa",
    author_name="GAA",
    channel_ids=("test:signals",),
)


TEST_MESSAGES: tuple[dict[str, object], ...] = (
    {
        "message_id": "test:2026-07-28:trim-20",
        "channel_id": TEST_PROFILE.channel_ids[0],
        "author_id": TEST_PROFILE.author_id,
        "author_name": TEST_PROFILE.author_name,
        "published_at": "2026-07-28T14:34:00Z",
        "content": "@everyone trim $spx 7430p @ 20\nHolding just 2 on breakeven now",
    },
    {
        "message_id": "test:2026-07-28:trim-22",
        "channel_id": TEST_PROFILE.channel_ids[0],
        "author_id": TEST_PROFILE.author_id,
        "author_name": TEST_PROFILE.author_name,
        "published_at": "2026-07-28T15:10:00Z",
        "content": "@everyone trim $spx 7430p @ 22\nHalf remaining on breakeven stops",
    },
    {
        "message_id": "test:2026-07-28:sold-26-5",
        "channel_id": TEST_PROFILE.channel_ids[0],
        "author_id": TEST_PROFILE.author_id,
        "author_name": TEST_PROFILE.author_name,
        "published_at": "2026-07-28T15:18:00Z",
        "content": "@everyone Sold $SPX 7430p July 28 @ 26.5\n\nWe happy?",
    },
    {
        "message_id": "test:2026-07-30:entry-7370c",
        "channel_id": TEST_PROFILE.channel_ids[0],
        "author_id": TEST_PROFILE.author_id,
        "author_name": TEST_PROFILE.author_name,
        "published_at": "2026-07-30T14:00:00Z",
        "content": "7370c @ 13.3",
    },
)


def _message_row(
    message_id: str,
    published_at: str,
    content: str,
    *,
    reply_to_message_id: str | None = None,
) -> dict[str, object]:
    row: dict[str, object] = {
        "message_id": message_id,
        "channel_id": TEST_PROFILE.channel_ids[0],
        "author_id": TEST_PROFILE.author_id,
        "author_name": TEST_PROFILE.author_name,
        "published_at": published_at,
        "content": content,
    }
    if reply_to_message_id is not None:
        row["reply_to_message_id"] = reply_to_message_id
    return row


def test_discord_signals_is_an_independent_dormant_service_module() -> None:
    spec = INDICATOR_MODULE.spec
    manifest = indicator_manifest()["discord_signals"]

    assert spec.pipeline_stage == "ui"
    assert spec.module_type == "ui-only"
    assert spec.settings_scope == "instrument"
    assert spec.default_calc is False
    assert spec.paper_tradable is False
    assert spec.candidate_promoter == "none"
    assert spec.overlay_collect is False
    assert spec.settings_status_ref == "discord_signals_connection"
    assert spec.renderer_kind == "service"
    assert spec.renderer_primitives == ("marker", "line")
    assert all(control.scope == "instrument" for control in spec.controls)
    assert [control.key for control in spec.controls] == [
        "entries",
        "exits",
        "trims",
        "management",
        "tradePaths",
        "directionalLevels",
        "textSize",
    ]
    assert all(control.compact is False for control in spec.controls)
    text_size = next(control for control in spec.controls if control.key == "textSize")
    assert text_size.default == "medium"
    assert text_size.options == ("small", "medium", "large")
    assert manifest["service_ref"].startswith("aef_terminal.indicators.modules.discord_signals.")
    assert manifest["extensions"]["router_ref"].startswith(
        "aef_terminal.indicators.modules.discord_signals."
    )
    assert manifest["extensions"]["ui_js_assets"] == ["client.js"]
    assert manifest["ui"]["settings_status_ref"] == "discord_signals_connection"


def test_parser_emits_contract_and_management_events_from_one_message() -> None:
    message = DiscordFeedMessage.from_mapping(TEST_MESSAGES[0])

    events = parse_discord_message(message, TEST_PROFILE)

    assert [event.action for event in events] == [
        DiscordSignalAction.REDUCE,
        DiscordSignalAction.POSITION_UPDATE,
    ]
    contract, management = events
    assert contract.source_underlying == "SPX"
    assert contract.expiry == "2026-07-28"
    assert contract.strike == 7430.0
    assert contract.option_right == "put"
    assert contract.reported_premium == 20.0
    assert management.quantity == 2
    assert management.stop_mode == "breakeven"


def test_parser_uses_profile_0dte_only_for_this_bound_source() -> None:
    message = DiscordFeedMessage.from_mapping(TEST_MESSAGES[-1])

    (event,) = parse_discord_message(message, TEST_PROFILE)

    assert event.action is DiscordSignalAction.ENTRY
    assert event.source_underlying is None
    assert event.expiry == "2026-07-30"
    assert event.strike == 7370.0
    assert event.option_right == "call"
    assert event.reported_premium == 13.3
    assert event.parse_status.value == "parsed_unqualified"
    assert event.reason_code == "provider_contract_not_qualified"


def test_parser_emits_each_numeric_directional_level_as_a_typed_advisory() -> None:
    message = DiscordFeedMessage.from_mapping(
        _message_row(
            "directional-pair",
            "2026-07-27T04:19:22Z",
            "@everyone no trade has set up yet\nIt’s either calls over 745 or puts below 742.8",
        )
    )

    events = parse_discord_message(message, TEST_PROFILE)

    assert [event.action for event in events] == [
        DiscordSignalAction.DIRECTIONAL_LEVEL,
        DiscordSignalAction.DIRECTIONAL_LEVEL,
    ]
    assert [
        (
            event.option_right,
            event.directional_condition,
            event.directional_level,
            event.directional_reference,
        )
        for event in events
    ] == [
        ("call", "above", 745.0, "price"),
        ("put", "below", 742.8, "price"),
    ]
    assert all(event.parse_status is DiscordSignalParseStatus.PARSED_CONTEXT for event in events)
    assert {event.directional_confirmation for event in events} == {"touch"}


def test_parser_types_dynamic_directional_references_without_inventing_prices() -> None:
    message = DiscordFeedMessage.from_mapping(
        _message_row(
            "directional-dynamic",
            "2026-07-23T14:00:23Z",
            "Below LOD is puts\nAbove HOD is calls\nRange in between",
        )
    )

    events = parse_discord_message(message, TEST_PROFILE)

    assert [
        (
            event.option_right,
            event.directional_condition,
            event.directional_level,
            event.directional_reference,
        )
        for event in events
    ] == [
        ("put", "below", None, "lod"),
        ("call", "above", None, "hod"),
    ]
    assert {event.directional_confirmation for event in events} == {"touch"}


def test_parser_preserves_a_session_close_directional_confirmation() -> None:
    (event,) = parse_discord_message(
        DiscordFeedMessage.from_mapping(
            _message_row(
                "directional-eod-close",
                "2026-07-10T18:09:35Z",
                "If we close eod below 753/ take weekend puts",
            )
        ),
        TEST_PROFILE,
    )

    assert event.action is DiscordSignalAction.DIRECTIONAL_LEVEL
    assert event.directional_level == 753.0
    assert event.directional_confirmation == "session_close"


def test_parser_rejects_foreign_directional_underlying_and_ignores_exit_language() -> None:
    (foreign,) = parse_discord_message(
        DiscordFeedMessage.from_mapping(
            _message_row(
                "iwm-level",
                "2026-07-15T19:19:27Z",
                "Another setup is IWM puts below 295.5",
            )
        ),
        TEST_PROFILE,
    )
    (exit_commentary,) = parse_discord_message(
        DiscordFeedMessage.from_mapping(
            _message_row(
                "call-exit-level",
                "2026-07-27T14:48:15Z",
                "Anything over 739 is a sell on these calls",
            )
        ),
        TEST_PROFILE,
    )

    assert foreign.action is DiscordSignalAction.DIRECTIONAL_LEVEL
    assert foreign.source_underlying == "IWM"
    assert foreign.parse_status is DiscordSignalParseStatus.REJECTED
    assert exit_commentary.action is DiscordSignalAction.COMMENTARY


def test_parser_does_not_treat_ordinary_be_as_breakeven() -> None:
    message = DiscordFeedMessage.from_mapping(
        _message_row(
            "ordinary-be",
            "2026-07-30T14:01:00Z",
            "He will be in the US next month",
        )
    )

    (event,) = parse_discord_message(message, TEST_PROFILE)

    assert event.action is DiscordSignalAction.COMMENTARY
    assert event.reason_code == "no_typed_trade_event"


def test_parser_preserves_reported_1dte_without_inventing_expiry_calendar() -> None:
    message = DiscordFeedMessage.from_mapping(
        _message_row(
            "reported-1dte",
            "2026-07-27T17:06:00Z",
            "SPY 737c 1dte @ 2.62",
        )
    )

    (event,) = parse_discord_message(message, TEST_PROFILE)

    assert event.action is DiscordSignalAction.ENTRY
    assert event.source_underlying == "SPY"
    assert event.expiry is None
    assert event.reported_dte == 1
    assert event.contract_key == (
        "2026-07-27:reported_dte:1",
        737.0,
        "call",
    )
    assert event.parse_status.value == "parsed_unqualified"


def test_explicit_future_expiry_overrides_0dte_profile_default() -> None:
    message = DiscordFeedMessage.from_mapping(
        _message_row(
            "future-expiry",
            "2026-07-24T15:00:00Z",
            "@everyone #alert I added $SPY 735p July 31 @ 3.1",
        )
    )

    (event,) = parse_discord_message(message, TEST_PROFILE)

    assert event.parse_status is DiscordSignalParseStatus.PARSED_UNQUALIFIED
    assert event.expiry == "2026-07-31"


def test_passive_market_language_does_not_emit_bare_exit() -> None:
    for index, content in enumerate(("Options closed now", "Ya see every bounce being sold")):
        (event,) = parse_discord_message(
            DiscordFeedMessage.from_mapping(
                _message_row(
                    f"exit-noise-{index}",
                    "2026-07-24T15:00:00Z",
                    content,
                )
            ),
            TEST_PROFILE,
        )

        assert event.action is DiscordSignalAction.COMMENTARY


def test_sold_three_fifths_is_a_partial_reduction() -> None:
    (event,) = parse_discord_message(
        DiscordFeedMessage.from_mapping(
            _message_row(
                "sold-three-fifths",
                "2026-07-24T15:00:00Z",
                "I sold 3/5",
            )
        ),
        TEST_PROFILE,
    )

    assert event.action is DiscordSignalAction.REDUCE
    assert event.remaining_fraction == 0.4


def test_entry_quantity_supports_explicit_contract_phrases() -> None:
    for index, (content, expected) in enumerate(
        (
            ("@everyone #alert I added $SPY 739c 1dte @ 2.6\nJust 2 cons", 2),
            ("@everyone #alert I added $SPY 739p 0dte @ .38\nOnly taking 10 cons", 10),
            ("@everyone #alert I added $SPX 7430p @ 7.7\n3 contracts", 3),
            ("7400p may 13 @ 10.3\nSL @ 739.9\nJust took 1", 1),
            ("7350p @ 14.9\nYolo just 1", 1),
        )
    ):
        events = parse_discord_message(
            DiscordFeedMessage.from_mapping(
                _message_row(
                    f"entry-quantity-{index}",
                    "2026-07-30T14:00:00Z",
                    content,
                )
            ),
            TEST_PROFILE,
        )

        event = next(item for item in events if item.action is DiscordSignalAction.ENTRY)
        assert event.quantity == expected


def test_numeric_sold_fraction_and_leading_decimal_are_typed_reduction() -> None:
    (event,) = parse_discord_message(
        DiscordFeedMessage.from_mapping(
            _message_row(
                "sold-five-of-ten",
                "2026-07-30T14:10:00Z",
                "@everyone sold 5/10 $spy 739p @ .52",
            )
        ),
        TEST_PROFILE,
    )

    assert event.action is DiscordSignalAction.REDUCE
    assert event.reported_premium == 0.52
    assert event.remaining_fraction == 0.5


def test_sold_contract_with_positive_remainder_is_not_a_full_exit() -> None:
    (event,) = parse_discord_message(
        DiscordFeedMessage.from_mapping(
            _message_row(
                "sold-with-remainder",
                "2026-07-30T14:10:00Z",
                "Sold $spy 739p @ .61\nDown to 2 on 0.50 stops",
            )
        ),
        TEST_PROFILE,
    )

    assert event.action is DiscordSignalAction.REDUCE
    assert event.reported_premium == 0.61
    assert event.quantity == 2


def test_bare_exit_for_foreign_underlying_is_rejected() -> None:
    (event,) = parse_discord_message(
        DiscordFeedMessage.from_mapping(
            _message_row(
                "foreign-bare-exit",
                "2026-07-24T15:00:00Z",
                "Sold my IWM puts 100%",
            )
        ),
        TEST_PROFILE,
    )

    assert event.action is DiscordSignalAction.EXIT
    assert event.parse_status is DiscordSignalParseStatus.REJECTED
    assert event.reason_code == "source_underlying_mismatch"
    assert event.source_underlying == "IWM"


def test_author_holding_through_hit_stop_does_not_close_position() -> None:
    rows = [
        _message_row(
            "hold-through-entry",
            "2026-07-07T17:20:00Z",
            "$SPX 7515p @ 6",
        ),
        _message_row(
            "hold-through-stop",
            "2026-07-07T17:31:00Z",
            "Breakeven stop hit if you wanna manage risk\nIma hold",
            reply_to_message_id="hold-through-entry",
        ),
    ]

    events = parse_discord_feed(rows, TEST_PROFILE)
    projection = project_discord_signal_lifecycle(
        events,
        as_of_session_date=date(2026, 7, 7),
    )

    stop_event = next(event for event in events if event.stop_mode == "not_executed")
    assert stop_event.action is DiscordSignalAction.STOP_UPDATE
    assert projection["positions"][0]["state"] == "open"
    assert projection["stats"]["explicit_stop_positions"] == 0
    assert projection["stats"]["explicit_stop_non_execution_events"] == 1


def test_future_bare_management_language_is_commentary() -> None:
    for index, content in enumerate(
        (
            "Next trim will be around 749.4",
            "I’d trim them btw",
            "I’ll cut below that only",
            "I was waiting to add 10",
            "Ima wait for a retrace and take a SL out",
        )
    ):
        (event,) = parse_discord_message(
            DiscordFeedMessage.from_mapping(
                _message_row(
                    f"future-management-{index}",
                    "2026-07-24T15:00:00Z",
                    content,
                )
            ),
            TEST_PROFILE,
        )

        assert event.action is DiscordSignalAction.COMMENTARY


def test_stop_discussion_and_subscriber_stop_are_commentary() -> None:
    for index, content in enumerate(
        (
            "Stop this money bullshit talk",
            "Grind don’t stop",
            "You stopped out for $10 loss when I said SL is 2.3?",
            "Why do you think they stopped the pump where they did",
        )
    ):
        (event,) = parse_discord_message(
            DiscordFeedMessage.from_mapping(
                _message_row(
                    f"stop-discussion-{index}",
                    "2026-07-24T15:00:00Z",
                    content,
                )
            ),
            TEST_PROFILE,
        )

        assert event.action is DiscordSignalAction.COMMENTARY


def test_future_stop_execution_language_does_not_close_position() -> None:
    for index, content in enumerate(
        (
            "We gotta hold or I’m gonna have to sl out",
            "If it doesn’t SL out yes",
            "Might early SL out",
            "Will SL out in 2min if doesn’t hold",
        )
    ):
        (event,) = parse_discord_message(
            DiscordFeedMessage.from_mapping(
                _message_row(
                    f"future-stop-{index}",
                    "2026-07-24T15:00:00Z",
                    content,
                )
            ),
            TEST_PROFILE,
        )

        assert event.action is DiscordSignalAction.COMMENTARY


def test_alert_added_contract_is_an_initial_entry() -> None:
    (event,) = parse_discord_message(
        DiscordFeedMessage.from_mapping(
            _message_row(
                "alert-added-entry",
                "2026-07-24T15:00:00Z",
                "@everyone #alert I added $SPY 745p July 31 @ 2.22",
            )
        ),
        TEST_PROFILE,
    )

    assert event.action is DiscordSignalAction.ENTRY
    assert event.expiry == "2026-07-31"


def test_contract_intent_is_advisory_not_an_entry() -> None:
    (event,) = parse_discord_message(
        DiscordFeedMessage.from_mapping(
            _message_row(
                "contract-intent",
                "2026-07-24T15:00:00Z",
                "Fighting the urge to take this 7520p @ 11.8",
            )
        ),
        TEST_PROFILE,
    )

    assert event.action is DiscordSignalAction.COMMENTARY
    assert event.parse_status is DiscordSignalParseStatus.COMMENTARY
    assert event.reason_code == "contract_discussion_only"


def test_exact_unmatched_contract_does_not_close_focused_position() -> None:
    rows = [
        _message_row(
            "focused-entry",
            "2026-07-24T15:00:00Z",
            "$SPX 7520p @ 11.8",
        ),
        _message_row(
            "different-contract-exit",
            "2026-07-24T15:05:00Z",
            "Stopped out $SPY 745p July 31 @ 2.02",
        ),
    ]

    projection = project_discord_signal_lifecycle(
        parse_discord_feed(rows, TEST_PROFILE),
        as_of_session_date=date(2026, 7, 24),
    )

    assert projection["positions"][0]["state"] == "open"
    assert projection["stats"]["unmatched_count"] == 1


def test_stale_relative_dte_position_is_not_closed_by_later_bare_exit() -> None:
    rows = [
        _message_row(
            "relative-entry",
            "2026-07-27T15:00:00Z",
            "$SPY 739c 1dte @ 3.4",
        ),
        _message_row(
            "later-bare-exit",
            "2026-07-31T15:00:00Z",
            "Sold all calls",
        ),
    ]

    projection = project_discord_signal_lifecycle(
        parse_discord_feed(rows, TEST_PROFILE),
        as_of_session_date=date(2026, 7, 31),
    )

    assert projection["positions"][0]["state"] == "open"
    assert projection["stats"]["unmatched_count"] == 1


def test_mixed_case_foreign_underlying_is_rejected() -> None:
    (event,) = parse_discord_message(
        DiscordFeedMessage.from_mapping(
            _message_row(
                "mixed-foreign-exit",
                "2026-07-24T15:00:00Z",
                "Sold my IWm puts 100%",
            )
        ),
        TEST_PROFILE,
    )

    assert event.parse_status is DiscordSignalParseStatus.REJECTED
    assert event.source_underlying == "IWM"


def test_candle_close_language_is_not_a_trade_exit() -> None:
    for index, content in enumerate(("Closed below", "lol closed over last second")):
        (event,) = parse_discord_message(
            DiscordFeedMessage.from_mapping(
                _message_row(
                    f"candle-close-{index}",
                    "2026-07-24T15:00:00Z",
                    content,
                )
            ),
            TEST_PROFILE,
        )

        assert event.action is DiscordSignalAction.COMMENTARY


def test_bare_sold_number_is_reported_exit_price() -> None:
    (event,) = parse_discord_message(
        DiscordFeedMessage.from_mapping(
            _message_row(
                "bare-sold-price",
                "2026-07-24T15:00:00Z",
                "Sold 16",
            )
        ),
        TEST_PROFILE,
    )

    assert event.action is DiscordSignalAction.EXIT
    assert event.reported_premium == 16.0


def test_breakeven_stop_keeps_stop_mechanism_and_zero_pnl_direction() -> None:
    rows = [
        _message_row(
            "be-stop-entry",
            "2026-07-24T15:00:00Z",
            "$SPX 7500p @ 10",
        ),
        _message_row(
            "be-stop-exit",
            "2026-07-24T15:05:00Z",
            "I personally got my BE stops hit on puts",
        ),
    ]

    events = parse_discord_feed(rows, TEST_PROFILE)
    projection = project_discord_signal_lifecycle(
        events,
        as_of_session_date=date(2026, 7, 24),
    )
    position = projection["positions"][0]

    assert position["state"] == "closed"
    assert position["outcome_category"] == "stop"
    assert position["pnl_direction"] == "breakeven"


def test_entry_price_is_selected_after_contract_not_from_prior_stop() -> None:
    message = DiscordFeedMessage.from_mapping(
        _message_row(
            "entry-with-stop-first",
            "2026-07-22T03:01:00Z",
            "Doing 1 with stops @ 14\n7500p @ 15.6",
        )
    )

    events = parse_discord_message(message, TEST_PROFILE)

    assert events[0].action is DiscordSignalAction.ENTRY
    assert events[0].reported_premium == 15.6
    assert events[1].action is DiscordSignalAction.STOP_UPDATE
    assert events[1].stop_value == 14.0


def test_partial_sold_and_reply_add_are_not_full_exits_or_duplicate_entries() -> None:
    rows = (
        _message_row("entry", "2026-07-30T14:00:00Z", "$SPX 7370c @ 10"),
        _message_row(
            "add",
            "2026-07-30T14:02:00Z",
            "Adding @ 8",
            reply_to_message_id="entry",
        ),
        _message_row(
            "partial",
            "2026-07-30T14:10:00Z",
            "Sold half $SPX 7370c @ 15",
        ),
    )

    events = parse_discord_feed(rows, TEST_PROFILE)
    projection = project_discord_signal_lifecycle(
        events,
        as_of_session_date=date(2026, 7, 30),
    )

    assert [event.action for event in events] == [
        DiscordSignalAction.ENTRY,
        DiscordSignalAction.ADD,
        DiscordSignalAction.REDUCE,
    ]
    position = projection["positions"][0]
    assert position["state"] == "open"
    assert position["remaining_fraction"] == 0.5
    assert position["entry_average_unknown"] is True
    assert projection["event_links"]["add:0"]["lifecycle_status"] == "added"


def test_declared_stop_does_not_close_until_explicit_stop_execution() -> None:
    rows = (
        _message_row(
            "entry",
            "2026-07-30T14:00:00Z",
            "$SPX 7370c @ 10\nSL @ 8",
        ),
        _message_row(
            "move-stop",
            "2026-07-30T14:05:00Z",
            "Moving SL to 9",
        ),
    )

    projection = project_discord_signal_lifecycle(
        parse_discord_feed(rows, TEST_PROFILE),
        as_of_session_date=date(2026, 7, 30),
    )

    position = projection["positions"][0]
    assert position["state"] == "open"
    assert position["stop_declaration_count"] == 2
    assert position["stop_override_count"] == 1
    assert projection["stats"]["declared_stop_positions"] == 1
    assert projection["stats"]["explicit_stop_positions"] == 0


def test_explicit_stop_exit_has_separate_outcome_and_pnl_direction() -> None:
    rows = (
        _message_row(
            "entry",
            "2026-07-30T14:00:00Z",
            "$SPX 7370c @ 10\nSL @ 8",
        ),
        _message_row(
            "stop-out",
            "2026-07-30T14:10:00Z",
            "SL out @ 7\n-30%",
        ),
    )

    projection = project_discord_signal_lifecycle(
        parse_discord_feed(rows, TEST_PROFILE),
        as_of_session_date=date(2026, 7, 30),
    )

    position = projection["positions"][0]
    assert position["state"] == "closed"
    assert position["outcome_category"] == "stop"
    assert position["pnl_direction"] == "loss"
    assert position["author_reported_return_pct"] == -30.0
    assert projection["stats"]["outcome_counts"]["stop"] == 1
    assert projection["stats"]["pnl_direction_counts"]["loss"] == 1


def test_reply_hit_confirms_the_specific_declared_stop() -> None:
    rows = (
        _message_row("entry", "2026-07-30T14:00:00Z", "$SPX 7370c @ 10"),
        _message_row(
            "stop",
            "2026-07-30T14:02:00Z",
            "Move SL to 9",
        ),
        _message_row(
            "hit",
            "2026-07-30T14:03:00Z",
            "Hit\nMight be too tight",
            reply_to_message_id="stop",
        ),
    )

    events = parse_discord_feed(rows, TEST_PROFILE)
    projection = project_discord_signal_lifecycle(
        events,
        as_of_session_date=date(2026, 7, 30),
    )

    hit_event = next(event for event in events if event.message_id == "hit")
    assert hit_event.action is DiscordSignalAction.COMMENTARY
    assert hit_event.context_cue is DiscordSignalCue.STOP_HIT
    assert hit_event.exit_reason == "stop"
    assert projection["positions"][0]["state"] == "closed"
    assert projection["stats"]["explicit_stop_positions"] == 1


def test_parser_rejects_source_identity_and_underlying_mismatches() -> None:
    wrong_author = DiscordFeedMessage.from_mapping(
        {
            **TEST_MESSAGES[-1],
            "message_id": "wrong-author",
            "author_id": "someone-else",
        }
    )
    wrong_underlying = DiscordFeedMessage.from_mapping(
        _message_row(
            "wrong-underlying",
            "2026-07-30T14:01:00Z",
            "$NDX 7370c @ 13.3",
        )
    )

    (author_event,) = parse_discord_message(wrong_author, TEST_PROFILE)
    (underlying_event,) = parse_discord_message(
        wrong_underlying,
        TEST_PROFILE,
    )

    assert author_event.parse_status.value == "rejected"
    assert author_event.reason_code == "source_identity_mismatch"
    assert underlying_event.parse_status.value == "rejected"
    assert underlying_event.reason_code == "source_underlying_mismatch"
    projection = project_discord_signal_lifecycle(
        (underlying_event,),
        as_of_session_date=date(2026, 7, 30),
    )
    assert projection["positions"] == []
    assert projection["event_links"][underlying_event.event_id] == {
        "lifecycle_status": "advisory_only",
        "position_id": "",
    }


def test_lifecycle_separates_calculated_return_from_author_report() -> None:
    rows = (
        _message_row("entry", "2026-07-30T14:00:00Z", "$SPX 7370c @ 10"),
        _message_row(
            "exit",
            "2026-07-30T14:30:00Z",
            "Sold $SPX 7370c July 30 @ 20",
        ),
    )
    events = parse_discord_feed(rows, TEST_PROFILE)

    projection = project_discord_signal_lifecycle(
        events,
        as_of_session_date=date(2026, 7, 30),
    )

    assert projection["stats"]["author_reported_return_trades"] == 0
    assert projection["stats"]["mean_author_reported_return_pct"] is None
    assert projection["stats"]["calculated_complete_trades"] == 1
    assert projection["stats"]["mean_calculated_return_pct"] == 100.0
    assert projection["positions"] == [
        {
            **projection["positions"][0],
            "state": "closed",
            "entry_premium": 10.0,
            "exit_premium": 20.0,
            "author_reported_return_pct": None,
            "calculated_points": 10.0,
            "calculated_return_pct": 100.0,
        }
    ]


def test_sold_percentage_is_reported_return_not_option_premium() -> None:
    rows = (
        _message_row("entry", "2026-06-09T18:40:00Z", "Mega lotto\nSPX 7340p @ 4.2"),
        _message_row("exit", "2026-06-09T19:42:00Z", "Sold 200%"),
    )

    events = parse_discord_feed(rows, TEST_PROFILE)
    projection = project_discord_signal_lifecycle(
        events,
        as_of_session_date=date(2026, 6, 9),
    )

    exit_event = next(event for event in events if event.message_id == "exit")
    position = projection["positions"][0]
    assert exit_event.action is DiscordSignalAction.EXIT
    assert exit_event.reported_premium is None
    assert exit_event.reported_return_pct == 200.0
    assert position["state"] == "closed"
    assert position["exit_premium"] is None
    assert position["author_reported_return_pct"] == 200.0
    assert position["calculated_return_pct"] is None
    assert position["pnl_direction"] == "profit"


def test_unknown_trim_size_blocks_complete_calculated_pnl() -> None:
    rows = (
        _message_row("entry", "2026-07-30T14:00:00Z", "$SPX 7370c @ 10"),
        _message_row("trim", "2026-07-30T14:15:00Z", "trim $SPX 7370c @ 15"),
        _message_row(
            "exit",
            "2026-07-30T14:30:00Z",
            "Sold $SPX 7370c July 30 @ 20",
        ),
    )

    projection = project_discord_signal_lifecycle(
        parse_discord_feed(rows, TEST_PROFILE),
        as_of_session_date=date(2026, 7, 30),
    )

    assert projection["positions"][0]["partial_exit_size_unknown"] is True
    assert projection["positions"][0]["calculated_return_pct"] is None
    assert projection["stats"]["calculated_complete_trades"] == 0


def test_same_contract_entry_is_a_rebroadcast_not_a_second_position() -> None:
    rows = (
        _message_row("entry-1", "2026-07-30T14:00:00Z", "$SPX 7370c @ 10"),
        _message_row("entry-2", "2026-07-30T14:01:00Z", "$SPX 7370c @ 11"),
    )

    projection = project_discord_signal_lifecycle(
        parse_discord_feed(rows, TEST_PROFILE),
        as_of_session_date=date(2026, 7, 30),
    )

    assert len(projection["positions"]) == 1
    assert projection["stats"]["unmatched_count"] == 0
    assert projection["event_links"]["entry-2:0"] == {
        "lifecycle_status": "entry_rebroadcast",
        "position_id": projection["positions"][0]["position_id"],
        "link_basis": "same_contract",
        "effective_action": "commentary",
    }


def test_feed_deduplicates_exact_messages_and_rejects_conflicting_identity() -> None:
    row = _message_row("same-message", "2026-07-30T14:00:00Z", "$SPX 7370c @ 10")

    events = parse_discord_feed((row, dict(row)), TEST_PROFILE)

    assert [event.event_id for event in events] == ["same-message:0"]
    try:
        parse_discord_feed(
            (row, {**row, "content": "$SPX 7370c @ 11"}),
            TEST_PROFILE,
        )
    except ValueError as exc:
        assert str(exc) == "DISCORD_SIGNAL_MESSAGE_CONFLICT"
    else:
        raise AssertionError("conflicting Discord message identity was accepted")


def test_cross_channel_messages_replay_in_one_global_chronological_position() -> None:
    profile = DiscordSignalProfile(
        source_id="gaa_multichannel",
        author_id=TEST_PROFILE.author_id,
        author_name=TEST_PROFILE.author_name,
        channel_ids=("test:signals", "test:management"),
    )
    rows = (
        {
            **_message_row("hit", "2026-07-30T14:02:00Z", "Hit"),
            "channel_id": "test:signals",
        },
        {
            **_message_row("stop", "2026-07-30T14:01:00Z", "Move SL to 9"),
            "channel_id": "test:management",
        },
        {
            **_message_row("entry", "2026-07-30T14:00:00Z", "$SPX 7370c @ 10"),
            "channel_id": "test:signals",
        },
    )

    events = parse_discord_feed(rows, profile)
    projection = project_discord_signal_lifecycle(
        events,
        as_of_session_date=date(2026, 7, 30),
    )

    assert [event.message_id for event in events] == ["entry", "stop", "hit"]
    assert projection["positions"][0]["state"] == "closed"
    assert projection["positions"][0]["terminal_reason"] == "stop_hit"
    assert projection["stats"]["open_positions"] == 0
    assert projection["stats"]["explicit_stop_positions"] == 1


def test_cash_is_a_flat_cue_but_cash_account_text_is_commentary() -> None:
    rows = (
        _message_row("entry", "2026-07-30T14:00:00Z", "$SPX 7370c @ 10"),
        _message_row("cash-noise", "2026-07-30T14:01:00Z", "Use a cash account"),
        _message_row("cash", "2026-07-30T14:02:00Z", "Cash"),
    )

    events = parse_discord_feed(rows, TEST_PROFILE)
    projection = project_discord_signal_lifecycle(
        events,
        as_of_session_date=date(2026, 7, 30),
    )

    cash_noise = next(event for event in events if event.message_id == "cash-noise")
    cash = next(event for event in events if event.message_id == "cash")
    assert cash_noise.context_cue is None
    assert cash.context_cue is DiscordSignalCue.FLAT
    assert cash.action is DiscordSignalAction.COMMENTARY
    assert projection["positions"][0]["state"] == "closed"
    assert projection["positions"][0]["terminal_reason"] == "flat_signal"
    assert projection["event_links"]["cash:0"]["effective_action"] == "exit"


def test_side_scoped_flatten_does_not_close_the_opposite_option_right() -> None:
    rows = (
        _message_row("entry", "2026-07-30T14:00:00Z", "$SPX 7370p @ 10"),
        _message_row("calls", "2026-07-30T14:01:00Z", "Sold all calls"),
        _message_row("puts", "2026-07-30T14:02:00Z", "Sold all puts"),
    )

    projection = project_discord_signal_lifecycle(
        parse_discord_feed(rows, TEST_PROFILE),
        as_of_session_date=date(2026, 7, 30),
    )

    assert projection["event_links"]["calls:0"]["lifecycle_status"] == ("active_scope_mismatch")
    assert projection["event_links"]["puts:0"]["lifecycle_status"] == "closed"
    assert projection["positions"][0]["state"] == "closed"


def test_bare_sale_requires_single_known_remaining_contract_to_close() -> None:
    rows = (
        _message_row(
            "entry",
            "2026-07-30T14:00:00Z",
            "$SPX 7370c @ 10\nTaking 2 contracts",
        ),
        _message_row("first-sold", "2026-07-30T14:01:00Z", "Sold"),
        _message_row("remaining", "2026-07-30T14:02:00Z", "Down to 1"),
        _message_row("last-sold", "2026-07-30T14:03:00Z", "Sold"),
    )

    projection = project_discord_signal_lifecycle(
        parse_discord_feed(rows, TEST_PROFILE),
        as_of_session_date=date(2026, 7, 30),
    )

    assert projection["event_links"]["first-sold:0"]["lifecycle_status"] == ("ambiguous_bare_sale")
    assert projection["event_links"]["remaining:0"]["lifecycle_status"] == ("position_updated")
    assert projection["event_links"]["last-sold:0"]["lifecycle_status"] == "closed"
    assert projection["positions"][0]["state"] == "closed"


def test_stale_reply_cannot_mutate_the_new_active_position() -> None:
    rows = (
        _message_row("entry-1", "2026-07-30T14:00:00Z", "$SPX 7370c @ 10"),
        _message_row("cash", "2026-07-30T14:01:00Z", "Cash"),
        _message_row("entry-2", "2026-07-30T14:02:00Z", "$SPX 7380p @ 12"),
        _message_row(
            "stale-exit",
            "2026-07-30T14:03:00Z",
            "Sold 15",
            reply_to_message_id="entry-1",
        ),
    )

    projection = project_discord_signal_lifecycle(
        parse_discord_feed(rows, TEST_PROFILE),
        as_of_session_date=date(2026, 7, 30),
    )

    assert [position["state"] for position in projection["positions"]] == [
        "closed",
        "open",
    ]
    assert projection["event_links"]["stale-exit:0"]["lifecycle_status"] == ("reply_terminal")
    assert projection["stats"]["open_positions"] == 1


def test_new_contract_entry_atomically_replaces_the_only_active_position() -> None:
    rows = (
        _message_row("entry-1", "2026-07-30T14:00:00Z", "$SPX 7370c @ 10"),
        _message_row("entry-2", "2026-07-30T14:01:00Z", "$SPX 7380p @ 12"),
    )

    projection = project_discord_signal_lifecycle(
        parse_discord_feed(rows, TEST_PROFILE),
        as_of_session_date=date(2026, 7, 30),
    )

    assert [position["state"] for position in projection["positions"]] == [
        "closed",
        "open",
    ]
    assert projection["positions"][0]["terminal_reason"] == "flipped_by_entry"
    assert projection["positions"][0]["exit_price_status"] == "missing"
    assert projection["event_links"]["entry-2:0"]["lifecycle_status"] == ("opened_after_flip")
    assert projection["stats"]["open_positions"] == 1


def test_add_with_contract_opens_when_flat_and_mismatch_does_not_replace() -> None:
    rows = (
        _message_row(
            "add-open",
            "2026-07-30T14:00:00Z",
            "Added $SPX 7370c @ 10 | 1 con",
        ),
        _message_row(
            "add-mismatch",
            "2026-07-30T14:01:00Z",
            "Added $SPX 7380p @ 12 | 1 con",
        ),
    )

    projection = project_discord_signal_lifecycle(
        parse_discord_feed(rows, TEST_PROFILE),
        as_of_session_date=date(2026, 7, 30),
    )

    assert len(projection["positions"]) == 1
    assert projection["positions"][0]["state"] == "open"
    assert projection["event_links"]["add-open:0"]["lifecycle_status"] == ("opened_from_add")
    assert projection["event_links"]["add-mismatch:0"]["lifecycle_status"] == ("contract_mismatch")


def test_lifecycle_expires_open_0dte_at_explicit_projection_horizon() -> None:
    rows = (_message_row("entry", "2026-07-30T14:00:00Z", "$SPX 7370c @ 10"),)
    events = parse_discord_feed(rows, TEST_PROFILE)

    current = project_discord_signal_lifecycle(
        events,
        as_of_session_date=date(2026, 7, 30),
    )
    elapsed = project_discord_signal_lifecycle(
        events,
        as_of_session_date=date(2026, 7, 31),
    )

    assert current["positions"][0]["state"] == "open"
    assert elapsed["as_of_session_date"] == "2026-07-31"
    assert elapsed["positions"][0]["state"] == "expired_unresolved"
    assert elapsed["positions"][0]["expired_unresolved_as_of_session_date"] == "2026-07-31"
    assert elapsed["stats"]["expired_unresolved_positions"] == 1
    current_payload = discord_signal_payload(
        instrument_id="ibkr|contract|756733",
        route_fingerprint="ibkr|contract|756733|route",
        timeframe="5m",
        rows=rows,
        profile=TEST_PROFILE,
        as_of_session_date=date(2026, 7, 30),
        mode="test",
        feed_status={"state": "test"},
    )
    elapsed_payload = discord_signal_payload(
        instrument_id="ibkr|contract|756733",
        route_fingerprint="ibkr|contract|756733|route",
        timeframe="5m",
        rows=rows,
        profile=TEST_PROFILE,
        as_of_session_date=date(2026, 7, 31),
        mode="test",
        feed_status={"state": "test"},
    )
    assert current_payload["revision"] != elapsed_payload["revision"]


def test_stopping_out_contract_is_an_executed_stop() -> None:
    (event,) = parse_discord_message(
        DiscordFeedMessage.from_mapping(
            _message_row(
                "stopping-out",
                "2026-07-02T15:07:33Z",
                "@everyone stopping out $spy 752c @ 0.10",
            )
        ),
        TEST_PROFILE,
    )

    assert event.action is DiscordSignalAction.EXIT
    assert event.exit_reason == "stop"
    assert event.reported_premium == 0.10


def test_stopped_contract_without_out_is_an_executed_stop() -> None:
    (event,) = parse_discord_message(
        DiscordFeedMessage.from_mapping(
            _message_row(
                "stopped-contract",
                "2026-07-10T16:45:13Z",
                "@everyone Stopped $SPY 753p @ .5\n-25%",
            )
        ),
        TEST_PROFILE,
    )

    assert event.action is DiscordSignalAction.EXIT
    assert event.exit_reason == "stop"
    assert event.reported_premium == 0.5


def test_contract_message_with_final_out_line_is_an_exit() -> None:
    (event,) = parse_discord_message(
        DiscordFeedMessage.from_mapping(
            _message_row(
                "final-out",
                "2026-07-14T02:19:46Z",
                "$SPX 7550c @ 8.3\nOut.",
            )
        ),
        TEST_PROFILE,
    )

    assert event.action is DiscordSignalAction.EXIT
    assert event.exit_reason == "manual"
    assert event.reported_premium == 8.3


def test_gth_0dte_entry_and_stop_share_next_profile_session_day() -> None:
    rows = (
        _message_row("gth-entry", "2026-07-20T02:40:00Z", "$SPX 7430p @ 13.6"),
        _message_row(
            "gth-stop",
            "2026-07-20T02:50:00Z",
            "@everyone stopped out $SPX 7430p @ 12",
            reply_to_message_id="gth-entry",
        ),
    )

    events = parse_discord_feed(rows, TEST_PROFILE)
    projection = project_discord_signal_lifecycle(
        events,
        as_of_session_date=date(2026, 7, 20),
    )

    assert {event.source_session_date for event in events} == {"2026-07-20"}
    assert {event.expiry for event in events} == {"2026-07-20"}
    assert projection["positions"][0]["state"] == "closed"
    assert projection["positions"][0]["exit_reason"] == "stop"


def test_add_parses_trailing_explicit_contract_quantity() -> None:
    rows = (
        _message_row("entry", "2026-07-30T14:00:00Z", "$SPX 7370c @ 10"),
        _message_row(
            "add",
            "2026-07-30T14:05:00Z",
            "Added $SPX 7370c @ 8 | Challenge | 1 con",
        ),
    )

    events = parse_discord_feed(rows, TEST_PROFILE)

    assert events[1].action is DiscordSignalAction.ADD
    assert events[1].quantity == 1


def test_payload_is_exact_route_scoped_and_typed() -> None:
    payload = discord_signal_payload(
        instrument_id="ibkr|contract|416904",
        route_fingerprint="ibkr|contract|416904|route:test",
        timeframe="5m",
        rows=TEST_MESSAGES,
        profile=TEST_PROFILE,
        as_of_session_date=date(2026, 7, 30),
        mode="test_fixture",
        feed_status={"state": "test"},
    )

    assert payload["ok"] is True
    assert payload["instrument_id"] == "ibkr|contract|416904"
    assert payload["route_fingerprint"] == "ibkr|contract|416904|route:test"
    assert payload["timeframe"] == "5m"
    assert payload["revision"]
    assert payload["stats"]["message_count"] == len(TEST_MESSAGES)
    assert payload["stats"]["unmatched_count"] >= 1
    assert all(
        event["instrument_id"] == payload["instrument_id"]
        and event["route_fingerprint"] == payload["route_fingerprint"]
        for event in payload["events"]
    )
    call_entry = next(
        event
        for event in payload["events"]
        if event["action"] == "entry" and event["option_right"] == "call"
    )
    assert call_entry["position_side"] == "long_premium"
    assert call_entry["underlying_bias"] == "long"
    for event in payload["events"]:
        validate_indicator_fact_fields(event["facts"])


def test_directional_level_payload_preserves_typed_fact_without_trade_linkage() -> None:
    payload = discord_signal_payload(
        instrument_id="ibkr|contract|756733",
        route_fingerprint="ibkr|contract|756733|route",
        timeframe="5m",
        rows=(
            _message_row(
                "directional-payload",
                "2026-07-20T00:25:25Z",
                "tonight's $SPY read is either puts below 744 or calls over 748",
            ),
        ),
        profile=TEST_PROFILE,
        as_of_session_date=date(2026, 7, 20),
        mode="test",
        feed_status={"state": "test"},
    )

    assert payload["stats"]["directional_level_count"] == 2
    assert payload["stats"]["unmatched_count"] == 0
    assert payload["positions"] == []
    assert [event["lifecycle_status"] for event in payload["events"]] == [
        "advisory_only",
        "advisory_only",
    ]
    assert {
        event["facts"]["metrics"]["directional_level"]["value"] for event in payload["events"]
    } == {744.0, 748.0}


def _live_feed_config() -> DiscordSignalFeedConfig:
    return DiscordSignalFeedConfig(
        enabled=True,
        bridge_secret="b" * 64,
        channel_ids=("2001", "2002"),
        author_id="3001",
        author_name="GAA",
        retention_hours=72,
        history_limit=100,
    )


def _rpc_envelope(event: str, **payload: object) -> dict[str, object]:
    return {
        "contract": DISCORD_RPC_BRIDGE_CONTRACT,
        "event": event,
        "channel_id": "2001",
        "instance_id": "companion-test",
        "sent_at": datetime.now(UTC).isoformat(),
        "heartbeat_seconds": 30,
        **payload,
    }


def test_official_discord_rpc_bridge_filters_exact_channel_and_author() -> None:
    messages = [
        {
            "id": "1002",
            "timestamp": datetime.now(UTC).isoformat(),
            "content": "7370c @ 13.3",
            "author": {"id": "3001", "username": "GAA"},
            "message_reference": {"message_id": "900"},
        },
        {
            "id": "1001",
            "channel_id": "2001",
            "timestamp": datetime.now(UTC).isoformat(),
            "content": "not this author",
            "author": {"id": "3999", "username": "Other"},
        },
    ]
    feed = DiscordSignalFeed(_live_feed_config())
    feed.set_targets(("ibkr|contract|756733",))
    feed.ingest_bridge_envelope(_rpc_envelope("snapshot", messages=messages))
    snapshot = feed.snapshot()

    assert [row["message_id"] for row in snapshot["rows"]] == ["1002"]
    assert snapshot["rows"][0]["reply_to_message_id"] == "900"
    assert snapshot["status"]["channel_ids"] == ["2001", "2002"]
    assert snapshot["status"]["author_id"] == "3001"
    assert snapshot["status"]["transport"] == "discord_desktop_rpc"
    assert snapshot["status"]["state"] == "companion_live"
    assert "b" * 64 not in json.dumps(snapshot)


def test_discord_channel_list_is_exact_deduplicated_identity() -> None:
    assert require_discord_snowflakes(
        "2002,2001",
        field="discord_channel_ids",
    ) == ("2001", "2002")

    for invalid in ("2001,2001", "2001,SPX", "2001, 2002"):
        try:
            require_discord_snowflakes(invalid, field="discord_channel_ids")
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted invalid channel identity list: {invalid}")


def test_discord_rpc_bridge_accepts_each_configured_channel_only() -> None:
    feed = DiscordSignalFeed(_live_feed_config())
    message = {
        "id": "1003",
        "timestamp": datetime.now(UTC).isoformat(),
        "content": "7375p @ 10.5",
        "author": {"id": "3001", "username": "GAA"},
    }

    feed.ingest_bridge_envelope(
        _rpc_envelope(
            "message_create",
            channel_id="2002",
            message=message,
        )
    )
    assert feed.snapshot()["rows"][0]["channel_id"] == "2002"

    try:
        feed.ingest_bridge_envelope(
            _rpc_envelope(
                "message_create",
                channel_id="2999",
                message={**message, "id": "1004"},
            )
        )
    except ValueError as exc:
        assert str(exc) == "DISCORD_RPC_BRIDGE_CHANNEL_MISMATCH"
    else:
        raise AssertionError("unconfigured Discord channel was accepted")


def test_discord_history_limit_is_applied_per_channel_before_global_replay() -> None:
    feed_config = DiscordSignalFeedConfig(
        enabled=True,
        bridge_secret="b" * 64,
        channel_ids=("2001", "2002"),
        author_id="3001",
        author_name="GAA",
        retention_hours=72,
        history_limit=1,
    )
    feed = DiscordSignalFeed(feed_config)
    published_at = datetime.now(UTC).isoformat()
    for channel_id, message_id in (("2001", "1001"), ("2002", "1002")):
        feed.ingest_bridge_envelope(
            _rpc_envelope(
                "message_create",
                channel_id=channel_id,
                message={
                    "id": message_id,
                    "timestamp": published_at,
                    "content": "7370c @ 13.3",
                    "author": {"id": "3001", "username": "GAA"},
                },
            )
        )

    assert feed_config.total_history_limit == 2
    assert [row["message_id"] for row in feed.snapshot()["rows"]] == ["1001", "1002"]

    companion = DiscordRpcCompanion(
        DiscordRpcCompanionConfig(
            client_id="1000",
            client_secret="secret",
            redirect_uri="http://127.0.0.1/callback",
            channel_ids=("2001", "2002"),
            author_id="3001",
            bridge_secret="b" * 64,
            terminal_base_url="http://127.0.0.1:8000",
            request_timeout_seconds=1.0,
            history_limit=1,
            snapshot_seconds=30.0,
            gate_poll_seconds=2.0,
        ),
        SimpleNamespace(publish=lambda _payload: None),
    )
    for channel_id, message_id in (("2001", "1001"), ("2002", "1002")):
        companion._remember_messages(
            (
                {
                    "id": message_id,
                    "timestamp": published_at,
                    "content": "7370c @ 13.3",
                    "author": {"id": "3001", "username": "GAA"},
                },
            ),
            channel_id=channel_id,
        )
    assert [row["id"] for row in companion._message_snapshot(channel_id="2001")] == ["1001"]
    assert [row["id"] for row in companion._message_snapshot(channel_id="2002")] == ["1002"]


def test_discord_rpc_feed_is_dormant_without_exact_targets() -> None:
    feed = DiscordSignalFeed(_live_feed_config())
    feed.set_targets(())
    assert feed.snapshot()["status"]["state"] == "dormant"


def test_discord_rpc_bridge_exposes_a_stale_heartbeat_and_validates_timestamp() -> None:
    feed = DiscordSignalFeed(_live_feed_config())
    feed.set_targets(("ibkr|contract|756733",))
    feed.ingest_bridge_envelope(
        _rpc_envelope(
            "status",
            state="live",
            sent_at="2026-07-01T10:00:00+00:00",
            heartbeat_seconds=5,
        )
    )

    status = feed.snapshot()["status"]
    assert status["state"] == "companion_stale"
    assert status["heartbeat_seconds"] == 5.0
    assert status["last_bridge_age_seconds"] > 30

    try:
        feed.ingest_bridge_envelope(
            _rpc_envelope("status", state="live", sent_at="2026-08-01T10:00:00")
        )
    except ValueError as exc:
        assert str(exc) == "DISCORD_RPC_BRIDGE_TIMESTAMP_INVALID"
    else:
        raise AssertionError("timezone-free Discord bridge timestamp was accepted")


def test_discord_rpc_bridge_applies_update_and_delete_by_message_id() -> None:
    feed = DiscordSignalFeed(_live_feed_config())
    feed.set_targets(("ibkr|contract|756733",))
    message = {
        "id": "1002",
        "timestamp": datetime.now(UTC).isoformat(),
        "content": "7370c @ 13.3",
        "author": {"id": "3001", "username": "GAA"},
    }
    feed.ingest_bridge_envelope(_rpc_envelope("message_create", message=message))
    feed.ingest_bridge_envelope(
        _rpc_envelope(
            "message_update",
            message={**message, "content": "7370c @ 14.0"},
        )
    )
    assert feed.snapshot()["rows"][0]["content"] == "7370c @ 14.0"

    feed.ingest_bridge_envelope(_rpc_envelope("message_delete", message_id="1002"))
    assert feed.snapshot()["rows"] == ()


def test_discord_snapshot_never_infers_delete_from_omitted_rows() -> None:
    feed = DiscordSignalFeed(_live_feed_config())
    published_at = datetime.now(UTC).isoformat()

    def message(message_id: str, content: str) -> dict[str, object]:
        return {
            "id": message_id,
            "timestamp": published_at,
            "content": content,
            "author": {"id": "3001", "username": "GAA"},
        }

    for message_id in ("100", "180", "200", "300"):
        feed.ingest_bridge_envelope(
            _rpc_envelope(
                "message_create",
                message=message(message_id, f"{message_id}c @ 10"),
            )
        )

    result = feed.ingest_bridge_envelope(
        _rpc_envelope(
            "snapshot",
            messages=[message("200", "200c @ 11")],
        )
    )

    assert result["journal_outcomes"] == {"200": "applied"}
    assert [row["message_id"] for row in feed.snapshot()["rows"]] == [
        "100",
        "180",
        "200",
        "300",
    ]


def test_discord_reconnect_replaces_only_the_companion_channel_cache() -> None:
    companion = DiscordRpcCompanion(
        DiscordRpcCompanionConfig(
            client_id="1000",
            client_secret="secret",
            redirect_uri="http://127.0.0.1/callback",
            channel_ids=("2001",),
            author_id="3001",
            bridge_secret="b" * 64,
            terminal_base_url="http://127.0.0.1:8000",
            request_timeout_seconds=1.0,
            history_limit=100,
            snapshot_seconds=30.0,
            gate_poll_seconds=2.0,
        ),
        SimpleNamespace(publish=lambda _payload: None),
    )

    def message(message_id: str) -> dict[str, object]:
        return {
            "id": message_id,
            "timestamp": "2026-08-01T13:30:00+00:00",
            "content": "7370c @ 13.3",
            "author": {"id": "3001", "username": "GAA"},
        }

    companion._remember_messages(
        tuple(message(message_id) for message_id in ("100", "180", "200", "300")),
        channel_id="2001",
    )
    companion._remember_messages(
        (message("200"),),
        channel_id="2001",
        replace_channel=True,
    )

    assert [row["id"] for row in companion._message_snapshot(channel_id="2001")] == ["200"]


def test_discord_reconnect_replays_live_events_after_older_snapshot() -> None:
    published: list[dict[str, object]] = []
    companion = DiscordRpcCompanion(
        DiscordRpcCompanionConfig(
            client_id="1000",
            client_secret="secret",
            redirect_uri="http://127.0.0.1/callback",
            channel_ids=("2001",),
            author_id="3001",
            bridge_secret="b" * 64,
            terminal_base_url="http://127.0.0.1:8000",
            request_timeout_seconds=1.0,
            history_limit=100,
            snapshot_seconds=30.0,
            gate_poll_seconds=2.0,
        ),
        SimpleNamespace(publish=lambda payload: published.append(dict(payload))),
    )
    old_message = {
        "id": "200",
        "timestamp": "2026-08-01T13:30:00+00:00",
        "content": "7370c @ 10",
        "author": {"id": "3001", "username": "GAA"},
    }
    live_update = {
        "evt": "MESSAGE_UPDATE",
        "data": {
            "channel_id": "2001",
            "message": {**old_message, "content": "7370c @ 14"},
        },
    }
    companion._reconciling_channel_ids.add("2001")
    companion._deferred_channel_events["2001"] = []
    companion._handle_event(live_update)
    companion._remember_messages(
        (old_message,),
        channel_id="2001",
        replace_channel=True,
    )
    deferred = tuple(companion._deferred_channel_events.pop("2001"))
    companion._reconciling_channel_ids.remove("2001")
    for event in deferred:
        companion._handle_event(event)

    assert published[0]["event"] == "message_update"
    assert companion._message_snapshot(channel_id="2001")[0]["content"] == ("7370c @ 14")


def test_discord_publisher_accepts_authoritative_rejected_ack() -> None:
    config = DiscordRpcCompanionConfig(
        client_id="1000",
        client_secret="secret",
        redirect_uri="http://127.0.0.1/callback",
        channel_ids=("2001",),
        author_id="3001",
        bridge_secret="b" * 64,
        terminal_base_url="http://127.0.0.1:8000",
        request_timeout_seconds=1.0,
        history_limit=100,
        snapshot_seconds=30.0,
        gate_poll_seconds=2.0,
    )

    class Response:
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(
                {
                    "ok": True,
                    "journal_outcomes": {"9999": "rejected"},
                }
            ).encode("utf-8")

    publisher = TerminalBridgePublisher(
        config,
        opener=lambda *_args, **_kwargs: Response(),
    )
    publisher._post(
        {
            "event": "message_delete",
            "message_id": "9999",
        }
    )


def test_discord_publisher_stop_waits_for_physical_worker(monkeypatch: MonkeyPatch) -> None:
    config = DiscordRpcCompanionConfig(
        client_id="1000",
        client_secret="secret",
        redirect_uri="http://127.0.0.1/callback",
        channel_ids=("2001",),
        author_id="3001",
        bridge_secret="b" * 64,
        terminal_base_url="http://127.0.0.1:8000",
        request_timeout_seconds=1.0,
        history_limit=100,
        snapshot_seconds=30.0,
        gate_poll_seconds=2.0,
    )
    publisher = TerminalBridgePublisher(config)
    started = threading.Event()
    release = threading.Event()

    def blocking_post(_payload: Mapping[str, object]) -> None:
        started.set()
        assert release.wait(timeout=2.0)

    monkeypatch.setattr(publisher, "_post", blocking_post)
    publisher.start()
    publisher.publish({"event": "status"})
    assert started.wait(timeout=1.0)

    stopper = threading.Thread(target=publisher.stop)
    stopper.start()
    stopper.join(timeout=0.05)

    assert stopper.is_alive()
    assert publisher._thread is not None
    release.set()
    stopper.join(timeout=1.0)
    assert not stopper.is_alive()
    assert publisher._thread is None


def test_discord_service_loop_reads_non_blocking_runtime_settings_snapshot_per_turn(
    monkeypatch: MonkeyPatch,
) -> None:
    from aef_terminal import config as config_module

    monkeypatch.setattr(
        config_module,
        "AppConfig",
        lambda: SimpleNamespace(
            discord_signals_enabled=True,
            discord_signals_bridge_secret="b" * 64,
            discord_signals_channel_ids="2001",
            discord_signals_author_id="3001",
            discord_signals_author_name="GAA",
            discord_signals_retention_hours=72,
            discord_signals_history_limit=100,
        ),
    )
    event_thread_id = threading.get_ident()
    store_factory_threads: list[int] = []
    storage_read_threads: list[int] = []
    settings_read_threads: list[int] = []

    class Store:
        def read_discord_signal_messages(self, **_kwargs: object) -> list[object]:
            storage_read_threads.append(threading.get_ident())
            return []

    def store_factory() -> Store:
        store_factory_threads.append(threading.get_ident())
        return Store()

    def client_settings_snapshot() -> dict[str, object]:
        settings_read_threads.append(threading.get_ident())
        return {}

    contribution = configure_service(
        SimpleNamespace(
            store_factory=store_factory,
            client_settings_snapshot=client_settings_snapshot,
            server_sleeping=lambda: False,
        )
    )

    class LoopTurnComplete(Exception):
        pass

    async def complete_turn(_seconds: float) -> None:
        raise LoopTurnComplete

    monkeypatch.setattr(discord_service.asyncio, "sleep", complete_turn)
    try:
        asyncio.run(contribution.tasks[0].factory())
    except LoopTurnComplete:
        pass
    else:
        raise AssertionError("Discord service loop did not complete one turn")

    assert len(settings_read_threads) == 1
    assert store_factory_threads and storage_read_threads
    assert all(thread_id != event_thread_id for thread_id in store_factory_threads)
    assert all(thread_id != event_thread_id for thread_id in storage_read_threads)
    assert settings_read_threads == [event_thread_id]


def test_discord_companion_forwards_configured_channel_delete_before_create() -> None:
    published: list[dict[str, object]] = []
    companion = DiscordRpcCompanion(
        DiscordRpcCompanionConfig(
            client_id="1000",
            client_secret="secret",
            redirect_uri="http://127.0.0.1/callback",
            channel_ids=("2001", "2002"),
            author_id="3001",
            bridge_secret="b" * 64,
            terminal_base_url="http://127.0.0.1:8000",
            request_timeout_seconds=1.0,
            history_limit=100,
            snapshot_seconds=30.0,
            gate_poll_seconds=2.0,
        ),
        SimpleNamespace(publish=lambda payload: published.append(dict(payload))),
    )
    companion._handle_event(
        {
            "evt": "MESSAGE_DELETE",
            "data": {"channel_id": "2001", "message": {"id": "9999"}},
        }
    )
    companion._handle_event(
        {
            "evt": "MESSAGE_CREATE",
            "data": {
                "channel_id": "2001",
                "message": {
                    "id": "1013",
                    "timestamp": "2026-08-01T13:30:00+00:00",
                    "content": "7370c @ 13.3",
                    "author": {"id": "3001", "username": "GAA"},
                },
            },
        }
    )

    assert [payload["event"] for payload in published] == [
        "message_delete",
        "message_create",
    ]
    assert published[0]["message_id"] == "9999"
    published_message = published[1]["message"]
    assert isinstance(published_message, Mapping)
    assert published_message["id"] == "1013"


def test_discord_delete_reaches_durable_row_after_bounded_eviction() -> None:
    journal: dict[str, dict[str, object] | None] = {}
    known_ids: set[str] = set()
    results: list[dict[str, object]] = []

    def persist(
        rows: object,
        source_event: str,
        observed_at: datetime,
    ) -> dict[str, dict[str, object]]:
        assert isinstance(rows, tuple)
        acknowledgements: dict[str, dict[str, object]] = {}
        for raw_row in rows:
            row = dict(raw_row)
            message_id = str(row["message_id"])
            known_ids.add(message_id)
            journal[message_id] = row
            acknowledgements[message_id] = {
                "message_id": message_id,
                "outcome": "applied",
                "message": row,
                "deleted": False,
                "source_event": source_event,
                "source_event_at": observed_at.isoformat(),
            }
        return acknowledgements

    def delete(
        message_id: str,
        _channel_id: str,
        observed_at: datetime,
    ) -> dict[str, object]:
        if message_id not in known_ids:
            return {
                "message_id": message_id,
                "outcome": "ignored_unknown",
                "message": None,
                "deleted": False,
                "source_event": "message_delete",
                "source_event_at": observed_at.isoformat(),
            }
        journal[message_id] = None
        return {
            "message_id": message_id,
            "outcome": "applied",
            "message": None,
            "deleted": True,
            "source_event": "message_delete",
            "source_event_at": observed_at.isoformat(),
        }

    feed = DiscordSignalFeed(
        DiscordSignalFeedConfig(
            enabled=True,
            bridge_secret="b" * 64,
            channel_ids=("2001",),
            author_id="3001",
            author_name="GAA",
            retention_hours=72,
            history_limit=1,
        ),
        upsert_messages=persist,
        delete_message=delete,
    )

    def publish(payload: object) -> None:
        assert isinstance(payload, Mapping)
        results.append(feed.ingest_bridge_envelope(payload))

    companion = DiscordRpcCompanion(
        DiscordRpcCompanionConfig(
            client_id="1000",
            client_secret="secret",
            redirect_uri="http://127.0.0.1/callback",
            channel_ids=("2001",),
            author_id="3001",
            bridge_secret="b" * 64,
            terminal_base_url="http://127.0.0.1:8000",
            request_timeout_seconds=1.0,
            history_limit=1,
            snapshot_seconds=30.0,
            gate_poll_seconds=2.0,
        ),
        SimpleNamespace(publish=publish),
    )

    def dispatch_create(message_id: str, published_at: str) -> None:
        companion._handle_event(
            {
                "evt": "MESSAGE_CREATE",
                "data": {
                    "channel_id": "2001",
                    "message": {
                        "id": message_id,
                        "timestamp": published_at,
                        "content": "7370c @ 13.3",
                        "author": {
                            "id": "3001",
                            "username": "GAA",
                        },
                    },
                },
            }
        )

    published_at = datetime.now(UTC).isoformat()
    dispatch_create("1010", published_at)
    dispatch_create("1011", published_at)
    assert "1010" not in companion._messages
    assert [row["message_id"] for row in feed.snapshot()["rows"]] == ["1011"]
    assert journal["1010"] is not None

    companion._handle_event(
        {
            "evt": "MESSAGE_DELETE",
            "data": {"channel_id": "2001", "message": {"id": "1010"}},
        }
    )
    companion._handle_event(
        {
            "evt": "MESSAGE_DELETE",
            "data": {"channel_id": "2001", "message": {"id": "9999"}},
        }
    )
    dispatch_create("1012", published_at)

    assert journal["1010"] is None
    assert results[-3]["journal_outcomes"] == {"1010": "applied"}
    assert results[-2]["journal_outcomes"] == {"9999": "ignored_unknown"}
    assert results[-1]["journal_outcomes"] == {"1012": "applied"}


def test_discord_rpc_bridge_commits_before_projection_and_restores_recent_rows() -> None:
    persistence_calls: list[tuple[str, str]] = []

    def persist(
        rows: object,
        source_event: str,
        observed_at: datetime,
    ) -> dict[str, dict[str, object]]:
        assert isinstance(rows, tuple)
        persistence_calls.append((source_event, rows[0]["message_id"]))
        row = dict(rows[0])
        return {
            str(row["message_id"]): {
                "message_id": row["message_id"],
                "outcome": "applied",
                "message": row,
                "deleted": False,
                "source_event": source_event,
                "source_event_at": observed_at.isoformat(),
            }
        }

    feed = DiscordSignalFeed(_live_feed_config(), upsert_messages=persist)
    message = {
        "id": "1010",
        "timestamp": datetime.now(UTC).isoformat(),
        "content": "  7370c @ 13.3  ",
        "author": {"id": "3001", "username": "GAA"},
    }
    feed.ingest_bridge_envelope(_rpc_envelope("message_create", message=message))

    assert persistence_calls == [("message_create", "1010")]
    assert feed.snapshot()["rows"][0]["content"] == "  7370c @ 13.3  "

    restored = DiscordSignalFeed(_live_feed_config())
    restored.restore(feed.snapshot()["rows"])
    assert restored.snapshot()["rows"] == feed.snapshot()["rows"]

    def reject_persistence(
        _rows: object,
        _source_event: str,
        _observed_at: datetime,
    ) -> None:
        raise RuntimeError("storage unavailable")

    rejected = DiscordSignalFeed(
        _live_feed_config(),
        upsert_messages=reject_persistence,
    )
    try:
        rejected.ingest_bridge_envelope(
            _rpc_envelope("message_create", message={**message, "id": "1011"})
        )
    except RuntimeError as exc:
        assert str(exc) == "storage unavailable"
    else:
        raise AssertionError("uncommitted Discord row became visible")
    assert rejected.snapshot()["rows"] == ()


def test_discord_rpc_bridge_reconciles_replays_and_rejected_mutations() -> None:
    journal_message: dict[str, object] | None = None
    journal_event_at: datetime | None = None
    journal_event = ""

    def acknowledgement(outcome: str) -> dict[str, object]:
        return {
            "message_id": "1012",
            "outcome": outcome,
            "message": dict(journal_message) if journal_message is not None else None,
            "deleted": journal_message is None,
            "source_event": journal_event,
            "source_event_at": (
                journal_event_at.isoformat() if journal_event_at is not None else None
            ),
        }

    def persist(
        rows: object,
        source_event: str,
        observed_at: datetime,
    ) -> dict[str, dict[str, object]]:
        nonlocal journal_event, journal_event_at, journal_message
        assert isinstance(rows, tuple)
        submitted = dict(rows[0])
        if journal_event_at is None or observed_at > journal_event_at:
            journal_message = submitted
            journal_event_at = observed_at
            journal_event = source_event
            outcome = "applied"
        elif observed_at == journal_event_at and submitted == journal_message:
            outcome = "replayed"
        else:
            outcome = "rejected"
        return {"1012": acknowledgement(outcome)}

    def delete(
        message_id: str,
        _channel_id: str,
        observed_at: datetime,
    ) -> dict[str, object]:
        nonlocal journal_event, journal_event_at, journal_message
        if message_id != "1012":
            return {
                "message_id": message_id,
                "outcome": "ignored_unknown",
                "message": None,
                "deleted": False,
                "source_event": "message_delete",
                "source_event_at": observed_at.isoformat(),
            }
        if journal_event_at is not None and observed_at > journal_event_at:
            journal_message = None
            journal_event_at = observed_at
            journal_event = "message_delete"
            outcome = "applied"
        elif observed_at == journal_event_at and journal_message is None:
            outcome = "replayed"
        else:
            outcome = "rejected"
        return acknowledgement(outcome)

    feed = DiscordSignalFeed(
        _live_feed_config(),
        upsert_messages=persist,
        delete_message=delete,
    )
    message = {
        "id": "1012",
        "timestamp": "2026-08-01T13:30:00+00:00",
        "content": "7370c @ 13.3",
        "author": {"id": "3001", "username": "GAA"},
    }
    unrelated_delete = feed.ingest_bridge_envelope(
        _rpc_envelope(
            "message_delete",
            sent_at="2026-08-01T13:29:59+00:00",
            message_id="9999",
        )
    )
    first = feed.ingest_bridge_envelope(
        _rpc_envelope(
            "message_create",
            sent_at="2026-08-01T13:30:01+00:00",
            message=message,
        )
    )
    replay = feed.ingest_bridge_envelope(
        _rpc_envelope(
            "message_create",
            sent_at="2026-08-01T13:30:01+00:00",
            message=message,
        )
    )
    feed.ingest_bridge_envelope(
        _rpc_envelope(
            "message_update",
            sent_at="2026-08-01T13:30:03+00:00",
            message={**message, "content": "7370c @ 14.0"},
        )
    )
    stale_update = feed.ingest_bridge_envelope(
        _rpc_envelope(
            "message_update",
            sent_at="2026-08-01T13:30:02+00:00",
            message={**message, "content": "stale update"},
        )
    )
    stale_delete = feed.ingest_bridge_envelope(
        _rpc_envelope(
            "message_delete",
            sent_at="2026-08-01T13:30:02+00:00",
            message_id="1012",
        )
    )

    assert unrelated_delete["journal_outcomes"] == {"9999": "ignored_unknown"}
    assert first["journal_outcomes"] == {"1012": "applied"}
    assert replay["journal_outcomes"] == {"1012": "replayed"}
    assert stale_update["journal_outcomes"] == {"1012": "rejected"}
    assert stale_delete["journal_outcomes"] == {"1012": "rejected"}
    assert feed.snapshot()["rows"][0]["content"] == "7370c @ 14.0"

    deleted = feed.ingest_bridge_envelope(
        _rpc_envelope(
            "message_delete",
            sent_at="2026-08-01T13:30:04+00:00",
            message_id="1012",
        )
    )
    rejected_after_delete = feed.ingest_bridge_envelope(
        _rpc_envelope(
            "message_update",
            sent_at="2026-08-01T13:30:03.500000+00:00",
            message={**message, "content": "resurrect stale row"},
        )
    )
    assert deleted["journal_outcomes"] == {"1012": "applied"}
    assert rejected_after_delete["journal_outcomes"] == {"1012": "rejected"}
    assert feed.snapshot()["rows"] == ()


def test_discord_rpc_ipc_connection_answers_ping_and_decodes_frame() -> None:
    local, peer = socket.socketpair()
    connection = DiscordIpcConnection(local)
    try:
        ping = b'{"nonce":"ping"}'
        payload = b'{"cmd":"DISPATCH","evt":"READY","data":{}}'
        peer.sendall(struct.pack("<II", 3, len(ping)) + ping)
        peer.sendall(struct.pack("<II", 1, len(payload)) + payload)

        assert connection.receive_json()["evt"] == "READY"
        header = peer.recv(8)
        opcode, length = struct.unpack("<II", header)
        assert opcode == 4
        assert peer.recv(length) == ping
    finally:
        connection.close()
        peer.close()


def test_exact_instrument_settings_form_the_chart_group_without_aliases() -> None:
    es_id = "ibkr|future_root|ES|CME|USD|ES"
    spy_id = "ibkr|contract|756733"
    settings = {
        f"aef:instrument:{json.dumps([es_id, 'regular'], separators=(',', ':'))}"
        ":indicator:discordSignalsCalcEnabled": "true",
        f"aef:instrument:{json.dumps([spy_id, 'regular'], separators=(',', ':'))}"
        ":indicator:discordSignalsCalcEnabled": "true",
    }

    assert instrument_ids_for_indicator_setting(
        settings,
        "discordSignalsCalcEnabled",
    ) == (spy_id, es_id)


def test_calc_enabled_settings_force_the_companion_gate_without_browser_lease(
    monkeypatch: MonkeyPatch,
) -> None:
    target_id = "ibkr|contract|756733"
    feed = DiscordSignalFeed(_live_feed_config())
    context = SimpleNamespace(server_sleeping=lambda: False)
    monkeypatch.setattr(discord_service, "_DISCORD_FEED", feed)
    monkeypatch.setattr(discord_service, "_SERVICE_CONTEXT", context)

    gate = discord_service._refresh_feed_gate((target_id,))

    assert gate == {
        "active": True,
        "forced_by_calc": True,
        "activation_source": "calc_enabled_settings",
        "live_target_instrument_ids": [target_id],
        "target_instrument_ids": [target_id],
        "server_sleeping": False,
    }
    assert feed.snapshot()["status"]["state"] == "waiting_for_companion"

    context.server_sleeping = lambda: True
    sleeping_gate = discord_service._refresh_feed_gate((target_id,))
    assert sleeping_gate["active"] is False
    assert sleeping_gate["forced_by_calc"] is True
    assert sleeping_gate["target_instrument_ids"] == []
    assert feed.snapshot()["status"]["state"] == "dormant"


def test_live_payload_projects_one_source_feed_to_an_exact_target_route(
    monkeypatch: MonkeyPatch,
) -> None:
    target_id = "ibkr|future_root|ES|CME|USD|ES"
    setting_key = (
        f"aef:instrument:{json.dumps([target_id, 'regular'], separators=(',', ':'))}"
        ":indicator:discordSignalsCalcEnabled"
    )
    message = {
        "id": "1002",
        "channel_id": "2001",
        "timestamp": datetime.now(UTC).isoformat(),
        "content": "7370c @ 13.3",
        "author": {"id": "3001", "username": "GAA"},
    }
    feed = DiscordSignalFeed(_live_feed_config())
    feed.ingest_bridge_envelope(_rpc_envelope("message_create", message=message))
    monkeypatch.setattr(discord_service, "_DISCORD_FEED", feed)
    monkeypatch.setattr(
        discord_service,
        "_SERVICE_CONTEXT",
        SimpleNamespace(
            client_settings_snapshot=lambda: (_ for _ in ()).throw(
                AssertionError("HTTP projection must not read the runtime settings snapshot")
            ),
            server_sleeping=lambda: False,
        ),
    )
    discord_service._refresh_feed_gate(
        discord_service._discord_signal_target_ids({setting_key: "true"})
    )

    payload = discord_signal_live_payload(
        instrument_id=target_id,
        route_fingerprint="ibkr|future_root|ES|CME|USD|ES|current:649180671",
        timeframe="5m",
    )

    assert payload["mode"] == "live_discord_rpc"
    assert payload["feed_status"]["selected_target_live"] is True
    assert payload["events"][0]["instrument_id"] == target_id
    assert payload["events"][0]["source_underlying"] is None


def test_live_payload_uses_the_next_source_session_during_a_closed_weekend(
    monkeypatch: MonkeyPatch,
) -> None:
    class SaturdayClock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:
            observed_at = datetime(2026, 8, 8, 17, 0, tzinfo=UTC)
            return observed_at if tz is None else observed_at.astimezone(tz)

    target_id = "ibkr|future_root|ES|CME|USD|ES"
    message = {
        "id": "1535493071863808011",
        "channel_id": "2001",
        "timestamp": "2026-08-08T03:41:16.414Z",
        "content": "Next week commentary",
        "author": {"id": "3001", "username": "GAA"},
    }
    monkeypatch.setattr(discord_rpc_bridge, "datetime", SaturdayClock)
    monkeypatch.setattr(discord_service, "datetime", SaturdayClock)
    feed = DiscordSignalFeed(_live_feed_config())
    feed.ingest_bridge_envelope(
        _rpc_envelope(
            "message_create",
            message=message,
            sent_at="2026-08-08T17:00:00Z",
        )
    )
    monkeypatch.setattr(discord_service, "_DISCORD_FEED", feed)
    monkeypatch.setattr(
        discord_service,
        "_SERVICE_CONTEXT",
        SimpleNamespace(server_sleeping=lambda: False),
    )
    discord_service._refresh_feed_gate((target_id,))

    payload = discord_signal_live_payload(
        instrument_id=target_id,
        route_fingerprint="ibkr|future_root|ES|CME|USD|ES|current:649180671",
        timeframe="5m",
    )

    assert payload["as_of_session_date"] == "2026-08-10"
    assert payload["events"][0]["source_session_date"] == "2026-08-10"


def test_live_feed_fails_closed_without_runtime_configuration(
    monkeypatch: MonkeyPatch,
) -> None:
    app = FastAPI()
    app.include_router(create_router())
    client = TestClient(app)
    query = {
        "instrument_id": "ibkr|contract|416904",
        "route_fingerprint": "ibkr|contract|416904|route:test",
        "timeframe": "5m",
    }

    monkeypatch.setattr(discord_service, "_DISCORD_FEED", None)
    response = client.get("/api/indicators/discord-signals/feed", params=query)
    assert response.status_code == 503
    assert response.json() == {
        "detail": {
            "ok": False,
            "error": {
                "code": "DISCORD_SIGNAL_SERVICE_NOT_INITIALIZED",
                "category": "discord_signals",
                "retryable": True,
                "message": "Discord signal service is not initialized",
            },
        }
    }

    monkeypatch.setattr(
        discord_service,
        "_DISCORD_FEED",
        DiscordSignalFeed(
            DiscordSignalFeedConfig(
                enabled=True,
                bridge_secret="",
                channel_ids=(),
                author_id="",
                author_name="GAA",
                retention_hours=72,
                history_limit=100,
            )
        ),
    )
    response = client.get("/api/indicators/discord-signals/feed", params=query)
    assert response.status_code == 503
    assert response.json() == {
        "detail": {
            "ok": False,
            "error": {
                "code": "DISCORD_SIGNAL_RPC_NOT_CONFIGURED",
                "category": "discord_signals",
                "retryable": False,
                "message": "Discord signal RPC is not configured",
            },
        }
    }


def test_router_and_client_use_package_owned_generic_extensions() -> None:
    paths = {route.path for route in create_router().routes}
    source = Path("src/aef_terminal/indicators/modules/discord_signals/client.js").read_text(
        encoding="utf-8"
    )
    companion_source = Path(
        "src/aef_terminal/indicators/modules/discord_signals/companion.py"
    ).read_text(encoding="utf-8")

    assert paths == {
        "/api/indicators/discord-signals/feed",
        "/api/indicators/discord-signals/rpc-ingest",
        "/api/indicators/discord-signals/status",
        "/api/indicators/discord-signals/companion-gate",
    }
    assert "registerIndicatorLifecycleSync(" in source
    assert '"discord_signals",' in source
    assert "{ poll: true }" in source
    assert 'registerIndicatorPanelRenderer("discord_signals"' in source
    assert 'registerIndicatorOverlayContribution("discord_signals"' in source
    assert 'registerIndicatorRuntimeStateRef("discord_signals_feed"' in source
    assert '"discord_signals_connection"' in source
    assert "/api/indicators/discord-signals/status" in source
    assert 'text: "LIVE"' in source
    assert 'text: "STALE"' in source
    assert "crypto.randomUUID()" not in source
    assert "/api/indicators/discord-signals/lease" not in source
    assert "forced by Calc" in source
    assert "countsAsSignal: false" in source
    assert "discordSignalsPayloadMatchesScope(payload, scope)" in source
    assert "const price = Number(anchorBar.close)" in source
    assert 'type: "marker"' in source
    assert 'type: "line"' in source
    assert "external_option_trade_path" in source
    assert "external_directional_level" in source
    assert "tradePaths" in source
    assert "directionalLevels" in source
    assert "event.underlying_bias || linkedPosition?.underlying_bias" in source
    assert "event.reported_premium === null || event.reported_premium === undefined" in source
    assert 'presentation_policy: "persistent"' in source
    assert 'marker_color_policy: "theme"' in source
    assert 'marker_glyph_color_policy: "tone"' in source
    assert "DISCORD_SIGNALS_TEXT_SIZE_SCALE" in source
    assert "small: 0.5" in source
    assert "medium: 0.6" in source
    assert "large: 0.7" in source
    assert "marker_font_scale: markerFontScale" in source
    assert 'marker_text_anchor: right === "call" ? "upper_left" : "lower_left"' in source
    assert "marker_anchor_dot: true" in source
    assert 'right === "put" ? "▼" : right === "call" ? "▲"' in source
    assert 'action === "entry" || action === "add"' in source
    assert 'if (action === "add") return `${directionalMark}+`' in source
    assert 'if (right === "call") return "positive"' in source
    assert 'if (right === "put") return "negative"' in source
    assert 'String(event.raw_content || "").trim()' in source
    assert "...(commentary ? { commentary } : {})" in source
    assert '["rpc", "identify", "messages.read"]' in companion_source
    assert '"MESSAGE_CREATE"' in companion_source
    assert '"MESSAGE_UPDATE"' in companion_source
    assert '"MESSAGE_DELETE"' in companion_source
    assert '"heartbeat_seconds": self.config.snapshot_seconds' in companion_source
    assert "/api/indicators/discord-signals/companion-gate" in companion_source
    assert "Discord Signals dormant" in companion_source
    assert "Bot " not in companion_source
    assert "/channels/" not in companion_source


def test_rpc_ingress_requires_the_exact_bridge_secret(
    monkeypatch: MonkeyPatch,
) -> None:
    feed = DiscordSignalFeed(_live_feed_config())
    monkeypatch.setattr(discord_service, "_DISCORD_FEED", feed)
    app = FastAPI()
    app.include_router(create_router())
    client = TestClient(app)
    envelope = _rpc_envelope(
        "message_create",
        message={
            "id": "1002",
            "timestamp": datetime.now(UTC).isoformat(),
            "content": "7370c @ 13.3",
            "author": {"id": "3001", "username": "GAA"},
        },
    )

    unauthorized = client.post(
        "/api/indicators/discord-signals/rpc-ingest",
        json=envelope,
    )
    authorized = client.post(
        "/api/indicators/discord-signals/rpc-ingest",
        json=envelope,
        headers={"X-MartinCall-Discord-RPC": "b" * 64},
    )

    assert unauthorized.status_code == 401
    assert unauthorized.json()["detail"]["error"] == {
        "code": "DISCORD_SIGNAL_RPC_UNAUTHORIZED",
        "category": "discord_signals",
        "retryable": False,
        "message": "Discord signal RPC authorization failed",
    }
    assert authorized.status_code == 200
    assert authorized.json()["accepted_messages"] == 1
