from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from aef_terminal.runtime.timeframes import require_aware_utc_datetime
from aef_terminal.storage.db_utils import ensure_utc


_MESSAGE_EVENTS = frozenset(("snapshot", "message_create", "message_update"))
_JOURNAL_OUTCOMES = frozenset(("applied", "replayed", "rejected", "ignored_unknown"))


def _exact_nonempty_text(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field} must be exact non-empty text")
    return value


def _nonempty_content(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("content must contain visible text")
    return value


def _journal_message_from_row(row: Sequence[Any]) -> dict[str, Any]:
    """Return the canonical normalized-message projection for one journal row."""

    if any(row[index] is None for index in (2, 3, 4, 5)):
        raise RuntimeError("DISCORD_SIGNAL_JOURNAL_ACTIVE_FACTS_REQUIRED")
    return {
        "message_id": str(row[0]),
        "channel_id": str(row[1]),
        "author_id": str(row[2]),
        "author_name": str(row[3]),
        "published_at": row[4].astimezone(UTC).isoformat(),
        "content": str(row[5]),
        "reply_to_message_id": str(row[6]) if row[6] is not None else None,
    }


def _journal_ack(
    row: Sequence[Any],
    *,
    message_id: str,
    outcome: str,
) -> dict[str, Any]:
    if outcome not in _JOURNAL_OUTCOMES:
        raise RuntimeError("DISCORD_SIGNAL_JOURNAL_OUTCOME_INVALID")
    deleted = row[9] is not None
    return {
        "message_id": str(row[0]),
        "outcome": outcome,
        "message": None if deleted else _journal_message_from_row(row),
        "deleted": deleted,
        "source_event": str(row[7]),
        "source_event_at": row[8].astimezone(UTC).isoformat(),
    }


def _message_matches_values(
    row: Sequence[Any],
    value: tuple[Any, ...],
) -> bool:
    return (
        row[9] is None
        and str(row[0]) == value[0]
        and str(row[1]) == value[1]
        and str(row[2]) == value[2]
        and str(row[3]) == value[3]
        and ensure_utc(row[4]) == value[4]
        and str(row[5]) == value[5]
        and (str(row[6]) if row[6] is not None else None) == value[6]
        and str(row[7]) == value[7]
    )


class DiscordSignalsRepoMixin:
    """Durable normalized-message journal for the Discord Signals service package."""

    def upsert_discord_signal_messages(
        self,
        rows: Sequence[Mapping[str, Any]],
        *,
        source_event: str,
        observed_at: datetime,
    ) -> dict[str, dict[str, Any]]:
        if source_event not in _MESSAGE_EVENTS:
            raise ValueError("DISCORD_SIGNAL_SOURCE_EVENT_INVALID")
        exact_observed_at = require_aware_utc_datetime(
            observed_at,
            field="observed_at",
        )
        values: list[tuple[Any, ...]] = []
        for row in rows:
            message_id = _exact_nonempty_text(row.get("message_id"), field="message_id")
            channel_id = _exact_nonempty_text(row.get("channel_id"), field="channel_id")
            author_id = _exact_nonempty_text(row.get("author_id"), field="author_id")
            author_name = _exact_nonempty_text(row.get("author_name"), field="author_name")
            content = _nonempty_content(row.get("content"))
            published_at = row.get("published_at")
            if isinstance(published_at, str):
                try:
                    published_at = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
                except ValueError as exc:
                    raise ValueError("published_at must be a timezone-aware timestamp") from exc
            exact_published_at = require_aware_utc_datetime(
                published_at,
                field="published_at",
            )
            reply_to = row.get("reply_to_message_id")
            if reply_to is not None:
                reply_to = _exact_nonempty_text(reply_to, field="reply_to_message_id")
            values.append(
                (
                    message_id,
                    channel_id,
                    author_id,
                    author_name,
                    exact_published_at,
                    content,
                    reply_to,
                    source_event,
                    exact_observed_at,
                )
            )
        if len({str(value[0]) for value in values}) != len(values):
            raise ValueError("DISCORD_SIGNAL_MESSAGE_IDS_MUST_BE_UNIQUE")
        if not values:
            return {}
        value_by_id = {str(value[0]): value for value in values}
        self._require_canonical_writer_lease()
        with self._connect() as conn:
            with conn.transaction(), conn.cursor() as cur:
                self._acquire_canonical_writer_fence_on_cursor(cur)
                cur.execute(
                    """
                    WITH incoming (
                        message_id,
                        channel_id,
                        author_id,
                        author_name,
                        published_at,
                        content,
                        reply_to_message_id,
                        source_event,
                        source_event_at
                    ) AS (
                        SELECT *
                        FROM unnest(
                            %s::text[],
                            %s::text[],
                            %s::text[],
                            %s::text[],
                            %s::timestamptz[],
                            %s::text[],
                            %s::text[],
                            %s::text[],
                            %s::timestamptz[]
                        )
                    )
                    INSERT INTO discord_signal_messages (
                        message_id,
                        channel_id,
                        author_id,
                        author_name,
                        published_at,
                        content,
                        reply_to_message_id,
                        source_event,
                        source_event_at
                    )
                    SELECT message_id,
                           channel_id,
                           author_id,
                           author_name,
                           published_at,
                           content,
                           reply_to_message_id,
                           source_event,
                           source_event_at
                    FROM incoming
                    ON CONFLICT (message_id) DO UPDATE
                    SET channel_id = EXCLUDED.channel_id,
                        author_id = EXCLUDED.author_id,
                        author_name = EXCLUDED.author_name,
                        published_at = EXCLUDED.published_at,
                        content = EXCLUDED.content,
                        reply_to_message_id = EXCLUDED.reply_to_message_id,
                        source_event = EXCLUDED.source_event,
                        source_event_at = EXCLUDED.source_event_at,
                        deleted_at = NULL,
                        updated_at = now()
                    WHERE EXCLUDED.source_event_at > discord_signal_messages.source_event_at
                      AND EXCLUDED.channel_id = discord_signal_messages.channel_id
                      AND (
                          discord_signal_messages.deleted_at IS NULL
                          OR EXCLUDED.source_event <> 'snapshot'
                      )
                    RETURNING message_id
                    """,
                    (
                        [value[0] for value in values],
                        [value[1] for value in values],
                        [value[2] for value in values],
                        [value[3] for value in values],
                        [value[4] for value in values],
                        [value[5] for value in values],
                        [value[6] for value in values],
                        [value[7] for value in values],
                        [value[8] for value in values],
                    ),
                )
                applied_ids = {str(row[0]) for row in cur.fetchall()}
                cur.execute(
                    """
                    SELECT message_id,
                           channel_id,
                           author_id,
                           author_name,
                           published_at,
                           content,
                           reply_to_message_id,
                           source_event,
                           source_event_at,
                           deleted_at
                    FROM discord_signal_messages
                    WHERE message_id = ANY(%s)
                    ORDER BY message_id
                    """,
                    (list(value_by_id),),
                )
                authoritative = {str(row[0]): row for row in cur.fetchall()}
                if set(authoritative) != set(value_by_id):
                    raise RuntimeError("DISCORD_SIGNAL_JOURNAL_ACK_STORAGE_INVALID")
                results: dict[str, dict[str, Any]] = {}
                for message_id, submitted in value_by_id.items():
                    row = authoritative[message_id]
                    matches = _message_matches_values(row, submitted)
                    stored_order = ensure_utc(row[8])
                    submitted_order = submitted[8]
                    if message_id in applied_ids:
                        if not matches or stored_order != submitted_order:
                            raise RuntimeError("DISCORD_SIGNAL_JOURNAL_APPLY_MISMATCH")
                        outcome = "applied"
                    elif matches:
                        outcome = "replayed"
                    elif stored_order > submitted_order:
                        outcome = "rejected"
                    elif stored_order == submitted_order:
                        outcome = "rejected"
                    elif str(row[1]) != submitted[1] or (
                        submitted[7] == "snapshot" and row[9] is not None
                    ):
                        outcome = "rejected"
                    else:
                        raise RuntimeError("DISCORD_SIGNAL_JOURNAL_ACK_STORAGE_INVALID")
                    results[message_id] = _journal_ack(
                        row,
                        message_id=message_id,
                        outcome=outcome,
                    )
        return results

    def mark_discord_signal_message_deleted(
        self,
        *,
        message_id: str,
        channel_id: str,
        observed_at: datetime,
    ) -> dict[str, Any]:
        exact_message_id = _exact_nonempty_text(message_id, field="message_id")
        exact_channel_id = _exact_nonempty_text(channel_id, field="channel_id")
        exact_observed_at = require_aware_utc_datetime(
            observed_at,
            field="observed_at",
        )
        self._require_canonical_writer_lease()
        with self._connect() as conn:
            with conn.transaction(), conn.cursor() as cur:
                self._acquire_canonical_writer_fence_on_cursor(cur)
                cur.execute(
                    """
                    INSERT INTO discord_signal_messages (
                        message_id,
                        channel_id,
                        author_id,
                        author_name,
                        published_at,
                        content,
                        reply_to_message_id,
                        source_event,
                        source_event_at,
                        deleted_at
                    )
                    VALUES (
                        %s,
                        %s,
                        NULL,
                        NULL,
                        NULL,
                        NULL,
                        NULL,
                        'message_delete',
                        %s,
                        %s
                    )
                    ON CONFLICT (message_id) DO UPDATE
                    SET author_id = NULL,
                        author_name = NULL,
                        published_at = NULL,
                        content = NULL,
                        reply_to_message_id = NULL,
                        source_event = 'message_delete',
                        source_event_at = EXCLUDED.source_event_at,
                        deleted_at = EXCLUDED.deleted_at,
                        updated_at = now()
                    WHERE EXCLUDED.channel_id = discord_signal_messages.channel_id
                      AND EXCLUDED.source_event_at > discord_signal_messages.source_event_at
                    RETURNING message_id,
                              channel_id,
                              author_id,
                              author_name,
                              published_at,
                              content,
                              reply_to_message_id,
                              source_event,
                              source_event_at,
                              deleted_at
                    """,
                    (
                        exact_message_id,
                        exact_channel_id,
                        exact_observed_at,
                        exact_observed_at,
                    ),
                )
                applied = cur.fetchone()
                if applied is not None:
                    return _journal_ack(
                        applied,
                        message_id=exact_message_id,
                        outcome="applied",
                    )
                cur.execute(
                    """
                    SELECT message_id,
                           channel_id,
                           author_id,
                           author_name,
                           published_at,
                           content,
                           reply_to_message_id,
                           source_event,
                           source_event_at,
                           deleted_at
                    FROM discord_signal_messages
                    WHERE message_id = %s
                      AND channel_id = %s
                    """,
                    (exact_message_id, exact_channel_id),
                )
                authoritative = cur.fetchone()
                if authoritative is None:
                    return {
                        "message_id": exact_message_id,
                        "outcome": "ignored_unknown",
                        "message": None,
                        "deleted": False,
                        "source_event": "message_delete",
                        "source_event_at": exact_observed_at.isoformat(),
                    }
                stored_order = ensure_utc(authoritative[8])
                deleted = authoritative[9] is not None
                if (
                    deleted
                    and str(authoritative[7]) == "message_delete"
                    and stored_order == exact_observed_at
                ):
                    outcome = "replayed"
                elif stored_order > exact_observed_at:
                    outcome = "rejected"
                elif stored_order == exact_observed_at:
                    outcome = "rejected"
                else:
                    raise RuntimeError("DISCORD_SIGNAL_JOURNAL_ACK_STORAGE_INVALID")
                return _journal_ack(
                    authoritative,
                    message_id=exact_message_id,
                    outcome=outcome,
                )

    def read_discord_signal_messages(
        self,
        *,
        channel_ids: Sequence[str],
        author_id: str,
        published_since: datetime,
        limit: int,
    ) -> list[dict[str, Any]]:
        exact_channels = tuple(
            _exact_nonempty_text(channel_id, field="channel_id") for channel_id in channel_ids
        )
        if not exact_channels or len(set(exact_channels)) != len(exact_channels):
            raise ValueError("channel_ids must contain unique exact values")
        exact_author_id = _exact_nonempty_text(author_id, field="author_id")
        exact_since = require_aware_utc_datetime(
            published_since,
            field="published_since",
        )
        exact_limit = int(limit)
        if isinstance(limit, bool) or not 1 <= exact_limit <= 1_000:
            raise ValueError("limit must be between 1 and 1000 per channel")
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    WITH ranked_messages AS (
                        SELECT message_id,
                               channel_id,
                               author_id,
                               author_name,
                               published_at,
                               content,
                               reply_to_message_id,
                               row_number() OVER (
                                   PARTITION BY channel_id
                                   ORDER BY published_at DESC, message_id::numeric DESC
                               ) AS channel_rank
                        FROM discord_signal_messages
                        WHERE channel_id = ANY(%s)
                          AND author_id = %s
                          AND published_at >= %s
                          AND deleted_at IS NULL
                    )
                    SELECT message_id,
                           channel_id,
                           author_id,
                           author_name,
                           published_at,
                           content,
                           reply_to_message_id
                    FROM ranked_messages
                    WHERE channel_rank <= %s
                    ORDER BY published_at ASC, message_id::numeric ASC
                    """,
                    (list(exact_channels), exact_author_id, exact_since, exact_limit),
                )
                rows = cur.fetchall()
        return [_journal_message_from_row(row) for row in rows]
