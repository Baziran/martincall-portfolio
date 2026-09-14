from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from aef_terminal.alerts.delivery_contract import TELEGRAM_CANCELLED_DELIVERY_STATE
from aef_terminal.alerts.definition_identity import price_alert_definition_identity
from aef_terminal.alerts.runtime_contract import (
    PRICE_ALERT_DIRECTIONS,
    PRICE_ALERT_KINDS,
    validate_price_alert,
)
from aef_terminal.data.gex.constants import (
    GEX_CONTEXT_MAX_LEVELS,
    GEX_CONTEXT_STALE_MINUTES,
    GEX_REQUEST_SNAPSHOT_SOURCE,
)
from aef_terminal.data.gex.contracts import (
    require_gex_market_data_entitlement,
    require_gex_option_universe_expiry,
)
from aef_terminal.data.gex.history import read_latest_gex_payload
from aef_terminal.data.gex.payload_contract import require_gex_levels
from aef_terminal.data.instrument_identity import require_exact_identity_text
from aef_terminal.data.providers import route_instrument
from aef_terminal.engine.common import parse_aware_utc_ts
from aef_terminal.runtime.timeframes import CHART_HISTORY_INTERVALS
from aef_terminal.ui.paper.utils import math_is_finite
from aef_terminal.ui.price_alert_services import (
    normalize_price_alert,
    price_alert_client_projection,
)


AlertStoreFactory = Callable[[], Any]
InstrumentLookup = Callable[[str], dict[str, Any]]

_ALERT_TYPES = frozenset({"price"})
_ALERT_ACTIONS = frozenset(
    {
        "list",
        "create_price",
        "create",
        "gex_levels",
        "create_gex",
        "update",
        "rearm",
        "enable",
        "disable",
        "delete",
        "delete_scope",
        "resolve",
    }
)


class AlertCommandError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = str(code)


def alert_callback_token(
    alert_id: str,
    instrument_id: str,
    route_fingerprint: str,
    *,
    alert_type: str = "price",
) -> str:
    """Return one Telegram-safe token for an exact persisted alert route."""

    from aef_terminal.alerts.telegram import telegram_callback_token

    normalized_type = _alert_type(alert_type)
    exact_id = require_exact_identity_text(alert_id, field="ALERT_ID")
    exact_instrument_id = require_exact_identity_text(
        instrument_id,
        field="ALERT_INSTRUMENT_ID",
    )
    exact_route_fingerprint = require_exact_identity_text(
        route_fingerprint,
        field="ALERT_ROUTE_FINGERPRINT",
    )
    return telegram_callback_token(
        json.dumps(
            [exact_id, exact_instrument_id, exact_route_fingerprint],
            separators=(",", ":"),
        ),
        namespace=f"alert:{normalized_type}",
    )


def execute_alert_command(
    action: str,
    payload: Mapping[str, Any] | None,
    *,
    store_factory: AlertStoreFactory,
    instrument_lookup: InstrumentLookup,
) -> dict[str, Any]:
    """Execute one PostgreSQL-authoritative alert command.

    Telegram and browser transports pass commands here; neither transport mutates
    alert payloads or resolves instrument identity itself.
    """

    command = str(action or "").strip().lower()
    request = dict(payload or {})
    if command not in _ALERT_ACTIONS:
        return _error("ALERT_COMMAND_UNKNOWN", f"unknown alert command: {command or '-'}")
    try:
        store = store_factory()
        if store is None:
            raise AlertCommandError("ALERT_STORAGE_NOT_CONFIGURED", "storage not configured")
        if command == "list":
            return _list_alerts(store, instrument_lookup, request)
        if command == "resolve":
            resolved = _resolve_callback(store, request)
            requested_type = str(request.get("alert_type") or "").strip().lower()
            if requested_type and _alert_type(requested_type) != resolved["alert_type"]:
                raise AlertCommandError(
                    "ALERT_CALLBACK_TYPE_MISMATCH",
                    "callback token does not match the requested alert type",
                )
            _current_route(
                instrument_lookup,
                resolved["instrument_id"],
                resolved["route_fingerprint"],
            )
            return resolved
        if command == "create_price":
            return _create_fixed_price_alert(store, instrument_lookup, request)
        if command == "create":
            return _create_price_alert(store, instrument_lookup, request)
        if command == "gex_levels":
            return _gex_levels(store, instrument_lookup, request)
        if command == "create_gex":
            return _create_gex_alert(store, instrument_lookup, request)
        if command == "delete_scope":
            return _delete_alert_scope(store, instrument_lookup, request)
        return _mutate_alert(store, instrument_lookup, command, request)
    except AlertCommandError as exc:
        return _error(exc.code, str(exc))
    except (TypeError, ValueError) as exc:
        return _error("ALERT_COMMAND_INVALID", str(exc))
    except Exception as exc:
        return _error("ALERT_COMMAND_FAILED", str(exc), retryable=True)


def _error(code: str, message: str, *, retryable: bool = False) -> dict[str, Any]:
    return {
        "ok": False,
        "code": str(code),
        "message": str(message),
        "retryable": bool(retryable),
    }


def _alert_type(value: Any) -> str:
    normalized = str(value or "price").strip().lower()
    if normalized not in _ALERT_TYPES:
        raise AlertCommandError(
            "ALERT_TYPE_INVALID", f"unsupported alert type: {normalized or '-'}"
        )
    return normalized


def _direction(value: Any) -> str:
    normalized = str(value or "cross").strip().lower()
    if normalized not in PRICE_ALERT_DIRECTIONS:
        raise AlertCommandError(
            "ALERT_DIRECTION_INVALID", f"unsupported alert direction: {normalized or '-'}"
        )
    return normalized


def _finite_price(value: Any, *, field: str = "price") -> float:
    if isinstance(value, bool):
        raise AlertCommandError("ALERT_PRICE_INVALID", f"{field} must be a finite number")
    try:
        price = float(value)
    except (TypeError, ValueError) as exc:
        raise AlertCommandError("ALERT_PRICE_INVALID", f"{field} must be a finite number") from exc
    if not math_is_finite(price):
        raise AlertCommandError("ALERT_PRICE_INVALID", f"{field} must be a finite number")
    return price


def _timeframe(value: Any) -> str:
    timeframe = require_exact_identity_text(value, field="ALERT_TIMEFRAME")
    if timeframe not in CHART_HISTORY_INTERVALS:
        raise AlertCommandError(
            "ALERT_TIMEFRAME_INVALID",
            f"unsupported alert timeframe: {timeframe}",
        )
    return timeframe


def _current_route(
    instrument_lookup: InstrumentLookup,
    instrument_id: Any,
    route_fingerprint: Any,
) -> Any:
    exact_instrument_id = require_exact_identity_text(
        instrument_id,
        field="ALERT_INSTRUMENT_ID",
    )
    expected_fingerprint = require_exact_identity_text(
        route_fingerprint,
        field="ALERT_ROUTE_FINGERPRINT",
    )
    try:
        route = route_instrument(instrument_lookup(exact_instrument_id))
    except Exception as exc:
        raise AlertCommandError(
            "ALERT_INSTRUMENT_UNAVAILABLE",
            f"alert instrument is unavailable: {exc}",
        ) from exc
    if route.instrument_id != exact_instrument_id or route.fingerprint != expected_fingerprint:
        raise AlertCommandError(
            "ALERT_ROUTE_MISMATCH",
            "persisted alert route does not match the current qualified instrument",
        )
    return route


def _route_for_creation(
    instrument_lookup: InstrumentLookup,
    instrument_id: Any,
) -> Any:
    exact_instrument_id = require_exact_identity_text(
        instrument_id,
        field="ALERT_INSTRUMENT_ID",
    )
    try:
        route = route_instrument(instrument_lookup(exact_instrument_id))
    except Exception as exc:
        raise AlertCommandError(
            "ALERT_INSTRUMENT_UNAVAILABLE",
            f"alert instrument is unavailable: {exc}",
        ) from exc
    if route.instrument_id != exact_instrument_id:
        raise AlertCommandError(
            "ALERT_INSTRUMENT_MISMATCH",
            "instrument lookup returned a different qualified instrument",
        )
    return route


def _route_is_current(instrument_lookup: InstrumentLookup, item: Mapping[str, Any]) -> bool:
    try:
        _current_route(
            instrument_lookup,
            item.get("instrument_id"),
            item.get("route_fingerprint"),
        )
    except AlertCommandError, ValueError:
        return False
    return True


def _status(*, enabled: bool, armed: bool, fired: bool, route_valid: bool) -> str:
    if not route_valid:
        return "blocked"
    if not enabled:
        return "disabled"
    if fired:
        return "fired"
    return "active" if armed else "disabled"


def _price_alert_item(
    alert: dict[str, Any],
    instrument_lookup: InstrumentLookup,
) -> dict[str, Any] | None:
    normalized = normalize_price_alert(
        alert,
        timeframe=alert.get("timeframe"),
        instrument_id=alert.get("instrument_id"),
        route_fingerprint=alert.get("route_fingerprint"),
        provider=alert.get("provider"),
        provider_contract_id=alert.get("provider_contract_id"),
    )
    validate_price_alert(normalized)
    alert_id = normalized["id"]
    instrument_id = normalized["instrument_id"]
    route_fingerprint = normalized["route_fingerprint"]
    price = normalized["price"]
    direction = normalized["direction"]
    route_valid = _route_is_current(instrument_lookup, normalized)
    enabled = normalized["enabled"]
    armed = normalized["armed"]
    fired = normalized["fired"]
    return {
        "alert_type": "price",
        "alert_id": alert_id,
        "callback_token": alert_callback_token(
            alert_id,
            instrument_id,
            route_fingerprint,
            alert_type="price",
        ),
        "instrument_id": instrument_id,
        "route_fingerprint": route_fingerprint,
        "symbol": normalized["symbol"],
        "timeframe": normalized["timeframe"],
        "kind": normalized["kind"],
        "label": normalized["label"],
        "direction": direction,
        "price": price,
        "enabled": enabled,
        "armed": armed,
        "fired": fired,
        "status": _status(
            enabled=enabled,
            armed=armed,
            fired=fired,
            route_valid=route_valid,
        ),
        "route_valid": route_valid,
        "level_source": dict(normalized["level_source"]),
        "definition_identity": price_alert_definition_identity(normalized),
    }


def _all_alert_items(store: Any, instrument_lookup: InstrumentLookup) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for alert in store.read_all_price_alerts():
        if not isinstance(alert, dict):
            raise ValueError("PRICE_ALERT_ROW_INVALID: stored alert must be an object")
        normalized = _price_alert_item(alert, instrument_lookup)
        if normalized is not None:
            items.append(normalized)
    return sorted(
        items,
        key=lambda item: (
            {"fired": 0, "active": 1, "disabled": 2, "blocked": 3}.get(item["status"], 4),
            str(item.get("symbol") or ""),
            str(item.get("timeframe") or ""),
            str(item.get("alert_id") or ""),
        ),
    )


def _list_alerts(
    store: Any,
    instrument_lookup: InstrumentLookup,
    request: Mapping[str, Any],
) -> dict[str, Any]:
    items = _all_alert_items(store, instrument_lookup)
    counts = {
        status: sum(1 for item in items if item["status"] == status)
        for status in ("active", "fired", "disabled", "blocked")
    }
    requested_status = str(request.get("status") or "").strip().lower()
    if requested_status and requested_status != "all":
        if requested_status not in {"active", "fired", "disabled", "blocked"}:
            raise AlertCommandError(
                "ALERT_STATUS_INVALID",
                f"unsupported alert status: {requested_status}",
            )
        items = [item for item in items if item["status"] == requested_status]
    requested_type = str(request.get("alert_type") or "").strip().lower()
    if requested_type:
        normalized_type = _alert_type(requested_type)
        items = [item for item in items if item["alert_type"] == normalized_type]
    return {
        "ok": True,
        "counts": counts,
        "total": len(items),
        "alerts": items,
    }


def _resolve_callback(
    store: Any,
    request: Mapping[str, Any],
) -> dict[str, Any]:
    callback_token = require_exact_identity_text(
        request.get("callback_token"),
        field="ALERT_CALLBACK_TOKEN",
    )
    matches: list[tuple[str, str, str]] = []
    for alert in store.read_all_price_alerts():
        if not isinstance(alert, dict):
            raise ValueError("PRICE_ALERT_ROW_INVALID: stored alert must be an object")
        normalized = normalize_price_alert(
            alert,
            timeframe=alert.get("timeframe"),
            instrument_id=alert.get("instrument_id"),
            route_fingerprint=alert.get("route_fingerprint"),
            provider=alert.get("provider"),
            provider_contract_id=alert.get("provider_contract_id"),
        )
        validate_price_alert(normalized)
        alert_id = normalized["id"]
        instrument_id = normalized["instrument_id"]
        route_fingerprint = normalized["route_fingerprint"]
        if (
            alert_callback_token(
                alert_id,
                instrument_id,
                route_fingerprint,
            )
            == callback_token
        ):
            matches.append((alert_id, instrument_id, route_fingerprint))
    unique_keys = sorted(set(matches))
    if not unique_keys:
        raise AlertCommandError("ALERT_CALLBACK_NOT_FOUND", "alert callback token is not current")
    if len(unique_keys) != 1:
        raise AlertCommandError("ALERT_CALLBACK_AMBIGUOUS", "alert callback token is ambiguous")
    alert_id, instrument_id, route_fingerprint = unique_keys[0]
    alert = store.read_price_alert_exact(alert_id, instrument_id, route_fingerprint)
    if not isinstance(alert, dict):
        raise AlertCommandError("ALERT_CALLBACK_NOT_FOUND", "alert callback token is not current")
    normalized = normalize_price_alert(
        alert,
        timeframe=alert.get("timeframe"),
        instrument_id=alert.get("instrument_id"),
        route_fingerprint=alert.get("route_fingerprint"),
        provider=alert.get("provider"),
        provider_contract_id=alert.get("provider_contract_id"),
    )
    validate_price_alert(normalized)
    return {
        "ok": True,
        "alert_type": "price",
        "alert_id": alert_id,
        "callback_token": callback_token,
        "instrument_id": instrument_id,
        "route_fingerprint": route_fingerprint,
        "alert": normalized,
    }


def _created_at_ms() -> int:
    return int(datetime.now(tz=UTC).timestamp() * 1000)


def _new_price_alert_id(prefix: str = "pa") -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _create_fixed_price_alert(
    store: Any,
    instrument_lookup: InstrumentLookup,
    request: Mapping[str, Any],
) -> dict[str, Any]:
    route = _route_for_creation(
        instrument_lookup,
        request.get("instrument_id"),
    )
    timeframe = _timeframe(request.get("timeframe"))
    price = _finite_price(request.get("price"))
    direction = _direction(request.get("direction"))
    return _persist_price_alert(
        store,
        route,
        timeframe=timeframe,
        price=price,
        direction=direction,
        label=str(request.get("label") or ""),
        level_source={"type": "fixed_price", "dynamic": False},
        alert_id=_new_price_alert_id(),
    )


def _non_negative_number(value: Any, *, field: str) -> float:
    number = _finite_price(value, field=field)
    if number < 0:
        raise AlertCommandError("ALERT_DEFINITION_INVALID", f"{field} must be non-negative")
    return number


def _rearm_minutes(value: Any) -> int:
    if isinstance(value, bool):
        raise AlertCommandError("ALERT_DEFINITION_INVALID", "rearmMinutes is invalid")
    try:
        minutes = int(value)
    except (TypeError, ValueError) as exc:
        raise AlertCommandError("ALERT_DEFINITION_INVALID", "rearmMinutes is invalid") from exc
    if minutes not in {15, 30, 60}:
        raise AlertCommandError("ALERT_DEFINITION_INVALID", "rearmMinutes is invalid")
    return minutes


def _create_price_alert(
    store: Any,
    instrument_lookup: InstrumentLookup,
    request: Mapping[str, Any],
) -> dict[str, Any]:
    route = _current_route(
        instrument_lookup,
        request.get("instrument_id"),
        request.get("route_fingerprint"),
    )
    kind = str(request.get("kind") or "").strip().lower()
    if kind not in PRICE_ALERT_KINDS:
        raise AlertCommandError("ALERT_KIND_INVALID", f"unsupported alert kind: {kind or '-'}")
    label = request.get("label")
    if not isinstance(label, str):
        raise AlertCommandError("ALERT_DEFINITION_INVALID", "label must be a string")
    level_source = (
        {"type": "fixed_price", "dynamic": False}
        if kind == "price"
        else {"type": kind, "dynamic": True}
    )
    return _persist_price_alert(
        store,
        route,
        timeframe=_timeframe(request.get("timeframe")),
        price=_finite_price(request.get("price")),
        direction=_direction(request.get("direction")),
        label=label,
        level_source=level_source,
        alert_id=require_exact_identity_text(request.get("alert_id"), field="ALERT_ID"),
        kind=kind,
        tolerance_atr=_non_negative_number(request.get("toleranceAtr"), field="toleranceAtr"),
        tolerance_points=_non_negative_number(
            request.get("tolerancePoints"), field="tolerancePoints"
        ),
        rearm_minutes=_rearm_minutes(request.get("rearmMinutes")),
    )


def _persist_price_alert(
    store: Any,
    route: Any,
    *,
    timeframe: str,
    price: float,
    direction: str,
    label: str,
    level_source: dict[str, Any],
    alert_id: str,
    kind: str = "price",
    tolerance_atr: float = 0.08,
    tolerance_points: float = 0.0,
    rearm_minutes: int = 60,
) -> dict[str, Any]:
    now_ms = _created_at_ms()
    candidate = normalize_price_alert(
        {
            "id": alert_id,
            "instrument_id": route.instrument_id,
            "route_fingerprint": route.fingerprint,
            "provider": route.provider,
            "provider_contract_id": route.adapter.session_contract_id(route.instrument),
            "symbol": route.instrument_key,
            "timeframe": timeframe,
            "price": price,
            "kind": kind,
            "label": label,
            "direction": direction,
            "toleranceAtr": tolerance_atr,
            "tolerancePoints": tolerance_points,
            "enabled": True,
            "armed": True,
            "fired": False,
            "cooldownUntil": 0,
            "rearmedAt": now_ms,
            "rearmMinutes": rearm_minutes,
            "createdAt": now_ms,
            "level_source": dict(level_source),
            **TELEGRAM_CANCELLED_DELIVERY_STATE,
        },
        timeframe=timeframe,
        instrument_id=route.instrument_id,
        route_fingerprint=route.fingerprint,
        provider=route.provider,
        provider_contract_id=route.adapter.session_contract_id(route.instrument),
    )
    validate_price_alert(candidate)
    result = store.create_or_rearm_price_alert_exact(
        candidate,
        rearmed_at=now_ms,
    )
    saved = result.get("payload") if isinstance(result, Mapping) else None
    created = result.get("created") if isinstance(result, Mapping) else None
    if not isinstance(saved, dict) or not isinstance(created, bool):
        raise AlertCommandError(
            "ALERT_CONCURRENT_CHANGE",
            "price alert changed concurrently",
        )
    item = _price_alert_item(saved, lambda _instrument_id: route.instrument)
    return {
        "ok": True,
        "created": created,
        "alert": item,
        "payload": price_alert_client_projection(saved),
    }


def _latest_publishable_gex(
    store: Any,
    route: Any,
) -> tuple[dict[str, Any], dict[str, Any], datetime, float]:
    latest = read_latest_gex_payload(
        route.instrument_id,
        route.fingerprint,
        store=store,
    )
    if latest is None:
        raise AlertCommandError(
            "ALERT_GEX_SNAPSHOT_MISSING", "no persisted GEX snapshot is available"
        )
    row, payload = latest
    if (
        row.get("instrument_id") != route.instrument_id
        or row.get("route_fingerprint") != route.fingerprint
        or payload.get("instrument_id") != route.instrument_id
        or payload.get("route_fingerprint") != route.fingerprint
    ):
        raise AlertCommandError(
            "ALERT_GEX_IDENTITY_MISMATCH", "persisted GEX snapshot identity mismatch"
        )
    diagnostics = (
        payload.get("diagnostics") if isinstance(payload.get("diagnostics"), Mapping) else {}
    )
    try:
        market_data_entitlement = require_gex_market_data_entitlement(
            payload.get("market_data_entitlement") or "unknown",
            allow_unknown=True,
        )
    except ValueError:
        market_data_entitlement = "unknown"
    if market_data_entitlement != "live":
        raise AlertCommandError(
            "ALERT_GEX_SNAPSHOT_DISPLAY_ONLY",
            f"latest GEX snapshot has market_data_entitlement={market_data_entitlement}; "
            "GEX alert creation requires LIVE market data",
        )
    if (
        payload.get("decision_authoritative") is not True
        or diagnostics.get("diag_frame_publishable") is False
    ):
        raise AlertCommandError(
            "ALERT_GEX_SNAPSHOT_PARTIAL", "latest GEX snapshot is not publishable"
        )
    captured_at = parse_aware_utc_ts(payload.get("captured_at") or row.get("captured_at"))
    if captured_at is None:
        raise AlertCommandError(
            "ALERT_GEX_TIMESTAMP_INVALID", "persisted GEX snapshot timestamp is invalid"
        )
    try:
        option_universe_expires_at = require_gex_option_universe_expiry(
            payload.get("option_universe_expires_at"),
            captured_at=captured_at,
            allow_unknown=True,
        )
    except ValueError as exc:
        raise AlertCommandError(
            "ALERT_GEX_OPTION_UNIVERSE_INVALID",
            "persisted GEX snapshot has invalid exact option expiry facts",
        ) from exc
    if option_universe_expires_at is None:
        raise AlertCommandError(
            "ALERT_GEX_OPTION_UNIVERSE_UNKNOWN",
            "persisted GEX snapshot has no exact provider option expiry",
        )
    now_utc = datetime.now(tz=UTC)
    if option_universe_expires_at <= now_utc:
        raise AlertCommandError(
            "ALERT_GEX_OPTION_UNIVERSE_EXPIRED",
            "persisted GEX snapshot option universe has expired",
        )
    age_minutes = max(0.0, (now_utc - captured_at).total_seconds() / 60.0)
    if age_minutes > float(GEX_CONTEXT_STALE_MINUTES):
        raise AlertCommandError(
            "ALERT_GEX_SNAPSHOT_STALE",
            f"latest GEX snapshot is {age_minutes:.0f} minutes old",
        )
    return dict(row), dict(payload), captured_at, age_minutes


def _gex_level_choices(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    choices: list[dict[str, Any]] = []
    for level_ref, field, label in (
        ("CALL_WALL", "call_wall", "CALL WALL"),
        ("PUT_WALL", "put_wall", "PUT WALL"),
        ("GAMMA_FLIP", "gamma_flip", "GAMMA FLIP"),
    ):
        value = payload.get(field)
        try:
            price = _finite_price(value, field=field)
        except AlertCommandError:
            continue
        choices.append(
            {
                "selector": level_ref,
                "level_kind": level_ref,
                "label": label,
                "price": price,
            }
        )
    raw_levels = payload.get("levels") if isinstance(payload.get("levels"), list) else []
    if raw_levels:
        try:
            levels = require_gex_levels(
                raw_levels,
                max_levels=GEX_CONTEXT_MAX_LEVELS,
                spot=payload.get("spot"),
            )
        except (TypeError, ValueError) as exc:
            raise AlertCommandError(
                "ALERT_GEX_LEVEL_CONTRACT_INVALID",
                f"persisted GEX levels are invalid: {exc}",
            ) from exc
    else:
        levels = []
    for index, level in enumerate(levels):
        kind = level["kind"]
        if kind in {"CALL_WALL", "PUT_WALL"}:
            continue
        try:
            price = _finite_price(
                level.get("price"),
                field="GEX level price",
            )
        except AlertCommandError:
            continue
        choices.append(
            {
                "selector": f"NODE_{index}",
                "level_kind": kind,
                "label": kind,
                "price": price,
                "strength": level.get("strength"),
            }
        )
    return choices


def _gex_snapshot_version(
    captured_at: datetime,
    *,
    route: Any,
    source: str,
    choices: list[dict[str, Any]],
) -> str:
    from aef_terminal.alerts.telegram import telegram_callback_token

    return telegram_callback_token(
        json.dumps(
            {
                "captured_at": captured_at.isoformat(),
                "levels": choices,
            },
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ),
        namespace=(f"gex-snapshot:{route.instrument_id}:{route.fingerprint}:{source}"),
    )


def _gex_levels(
    store: Any,
    instrument_lookup: InstrumentLookup,
    request: Mapping[str, Any],
) -> dict[str, Any]:
    route = _route_for_creation(
        instrument_lookup,
        request.get("instrument_id"),
    )
    row, payload, captured_at, age_minutes = _latest_publishable_gex(store, route)
    snapshot_source = str(row.get("source") or GEX_REQUEST_SNAPSHOT_SOURCE)
    choices = _gex_level_choices(payload)
    return {
        "ok": True,
        "instrument_id": route.instrument_id,
        "route_fingerprint": route.fingerprint,
        "captured_at": captured_at.isoformat(),
        "snapshot_version": _gex_snapshot_version(
            captured_at,
            route=route,
            source=snapshot_source,
            choices=choices,
        ),
        "snapshot_source": snapshot_source,
        "market_data_entitlement": payload["market_data_entitlement"],
        "decision_authoritative": payload["decision_authoritative"],
        "age_minutes": round(age_minutes, 2),
        "levels": choices,
    }


def _create_gex_alert(
    store: Any,
    instrument_lookup: InstrumentLookup,
    request: Mapping[str, Any],
) -> dict[str, Any]:
    route = _route_for_creation(
        instrument_lookup,
        request.get("instrument_id"),
    )
    timeframe = _timeframe(request.get("timeframe"))
    row, snapshot, captured_at, age_minutes = _latest_publishable_gex(store, route)
    snapshot_source = str(row.get("source") or GEX_REQUEST_SNAPSHOT_SOURCE)
    choices = _gex_level_choices(snapshot)
    expected_version = str(request.get("snapshot_version") or "").strip()
    current_version = _gex_snapshot_version(
        captured_at,
        route=route,
        source=snapshot_source,
        choices=choices,
    )
    if expected_version != current_version:
        raise AlertCommandError(
            "ALERT_GEX_SELECTION_CHANGED",
            "the selected GEX snapshot is no longer the latest publishable snapshot",
        )
    selector = str(request.get("selector") or "").strip().upper()
    choice = next(
        (item for item in choices if str(item.get("selector") or "").upper() == selector),
        None,
    )
    if choice is None:
        raise AlertCommandError(
            "ALERT_GEX_LEVEL_UNAVAILABLE", f"GEX level is unavailable: {selector or '-'}"
        )
    metadata = {
        "type": "gex_snapshot",
        "dynamic": False,
        "level_ref": str(choice["selector"]),
        "level_kind": str(choice["level_kind"]),
        "label": str(choice["label"]),
        "snapshot_captured_at": captured_at.isoformat(),
        "snapshot_version": current_version,
        "snapshot_source": snapshot_source,
        "snapshot_capture_mode": snapshot["capture_mode"],
        "snapshot_market_data_entitlement": snapshot["market_data_entitlement"],
        "snapshot_decision_authoritative": snapshot["decision_authoritative"],
        "snapshot_age_minutes_at_creation": round(age_minutes, 2),
    }
    return _persist_price_alert(
        store,
        route,
        timeframe=timeframe,
        price=float(choice["price"]),
        direction=_direction(request.get("direction")),
        label=f"GEX {choice['label']}",
        level_source=metadata,
        alert_id=_new_price_alert_id("pa-gex"),
    )


def _read_exact_target(
    store: Any,
    instrument_lookup: InstrumentLookup,
    alert_type: str,
    alert_id: str,
    instrument_id: str,
    route_fingerprint: str,
    *,
    require_current_route: bool = True,
) -> tuple[dict[str, Any], Any | None]:
    _alert_type(alert_type)
    alert = store.read_price_alert_exact(
        alert_id,
        instrument_id,
        route_fingerprint,
    )
    if not isinstance(alert, dict):
        raise AlertCommandError("ALERT_NOT_FOUND", "alert not found")
    alert = normalize_price_alert(
        alert,
        timeframe=alert.get("timeframe"),
        instrument_id=alert.get("instrument_id"),
        route_fingerprint=alert.get("route_fingerprint"),
        provider=alert.get("provider"),
        provider_contract_id=alert.get("provider_contract_id"),
    )
    validate_price_alert(alert)
    if (
        alert.get("id") != alert_id
        or alert.get("instrument_id") != instrument_id
        or alert.get("route_fingerprint") != route_fingerprint
    ):
        raise AlertCommandError(
            "ALERT_ROUTE_MISMATCH",
            "stored alert payload does not match its qualified route",
        )
    route = (
        _current_route(
            instrument_lookup,
            instrument_id,
            route_fingerprint,
        )
        if require_current_route
        else None
    )
    return alert, route


def _alert_definition_patch(request: Mapping[str, Any]) -> dict[str, Any]:
    patch: dict[str, Any] = {}
    if "price" in request:
        patch["price"] = _finite_price(request.get("price"))
    if "direction" in request:
        patch["direction"] = _direction(request.get("direction"))
    if "label" in request:
        label = request.get("label")
        if not isinstance(label, str):
            raise AlertCommandError("ALERT_DEFINITION_INVALID", "label must be a string")
        patch["label"] = label
    if "toleranceAtr" in request:
        patch["toleranceAtr"] = _non_negative_number(
            request.get("toleranceAtr"), field="toleranceAtr"
        )
    if "tolerancePoints" in request:
        patch["tolerancePoints"] = _non_negative_number(
            request.get("tolerancePoints"), field="tolerancePoints"
        )
    if "rearmMinutes" in request:
        patch["rearmMinutes"] = _rearm_minutes(request.get("rearmMinutes"))
    if not patch:
        raise AlertCommandError("ALERT_DEFINITION_INVALID", "definition patch is empty")
    return patch


def _delete_alert_scope(
    store: Any,
    instrument_lookup: InstrumentLookup,
    request: Mapping[str, Any],
) -> dict[str, Any]:
    route = _current_route(
        instrument_lookup,
        request.get("instrument_id"),
        request.get("route_fingerprint"),
    )
    timeframe = _timeframe(request.get("timeframe"))
    deleted = store.delete_price_alert_scope_exact(
        route.instrument_id,
        route.fingerprint,
        timeframe,
    )
    if isinstance(deleted, bool) or not isinstance(deleted, int) or deleted < 0:
        raise AlertCommandError(
            "ALERT_CONCURRENT_CHANGE", "price alert scope deletion returned invalid state"
        )
    return {
        "ok": True,
        "action": "delete_scope",
        "alert_type": "price",
        "instrument_id": route.instrument_id,
        "route_fingerprint": route.fingerprint,
        "timeframe": timeframe,
        "deleted": deleted,
    }


def _mutate_alert(
    store: Any,
    instrument_lookup: InstrumentLookup,
    action: str,
    request: Mapping[str, Any],
) -> dict[str, Any]:
    callback_token = str(request.get("callback_token") or "").strip()
    requested_id = str(request.get("alert_id") or "").strip()
    requested_type = str(request.get("alert_type") or "").strip().lower()
    if callback_token:
        resolved = _resolve_callback(
            store,
            {"callback_token": callback_token},
        )
        alert_type = str(resolved["alert_type"])
        alert_id = str(resolved["alert_id"])
        instrument_id = require_exact_identity_text(
            resolved.get("instrument_id"),
            field="ALERT_INSTRUMENT_ID",
        )
        route_fingerprint = require_exact_identity_text(
            resolved.get("route_fingerprint"),
            field="ALERT_ROUTE_FINGERPRINT",
        )
        if requested_type and _alert_type(requested_type) != alert_type:
            raise AlertCommandError(
                "ALERT_CALLBACK_TYPE_MISMATCH",
                "callback token does not match the requested alert type",
            )
        if requested_id and requested_id != alert_id:
            raise AlertCommandError(
                "ALERT_CALLBACK_ID_MISMATCH",
                "callback token does not match the requested alert ID",
            )
    else:
        alert_type = _alert_type(requested_type)
        alert_id = require_exact_identity_text(requested_id, field="ALERT_ID")
        instrument_id = require_exact_identity_text(
            request.get("instrument_id"),
            field="ALERT_INSTRUMENT_ID",
        )
        route_fingerprint = require_exact_identity_text(
            request.get("route_fingerprint"),
            field="ALERT_ROUTE_FINGERPRINT",
        )
    alert, route = _read_exact_target(
        store,
        instrument_lookup,
        alert_type,
        alert_id,
        instrument_id,
        route_fingerprint,
        require_current_route=action != "delete",
    )
    requested_generation = request.get("expected_rearmed_at")
    if requested_generation is not None:
        if (
            isinstance(requested_generation, bool)
            or not isinstance(requested_generation, int)
            or requested_generation < 0
        ):
            raise AlertCommandError("ALERT_GENERATION_INVALID", "expected_rearmed_at is invalid")
        if alert.get("rearmedAt") != requested_generation:
            raise AlertCommandError("ALERT_CONCURRENT_CHANGE", "price alert generation changed")
    expected_rearmed_at = alert["rearmedAt"]
    now_ms = _created_at_ms()
    if action == "update":
        saved = store.rearm_price_alert_exact(
            alert_id,
            instrument_id,
            route_fingerprint,
            rearmed_at=now_ms,
            definition_patch=_alert_definition_patch(request),
            expected_rearmed_at=expected_rearmed_at,
        )
    elif action == "rearm":
        saved = store.rearm_price_alert_exact(
            alert_id,
            instrument_id,
            route_fingerprint,
            rearmed_at=now_ms,
            expected_rearmed_at=expected_rearmed_at,
        )
    elif action in {"enable", "disable"}:
        saved = store.set_price_alert_enabled_exact(
            alert_id,
            instrument_id,
            route_fingerprint,
            enabled=action == "enable",
            rearmed_at=now_ms,
            expected_rearmed_at=expected_rearmed_at,
        )
    else:
        saved = store.delete_price_alert_exact(
            alert_id,
            instrument_id,
            route_fingerprint,
            deleted_at=now_ms,
            expected_rearmed_at=expected_rearmed_at,
        )
        if saved is None:
            raise AlertCommandError("ALERT_CONCURRENT_CHANGE", "price alert changed concurrently")
        return {
            "ok": True,
            "action": action,
            "alert_type": alert_type,
            "alert_id": alert_id,
            "deleted": True,
        }
    if saved is None:
        raise AlertCommandError("ALERT_CONCURRENT_CHANGE", "price alert changed concurrently")
    item = _price_alert_item(
        saved,
        ((lambda _instrument_id: route.instrument) if route is not None else instrument_lookup),
    )
    return {
        "ok": True,
        "action": action,
        "alert_type": alert_type,
        "alert_id": alert_id,
        "alert": item,
        "payload": price_alert_client_projection(saved),
    }
