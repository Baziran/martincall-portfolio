from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from aef_terminal.data.instrument_identity import require_exact_identity_text


DISCORD_SIGNALS_TRACKED_CHANNEL_IDS = ("1533632385151799377",)


def require_discord_snowflake(value: object, *, field: str) -> str:
    exact = require_exact_identity_text(value, field=field)
    if not exact.isascii() or not exact.isdigit():
        raise ValueError(f"{field} must be an exact Discord snowflake")
    return exact


def require_discord_snowflakes(value: object, *, field: str) -> tuple[str, ...]:
    exact = require_exact_identity_text(value, field=field)
    parts = exact.split(",")
    if len(parts) > 50:
        raise ValueError(f"{field} is limited to 50 exact Discord snowflakes")
    snowflakes = tuple(
        require_discord_snowflake(part, field=f"{field}[{index}]")
        for index, part in enumerate(parts)
    )
    if len(set(snowflakes)) != len(snowflakes):
        raise ValueError(f"{field} must not contain duplicate Discord snowflakes")
    return tuple(sorted(snowflakes, key=int))


def resolve_discord_signal_channel_ids(
    value: object | None,
    *,
    field: str,
) -> tuple[str, ...]:
    configured = (
        require_discord_snowflakes(value, field=field) if value is not None and value != "" else ()
    )
    return tuple(
        sorted(
            {*configured, *DISCORD_SIGNALS_TRACKED_CHANNEL_IDS},
            key=int,
        )
    )


class DiscordSignalAction(StrEnum):
    ENTRY = "entry"
    ADD = "add"
    REDUCE = "reduce"
    EXIT = "exit"
    POSITION_UPDATE = "position_update"
    STOP_UPDATE = "stop_update"
    DIRECTIONAL_LEVEL = "directional_level"
    COMMENTARY = "commentary"
    UNPARSED = "unparsed"


class DiscordSignalCue(StrEnum):
    STOP_HIT = "stop_hit"
    FLAT = "flat"
    FLATTEN_CALLS = "flatten_calls"
    FLATTEN_PUTS = "flatten_puts"
    BARE_SALE = "bare_sale"


class DiscordSignalParseStatus(StrEnum):
    PARSED_UNQUALIFIED = "parsed_unqualified"
    PARSED_CONTEXT = "parsed_context"
    AMBIGUOUS = "ambiguous"
    COMMENTARY = "commentary"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class DiscordSignalProfile:
    source_id: str
    author_id: str
    author_name: str
    channel_ids: tuple[str, ...]
    underlying_tokens: tuple[str, ...] = ("SPX", "SPY")
    default_dte: int = 0
    default_position_side: str = "long_premium"
    exchange_timezone: str = "America/New_York"
    minimum_strike: float = 100.0


@dataclass(frozen=True, slots=True)
class DiscordFeedMessage:
    message_id: str
    channel_id: str
    author_id: str
    author_name: str
    published_at: datetime
    content: str
    reply_to_message_id: str | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> DiscordFeedMessage:
        message_id = require_exact_identity_text(
            value.get("message_id", value.get("id")),
            field="message_id",
        )
        channel_id = require_exact_identity_text(
            value.get("channel_id"),
            field="channel_id",
        )
        author_id = require_exact_identity_text(
            value.get("author_id"),
            field="author_id",
        )
        author_name = require_exact_identity_text(
            value.get("author_name"),
            field="author_name",
        )
        raw_published_at = value.get("published_at", value.get("timestamp"))
        if not isinstance(raw_published_at, str) or not raw_published_at:
            raise ValueError("published_at is required")
        try:
            published_at = datetime.fromisoformat(raw_published_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("published_at must be an ISO timestamp") from exc
        if published_at.tzinfo is None:
            raise ValueError("published_at must include an exact timezone")
        content = value.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("content must be a non-empty string")
        raw_reply_to_message_id = value.get("reply_to_message_id")
        reply_to_message_id = (
            require_exact_identity_text(
                raw_reply_to_message_id,
                field="reply_to_message_id",
            )
            if raw_reply_to_message_id is not None
            else None
        )
        return cls(
            message_id=message_id,
            channel_id=channel_id,
            author_id=author_id,
            author_name=author_name,
            published_at=published_at.astimezone(UTC),
            content=content,
            reply_to_message_id=reply_to_message_id,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "channel_id": self.channel_id,
            "author_id": self.author_id,
            "author_name": self.author_name,
            "published_at": self.published_at.isoformat(),
            "content": self.content,
            "reply_to_message_id": self.reply_to_message_id,
        }


@dataclass(frozen=True, slots=True)
class DiscordSignalEvent:
    event_id: str
    message_id: str
    channel_id: str
    reply_to_message_id: str | None
    ordinal: int
    published_at: datetime
    action: DiscordSignalAction
    context_cue: DiscordSignalCue | None
    parse_status: DiscordSignalParseStatus
    reason_code: str
    source_underlying: str | None
    source_session_date: str | None
    expiry: str | None
    reported_dte: int | None
    strike: float | None
    option_right: str | None
    reported_premium: float | None
    quantity: int | None
    sold_quantity: int | None
    remaining_quantity: int | None
    remaining_fraction: float | None
    stop_mode: str | None
    stop_value: float | None
    stop_basis: str | None
    directional_condition: str | None
    directional_level: float | None
    directional_reference: str | None
    directional_confirmation: str | None
    exit_reason: str | None
    reported_return_pct: float | None
    outcome_hint: str | None
    raw_content: str
    parser_version: str

    @property
    def contract_key(self) -> tuple[str, float, str] | None:
        if self.strike is None or not self.option_right:
            return None
        expiry_key = self.expiry
        if expiry_key is None and self.source_session_date and self.reported_dte is not None:
            expiry_key = f"{self.source_session_date}:reported_dte:{self.reported_dte}"
        if expiry_key is None:
            return None
        return (expiry_key, self.strike, self.option_right)

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "message_id": self.message_id,
            "channel_id": self.channel_id,
            "reply_to_message_id": self.reply_to_message_id,
            "ordinal": self.ordinal,
            "published_at": self.published_at.isoformat(),
            "action": self.action.value,
            "context_cue": self.context_cue.value if self.context_cue is not None else None,
            "parse_status": self.parse_status.value,
            "reason_code": self.reason_code,
            "source_underlying": self.source_underlying,
            "source_session_date": self.source_session_date,
            "expiry": self.expiry,
            "reported_dte": self.reported_dte,
            "strike": self.strike,
            "option_right": self.option_right,
            "reported_premium": self.reported_premium,
            "quantity": self.quantity,
            "sold_quantity": self.sold_quantity,
            "remaining_quantity": self.remaining_quantity,
            "remaining_fraction": self.remaining_fraction,
            "stop_mode": self.stop_mode,
            "stop_value": self.stop_value,
            "stop_basis": self.stop_basis,
            "directional_condition": self.directional_condition,
            "directional_level": self.directional_level,
            "directional_reference": self.directional_reference,
            "directional_confirmation": self.directional_confirmation,
            "exit_reason": self.exit_reason,
            "reported_return_pct": self.reported_return_pct,
            "outcome_hint": self.outcome_hint,
            "raw_content": self.raw_content,
            "parser_version": self.parser_version,
        }


def discord_feed_message_order_key(
    message: DiscordFeedMessage,
) -> tuple[datetime, int, int, str]:
    numeric = message.message_id.isascii() and message.message_id.isdigit()
    return (
        message.published_at,
        0 if numeric else 1,
        int(message.message_id) if numeric else 0,
        message.message_id,
    )


def discord_signal_event_order_key(
    event: DiscordSignalEvent,
) -> tuple[datetime, int, int, str, int]:
    numeric = event.message_id.isascii() and event.message_id.isdigit()
    return (
        event.published_at,
        0 if numeric else 1,
        int(event.message_id) if numeric else 0,
        event.message_id,
        event.ordinal,
    )
