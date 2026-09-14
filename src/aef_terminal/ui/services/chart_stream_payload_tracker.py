from __future__ import annotations

from collections.abc import Callable, Collection
from datetime import datetime
from typing import Any


class ChartStreamPayloadTracker:
    def __init__(
        self,
        *,
        parse_stream_ts: Callable[[dict[str, Any] | None], datetime | None],
        stream_bar_signature: Callable[[dict[str, Any] | None], tuple],
        resolve_payload: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None,
        initial_ts: datetime | None = None,
        max_signatures: int = 512,
    ) -> None:
        self._parse_stream_ts = parse_stream_ts
        self._stream_bar_signature = stream_bar_signature
        self._sent_signatures: dict[str, tuple] = {}
        self._sent_payloads: dict[str, dict[str, Any]] = {}
        self._invalidated_timestamps: set[str] = set()
        self._resolve_payload = resolve_payload
        if (
            isinstance(max_signatures, bool)
            or not isinstance(max_signatures, int)
            or max_signatures < 16
        ):
            raise ValueError("CHART_STREAM_TRACKER_MAX_SIGNATURES_INVALID")
        self._max_signatures = max_signatures
        self.last_sent_ts = initial_ts
        self.last_signature: tuple | None = None
        self.last_sent_closed = True

    def _payload_key(self, payload: dict[str, Any]) -> str:
        if not isinstance(payload, dict):
            raise TypeError("chart stream tracker payload must be a mapping")
        self._stream_bar_signature(payload)
        try:
            parsed = self._parse_stream_ts(payload)
        except (TypeError, ValueError) as exc:
            raise ValueError("CHART_STREAM_TRACKER_TIMESTAMP_INVALID") from exc
        if parsed is None:
            raise ValueError("CHART_STREAM_TRACKER_TIMESTAMP_INVALID")
        return parsed.isoformat()

    def _invalidation_key(self, value: str | datetime) -> str:
        if isinstance(value, datetime):
            raw = value.isoformat()
        elif isinstance(value, str) and value:
            raw = value
        else:
            raise TypeError("chart stream invalidation timestamp must be text or datetime")
        try:
            parsed = self._parse_stream_ts({"ts": raw})
        except (TypeError, ValueError) as exc:
            raise ValueError("CHART_STREAM_INVALIDATION_TIMESTAMP_INVALID") from exc
        if parsed is None:
            raise ValueError("CHART_STREAM_INVALIDATION_TIMESTAMP_INVALID")
        return parsed.isoformat()

    def invalidate_timestamps(self, timestamps: Collection[str | datetime]) -> int:
        """Require exact slot revisions to be delivered regardless of the live-tail cursor."""

        before = len(self._invalidated_timestamps)
        for timestamp in timestamps:
            self._invalidated_timestamps.add(self._invalidation_key(timestamp))
        return len(self._invalidated_timestamps) - before

    @property
    def invalidated_timestamps(self) -> tuple[str, ...]:
        return tuple(sorted(self._invalidated_timestamps))

    def remember(self, payloads: list[dict[str, Any]]) -> None:
        if not isinstance(payloads, list):
            raise TypeError("chart stream tracker payload batch must be a list")
        for payload in payloads:
            payload_key = self._payload_key(payload)
            signature = self._stream_bar_signature(payload)
            existing = self._sent_payloads.get(payload_key)
            resolved = (
                self._resolve_payload(existing, payload)
                if existing is not None and self._resolve_payload is not None
                else dict(payload)
            )
            self._sent_payloads[payload_key] = resolved
            self._sent_signatures[payload_key] = self._stream_bar_signature(resolved)
            self._invalidated_timestamps.discard(payload_key)
            payload_ts = self._parse_stream_ts(payload)
            assert payload_ts is not None
            if self.last_sent_ts is None or payload_ts >= self.last_sent_ts:
                self.last_sent_ts = payload_ts
                self.last_signature = signature
                self.last_sent_closed = payload["closed"]
        if len(self._sent_signatures) > self._max_signatures:
            stale_keys = sorted(self._sent_signatures)[
                : len(self._sent_signatures) - self._max_signatures
            ]
            for stale_key in stale_keys:
                self._sent_signatures.pop(stale_key, None)
                self._sent_payloads.pop(stale_key, None)

    def resolve(self, payload: dict[str, Any]) -> dict[str, Any]:
        payload_key = self._payload_key(payload)
        existing = self._sent_payloads.get(payload_key)
        if existing is not None and self._resolve_payload is not None:
            return self._resolve_payload(existing, payload)
        return dict(payload)

    def payload_is_new_or_changed(self, payload: dict[str, Any]) -> bool:
        payload_key = self._payload_key(payload)
        payload_ts = self._parse_stream_ts(payload)
        assert payload_ts is not None
        if payload_key in self._invalidated_timestamps:
            return True
        if payload_key in self._sent_payloads:
            resolved = self.resolve(payload)
            return self._stream_bar_signature(resolved) != self._sent_signatures.get(payload_key)
        return self.last_sent_ts is None or payload_ts >= self.last_sent_ts
