from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from statistics import fmean
from typing import Any

from aef_terminal.runtime.stable_hash import stable_hash

from .contracts import (
    DiscordSignalAction,
    DiscordSignalCue,
    DiscordSignalEvent,
    DiscordSignalParseStatus,
    discord_signal_event_order_key,
)


def project_discord_signal_lifecycle(
    events: Sequence[DiscordSignalEvent],
    *,
    as_of_session_date: date,
) -> dict[str, Any]:
    """Replay one author's global Discord journal into at most one active position."""

    if isinstance(as_of_session_date, datetime) or not isinstance(as_of_session_date, date):
        raise TypeError("as_of_session_date must be a date")
    ordered_events = tuple(sorted(events, key=discord_signal_event_order_key))
    event_session_dates = tuple(
        date.fromisoformat(event.source_session_date)
        for event in ordered_events
        if event.source_session_date is not None
    )
    if event_session_dates and max(event_session_dates) > as_of_session_date:
        raise ValueError("as_of_session_date precedes projected events")

    positions: list[dict[str, Any]] = []
    positions_by_id: dict[str, dict[str, Any]] = {}
    position_id_by_message: dict[str, str] = {}
    links: dict[str, dict[str, Any]] = {}
    active_position: dict[str, Any] | None = None
    unmatched_count = 0

    for event in ordered_events:
        if event.source_session_date is not None:
            active_position = _expire_elapsed_position(
                active_position,
                date.fromisoformat(event.source_session_date),
            )
        if event.parse_status not in {
            DiscordSignalParseStatus.PARSED_UNQUALIFIED,
            DiscordSignalParseStatus.PARSED_CONTEXT,
        }:
            links[event.event_id] = _advisory_link()
            continue

        if event.action is DiscordSignalAction.ENTRY:
            if event.contract_key is None:
                unmatched_count += 1
                links[event.event_id] = _unmatched_link("unmatched_entry")
                continue
            boundary_kind: str | None = None
            if active_position is not None:
                if _position_contract_key(active_position) == event.contract_key:
                    _link_event(active_position, event, position_id_by_message)
                    links[event.event_id] = {
                        "lifecycle_status": "entry_rebroadcast",
                        "position_id": active_position["position_id"],
                        "link_basis": "same_contract",
                        "effective_action": DiscordSignalAction.COMMENTARY.value,
                    }
                    continue
                boundary_kind = (
                    "flip"
                    if active_position.get("option_right") != event.option_right
                    else "replacement"
                )
                _terminalize_without_fill(active_position, event)
                active_position = None
            position = _open_position(event)
            positions.append(position)
            positions_by_id[position["position_id"]] = position
            position_id_by_message[event.message_id] = position["position_id"]
            active_position = position
            opened_status = (
                "opened_after_flip"
                if boundary_kind == "flip"
                else "opened_after_replacement"
                if boundary_kind == "replacement"
                else "opened"
            )
            links[event.event_id] = {
                "lifecycle_status": opened_status,
                "position_id": position["position_id"],
                "link_basis": "global_sequence",
                "effective_action": DiscordSignalAction.ENTRY.value,
            }
            continue

        if event.context_cue is not None:
            position, link_basis = _resolve_active_position(
                event,
                active_position=active_position,
                positions_by_id=positions_by_id,
                position_id_by_message=position_id_by_message,
            )
            cue_status = _cue_resolution_status(event, position)
            if position is None or cue_status is not None:
                unmatched_count += 1
                links[event.event_id] = _unmatched_link(
                    cue_status or link_basis or "ambiguous_open_position"
                )
                continue
            _close_position(position, event)
            position_id_by_message[event.message_id] = position["position_id"]
            active_position = None
            links[event.event_id] = {
                "lifecycle_status": (
                    "stopped" if event.context_cue is DiscordSignalCue.STOP_HIT else "closed"
                ),
                "position_id": position["position_id"],
                "link_basis": link_basis,
                "effective_action": DiscordSignalAction.EXIT.value,
            }
            continue

        if event.action is DiscordSignalAction.ADD and active_position is None:
            if event.contract_key is None:
                unmatched_count += 1
                links[event.event_id] = _unmatched_link("unmatched_add")
                continue
            position = _open_position(event)
            positions.append(position)
            positions_by_id[position["position_id"]] = position
            position_id_by_message[event.message_id] = position["position_id"]
            active_position = position
            links[event.event_id] = {
                "lifecycle_status": "opened_from_add",
                "position_id": position["position_id"],
                "link_basis": "global_sequence",
                "effective_action": DiscordSignalAction.ENTRY.value,
            }
            continue

        position, link_basis = _resolve_active_position(
            event,
            active_position=active_position,
            positions_by_id=positions_by_id,
            position_id_by_message=position_id_by_message,
        )

        if event.action is DiscordSignalAction.ADD:
            if position is None:
                unmatched_count += 1
                links[event.event_id] = _unmatched_link(link_basis or "unmatched_add")
                continue
            _link_event(position, event, position_id_by_message)
            position["entry_legs"].append(_leg(event))
            _apply_add_quantity(position, event)
            _refresh_entry_average(position)
            links[event.event_id] = {
                "lifecycle_status": "added",
                "position_id": position["position_id"],
                "link_basis": link_basis,
                "effective_action": DiscordSignalAction.ADD.value,
            }
            continue

        if event.action is DiscordSignalAction.REDUCE:
            if position is None:
                unmatched_count += 1
                links[event.event_id] = _unmatched_link(link_basis or "unmatched_contract")
                continue
            _link_event(position, event, position_id_by_message)
            position["exit_legs"].append(_leg(event))
            fully_reduced = _apply_reduction(position, event)
            if fully_reduced:
                _terminalize_position(position, event, terminal_reason="fully_reduced")
                active_position = None
                lifecycle_status = "closed"
                effective_action = DiscordSignalAction.EXIT.value
            else:
                lifecycle_status = "reduced"
                effective_action = DiscordSignalAction.REDUCE.value
            links[event.event_id] = {
                "lifecycle_status": lifecycle_status,
                "position_id": position["position_id"],
                "link_basis": link_basis,
                "effective_action": effective_action,
            }
            continue

        if event.action is DiscordSignalAction.EXIT:
            if position is None:
                unmatched_count += 1
                links[event.event_id] = _unmatched_link(link_basis or "unmatched_contract")
                continue
            _close_position(position, event)
            position_id_by_message[event.message_id] = position["position_id"]
            active_position = None
            links[event.event_id] = {
                "lifecycle_status": "stopped" if event.exit_reason == "stop" else "closed",
                "position_id": position["position_id"],
                "link_basis": link_basis,
                "effective_action": DiscordSignalAction.EXIT.value,
            }
            continue

        if event.action is DiscordSignalAction.STOP_UPDATE:
            if position is None:
                unmatched_count += 1
                links[event.event_id] = _unmatched_link(link_basis or "ambiguous_open_position")
                continue
            _link_event(position, event, position_id_by_message)
            stop_update = {
                "event_id": event.event_id,
                "message_id": event.message_id,
                "published_at": event.published_at.isoformat(),
                "mode": event.stop_mode,
                "value": event.stop_value,
                "basis": event.stop_basis,
            }
            position["stop_updates"].append(stop_update)
            if event.stop_mode in {"declared", "moved", "breakeven"}:
                position["stop_declaration_count"] += 1
                position["stop_override_count"] = max(
                    position["stop_declaration_count"] - 1,
                    0,
                )
            if event.stop_mode == "not_executed":
                position["stop_not_executed_count"] += 1
            position["latest_stop_event_id"] = event.event_id
            position["latest_stop_mode"] = event.stop_mode
            position["latest_stop_value"] = event.stop_value
            position["latest_stop_basis"] = event.stop_basis
            links[event.event_id] = {
                "lifecycle_status": "stop_updated",
                "position_id": position["position_id"],
                "link_basis": link_basis,
                "effective_action": DiscordSignalAction.STOP_UPDATE.value,
            }
            continue

        if event.action is DiscordSignalAction.POSITION_UPDATE:
            if position is None:
                unmatched_count += 1
                links[event.event_id] = _unmatched_link(link_basis or "ambiguous_open_position")
                continue
            _link_event(position, event, position_id_by_message)
            remaining_quantity = event.remaining_quantity
            if remaining_quantity is None:
                remaining_quantity = event.quantity
            if remaining_quantity is not None:
                position["remaining_quantity"] = remaining_quantity
                _refresh_remaining_fraction(position)
            if event.remaining_fraction is not None:
                position["remaining_fraction"] = event.remaining_fraction
            links[event.event_id] = {
                "lifecycle_status": "position_updated",
                "position_id": position["position_id"],
                "link_basis": link_basis,
                "effective_action": DiscordSignalAction.POSITION_UPDATE.value,
            }
            continue

        links[event.event_id] = _advisory_link()

    active_position = _expire_elapsed_position(active_position, as_of_session_date)
    if sum(position["state"] == "open" for position in positions) > 1:
        raise RuntimeError("DISCORD_SIGNAL_MULTIPLE_ACTIVE_POSITIONS")

    author_reported_returns = [
        float(position["author_reported_return_pct"])
        for position in positions
        if isinstance(position.get("author_reported_return_pct"), (int, float))
    ]
    calculated_returns = [
        float(position["calculated_return_pct"])
        for position in positions
        if isinstance(position.get("calculated_return_pct"), (int, float))
    ]
    outcome_counts = Counter(
        str(position.get("outcome_category") or "unknown")
        for position in positions
        if position["state"] == "closed"
    )
    pnl_direction_counts = Counter(
        str(position.get("pnl_direction") or "unknown")
        for position in positions
        if position["state"] == "closed"
    )
    declared_stop_positions = sum(
        int(position["stop_declaration_count"]) > 0 for position in positions
    )
    explicit_stop_positions = sum(
        position["stop_execution_event_id"] is not None for position in positions
    )
    explicit_stop_non_execution_positions = sum(
        int(position["stop_not_executed_count"]) > 0 for position in positions
    )
    declared_stop_without_explicit_execution = sum(
        int(position["stop_declaration_count"]) > 0 and position["stop_execution_event_id"] is None
        for position in positions
    )
    return {
        "as_of_session_date": as_of_session_date.isoformat(),
        "positions": positions,
        "event_links": links,
        "stats": {
            "event_count": len(ordered_events),
            "parsed_count": sum(
                event.parse_status.value.startswith("parsed_") for event in ordered_events
            ),
            "ambiguous_count": sum(
                event.parse_status.value in {"ambiguous", "rejected"} for event in ordered_events
            ),
            "commentary_count": sum(
                event.action is DiscordSignalAction.COMMENTARY for event in ordered_events
            ),
            "directional_level_count": sum(
                event.action is DiscordSignalAction.DIRECTIONAL_LEVEL
                and event.parse_status is DiscordSignalParseStatus.PARSED_CONTEXT
                for event in ordered_events
            ),
            "unmatched_count": unmatched_count,
            "open_positions": sum(position["state"] == "open" for position in positions),
            "closed_positions": sum(position["state"] == "closed" for position in positions),
            "expired_unresolved_positions": sum(
                position["state"] == "expired_unresolved" for position in positions
            ),
            "outcome_counts": {
                key: outcome_counts.get(key, 0)
                for key in ("profit", "loss", "stop", "breakeven", "unknown")
            },
            "pnl_direction_counts": {
                key: pnl_direction_counts.get(key, 0)
                for key in ("profit", "loss", "breakeven", "unknown")
            },
            "declared_stop_positions": declared_stop_positions,
            "explicit_stop_positions": explicit_stop_positions,
            "explicit_stop_non_execution_positions": explicit_stop_non_execution_positions,
            "explicit_stop_non_execution_events": sum(
                int(position["stop_not_executed_count"]) for position in positions
            ),
            "declared_stop_without_explicit_execution": max(
                declared_stop_without_explicit_execution,
                0,
            ),
            "stop_override_count": sum(
                int(position["stop_override_count"]) for position in positions
            ),
            "author_reported_return_trades": len(author_reported_returns),
            "mean_author_reported_return_pct": (
                round(fmean(author_reported_returns), 4) if author_reported_returns else None
            ),
            "calculated_complete_trades": len(calculated_returns),
            "mean_calculated_return_pct": (
                round(fmean(calculated_returns), 4) if calculated_returns else None
            ),
        },
    }


def _open_position(event: DiscordSignalEvent) -> dict[str, Any]:
    contract_key = event.contract_key
    if contract_key is None:
        raise ValueError("opening event requires an exact source contract")
    position_id = stable_hash(
        {"contract": contract_key, "entry_event_id": event.event_id},
        length=16,
    )
    initial_quantity = event.quantity if isinstance(event.quantity, int) else None
    return {
        "position_id": position_id,
        "state": "open",
        "source_underlying": event.source_underlying,
        "source_session_date": event.source_session_date,
        "expiry": event.expiry,
        "reported_dte": event.reported_dte,
        "strike": event.strike,
        "option_right": event.option_right,
        "entry_event_id": event.event_id,
        "entry_at": event.published_at.isoformat(),
        "entry_premium": event.reported_premium,
        "entry_average_premium": event.reported_premium,
        "entry_average_unknown": False,
        "entry_legs": [_leg(event)],
        "exit_legs": [],
        "exit_event_id": None,
        "exit_at": None,
        "exit_premium": None,
        "exit_reason": None,
        "exit_price_status": None,
        "terminal_reason": None,
        "terminal_evidence": None,
        "initial_quantity": initial_quantity,
        "remaining_quantity": initial_quantity,
        "remaining_fraction": 1.0,
        "partial_exit_size_unknown": False,
        "author_reported_return_pct": None,
        "calculated_points": None,
        "calculated_return_pct": None,
        "outcome_category": None,
        "pnl_direction": None,
        "expired_unresolved_as_of_session_date": None,
        "stop_updates": [],
        "stop_declaration_count": 0,
        "stop_override_count": 0,
        "stop_not_executed_count": 0,
        "latest_stop_event_id": None,
        "latest_stop_mode": None,
        "latest_stop_value": None,
        "latest_stop_basis": None,
        "stop_execution_event_id": None,
        "event_ids": [event.event_id],
    }


def _resolve_active_position(
    event: DiscordSignalEvent,
    *,
    active_position: dict[str, Any] | None,
    positions_by_id: Mapping[str, dict[str, Any]],
    position_id_by_message: Mapping[str, str],
) -> tuple[dict[str, Any] | None, str]:
    if event.reply_to_message_id is not None:
        replied_position_id = position_id_by_message.get(event.reply_to_message_id)
        if replied_position_id is None:
            return None, "reply_unknown"
        replied_position = positions_by_id.get(replied_position_id)
        if (
            replied_position is None
            or replied_position.get("state") != "open"
            or active_position is None
            or replied_position["position_id"] != active_position["position_id"]
        ):
            return None, "reply_terminal"
        if not _event_matches_position_scope(event, replied_position):
            return None, "reply_scope_mismatch"
        return replied_position, "reply"
    if active_position is None:
        return None, "no_active_position"
    if event.contract_key is not None:
        if _position_contract_key(active_position) != event.contract_key:
            return None, "contract_mismatch"
        return active_position, "contract"
    same_message_position_id = position_id_by_message.get(event.message_id)
    if same_message_position_id == active_position["position_id"]:
        return active_position, "same_message"
    if not _fallback_position_is_current(active_position, event):
        return None, "stale_context"
    if not _event_matches_position_scope(event, active_position):
        return None, "active_scope_mismatch"
    return active_position, "active_position"


def _event_matches_position_scope(
    event: DiscordSignalEvent,
    position: Mapping[str, Any],
) -> bool:
    if event.contract_key is not None and event.contract_key != _position_contract_key(position):
        return False
    if event.source_underlying is not None and position.get("source_underlying") not in {
        None,
        event.source_underlying,
    }:
        return False
    if event.context_cue in {
        DiscordSignalCue.FLATTEN_CALLS,
        DiscordSignalCue.FLATTEN_PUTS,
    } and event.option_right != position.get("option_right"):
        return False
    return True


def _cue_resolution_status(
    event: DiscordSignalEvent,
    position: Mapping[str, Any] | None,
) -> str | None:
    if position is None:
        return None
    if event.context_cue is DiscordSignalCue.STOP_HIT:
        if event.reply_to_message_id is not None and not any(
            update.get("message_id") == event.reply_to_message_id
            for update in position["stop_updates"]
        ):
            return "reply_not_stop"
        if int(position["stop_declaration_count"]) <= 0:
            return "stop_hit_without_active_stop"
    if event.context_cue is DiscordSignalCue.BARE_SALE:
        if event.reported_premium is not None or event.reported_return_pct is not None:
            return None
        if position.get("remaining_quantity") == 1:
            return None
        return "ambiguous_bare_sale"
    return None


def _link_event(
    position: dict[str, Any],
    event: DiscordSignalEvent,
    position_id_by_message: dict[str, str],
) -> None:
    position["event_ids"].append(event.event_id)
    position_id_by_message[event.message_id] = position["position_id"]


def _apply_add_quantity(position: dict[str, Any], event: DiscordSignalEvent) -> None:
    if not isinstance(event.quantity, int) or event.quantity <= 0:
        position["initial_quantity"] = None
        position["remaining_quantity"] = None
        return
    if not isinstance(position.get("initial_quantity"), int) or not isinstance(
        position.get("remaining_quantity"), int
    ):
        position["initial_quantity"] = None
        position["remaining_quantity"] = None
        return
    position["initial_quantity"] += event.quantity
    position["remaining_quantity"] += event.quantity
    _refresh_remaining_fraction(position)


def _apply_reduction(position: dict[str, Any], event: DiscordSignalEvent) -> bool:
    remaining_quantity = event.remaining_quantity
    known_before = position.get("remaining_quantity")
    if (
        remaining_quantity is None
        and isinstance(event.sold_quantity, int)
        and isinstance(
            known_before,
            int,
        )
    ):
        remaining_quantity = max(known_before - event.sold_quantity, 0)
    if remaining_quantity is not None:
        position["remaining_quantity"] = remaining_quantity
        if position.get("initial_quantity") is None and isinstance(event.sold_quantity, int):
            position["initial_quantity"] = remaining_quantity + event.sold_quantity
        _refresh_remaining_fraction(position)
    elif event.remaining_fraction is not None:
        position["remaining_fraction"] = event.remaining_fraction
    else:
        position["partial_exit_size_unknown"] = True
    if event.remaining_fraction is not None:
        position["remaining_fraction"] = event.remaining_fraction
    return position.get("remaining_quantity") == 0 or position.get("remaining_fraction") == 0


def _refresh_remaining_fraction(position: dict[str, Any]) -> None:
    initial = position.get("initial_quantity")
    remaining = position.get("remaining_quantity")
    if isinstance(initial, int) and initial > 0 and isinstance(remaining, int):
        position["remaining_fraction"] = round(remaining / initial, 8)


def _close_position(position: dict[str, Any], event: DiscordSignalEvent) -> None:
    position["event_ids"].append(event.event_id)
    position["exit_legs"].append(_leg(event))
    terminal_reason = (
        "stop_hit"
        if event.context_cue is DiscordSignalCue.STOP_HIT
        else "flat_signal"
        if event.context_cue is DiscordSignalCue.FLAT
        else "side_flattened"
        if event.context_cue
        in {
            DiscordSignalCue.FLATTEN_CALLS,
            DiscordSignalCue.FLATTEN_PUTS,
        }
        else event.exit_reason or "explicit_exit"
    )
    _terminalize_position(position, event, terminal_reason=terminal_reason)
    stop_execution = event.exit_reason == "stop" or event.context_cue is DiscordSignalCue.STOP_HIT
    if stop_execution:
        position["stop_execution_event_id"] = event.event_id
    reported_return_override = (
        0.0
        if stop_execution
        and position.get("latest_stop_mode") == "breakeven"
        and event.reported_return_pct is None
        else None
    )
    _finalize_outcome(
        position,
        event,
        reported_return_override=reported_return_override,
        stop_execution=stop_execution,
    )


def _terminalize_position(
    position: dict[str, Any],
    event: DiscordSignalEvent,
    *,
    terminal_reason: str,
) -> None:
    position["state"] = "closed"
    position["remaining_quantity"] = 0
    position["remaining_fraction"] = 0.0
    position["exit_event_id"] = event.event_id
    position["exit_at"] = event.published_at.isoformat()
    position["exit_premium"] = event.reported_premium
    position["exit_reason"] = event.exit_reason
    position["exit_price_status"] = (
        "reported_fill" if event.reported_premium is not None else "missing"
    )
    position["terminal_reason"] = terminal_reason
    position["terminal_evidence"] = event.event_id


def _terminalize_without_fill(
    position: dict[str, Any],
    boundary_event: DiscordSignalEvent,
) -> None:
    prior_right = position.get("option_right")
    terminal_reason = (
        "flipped_by_entry" if prior_right != boundary_event.option_right else "replaced_by_entry"
    )
    position["state"] = "closed"
    position["event_ids"].append(boundary_event.event_id)
    position["remaining_quantity"] = 0
    position["remaining_fraction"] = 0.0
    position["exit_event_id"] = boundary_event.event_id
    position["exit_at"] = boundary_event.published_at.isoformat()
    position["exit_premium"] = None
    position["exit_reason"] = "superseded"
    position["exit_price_status"] = "missing"
    position["terminal_reason"] = terminal_reason
    position["terminal_evidence"] = boundary_event.event_id
    position["outcome_category"] = "unknown"
    position["pnl_direction"] = None


def _expire_elapsed_position(
    active_position: dict[str, Any] | None,
    source_session_date: date,
) -> dict[str, Any] | None:
    if active_position is None:
        return None
    expiry = active_position.get("expiry")
    if not isinstance(expiry, str) or expiry >= source_session_date.isoformat():
        return active_position
    active_position["state"] = "expired_unresolved"
    active_position["remaining_quantity"] = 0
    active_position["remaining_fraction"] = 0.0
    active_position["expired_unresolved_as_of_session_date"] = source_session_date.isoformat()
    active_position["exit_price_status"] = "missing"
    active_position["terminal_reason"] = "expiry_elapsed"
    active_position["terminal_evidence"] = expiry
    active_position["outcome_category"] = "unknown"
    active_position["pnl_direction"] = None
    return None


def _fallback_position_is_current(
    position: Mapping[str, Any],
    event: DiscordSignalEvent,
) -> bool:
    event_date = event.source_session_date
    if event_date is None:
        return False
    expiry = position.get("expiry")
    if isinstance(expiry, str):
        return event_date <= expiry
    return event_date == position.get("source_session_date")


def _leg(event: DiscordSignalEvent) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "message_id": event.message_id,
        "channel_id": event.channel_id,
        "published_at": event.published_at.isoformat(),
        "premium": event.reported_premium,
        "quantity": event.quantity,
        "sold_quantity": event.sold_quantity,
        "remaining_quantity": event.remaining_quantity,
        "remaining_fraction": event.remaining_fraction,
    }


def _refresh_entry_average(position: dict[str, Any]) -> None:
    legs = position["entry_legs"]
    if any(
        not isinstance(leg.get("premium"), (int, float))
        or not isinstance(leg.get("quantity"), int)
        or int(leg["quantity"]) <= 0
        for leg in legs
    ):
        position["entry_average_unknown"] = True
        position["entry_average_premium"] = None
        return
    total_quantity = sum(int(leg["quantity"]) for leg in legs)
    weighted = sum(float(leg["premium"]) * int(leg["quantity"]) for leg in legs)
    position["entry_average_unknown"] = False
    position["entry_average_premium"] = round(weighted / total_quantity, 8)


def _finalize_outcome(
    position: dict[str, Any],
    event: DiscordSignalEvent,
    *,
    reported_return_override: float | None = None,
    stop_execution: bool = False,
) -> None:
    entry_premium = position.get("entry_average_premium")
    exit_premium = event.reported_premium
    pnl_direction: str | None = None
    points: float | None = None
    calculated_return: float | None = None
    if (
        isinstance(entry_premium, (int, float))
        and float(entry_premium) > 0
        and isinstance(exit_premium, (int, float))
    ):
        points = float(exit_premium) - float(entry_premium)
        calculated_return = points / float(entry_premium) * 100.0
        pnl_direction = "profit" if points > 0 else "loss" if points < 0 else "breakeven"
    reported_return = (
        reported_return_override
        if reported_return_override is not None
        else event.reported_return_pct
    )
    complete_price_pair = (
        not position["partial_exit_size_unknown"]
        and len(position["exit_legs"]) == 1
        and not position["entry_average_unknown"]
        and points is not None
        and calculated_return is not None
    )
    if reported_return is not None:
        position["author_reported_return_pct"] = reported_return
    if complete_price_pair:
        position["calculated_points"] = round(points, 4)
        position["calculated_return_pct"] = round(calculated_return, 4)
    position["pnl_direction"] = (pnl_direction if complete_price_pair else None) or (
        "profit"
        if isinstance(reported_return, (int, float)) and reported_return > 0
        else "loss"
        if isinstance(reported_return, (int, float)) and reported_return < 0
        else "breakeven"
        if reported_return == 0
        else None
    )
    position["outcome_category"] = (
        "stop" if stop_execution else event.outcome_hint or position["pnl_direction"] or "unknown"
    )


def _position_contract_key(position: Mapping[str, Any]) -> tuple[str, float, str]:
    expiry_key = position.get("expiry")
    if expiry_key is None:
        expiry_key = f"{position['source_session_date']}:reported_dte:{position['reported_dte']}"
    return (
        str(expiry_key),
        float(position["strike"]),
        str(position["option_right"]),
    )


def _advisory_link() -> dict[str, Any]:
    return {"lifecycle_status": "advisory_only", "position_id": ""}


def _unmatched_link(status: str) -> dict[str, Any]:
    return {"lifecycle_status": status, "position_id": ""}
