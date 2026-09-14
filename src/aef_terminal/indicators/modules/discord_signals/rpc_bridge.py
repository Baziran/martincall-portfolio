from __future__ import annotations

import hmac
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from aef_terminal.config import AppConfig

from .contracts import (
    DiscordFeedMessage,
    DiscordSignalProfile,
    require_discord_snowflake,
    resolve_discord_signal_channel_ids,
)


DISCORD_RPC_BRIDGE_CONTRACT = "discord-rpc-bridge-v2"
_DEFAULT_BRIDGE_HEARTBEAT_SECONDS = 30.0
_BRIDGE_EVENTS = frozenset(
    {
        "snapshot",
        "message_create",
        "message_update",
        "message_delete",
        "status",
    }
)
DISCORD_SIGNAL_JOURNAL_OUTCOMES = frozenset(("applied", "replayed", "rejected", "ignored_unknown"))
_JOURNAL_SOURCE_EVENTS = frozenset(
    ("snapshot", "message_create", "message_update", "message_delete")
)


def _canonical_message_from_journal_ack(
    ack: Mapping[str, Any],
    *,
    message_id: str,
) -> tuple[str, dict[str, Any] | None]:
    if ack.get("message_id") != message_id:
        raise RuntimeError("DISCORD_SIGNAL_JOURNAL_ACK_MESSAGE_ID_INVALID")
    outcome = ack.get("outcome")
    if outcome not in DISCORD_SIGNAL_JOURNAL_OUTCOMES:
        raise RuntimeError("DISCORD_SIGNAL_JOURNAL_ACK_OUTCOME_INVALID")
    deleted = ack.get("deleted")
    if not isinstance(deleted, bool):
        raise RuntimeError("DISCORD_SIGNAL_JOURNAL_ACK_DELETED_INVALID")
    source_event = ack.get("source_event")
    if source_event not in _JOURNAL_SOURCE_EVENTS:
        raise RuntimeError("DISCORD_SIGNAL_JOURNAL_ACK_SOURCE_EVENT_INVALID")
    raw_source_event_at = ack.get("source_event_at")
    if not isinstance(raw_source_event_at, str):
        raise RuntimeError("DISCORD_SIGNAL_JOURNAL_ACK_SOURCE_EVENT_AT_INVALID")
    try:
        source_event_at = datetime.fromisoformat(raw_source_event_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError("DISCORD_SIGNAL_JOURNAL_ACK_SOURCE_EVENT_AT_INVALID") from exc
    if source_event_at.tzinfo is None or source_event_at.utcoffset() is None:
        raise RuntimeError("DISCORD_SIGNAL_JOURNAL_ACK_SOURCE_EVENT_AT_INVALID")
    raw_message = ack.get("message")
    if outcome == "ignored_unknown":
        if deleted is not False or source_event != "message_delete" or raw_message is not None:
            raise RuntimeError("DISCORD_SIGNAL_JOURNAL_ACK_STATE_INVALID")
        return str(outcome), None
    if deleted != (source_event == "message_delete"):
        raise RuntimeError("DISCORD_SIGNAL_JOURNAL_ACK_STATE_INVALID")
    if deleted:
        if raw_message is not None:
            raise RuntimeError("DISCORD_SIGNAL_JOURNAL_ACK_STATE_INVALID")
        return str(outcome), None
    if not isinstance(raw_message, Mapping):
        raise RuntimeError("DISCORD_SIGNAL_JOURNAL_ACK_STATE_INVALID")
    message = DiscordFeedMessage.from_mapping(raw_message)
    if message.message_id != message_id:
        raise RuntimeError("DISCORD_SIGNAL_JOURNAL_ACK_MESSAGE_ID_INVALID")
    return str(outcome), message.as_dict()


@dataclass(frozen=True, slots=True)
class DiscordSignalFeedConfig:
    enabled: bool
    bridge_secret: str
    channel_ids: tuple[str, ...]
    author_id: str
    author_name: str
    retention_hours: int
    history_limit: int

    @classmethod
    def from_app_config(cls, config: AppConfig) -> DiscordSignalFeedConfig:
        raw_channel_ids = str(config.discord_signals_channel_ids or "").strip()
        author_id = str(config.discord_signals_author_id or "").strip()
        channel_ids = resolve_discord_signal_channel_ids(
            raw_channel_ids,
            field="discord_channel_ids",
        )
        if author_id:
            author_id = require_discord_snowflake(
                author_id,
                field="discord_author_id",
            )
        return cls(
            enabled=bool(config.discord_signals_enabled),
            bridge_secret=str(config.discord_signals_bridge_secret or "").strip(),
            channel_ids=channel_ids,
            author_id=author_id,
            author_name=(str(config.discord_signals_author_name or "GAA").strip() or "GAA"),
            retention_hours=min(
                max(int(config.discord_signals_retention_hours), 1),
                24 * 7,
            ),
            history_limit=min(
                max(int(config.discord_signals_history_limit), 1),
                1_000,
            ),
        )

    @property
    def configured(self) -> bool:
        return bool(self.channel_ids and self.author_id and len(self.bridge_secret) >= 32)

    @property
    def total_history_limit(self) -> int:
        return min(self.history_limit * max(len(self.channel_ids), 1), 50_000)

    def accepts_bridge_secret(self, candidate: object) -> bool:
        if not self.configured or not isinstance(candidate, str):
            return False
        return hmac.compare_digest(self.bridge_secret, candidate)

    def profile(self) -> DiscordSignalProfile:
        if not self.configured:
            raise RuntimeError("DISCORD_SIGNALS_NOT_CONFIGURED")
        return DiscordSignalProfile(
            source_id=(f"discord-rpc:{','.join(self.channel_ids)}:{self.author_id}"),
            author_id=self.author_id,
            author_name=self.author_name,
            channel_ids=self.channel_ids,
        )


class DiscordSignalFeed:
    """Durable-ingest boundary plus bounded projection of authenticated RPC events."""

    def __init__(
        self,
        config: DiscordSignalFeedConfig,
        *,
        upsert_messages: Callable[
            [Sequence[Mapping[str, Any]], str, datetime],
            Mapping[str, Mapping[str, Any]],
        ]
        | None = None,
        delete_message: Callable[[str, str, datetime], Mapping[str, Any]] | None = None,
    ) -> None:
        self.config = config
        self._upsert_messages = upsert_messages
        self._delete_message = delete_message
        self._lock = threading.RLock()
        self._rows: dict[str, dict[str, Any]] = {}
        self._latest_channel_message_ids = {channel_id: "" for channel_id in config.channel_ids}
        self._state = (
            "disabled"
            if not config.enabled
            else "configuration_required"
            if not config.configured
            else "waiting_for_companion"
        )
        self._companion_state = ""
        self._last_error = ""
        self._last_bridge_at = ""
        self._last_bridge_at_datetime: datetime | None = None
        self._last_bridge_instance_id = ""
        self._bridge_heartbeat_seconds = _DEFAULT_BRIDGE_HEARTBEAT_SECONDS
        self._content_unavailable_count = 0
        self._live_target_instrument_ids: tuple[str, ...] = ()
        self._target_instrument_ids: tuple[str, ...] = ()

    def restore(self, rows: Sequence[Mapping[str, Any]]) -> None:
        if len(rows) > self.config.total_history_limit:
            raise ValueError("DISCORD_SIGNAL_RESTORE_LIMIT_EXCEEDED")
        restored: dict[str, dict[str, Any]] = {}
        cutoff = datetime.now(UTC) - timedelta(hours=self.config.retention_hours)
        for raw_row in rows:
            message = DiscordFeedMessage.from_mapping(raw_row)
            if (
                message.channel_id not in self.config.channel_ids
                or message.author_id != self.config.author_id
                or message.published_at < cutoff
            ):
                raise ValueError("DISCORD_SIGNAL_RESTORE_SCOPE_INVALID")
            restored[message.message_id] = message.as_dict()
        if any(
            sum(row["channel_id"] == channel_id for row in restored.values())
            > self.config.history_limit
            for channel_id in self.config.channel_ids
        ):
            raise ValueError("DISCORD_SIGNAL_RESTORE_CHANNEL_LIMIT_EXCEEDED")
        with self._lock:
            self._rows = restored
            for channel_id in self.config.channel_ids:
                channel_ids = [
                    message_id
                    for message_id, row in restored.items()
                    if row["channel_id"] == channel_id
                ]
                self._latest_channel_message_ids[channel_id] = (
                    max(channel_ids, key=int) if channel_ids else ""
                )

    def set_targets(
        self,
        instrument_ids: tuple[str, ...],
        *,
        live_instrument_ids: tuple[str, ...] | None = None,
    ) -> None:
        exact_targets = tuple(sorted(set(instrument_ids)))
        exact_live_targets = tuple(
            sorted(set(exact_targets if live_instrument_ids is None else live_instrument_ids))
        )
        if not set(exact_targets).issubset(exact_live_targets):
            raise ValueError("DISCORD_SIGNAL_ACTIVE_TARGET_SCOPE_INVALID")
        with self._lock:
            self._live_target_instrument_ids = exact_live_targets
            self._target_instrument_ids = exact_targets
            if not self.config.enabled:
                self._state = "disabled"
            elif not self.config.configured:
                self._state = "configuration_required"
            elif not exact_targets:
                self._state = "dormant"
            elif self._companion_state:
                self._state = self._companion_state
            else:
                self._state = "waiting_for_companion"

    def ingest_bridge_envelope(self, envelope: Mapping[str, Any]) -> dict[str, Any]:
        contract = envelope.get("contract")
        if contract != DISCORD_RPC_BRIDGE_CONTRACT:
            raise ValueError("DISCORD_RPC_BRIDGE_CONTRACT_INVALID")
        event = str(envelope.get("event") or "").strip().lower()
        if event not in _BRIDGE_EVENTS:
            raise ValueError("DISCORD_RPC_BRIDGE_EVENT_INVALID")
        channel_id = require_discord_snowflake(
            envelope.get("channel_id"),
            field="channel_id",
        )
        if channel_id not in self.config.channel_ids:
            raise ValueError("DISCORD_RPC_BRIDGE_CHANNEL_MISMATCH")
        instance_id = str(envelope.get("instance_id") or "").strip()
        if not instance_id or len(instance_id) > 128:
            raise ValueError("DISCORD_RPC_BRIDGE_INSTANCE_INVALID")
        sent_at = str(envelope.get("sent_at") or "").strip()
        if not sent_at or len(sent_at) > 64:
            raise ValueError("DISCORD_RPC_BRIDGE_TIMESTAMP_INVALID")
        try:
            sent_at_datetime = datetime.fromisoformat(sent_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("DISCORD_RPC_BRIDGE_TIMESTAMP_INVALID") from exc
        if sent_at_datetime.tzinfo is None:
            raise ValueError("DISCORD_RPC_BRIDGE_TIMESTAMP_INVALID")
        sent_at_datetime = sent_at_datetime.astimezone(UTC)
        raw_heartbeat_seconds = envelope.get("heartbeat_seconds")
        if raw_heartbeat_seconds is not None:
            try:
                heartbeat_seconds = float(raw_heartbeat_seconds)
            except (TypeError, ValueError) as exc:
                raise ValueError("DISCORD_RPC_BRIDGE_HEARTBEAT_INVALID") from exc
            if not 5.0 <= heartbeat_seconds <= 3_600.0:
                raise ValueError("DISCORD_RPC_BRIDGE_HEARTBEAT_INVALID")
        else:
            heartbeat_seconds = _DEFAULT_BRIDGE_HEARTBEAT_SECONDS

        with self._lock:
            if (
                self._last_bridge_at_datetime is None
                or sent_at_datetime >= self._last_bridge_at_datetime
            ):
                self._last_bridge_at = sent_at_datetime.isoformat()
                self._last_bridge_at_datetime = sent_at_datetime
                self._last_bridge_instance_id = instance_id
                self._bridge_heartbeat_seconds = heartbeat_seconds

        journal_outcomes: dict[str, str] = {}
        if event == "status":
            self._ingest_status(envelope)
        elif event == "message_delete":
            message_id = require_discord_snowflake(
                envelope.get("message_id"),
                field="message_id",
            )
            with self._lock:
                if self._delete_message is None:
                    outcome = "applied" if message_id in self._rows else "ignored_unknown"
                    canonical_message = None
                else:
                    ack = self._delete_message(
                        message_id,
                        channel_id,
                        sent_at_datetime,
                    )
                    if not isinstance(ack, Mapping):
                        raise RuntimeError("DISCORD_SIGNAL_JOURNAL_ACK_INVALID")
                    outcome, canonical_message = _canonical_message_from_journal_ack(
                        ack,
                        message_id=message_id,
                    )
                if outcome == "ignored_unknown":
                    pass
                elif canonical_message is None:
                    self._rows.pop(message_id, None)
                else:
                    self._require_canonical_message_scope(canonical_message)
                    self._rows[message_id] = canonical_message
                journal_outcomes[message_id] = outcome
        else:
            raw_messages: object
            if event == "snapshot":
                raw_messages = envelope.get("messages")
            else:
                raw_messages = (envelope.get("message"),)
            if "coverage_start_message_id" in envelope or "coverage_end_message_id" in envelope:
                raise ValueError("DISCORD_RPC_BRIDGE_COVERAGE_UNSUPPORTED")
            if (
                isinstance(raw_messages, (str, bytes))
                or not isinstance(raw_messages, Sequence)
                or len(raw_messages) > self.config.history_limit
                or any(not isinstance(item, Mapping) for item in raw_messages)
            ):
                raise ValueError("DISCORD_RPC_BRIDGE_MESSAGES_INVALID")
            journal_outcomes = self._ingest_messages(
                raw_messages,
                channel_id=channel_id,
                source_event=event,
                observed_at=sent_at_datetime,
            )

        snapshot = self.snapshot()
        return {
            "accepted_messages": snapshot["status"]["accepted_messages"],
            "state": snapshot["status"]["state"],
            "journal_outcomes": journal_outcomes,
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            bridge_age_seconds = (
                max(
                    (datetime.now(UTC) - self._last_bridge_at_datetime).total_seconds(),
                    0.0,
                )
                if self._last_bridge_at_datetime is not None
                else None
            )
            effective_state = self._state
            if (
                effective_state == "companion_live"
                and bridge_age_seconds is not None
                and bridge_age_seconds > max(self._bridge_heartbeat_seconds * 3.0, 30.0)
            ):
                effective_state = "companion_stale"
            return {
                "rows": tuple(
                    sorted(
                        (dict(row) for row in self._rows.values()),
                        key=lambda row: (
                            row["published_at"],
                            int(row["message_id"]),
                        ),
                    )
                ),
                "status": {
                    "enabled": self.config.enabled,
                    "configured": self.config.configured,
                    "transport": "discord_desktop_rpc",
                    "state": effective_state,
                    "companion_state": self._companion_state,
                    "channel_ids": list(self.config.channel_ids),
                    "author_id": self.config.author_id,
                    "live_target_instrument_ids": list(self._live_target_instrument_ids),
                    "target_instrument_ids": list(self._target_instrument_ids),
                    "accepted_messages": len(self._rows),
                    "latest_channel_message_ids": dict(self._latest_channel_message_ids),
                    "last_bridge_at": self._last_bridge_at,
                    "last_bridge_instance_id": self._last_bridge_instance_id,
                    "last_bridge_age_seconds": (
                        round(bridge_age_seconds, 3) if bridge_age_seconds is not None else None
                    ),
                    "heartbeat_seconds": self._bridge_heartbeat_seconds,
                    "last_error": self._last_error,
                    "content_unavailable_count": self._content_unavailable_count,
                    "retention_hours": self.config.retention_hours,
                    "history_limit": self.config.history_limit,
                    "total_history_limit": self.config.total_history_limit,
                },
            }

    def _ingest_status(self, envelope: Mapping[str, Any]) -> None:
        raw_state = str(envelope.get("state") or "").strip().lower()
        allowed = {
            "starting",
            "authorizing",
            "live",
            "reconnecting",
            "error",
            "stopped",
        }
        if raw_state not in allowed:
            raise ValueError("DISCORD_RPC_BRIDGE_STATE_INVALID")
        detail = str(envelope.get("detail") or "").strip()[:500]
        companion_state = f"companion_{raw_state}"
        with self._lock:
            self._companion_state = companion_state
            if self._target_instrument_ids:
                self._state = companion_state
            self._last_error = detail if raw_state == "error" else ""

    def _ingest_messages(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        channel_id: str,
        source_event: str,
        observed_at: datetime,
    ) -> dict[str, str]:
        accepted: dict[str, dict[str, Any]] = {}
        latest_id = ""
        content_unavailable = 0
        for raw_message in messages:
            message_id = require_discord_snowflake(
                raw_message.get("id", raw_message.get("message_id")),
                field="message_id",
            )
            latest_id = max(
                (latest_id, message_id),
                key=lambda value: int(value or "0"),
            )
            author = raw_message.get("author")
            if not isinstance(author, Mapping):
                content_unavailable += 1
                continue
            author_id = require_discord_snowflake(
                author.get("id", raw_message.get("author_id")),
                field="author_id",
            )
            if author_id != self.config.author_id:
                continue
            content = raw_message.get("content")
            if not isinstance(content, str) or not content.strip():
                content_unavailable += 1
                continue
            author_name = str(
                author.get("global_name")
                or author.get("username")
                or raw_message.get("author_name")
                or self.config.author_name
            ).strip()
            message_reference = raw_message.get("message_reference")
            referenced_message = raw_message.get("referenced_message")
            reply_to_message_id = raw_message.get("reply_to_message_id")
            if reply_to_message_id is None and isinstance(
                message_reference,
                Mapping,
            ):
                reply_to_message_id = message_reference.get("message_id")
            if reply_to_message_id is None and isinstance(
                referenced_message,
                Mapping,
            ):
                reply_to_message_id = referenced_message.get("id")
            message = DiscordFeedMessage.from_mapping(
                {
                    "message_id": message_id,
                    "channel_id": channel_id,
                    "author_id": author_id,
                    "author_name": author_name or self.config.author_name,
                    "published_at": raw_message.get(
                        "timestamp",
                        raw_message.get("published_at"),
                    ),
                    "content": content,
                    "reply_to_message_id": reply_to_message_id,
                }
            )
            accepted[message_id] = message.as_dict()

        cutoff = datetime.now(UTC) - timedelta(hours=self.config.retention_hours)
        with self._lock:
            authoritative = accepted
            journal_outcomes = {message_id: "applied" for message_id in accepted}
            acks: Mapping[str, Mapping[str, Any]] | None = None
            if accepted and self._upsert_messages is not None:
                acks = self._upsert_messages(
                    tuple(accepted.values()),
                    source_event,
                    observed_at,
                )
            if acks is not None:
                if not isinstance(acks, Mapping) or set(accepted) != set(acks):
                    raise RuntimeError("DISCORD_SIGNAL_JOURNAL_ACK_SET_INVALID")
                authoritative = {}
                journal_outcomes = {}
                for message_id, ack in acks.items():
                    if not isinstance(ack, Mapping):
                        raise RuntimeError("DISCORD_SIGNAL_JOURNAL_ACK_INVALID")
                    outcome, canonical_message = _canonical_message_from_journal_ack(
                        ack,
                        message_id=message_id,
                    )
                    journal_outcomes[message_id] = outcome
                    if canonical_message is not None:
                        self._require_canonical_message_scope(canonical_message)
                        authoritative[message_id] = canonical_message
            for message_id in accepted:
                canonical_message = authoritative.get(message_id)
                if canonical_message is None:
                    self._rows.pop(message_id, None)
                else:
                    self._rows[message_id] = canonical_message
            if latest_id:
                self._latest_channel_message_ids[channel_id] = max(
                    (self._latest_channel_message_ids[channel_id], latest_id),
                    key=lambda value: int(value or "0"),
                )
            self._content_unavailable_count += content_unavailable
            self._rows = {
                message_id: row
                for message_id, row in self._rows.items()
                if DiscordFeedMessage.from_mapping(row).published_at >= cutoff
            }
            channel_message_ids = [
                message_id
                for message_id, row in sorted(
                    (
                        (message_id, row)
                        for message_id, row in self._rows.items()
                        if row["channel_id"] == channel_id
                    ),
                    key=lambda item: (
                        item[1]["published_at"],
                        int(item[0]),
                    ),
                )
            ]
            for message_id in channel_message_ids[: -self.config.history_limit]:
                self._rows.pop(message_id, None)
            if len(self._rows) > self.config.total_history_limit:
                retained = sorted(
                    self._rows.values(),
                    key=lambda row: (
                        row["published_at"],
                        int(row["message_id"]),
                    ),
                )[-self.config.total_history_limit :]
                self._rows = {row["message_id"]: row for row in retained}
            self._companion_state = "companion_live"
            if self._target_instrument_ids:
                self._state = "companion_live"
            self._last_error = ""
            return journal_outcomes

    def _require_canonical_message_scope(self, row: Mapping[str, Any]) -> None:
        message = DiscordFeedMessage.from_mapping(row)
        if (
            message.channel_id not in self.config.channel_ids
            or message.author_id != self.config.author_id
        ):
            raise RuntimeError("DISCORD_SIGNAL_JOURNAL_ACK_SCOPE_INVALID")
