from __future__ import annotations

from typing import Any

from aef_terminal.data.instrument_identity import require_exact_identity_text
from aef_terminal.runtime.math_utils import exact_finite_number_or_none


QUOTE_STREAM_ROW_FIELDS = (
    "key",
    "display",
    "instrument_id",
    "route_fingerprint",
    "provider_symbol",
    "price",
    "bid",
    "ask",
    "last",
    "quote_close",
    "quote_ts",
    "price_source",
    "quote_time_basis",
    "quote_provider_ts",
    "quote_received_at",
    "quote_status",
    "quote_entitlement",
    "quote_is_delayed",
    "quote_is_stale",
    "last_provider_ts",
    "last_status",
    "bid_ask_received_at",
    "bid_ask_status",
    "change",
    "change_pct",
    "previous_session_close",
    "change_base",
    "source",
    "live_quote",
    "warning",
    "contract",
    "local_symbol",
    "contract_month",
    "contract_rollover_due",
    "contract_rollover_warning",
    "contract_rollover_new",
    "contract_rollover_new_message",
)
QUOTE_STREAM_ROW_NUMBER_FIELDS = (
    "price",
    "bid",
    "ask",
    "last",
    "quote_close",
    "change",
    "change_pct",
    "previous_session_close",
    "change_base",
)
QUOTE_STREAM_ROW_BOOLEAN_FIELDS = (
    "quote_is_delayed",
    "quote_is_stale",
    "live_quote",
    "contract_rollover_due",
    "contract_rollover_new",
)
QUOTE_STREAM_ROW_TIME_FIELDS = (
    "quote_ts",
    "quote_provider_ts",
    "quote_received_at",
    "last_provider_ts",
    "bid_ask_received_at",
)
QUOTE_STREAM_ROW_STATUS_FIELDS = (
    "quote_status",
    "last_status",
    "bid_ask_status",
)
QUOTE_STREAM_ROW_TEXT_FIELDS = (
    "key",
    "display",
    "instrument_id",
    "route_fingerprint",
    "provider_symbol",
    "source",
    "warning",
)
QUOTE_STREAM_ROW_NON_EMPTY_TEXT_FIELDS = (
    "key",
    "display",
    "instrument_id",
    "route_fingerprint",
    "provider_symbol",
    "source",
)
QUOTE_STREAM_ROW_NULLABLE_TEXT_FIELDS = (
    "price_source",
    "quote_time_basis",
    "contract",
    "local_symbol",
    "contract_month",
    "contract_rollover_new_message",
)
QUOTE_STREAM_ROW_STATUSES = (
    "live",
    "stale",
    "frozen",
    "delayed",
    "delayed_frozen",
    "unknown",
    "unavailable",
)
QUOTE_STREAM_ROW_ENTITLEMENTS = (
    "live",
    "frozen",
    "delayed",
    "delayed_frozen",
    "unknown",
)
QUOTE_STREAM_ROW_ENTITLEMENT_FIELD = "quote_entitlement"
QUOTE_STREAM_ROLLOVER_WARNING_FIELD = "contract_rollover_warning"
QUOTE_STREAM_ROLLOVER_WARNING_FIELDS = (
    "status",
    "days_left",
    "expiry_date",
    "contract_month",
    "message",
)
QUOTE_STREAM_ROLLOVER_WARNING_STATUSES = ("expired", "rollover_due")

_QUOTE_STREAM_ROW_STATUS_VALUES = frozenset(QUOTE_STREAM_ROW_STATUSES)
_QUOTE_STREAM_ROW_ENTITLEMENT_VALUES = frozenset(QUOTE_STREAM_ROW_ENTITLEMENTS)


def quote_stream_row_contract_manifest() -> dict[str, Any]:
    """Return the generated browser/worker grammar for one full quote row."""

    return {
        "version": 1,
        "required_fields": list(QUOTE_STREAM_ROW_FIELDS),
        "number_fields": list(QUOTE_STREAM_ROW_NUMBER_FIELDS),
        "boolean_fields": list(QUOTE_STREAM_ROW_BOOLEAN_FIELDS),
        "time_fields": list(QUOTE_STREAM_ROW_TIME_FIELDS),
        "status_fields": list(QUOTE_STREAM_ROW_STATUS_FIELDS),
        "text_fields": list(QUOTE_STREAM_ROW_TEXT_FIELDS),
        "non_empty_text_fields": list(QUOTE_STREAM_ROW_NON_EMPTY_TEXT_FIELDS),
        "nullable_text_fields": list(QUOTE_STREAM_ROW_NULLABLE_TEXT_FIELDS),
        "nullable_object_contracts": {
            QUOTE_STREAM_ROLLOVER_WARNING_FIELD: {
                "required_fields": list(QUOTE_STREAM_ROLLOVER_WARNING_FIELDS),
                "integer_fields": ["days_left"],
                "non_empty_text_fields": [
                    "status",
                    "expiry_date",
                    "contract_month",
                    "message",
                ],
                "enum_fields": {
                    "status": list(QUOTE_STREAM_ROLLOVER_WARNING_STATUSES),
                },
            }
        },
        "statuses": list(QUOTE_STREAM_ROW_STATUSES),
        "entitlement_field": QUOTE_STREAM_ROW_ENTITLEMENT_FIELD,
        "entitlements": list(QUOTE_STREAM_ROW_ENTITLEMENTS),
    }


def _quote_stream_rollover_warning_valid(value: Any) -> bool:
    if value is None:
        return True
    if (
        not isinstance(value, dict)
        or len(value) != len(QUOTE_STREAM_ROLLOVER_WARNING_FIELDS)
        or any(field not in value for field in QUOTE_STREAM_ROLLOVER_WARNING_FIELDS)
    ):
        return False
    return (
        value["status"] in QUOTE_STREAM_ROLLOVER_WARNING_STATUSES
        and type(value["days_left"]) is int
        and isinstance(value["expiry_date"], str)
        and bool(value["expiry_date"])
        and isinstance(value["contract_month"], str)
        and bool(value["contract_month"])
        and isinstance(value["message"], str)
        and bool(value["message"])
    )


def project_quote_stream_row(row: dict[str, Any]) -> dict[str, Any]:
    """Validate and project one full screener row onto the realtime wire contract."""

    require_exact_identity_text(row.get("instrument_id"), field="instrument_id")
    require_exact_identity_text(row.get("route_fingerprint"), field="route_fingerprint")
    missing = tuple(key for key in QUOTE_STREAM_ROW_FIELDS if key not in row)
    if missing:
        raise ValueError(f"quote stream row is incomplete: {', '.join(missing)}")
    malformed_numbers = tuple(
        key
        for key in QUOTE_STREAM_ROW_NUMBER_FIELDS
        if row[key] is not None and exact_finite_number_or_none(row[key]) is None
    )
    malformed_booleans = tuple(
        key for key in QUOTE_STREAM_ROW_BOOLEAN_FIELDS if type(row[key]) is not bool
    )
    malformed_times = tuple(
        key
        for key in QUOTE_STREAM_ROW_TIME_FIELDS
        if row[key] is not None and (not isinstance(row[key], str) or not row[key])
    )
    malformed_statuses = tuple(
        key
        for key in QUOTE_STREAM_ROW_STATUS_FIELDS
        if row[key] not in _QUOTE_STREAM_ROW_STATUS_VALUES
    )
    malformed_text = tuple(
        key
        for key in QUOTE_STREAM_ROW_TEXT_FIELDS
        if not isinstance(row[key], str)
        or (key in QUOTE_STREAM_ROW_NON_EMPTY_TEXT_FIELDS and not row[key])
    )
    malformed_nullable_text = tuple(
        key
        for key in QUOTE_STREAM_ROW_NULLABLE_TEXT_FIELDS
        if row[key] is not None and not isinstance(row[key], str)
    )
    if (
        malformed_numbers
        or malformed_booleans
        or malformed_times
        or malformed_statuses
        or malformed_text
        or malformed_nullable_text
        or row[QUOTE_STREAM_ROW_ENTITLEMENT_FIELD] not in _QUOTE_STREAM_ROW_ENTITLEMENT_VALUES
        or not _quote_stream_rollover_warning_valid(row[QUOTE_STREAM_ROLLOVER_WARNING_FIELD])
    ):
        raise ValueError("quote stream row fields are malformed")
    return {key: row[key] for key in QUOTE_STREAM_ROW_FIELDS}
