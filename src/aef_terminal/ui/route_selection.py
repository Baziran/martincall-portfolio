from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from aef_terminal.data.providers import route_instrument


MAX_ROUTE_SELECTIONS = 256


class RouteSelectionError(ValueError):
    """Typed rejection for one malformed exact-route selection payload."""

    def __init__(self, code: str, message: str | None = None) -> None:
        exact_code = str(code or "").strip()
        if not exact_code:
            raise ValueError("ROUTE_SELECTION_ERROR_CODE_REQUIRED")
        self.code = exact_code
        super().__init__(message or exact_code)


@dataclass(frozen=True)
class RequestedInstrumentRoute:
    instrument_id: str
    route_fingerprint: str

    @property
    def identity(self) -> tuple[str, str]:
        return self.instrument_id, self.route_fingerprint

    def payload(self) -> dict[str, str]:
        return {
            "instrument_id": self.instrument_id,
            "route_fingerprint": self.route_fingerprint,
        }


class RouteSelectionMismatch(RouteSelectionError):
    def __init__(
        self,
        expected: Sequence[tuple[str, str]],
        actual: Sequence[tuple[str, str]],
    ) -> None:
        self.expected = tuple(expected)
        self.actual = tuple(actual)
        super().__init__(
            "ROUTE_SELECTION_MISMATCH",
            (
                "ROUTE_SELECTION_MISMATCH "
                f"expected={json.dumps(self.expected, ensure_ascii=False)} "
                f"actual={json.dumps(self.actual, ensure_ascii=False)}"
            ),
        )


def parse_route_selection(value: str) -> tuple[RequestedInstrumentRoute, ...]:
    try:
        payload = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RouteSelectionError("ROUTES_JSON_INVALID") from exc
    if not isinstance(payload, list) or not payload:
        raise RouteSelectionError("ROUTES_ARRAY_REQUIRED")
    if len(payload) > MAX_ROUTE_SELECTIONS:
        raise RouteSelectionError(
            "ROUTES_LIMIT_EXCEEDED",
            f"ROUTES_LIMIT_EXCEEDED max={MAX_ROUTE_SELECTIONS}",
        )

    routes: list[RequestedInstrumentRoute] = []
    seen: set[tuple[str, str]] = set()
    fingerprints_by_instrument_id: dict[str, str] = {}
    required_fields = {"instrument_id", "route_fingerprint"}
    for item in payload:
        if not isinstance(item, dict) or set(item) != required_fields:
            raise RouteSelectionError("ROUTE_OBJECT_INVALID")
        instrument_id = item.get("instrument_id")
        route_fingerprint = item.get("route_fingerprint")
        if not isinstance(instrument_id, str) or not instrument_id:
            raise RouteSelectionError("ROUTE_INSTRUMENT_ID_REQUIRED")
        if not isinstance(route_fingerprint, str) or not route_fingerprint:
            raise RouteSelectionError("ROUTE_FINGERPRINT_REQUIRED")
        previous_fingerprint = fingerprints_by_instrument_id.get(instrument_id)
        if previous_fingerprint is not None and previous_fingerprint != route_fingerprint:
            raise RouteSelectionError("ROUTE_INSTRUMENT_CONFLICT")
        fingerprints_by_instrument_id[instrument_id] = route_fingerprint
        identity = (instrument_id, route_fingerprint)
        if identity in seen:
            continue
        seen.add(identity)
        routes.append(RequestedInstrumentRoute(*identity))
    return tuple(routes)


def route_selection_json(routes: Sequence[RequestedInstrumentRoute]) -> str:
    return json.dumps(
        [route.payload() for route in routes],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def resolve_route_selection(
    selected_instruments: Callable[[Sequence[str]], list[dict[str, Any]]],
    requested_routes: Sequence[RequestedInstrumentRoute],
) -> list[dict[str, Any]]:
    expected = tuple(route.identity for route in requested_routes)
    instruments = selected_instruments(tuple(route.instrument_id for route in requested_routes))
    actual = tuple(
        (route.instrument_id, route.fingerprint) for route in map(route_instrument, instruments)
    )
    if actual != expected:
        raise RouteSelectionMismatch(expected, actual)
    return instruments
