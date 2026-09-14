from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .contracts import (
    DiscordFeedMessage,
    DiscordSignalAction,
    DiscordSignalCue,
    DiscordSignalEvent,
    DiscordSignalParseStatus,
    DiscordSignalProfile,
    discord_feed_message_order_key,
    discord_signal_event_order_key,
)


DISCORD_SIGNAL_PARSER_VERSION = "discord-gaa-v14"
_MAX_FEED_MESSAGES = 50_000

_CONTRACT_RE = re.compile(
    r"(?i)(?:(?P<underlying>\$?(?:spx|spy))\s+)?"
    r"(?P<strike>\d+(?:\.\d+)?)\s*(?P<right>[cp])\b"
)
_DOLLAR_UNDERLYING_RE = re.compile(
    r"(?i)\$(?P<underlying>[a-z][a-z0-9.]*)\s+"
    r"(?=\d+(?:\.\d+)?\s*[cp]\b)"
)
_DECIMAL_PRICE = r"(?:\d+(?:\.\d+)?|\.\d+)"
_PRICE_RE = re.compile(rf"(?i)@\s*\$?(?P<premium>{_DECIMAL_PRICE})")
_SOLD_PRICE_RE = re.compile(
    r"(?i)\b(?:sold|out)\s*(?:at|@)?\s*\$?"
    rf"(?P<premium>{_DECIMAL_PRICE})(?![\d.]|\s*(?:%|/|cons?\b|contracts?\b))"
)
_MONTH_DAY_RE = re.compile(
    r"(?i)\b(?P<month>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|"
    r"may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|"
    r"nov(?:ember)?|dec(?:ember)?)\s+(?P<day>\d{1,2})\b"
)
_DTE_RE = re.compile(r"(?i)\b(?P<dte>\d+)\s*dte\b")
_ADD_RE = re.compile(r"(?i)\b(?:add|added|adding|averaged)\b")
_REDUCE_RE = re.compile(r"(?i)\btrim(?:med|ming)?\b")
_INITIAL_ALERT_RE = re.compile(r"(?i)#alert\b")
_CONTRACT_DISCUSSION_RE = re.compile(
    r"(?i)\b(?:fighting\s+the\s+urge|thinking\s+about|considering|"
    r"looking\s+to|want(?:ing)?\s+to)\b[^\n]{0,80}\b(?:take|enter|buy)\b"
)
_CONTRACT_MARK_RE = re.compile(r"(?i)\b(?:now|currently|back\s+in\s+profit|in\s+profit)\b")
_EXIT_RE = re.compile(
    r"(?i)(?:(?:^|\n)\s*(?:@everyone\s+)?out\b|"
    r"\b(?:i(?:'m| am)\s+out|sold|closed|exited)\b|"
    r"\bcut(?:ting)?(?:\s+it)?\b)"
)
_EXIT_NOISE_RE = re.compile(
    r"(?i)(?:\bbeing\s+sold\b|"
    r"\b(?:market|markets|options?|spx|spy)\s+(?:is\s+|are\s+|was\s+|were\s+)?closed\b|"
    r"^\s*(?:lol\s+)?closed\s+(?:above|below|over|under)\b)"
)
_FUTURE_BARE_ACTION_RE = re.compile(
    r"(?i)(?:\b(?:next\s+trim|trim\s+will|waiting\s+to\s+add|"
    r"(?:i(?:['’]d|['’]ll)|ima|i\s+would|i\s+will)\s+"
    r"(?:trim|sell|cut|exit|take\s+(?:a\s+)?sl\s+out))\b|"
    r"\b(?:ima|i(?:['’]m|\s+am))[^\n]{0,80}\b(?:wait|waiting)\b"
    r"[^\n]{0,80}\b(?:trim|sell|cut|exit|sl\s+out)\b|"
    r"\b(?:if|once|when)\b[^\n]{0,160}\b(?:trim|sell|cut|exit)\b|"
    r"\bi(?:['’]m|\s+am)\s+cutting\s+(?:above|below|if|when)\b|"
    r"\bcan(?:not|['’]t)\s+sell\b|\bwhere\s+i\s+sold\b)"
)
_POSITION_CONTRACT_RE = re.compile(
    r"(?i)\b(?:holding|still\s+holding|have\s+\d+\s+(?:cons?|contracts?)|"
    r"avg\s*@|average\s*@)\b"
)
_HOLDING_RE = re.compile(
    r"(?i)\b(?:holding\s+(?:just\s+)?|down\s+to\s+|"
    r"have\s+)(?P<quantity>\d+)(?:\s+(?:left|remaining))?\b"
)
_ADD_QUANTITY_RE = re.compile(r"(?i)\b(?:add|added|adding)\s+(?P<quantity>\d+)(?:\s+more)?\b")
_ADD_BACK_QUANTITY_RE = re.compile(
    r"(?i)\b(?:add|added|adding)\s+back\s+(?:the\s+)?"
    r"(?P<quantity>\d+)\s+(?:cons?|contracts?)\b"
)
_ENTRY_QUANTITY_RE = re.compile(
    r"(?i)(?:^|\n)\s*(?P<quantity>\d+)\s+"
    r"(?=\d+(?:\.\d+)?\s*[cp]\b)"
)
_ENTRY_CONTRACT_QUANTITY_RE = re.compile(
    r"(?i)\b(?:(?:only\s+)?taking\s+|just\s+)?"
    r"(?P<quantity>\d+)\s+(?:cons?|contracts?)\b"
)
_ENTRY_BARE_QUANTITY_RE = re.compile(r"(?i)\b(?:just\s+took|just|only)\s+(?P<quantity>\d+)\b")
_FRACTION_TOKEN = (
    r"half|quarter|three\s+quarters?|1\s*/\s*2|1\s*/\s*3|"
    r"2\s*/\s*3|1\s*/\s*4|3\s*/\s*4|1\s*/\s*5|2\s*/\s*5|"
    r"3\s*/\s*5|4\s*/\s*5"
)
_REMAINING_FRACTION_RE = re.compile(
    rf"(?i)\b(?:down\s+to|holding|remaining)\s+(?P<fraction>{_FRACTION_TOKEN})\b"
)
_SOLD_FRACTION_RE = re.compile(
    rf"(?i)\b(?:trim(?:med|ming)?|sold|sell)\s+(?P<fraction>{_FRACTION_TOKEN})\b"
)
_SOLD_PARTIAL_RE = re.compile(r"(?i)\b(?:sold|sell)\s+(?:some|most|another)\b")
_SOLD_NUMERIC_FRACTION_RE = re.compile(
    r"(?i)\b(?:sold|sell|trim(?:med|ming)?)\s+"
    r"(?P<numerator>\d+)\s*/\s*(?P<denominator>\d+)\b"
)
_BREAKEVEN_LONG_RE = re.compile(r"(?i)\b(?:break[ -]?even|breakeven)\b")
_BREAKEVEN_ABBREVIATION_RE = re.compile(r"\bBE\b")
_STOP_TERM_RE = re.compile(r"(?i)\b(?:sl|stop|stops|stopped)\b")
_STOP_NOISE_RE = re.compile(
    r"(?i)(?:\bstop[\s.!,:;-]+(?:asking|complaining|panicking|touching|"
    r"trying|micromanaging|fighting|fucking|talking|this)\b|"
    r"\bstopped\s+(?:answering|working|going|the\s+pump)\b|"
    r"\b(?:don['’]?t|do\s+not)\s+stop\b|"
    r"\bprefer\s+not\s+to\s+set\s+stops?\b)"
)
_STOP_EXECUTION_RE = re.compile(
    r"(?i)(?:\bsl\s+out\b|\bstopp(?:ed|ing)\s+out\b|"
    r"\bstopp(?:ed|ing)\s+\$?(?:spx|spy)\b|"
    r"(?:^|\n)\s*(?:@everyone\s+)?(?:oof\s+)?stopped(?:\s+\$?(?:spx|spy))?\s*$|"
    r"stops?\s+(?:was\s+)?hit|hit\s+(?:the\s+)?stop|be\s+stopped)\b"
)
_STOP_NON_AUTHOR_RE = re.compile(
    r"(?i)\b(?:you|they|he|she|someone)\s+(?:got\s+)?stopped(?:\s+out)?\b"
)
_STOP_HOLD_THROUGH_RE = re.compile(r"(?i)\b(?:ima|i(?:['’]m|\s+am))\s+(?:still\s+)?hold(?:ing)?\b")
_FUTURE_STOP_EXECUTION_RE = re.compile(
    r"(?i)(?:\b(?:will|might|may|gonna|going\s+to|have\s+to)\b"
    r"[^\n]{0,60}\bsl\s+out\b|"
    r"\bif\b[^\n]{0,80}\bsl\s+out\b|"
    r"\bsl\s+out\b[^\n]{0,80}\bif\b)"
)
_STOP_MANAGEMENT_DIRECTIVE_RE = re.compile(
    r"(?i)(?:\b(?:move|moving|raise|raising|set|setting|make|making|"
    r"keep|keeping|loosen|tighten)(?:\s+\w+){0,3}\s+(?:sl|stops?)\b|"
    r"\b(?:sl|stops?)\s*(?:@|at|to|is|are|will\s+be|below|above)\b)"
)
_STOP_VALUE_AFTER_RE = re.compile(
    r"(?i)\b(?:sl|stops?)\b\s*(?:will\s+be|is|are|now|to|@)?\s*"
    r"(?:\b(?:spx|spy)\b\s*)?@?\s*(?P<value>\d+(?:\.\d+)?)"
)
_STOP_VALUE_BEFORE_RE = re.compile(r"(?i)\b(?P<value>\d+(?:\.\d+)?)\s+(?:new\s+)?sl\b")
_RETURN_RE = re.compile(r"(?P<sign>[+-])\s*(?P<value>\d+(?:\.\d+)?)\s*%")
_SOLD_FOR_RETURN_RE = re.compile(
    r"(?i)\b(?:sold|out|closed)\b(?:[^%\n]{0,30}\bfor\s+|\s+)"
    r"(?P<value>\d+(?:\.\d+)?)\s*%"
)
_DIRECTIONAL_REFERENCE = (
    r"\d+(?:\.\d+)?|hod|lod|vwap|today(?:['’]?s)?\s+highs?|"
    r"intraday\s+high|open\s+high"
)
_DIRECTIONAL_RIGHT_FIRST_RE = re.compile(
    rf"(?i)\b(?P<right>calls?|puts?)\b[^\n]{{0,48}}?"
    rf"\b(?P<condition>above|over|below|under|bellow)\b\s*"
    rf"(?:the\s+)?(?:\$?(?:spx|spy|iwm)\s+)?(?P<reference>{_DIRECTIONAL_REFERENCE})\b"
)
_DIRECTIONAL_CONDITION_FIRST_RE = re.compile(
    rf"(?i)\b(?P<condition>above|over|below|under|bellow)\b\s*"
    rf"(?:the\s+)?(?:\$?(?:spx|spy|iwm)\s+)?(?P<reference>{_DIRECTIONAL_REFERENCE})\b"
    rf"[^\n]{{0,64}}?\b(?P<right>calls?|puts?)\b"
)
_DIRECTIONAL_EXIT_NOISE_RE = re.compile(
    r"(?i)\b(?:sell|sold|trim|exit|out)\b[^\n]{0,48}\b(?:calls?|puts?)\b"
)
_DIRECTIONAL_UNDERLYING_RE = re.compile(r"(?i)\$?\b(?P<underlying>spx|spy|iwm)\b")
_STOP_HIT_CUE_RE = re.compile(r"(?i)^\s*(?:@everyone\s+)?hit[.!]?\s*(?:\n|$)")
_FLAT_CUE_RE = re.compile(r"(?i)^\s*(?:@everyone\s+)?cash[.!]?\s*$")
_FLATTEN_SIDE_CUE_RE = re.compile(
    r"(?i)^\s*(?:@everyone\s+)?(?:i\s+)?"
    r"(?:sold|closed|exited|out(?:\s+of)?)\s+"
    r"(?:(?:all|my|the|these)\s+)?(?P<right>calls?|puts?)"
    r"(?:\s+100\s*%)?\s*[.!]?\s*$"
)
_BARE_SALE_CUE_RE = re.compile(
    rf"(?i)^\s*(?:@everyone\s+)?sold"
    rf"(?:\s+(?:at|@))?\s*(?:\$?{_DECIMAL_PRICE}\s*%?)?\s*[.!]?\s*$"
)
_SOLD_QUANTITY_RE = re.compile(
    r"(?i)\b(?:sold|sell|trim(?:med|ming)?)\s+"
    r"(?P<quantity>\d+)\s+(?:cons?|contracts?)\b"
)


def parse_discord_message(
    message: DiscordFeedMessage,
    profile: DiscordSignalProfile,
) -> tuple[DiscordSignalEvent, ...]:
    published_day = discord_source_session_date(message.published_at, profile)
    if message.channel_id not in profile.channel_ids or message.author_id != profile.author_id:
        return (
            _event(
                message,
                ordinal=0,
                action=DiscordSignalAction.UNPARSED,
                parse_status=DiscordSignalParseStatus.REJECTED,
                reason_code="source_identity_mismatch",
                source_session_date=published_day.isoformat(),
            ),
        )

    content = message.content.replace("**", "").strip()
    context_cue = _context_cue(content)
    contract_match = _CONTRACT_RE.search(content)
    events: list[DiscordSignalEvent] = []

    if contract_match is not None:
        strike = float(contract_match.group("strike"))
        source_underlying = _source_underlying(content, contract_match)
        contract_discussion = _CONTRACT_DISCUSSION_RE.search(content) is not None
        action = (
            DiscordSignalAction.COMMENTARY if contract_discussion else _contract_action(content)
        )
        premium = _trade_price(content, contract_match, action)
        expiry, reported_dte, expiry_reason = _expiry_from_content(
            content,
            published_day,
            profile,
        )
        underlying_allowed = source_underlying is None or source_underlying in {
            token.upper() for token in profile.underlying_tokens
        }
        if not underlying_allowed:
            parse_status = DiscordSignalParseStatus.REJECTED
            reason_code = "source_underlying_mismatch"
        elif contract_discussion:
            parse_status = DiscordSignalParseStatus.COMMENTARY
            reason_code = "contract_discussion_only"
        elif strike < profile.minimum_strike:
            parse_status = DiscordSignalParseStatus.AMBIGUOUS
            reason_code = "contract_strike_below_source_minimum"
        elif premium is None:
            parse_status = DiscordSignalParseStatus.AMBIGUOUS
            reason_code = "reported_premium_missing"
        elif expiry is None and reported_dte is None:
            parse_status = DiscordSignalParseStatus.AMBIGUOUS
            reason_code = expiry_reason
        else:
            parse_status = DiscordSignalParseStatus.PARSED_UNQUALIFIED
            reason_code = "provider_contract_not_qualified"
        exit_reason = (
            "stop"
            if _is_stop_execution(content)
            else ("manual" if action is DiscordSignalAction.EXIT else None)
        )
        events.append(
            _event(
                message,
                ordinal=len(events),
                action=action,
                parse_status=parse_status,
                reason_code=reason_code,
                source_underlying=source_underlying,
                source_session_date=published_day.isoformat(),
                expiry=expiry,
                reported_dte=reported_dte,
                strike=strike,
                option_right=("call" if contract_match.group("right").lower() == "c" else "put"),
                reported_premium=premium,
                quantity=_quantity_from_content(content, action),
                sold_quantity=_sold_quantity_from_content(content),
                remaining_quantity=_remaining_quantity_from_content(content),
                remaining_fraction=_remaining_fraction(content, action),
                exit_reason=exit_reason,
                reported_return_pct=(
                    0.0
                    if exit_reason == "stop" and _has_breakeven(content)
                    else _reported_return_pct(content)
                ),
                outcome_hint=_outcome_hint(content, exit_reason=exit_reason),
            )
        )

    contract_action = events[0].action if events else None
    bare_action = _bare_trade_action(content)
    if context_cue in {DiscordSignalCue.STOP_HIT, DiscordSignalCue.FLAT}:
        bare_action = DiscordSignalAction.COMMENTARY
    elif context_cue in {
        DiscordSignalCue.FLATTEN_CALLS,
        DiscordSignalCue.FLATTEN_PUTS,
    }:
        bare_action = DiscordSignalAction.EXIT
    if bare_action is not None and contract_action is None:
        exit_reason = (
            "stop"
            if _is_stop_execution(content) or context_cue is DiscordSignalCue.STOP_HIT
            else ("manual" if bare_action is DiscordSignalAction.EXIT else None)
        )
        explicit_bare_underlying = _explicit_bare_option_underlying(content)
        bare_underlying_allowed = explicit_bare_underlying is None or explicit_bare_underlying in {
            token.upper() for token in profile.underlying_tokens
        }
        events.append(
            _event(
                message,
                ordinal=len(events),
                action=bare_action,
                context_cue=context_cue,
                parse_status=(
                    DiscordSignalParseStatus.PARSED_CONTEXT
                    if bare_underlying_allowed
                    else DiscordSignalParseStatus.REJECTED
                ),
                reason_code=(
                    "reply_or_active_position_trade_action"
                    if bare_underlying_allowed
                    else "source_underlying_mismatch"
                ),
                source_underlying=explicit_bare_underlying,
                source_session_date=published_day.isoformat(),
                option_right=(
                    "call"
                    if context_cue is DiscordSignalCue.FLATTEN_CALLS
                    else "put"
                    if context_cue is DiscordSignalCue.FLATTEN_PUTS
                    else None
                ),
                reported_premium=_trade_price(content, None, bare_action),
                quantity=_quantity_from_content(content, bare_action),
                sold_quantity=_sold_quantity_from_content(content),
                remaining_quantity=_remaining_quantity_from_content(content),
                remaining_fraction=_remaining_fraction(content, bare_action),
                exit_reason=exit_reason,
                reported_return_pct=(
                    0.0
                    if exit_reason == "stop" and _has_breakeven(content)
                    else _reported_return_pct(content)
                ),
                outcome_hint=_outcome_hint(content, exit_reason=exit_reason),
            )
        )

    stop_details = _stop_details(content)
    if (
        stop_details is not None
        and not _is_stop_execution(content)
        and contract_action is not DiscordSignalAction.EXIT
    ):
        stop_mode, stop_value, stop_basis = stop_details
        events.append(
            _event(
                message,
                ordinal=len(events),
                action=DiscordSignalAction.STOP_UPDATE,
                parse_status=DiscordSignalParseStatus.PARSED_CONTEXT,
                reason_code="stop_management_context",
                source_underlying=_explicit_underlying(content),
                source_session_date=published_day.isoformat(),
                stop_mode=stop_mode,
                stop_value=stop_value,
                stop_basis=stop_basis,
            )
        )

    holding_match = _HOLDING_RE.search(content)
    remaining_fraction = _remaining_fraction(
        content,
        DiscordSignalAction.POSITION_UPDATE,
    )
    breakeven = _has_breakeven(content)
    position_quantity = int(holding_match.group("quantity")) if holding_match is not None else None
    contract_event = events[0] if contract_action is not None else None
    position_context_already_typed = (
        contract_event is not None
        and contract_event.action
        in {
            DiscordSignalAction.REDUCE,
            DiscordSignalAction.POSITION_UPDATE,
        }
        and not breakeven
        and contract_event.quantity == position_quantity
        and contract_event.remaining_fraction == remaining_fraction
    )
    if (
        holding_match is not None
        or remaining_fraction is not None
        or (breakeven and stop_details is None)
    ) and not position_context_already_typed:
        events.append(
            _event(
                message,
                ordinal=len(events),
                action=DiscordSignalAction.POSITION_UPDATE,
                parse_status=DiscordSignalParseStatus.PARSED_CONTEXT,
                reason_code="position_management_context",
                source_session_date=published_day.isoformat(),
                quantity=position_quantity,
                remaining_quantity=position_quantity,
                remaining_fraction=remaining_fraction,
                stop_mode="breakeven" if breakeven else None,
            )
        )

    directional_underlying_match = _DIRECTIONAL_UNDERLYING_RE.search(content)
    directional_underlying = (
        directional_underlying_match.group("underlying").upper()
        if directional_underlying_match is not None
        else None
    )
    directional_underlying_allowed = directional_underlying is None or directional_underlying in {
        token.upper() for token in profile.underlying_tokens
    }
    for option_right, condition, level, reference, confirmation in _directional_levels(content):
        events.append(
            _event(
                message,
                ordinal=len(events),
                action=DiscordSignalAction.DIRECTIONAL_LEVEL,
                parse_status=(
                    DiscordSignalParseStatus.PARSED_CONTEXT
                    if directional_underlying_allowed
                    else DiscordSignalParseStatus.REJECTED
                ),
                reason_code=(
                    "conditional_directional_level"
                    if directional_underlying_allowed and level is not None
                    else "conditional_directional_reference"
                    if directional_underlying_allowed
                    else "source_underlying_mismatch"
                ),
                source_underlying=directional_underlying,
                source_session_date=published_day.isoformat(),
                option_right=option_right,
                directional_condition=condition,
                directional_level=level,
                directional_reference=reference,
                directional_confirmation=confirmation,
            )
        )

    if not events:
        events.append(
            _event(
                message,
                ordinal=0,
                action=DiscordSignalAction.COMMENTARY,
                parse_status=DiscordSignalParseStatus.COMMENTARY,
                reason_code="no_typed_trade_event",
                source_session_date=published_day.isoformat(),
            )
        )
    return tuple(events)


def parse_discord_feed(
    rows: Sequence[Mapping[str, Any]],
    profile: DiscordSignalProfile,
) -> tuple[DiscordSignalEvent, ...]:
    if len(rows) > _MAX_FEED_MESSAGES:
        raise ValueError(f"discord feed is limited to {_MAX_FEED_MESSAGES} messages")
    messages_by_id: dict[str, DiscordFeedMessage] = {}
    for row in rows:
        message = DiscordFeedMessage.from_mapping(row)
        existing = messages_by_id.get(message.message_id)
        if existing is not None and existing != message:
            raise ValueError("DISCORD_SIGNAL_MESSAGE_CONFLICT")
        messages_by_id[message.message_id] = message
    events = [
        event
        for message in sorted(messages_by_id.values(), key=discord_feed_message_order_key)
        for event in parse_discord_message(message, profile)
    ]
    return tuple(
        sorted(
            events,
            key=discord_signal_event_order_key,
        )
    )


def _event(
    message: DiscordFeedMessage,
    *,
    ordinal: int,
    action: DiscordSignalAction,
    context_cue: DiscordSignalCue | None = None,
    parse_status: DiscordSignalParseStatus,
    reason_code: str,
    source_underlying: str | None = None,
    source_session_date: str | None = None,
    expiry: str | None = None,
    reported_dte: int | None = None,
    strike: float | None = None,
    option_right: str | None = None,
    reported_premium: float | None = None,
    quantity: int | None = None,
    sold_quantity: int | None = None,
    remaining_quantity: int | None = None,
    remaining_fraction: float | None = None,
    stop_mode: str | None = None,
    stop_value: float | None = None,
    stop_basis: str | None = None,
    directional_condition: str | None = None,
    directional_level: float | None = None,
    directional_reference: str | None = None,
    directional_confirmation: str | None = None,
    exit_reason: str | None = None,
    reported_return_pct: float | None = None,
    outcome_hint: str | None = None,
) -> DiscordSignalEvent:
    return DiscordSignalEvent(
        event_id=f"{message.message_id}:{ordinal}",
        message_id=message.message_id,
        channel_id=message.channel_id,
        reply_to_message_id=message.reply_to_message_id,
        ordinal=ordinal,
        published_at=message.published_at,
        action=action,
        context_cue=context_cue,
        parse_status=parse_status,
        reason_code=reason_code,
        source_underlying=source_underlying,
        source_session_date=source_session_date,
        expiry=expiry,
        reported_dte=reported_dte,
        strike=strike,
        option_right=option_right,
        reported_premium=reported_premium,
        quantity=quantity,
        sold_quantity=sold_quantity,
        remaining_quantity=remaining_quantity,
        remaining_fraction=remaining_fraction,
        stop_mode=stop_mode,
        stop_value=stop_value,
        stop_basis=stop_basis,
        directional_condition=directional_condition,
        directional_level=directional_level,
        directional_reference=directional_reference,
        directional_confirmation=directional_confirmation,
        exit_reason=exit_reason,
        reported_return_pct=reported_return_pct,
        outcome_hint=outcome_hint,
        raw_content=message.content,
        parser_version=DISCORD_SIGNAL_PARSER_VERSION,
    )


def _source_underlying(content: str, contract_match: re.Match[str]) -> str | None:
    explicit = _DOLLAR_UNDERLYING_RE.search(content)
    if explicit is not None and explicit.end() <= contract_match.end():
        return explicit.group("underlying").upper()
    raw = str(contract_match.group("underlying") or "").lstrip("$")
    return raw.upper() or None


def _explicit_underlying(content: str) -> str | None:
    match = re.search(r"(?i)\$?\b(?P<underlying>spx|spy)\b", content)
    return match.group("underlying").upper() if match is not None else None


def _explicit_bare_option_underlying(content: str) -> str | None:
    supported = _explicit_underlying(content)
    if supported is not None:
        return supported
    dollar = re.search(r"\$(?P<underlying>[A-Za-z][A-Za-z0-9.]*)\b", content)
    if dollar is not None:
        return dollar.group("underlying").upper()
    named_option = re.search(
        r"\b(?P<underlying>[A-Z][A-Za-z]{1,5})\s+(?:calls?|puts?)\b",
        content,
    )
    if named_option is None:
        return None
    candidate = named_option.group("underlying").upper()
    return (
        None
        if candidate in {"ALL", "BOTH", "MY", "OUR", "SOME", "THE", "THESE", "THOSE"}
        else candidate
    )


def _directional_levels(
    content: str,
) -> tuple[tuple[str, str, float | None, str, str], ...]:
    if _DIRECTIONAL_EXIT_NOISE_RE.search(content):
        return ()
    candidates = sorted(
        (
            *(_DIRECTIONAL_RIGHT_FIRST_RE.finditer(content)),
            *(_DIRECTIONAL_CONDITION_FIRST_RE.finditer(content)),
        ),
        key=lambda match: (match.start(), match.end()),
    )
    levels: list[tuple[str, str, float | None, str, str]] = []
    occupied: list[tuple[int, int]] = []
    for match in candidates:
        if any(match.start() < end and match.end() > start for start, end in occupied):
            continue
        option_right = "call" if match.group("right").lower().startswith("call") else "put"
        raw_condition = match.group("condition").lower()
        condition = "above" if raw_condition in {"above", "over"} else "below"
        if (option_right, condition) not in {("call", "above"), ("put", "below")}:
            continue
        raw_reference = re.sub(r"\s+", "_", match.group("reference").lower())
        try:
            level = float(raw_reference)
            reference = "price"
        except ValueError:
            level = None
            reference = {
                "today's_high": "today_high",
                "today’s_high": "today_high",
                "todays_high": "today_high",
                "today's_highs": "today_high",
                "today’s_highs": "today_high",
                "todays_highs": "today_high",
            }.get(raw_reference, raw_reference)
        confirmation_context = content[max(match.start() - 32, 0) : match.end()]
        confirmation = (
            "session_close"
            if re.search(r"(?i)\b(?:close\s+eod|eod\s+close)\b", confirmation_context)
            else "bar_close"
            if re.search(r"(?i)\bclos(?:e|es|ed|ing)\b", confirmation_context)
            else "touch"
        )
        levels.append((option_right, condition, level, reference, confirmation))
        occupied.append((match.start(), match.end()))
    return tuple(levels)


def _context_cue(content: str) -> DiscordSignalCue | None:
    if _STOP_HIT_CUE_RE.search(content):
        return DiscordSignalCue.STOP_HIT
    if _FLAT_CUE_RE.fullmatch(content):
        return DiscordSignalCue.FLAT
    side = _FLATTEN_SIDE_CUE_RE.fullmatch(content)
    if side is not None:
        return (
            DiscordSignalCue.FLATTEN_CALLS
            if side.group("right").lower().startswith("call")
            else DiscordSignalCue.FLATTEN_PUTS
        )
    if _BARE_SALE_CUE_RE.fullmatch(content):
        return DiscordSignalCue.BARE_SALE
    return None


def _contract_action(content: str) -> DiscordSignalAction:
    if (
        _REDUCE_RE.search(content)
        or _SOLD_FRACTION_RE.search(content)
        or _SOLD_PARTIAL_RE.search(content)
        or _sold_numeric_fraction(content) is not None
        or (
            _EXIT_RE.search(content) is not None and _positive_holding_quantity(content) is not None
        )
    ):
        return DiscordSignalAction.REDUCE
    if _is_stop_execution(content) or (
        _EXIT_RE.search(content) and _EXIT_NOISE_RE.search(content) is None
    ):
        return DiscordSignalAction.EXIT
    if _INITIAL_ALERT_RE.search(content):
        return DiscordSignalAction.ENTRY
    if _ADD_RE.search(content):
        return DiscordSignalAction.ADD
    if _POSITION_CONTRACT_RE.search(content) or _CONTRACT_MARK_RE.search(content):
        return DiscordSignalAction.POSITION_UPDATE
    return DiscordSignalAction.ENTRY


def _bare_trade_action(content: str) -> DiscordSignalAction | None:
    if _FUTURE_BARE_ACTION_RE.search(content):
        return None
    if (
        _REDUCE_RE.search(content)
        or _SOLD_FRACTION_RE.search(content)
        or _SOLD_PARTIAL_RE.search(content)
        or _sold_numeric_fraction(content) is not None
        or (
            _EXIT_RE.search(content) is not None and _positive_holding_quantity(content) is not None
        )
    ):
        return DiscordSignalAction.REDUCE
    if _ADD_RE.search(content) and (
        _PRICE_RE.search(content) is not None
        or re.search(r"(?i)\b(?:add|added|adding)\s+\d+", content) is not None
    ):
        return DiscordSignalAction.ADD
    if _is_stop_execution(content):
        return DiscordSignalAction.EXIT
    if _EXIT_RE.search(content) and _EXIT_NOISE_RE.search(content) is None:
        return DiscordSignalAction.EXIT
    return None


def _trade_price(
    content: str,
    contract_match: re.Match[str] | None,
    action: DiscordSignalAction,
) -> float | None:
    prices = tuple(_PRICE_RE.finditer(content))
    if not prices:
        sold_price = _SOLD_PRICE_RE.search(content) if action is DiscordSignalAction.EXIT else None
        if sold_price is not None:
            return float(sold_price.group("premium"))
        return None
    if contract_match is None:
        return float(prices[0].group("premium"))
    after = tuple(
        match
        for match in prices
        if match.start() >= contract_match.end() and match.start() - contract_match.end() <= 80
    )
    if after:
        return float(after[0].group("premium"))
    if action in {
        DiscordSignalAction.ADD,
        DiscordSignalAction.REDUCE,
        DiscordSignalAction.EXIT,
        DiscordSignalAction.POSITION_UPDATE,
    }:
        before = tuple(
            match
            for match in prices
            if match.end() <= contract_match.start() and contract_match.start() - match.end() <= 80
        )
        if before:
            return float(before[-1].group("premium"))
    return None


def _quantity_from_content(
    content: str,
    action: DiscordSignalAction,
) -> int | None:
    patterns = (
        (_ADD_QUANTITY_RE, _ADD_BACK_QUANTITY_RE, _ENTRY_CONTRACT_QUANTITY_RE)
        if action is DiscordSignalAction.ADD
        else (_ENTRY_QUANTITY_RE, _ENTRY_CONTRACT_QUANTITY_RE, _ENTRY_BARE_QUANTITY_RE)
        if action is DiscordSignalAction.ENTRY
        else (_HOLDING_RE,)
    )
    for pattern in patterns:
        match = pattern.search(content)
        if match is not None:
            return int(match.group("quantity"))
    return None


def _sold_quantity_from_content(content: str) -> int | None:
    numeric_fraction = _sold_numeric_fraction(content)
    if numeric_fraction is not None:
        return numeric_fraction[0]
    match = _SOLD_QUANTITY_RE.search(content)
    return int(match.group("quantity")) if match is not None else None


def _remaining_quantity_from_content(content: str) -> int | None:
    holding = _HOLDING_RE.search(content)
    if holding is not None:
        return int(holding.group("quantity"))
    numeric_fraction = _sold_numeric_fraction(content)
    if numeric_fraction is not None:
        numerator, denominator = numeric_fraction
        return denominator - numerator
    return None


def _sold_numeric_fraction(content: str) -> tuple[int, int] | None:
    match = _SOLD_NUMERIC_FRACTION_RE.search(content)
    if match is None:
        return None
    numerator = int(match.group("numerator"))
    denominator = int(match.group("denominator"))
    if denominator <= 0 or numerator <= 0 or numerator >= denominator:
        return None
    return numerator, denominator


def _positive_holding_quantity(content: str) -> int | None:
    match = _HOLDING_RE.search(content)
    if match is None:
        return None
    quantity = int(match.group("quantity"))
    return quantity if quantity > 0 else None


def _fraction_value(raw: str) -> float | None:
    normalized = re.sub(r"\s+", " ", raw.lower()).replace(" ", "")
    return {
        "half": 0.5,
        "quarter": 0.25,
        "threequarter": 0.75,
        "threequarters": 0.75,
        "1/2": 0.5,
        "1/3": 1.0 / 3.0,
        "2/3": 2.0 / 3.0,
        "1/4": 0.25,
        "3/4": 0.75,
        "1/5": 0.2,
        "2/5": 0.4,
        "3/5": 0.6,
        "4/5": 0.8,
    }.get(normalized)


def _remaining_fraction(
    content: str,
    action: DiscordSignalAction,
) -> float | None:
    remaining = _REMAINING_FRACTION_RE.search(content)
    if remaining is not None:
        return _fraction_value(remaining.group("fraction"))
    if action is DiscordSignalAction.REDUCE:
        numeric = _sold_numeric_fraction(content)
        if numeric is not None:
            numerator, denominator = numeric
            return round(1.0 - numerator / denominator, 8)
        sold = _SOLD_FRACTION_RE.search(content)
        sold_fraction = _fraction_value(sold.group("fraction")) if sold is not None else None
        if sold_fraction is not None:
            return round(1.0 - sold_fraction, 8)
    return None


def _has_breakeven(content: str) -> bool:
    if _BREAKEVEN_LONG_RE.search(content) or _BREAKEVEN_ABBREVIATION_RE.search(content):
        return True
    if re.search(r"(?i)\bbe\s+stopped\b", content):
        return True
    return bool(
        _STOP_TERM_RE.search(content) and re.search(r"(?i)\b(?:is|to|at|on)\s+be\b", content)
    )


def _is_stop_execution(content: str) -> bool:
    return bool(
        _STOP_EXECUTION_RE.search(content)
        and _STOP_NOISE_RE.search(content) is None
        and _STOP_NON_AUTHOR_RE.search(content) is None
        and _STOP_HOLD_THROUGH_RE.search(content) is None
        and _FUTURE_STOP_EXECUTION_RE.search(content) is None
    )


def _stop_details(content: str) -> tuple[str, float | None, str | None] | None:
    if _STOP_TERM_RE.search(content) is None:
        return None
    if _STOP_NOISE_RE.search(content) and re.search(r"(?i)\bsl\b", content) is None:
        return None
    if _STOP_NON_AUTHOR_RE.search(content):
        return None
    breakeven = _has_breakeven(content)
    technical = re.search(r"(?i)\b(?:lod|hod|vwap)\b", content) is not None
    explicit_underlying = _explicit_underlying(content)
    value_match = _STOP_VALUE_AFTER_RE.search(content) or _STOP_VALUE_BEFORE_RE.search(content)
    stop_value = float(value_match.group("value")) if value_match is not None else None
    if not (
        value_match is not None
        or breakeven
        or technical
        or _STOP_MANAGEMENT_DIRECTIVE_RE.search(content)
    ):
        return None
    if _STOP_HOLD_THROUGH_RE.search(content) and _STOP_EXECUTION_RE.search(content):
        stop_mode = "not_executed"
    elif _is_stop_execution(content):
        stop_mode = "executed"
    elif re.search(r"(?i)\btest(?:ing)?\b", content):
        stop_mode = "testing"
    elif re.search(r"(?i)\b(?:move|moving|raise|raising|new|now)\b", content):
        stop_mode = "moved"
    elif breakeven:
        stop_mode = "breakeven"
    else:
        stop_mode = "declared"
    stop_basis = (
        "breakeven"
        if breakeven
        else "underlying"
        if explicit_underlying is not None
        else "technical"
        if technical
        else "unspecified_numeric"
        if stop_value is not None
        else None
    )
    return stop_mode, stop_value, stop_basis


def _reported_return_pct(content: str) -> float | None:
    signed = _RETURN_RE.search(content)
    if signed is not None:
        value = float(signed.group("value"))
        return -value if signed.group("sign") == "-" else value
    sold_for = _SOLD_FOR_RETURN_RE.search(content)
    return float(sold_for.group("value")) if sold_for is not None else None


def _outcome_hint(content: str, *, exit_reason: str | None) -> str | None:
    if exit_reason == "stop":
        return "stop"
    reported_return = _reported_return_pct(content)
    if reported_return is not None:
        return "profit" if reported_return > 0 else "loss" if reported_return < 0 else "breakeven"
    if re.search(r"(?i)\b(?:out\s+flat|flat\s+exit|breakeven\s+hit)\b", content):
        return "breakeven"
    if re.search(r"(?i)\b(?:profit|green|winner|won|closed\s+itm)\b", content):
        return "profit"
    if re.search(r"(?i)\b(?:loss|lost|deep\s+red)\b", content):
        return "loss"
    return None


def _expiry_from_content(
    content: str,
    published_day: date,
    profile: DiscordSignalProfile,
) -> tuple[str | None, int | None, str]:
    explicit = _MONTH_DAY_RE.search(content)
    if explicit is not None:
        try:
            month = {
                "jan": 1,
                "january": 1,
                "feb": 2,
                "february": 2,
                "mar": 3,
                "march": 3,
                "apr": 4,
                "april": 4,
                "may": 5,
                "jun": 6,
                "june": 6,
                "jul": 7,
                "july": 7,
                "aug": 8,
                "august": 8,
                "sep": 9,
                "september": 9,
                "oct": 10,
                "october": 10,
                "nov": 11,
                "november": 11,
                "dec": 12,
                "december": 12,
            }[explicit.group("month").lower()]
            expiry = date(published_day.year, month, int(explicit.group("day")))
        except KeyError, ValueError:
            return None, None, "explicit_expiry_invalid"
        return expiry.isoformat(), None, "explicit_expiry"
    relative = _DTE_RE.search(content)
    if relative is not None:
        reported_dte = int(relative.group("dte"))
        if reported_dte == 0:
            return published_day.isoformat(), 0, "source_reported_0dte"
        return None, reported_dte, "relative_expiry_reported"
    if profile.default_dte == 0:
        return published_day.isoformat(), 0, "source_profile_0dte"
    return None, None, "expiry_missing"


def discord_source_session_date(
    published_at: datetime,
    profile: DiscordSignalProfile,
) -> date:
    """Resolve an aware source timestamp to the profile's canonical session date."""

    if published_at.tzinfo is None or published_at.utcoffset() is None:
        raise ValueError("published_at must be timezone-aware")
    published_local = published_at.astimezone(ZoneInfo(profile.exchange_timezone))
    session_day = published_local.date()
    if published_local.timetz().replace(tzinfo=None) >= time(20, 15):
        session_day += timedelta(days=1)
    while session_day.weekday() >= 5:
        session_day += timedelta(days=1)
    return session_day
