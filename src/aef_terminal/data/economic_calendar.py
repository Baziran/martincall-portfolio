from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


ECONOMIC_CALENDAR_SCHEMA = "economic-calendar-v1"
ECONOMIC_CALENDAR_IMPACT_POLICY = "martincall-us-macro-v1"

_FRED_CALENDAR_URL = "https://fred.stlouisfed.org/releases/calendar"
_FED_CALENDAR_ROOT = "https://www.federalreserve.gov/newsevents/"
_BEA_CALENDAR_URL = "https://apps.bea.gov/API/signup/release_dates.json"
_MAX_SOURCE_BYTES = 2_000_000
_HTTP_TIMEOUT_SECONDS = 5.0
_MAX_SOURCE_WORKERS = 4

_FRED_RELEASE_POLICY: dict[int, tuple[str, str]] = {
    9: ("retail_sales", "medium"),
    10: ("consumer_price_index", "high"),
    13: ("industrial_production", "medium"),
    27: ("housing_starts", "medium"),
    46: ("producer_price_index", "medium"),
    50: ("employment_situation", "high"),
    51: ("international_trade", "medium"),
    53: ("gross_domestic_product", "high"),
    54: ("personal_income_and_outlays", "high"),
    95: ("factory_orders", "medium"),
    180: ("weekly_jobless_claims", "medium"),
    192: ("job_openings_and_labor_turnover", "medium"),
}

_FED_CATEGORY_POLICY: dict[str, tuple[str, str]] = {
    "FOMC Meetings": ("fomc", "high"),
    "Beige Book": ("beige_book", "high"),
    "Testimony": ("fed_testimony", "high"),
    "Speeches": ("fed_speech", "medium"),
}

_BEA_RELEASE_POLICY: dict[str, tuple[str, str]] = {
    "U.S. International Trade in Goods and Services": ("international_trade", "medium"),
    "Gross Domestic Product": ("gross_domestic_product", "high"),
    "Personal Income and Outlays": ("personal_income_and_outlays", "high"),
}

_FRED_DATE_PATTERN = re.compile(
    r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday) "
    r"(?:January|February|March|April|May|June|July|August|September|October|"
    r"November|December) \d{1,2}, \d{4}"
)
_CLOCK_PATTERN = re.compile(
    r"^(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*(?P<meridiem>[ap])\.?m\.?$",
    re.IGNORECASE,
)


class EconomicCalendarSourceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = str(code)


@dataclass(frozen=True, slots=True)
class EconomicCalendarEvent:
    provider: str
    provider_event_id: str
    scheduled_at: datetime
    title: str
    event_type: str
    category: str
    impact: str
    source_name: str
    source_url: str
    source_timezone: str
    country: str = "US"
    time_precision: str = "exact"
    impact_policy: str = ECONOMIC_CALENDAR_IMPACT_POLICY

    def __post_init__(self) -> None:
        required = {
            "provider": self.provider,
            "provider_event_id": self.provider_event_id,
            "title": self.title,
            "event_type": self.event_type,
            "category": self.category,
            "source_name": self.source_name,
            "source_url": self.source_url,
            "source_timezone": self.source_timezone,
            "country": self.country,
        }
        for field_name, value in required.items():
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"economic calendar {field_name} is required")
        if self.scheduled_at.tzinfo is None or self.scheduled_at.utcoffset() is None:
            raise ValueError("economic calendar scheduled_at must be timezone-aware")
        if self.impact not in {"high", "medium"}:
            raise ValueError("economic calendar impact must be high or medium")
        if self.time_precision != "exact":
            raise ValueError("economic calendar events require exact time precision")
        if self.impact_policy != ECONOMIC_CALENDAR_IMPACT_POLICY:
            raise ValueError("economic calendar impact policy is invalid")
        if not self.source_url.startswith("https://"):
            raise ValueError("economic calendar source_url must use https")
        for field_name, value in required.items():
            object.__setattr__(self, field_name, value.strip())
        object.__setattr__(self, "scheduled_at", self.scheduled_at.astimezone(UTC))

    def as_payload(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "provider_event_id": self.provider_event_id,
            "scheduled_at": _iso_utc(self.scheduled_at),
            "title": self.title,
            "event_type": self.event_type,
            "category": self.category,
            "impact": self.impact,
            "impact_policy": self.impact_policy,
            "source_name": self.source_name,
            "source_url": self.source_url,
            "source_timezone": self.source_timezone,
            "country": self.country,
            "time_precision": self.time_precision,
        }


@dataclass(frozen=True, slots=True)
class EconomicCalendarSourceResult:
    provider: str
    source_name: str
    source_url: str
    status: str
    fetched_at: datetime
    events: tuple[EconomicCalendarEvent, ...] = ()
    error_code: str | None = None
    failed_scopes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.status not in {"ok", "partial", "unavailable"}:
            raise ValueError("economic calendar source status is invalid")
        if self.status == "ok" and (self.error_code or self.failed_scopes):
            raise ValueError("successful economic calendar source cannot carry failures")
        if self.status != "ok" and not self.error_code:
            raise ValueError("failed economic calendar source requires an error code")
        if self.fetched_at.tzinfo is None or self.fetched_at.utcoffset() is None:
            raise ValueError("economic calendar fetched_at must be timezone-aware")

    def as_payload(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "source_name": self.source_name,
            "source_url": self.source_url,
            "status": self.status,
            "fetched_at": _iso_utc(self.fetched_at),
            "event_count": len(self.events),
            "error_code": self.error_code,
            "failed_scopes": list(self.failed_scopes),
        }


@dataclass(slots=True)
class _HtmlNode:
    tag: str
    attrs: dict[str, str]
    children: list[_HtmlNode | str] = field(default_factory=list)


class _HtmlTreeParser(HTMLParser):
    _VOID_TAGS = frozenset(
        {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta"}
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _HtmlNode("document", {})
        self._stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized_tag = tag.lower()
        if normalized_tag == "p" and self._stack[-1].tag == "p":
            self._stack.pop()
        node = _HtmlNode(
            normalized_tag,
            {str(key).lower(): str(value or "") for key, value in attrs},
        )
        self._stack[-1].children.append(node)
        if normalized_tag not in self._VOID_TAGS:
            self._stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if self._stack[-1].tag == tag.lower():
            self._stack.pop()

    def handle_endtag(self, tag: str) -> None:
        normalized_tag = tag.lower()
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == normalized_tag:
                del self._stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if data:
            self._stack[-1].children.append(data)


def _iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_html(source: str) -> _HtmlNode:
    parser = _HtmlTreeParser()
    parser.feed(source)
    parser.close()
    return parser.root


def _iter_nodes(node: _HtmlNode) -> Iterator[_HtmlNode]:
    yield node
    for child in node.children:
        if isinstance(child, _HtmlNode):
            yield from _iter_nodes(child)


def _node_text(node: _HtmlNode) -> str:
    parts: list[str] = []
    for child in node.children:
        parts.append(_node_text(child) if isinstance(child, _HtmlNode) else child)
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def _node_classes(node: _HtmlNode) -> frozenset[str]:
    return frozenset(node.attrs.get("class", "").split())


def _first_node(node: _HtmlNode, predicate: Callable[[_HtmlNode], bool]) -> _HtmlNode | None:
    return next((candidate for candidate in _iter_nodes(node) if predicate(candidate)), None)


def _parse_clock(value: str) -> time | None:
    normalized = re.sub(r"\s+", " ", str(value or "").strip().lower())
    match = _CLOCK_PATTERN.fullmatch(normalized)
    if not match:
        return None
    hour = int(match.group("hour"))
    minute = int(match.group("minute"))
    if hour < 1 or hour > 12 or minute > 59:
        return None
    if match.group("meridiem").lower() == "p" and hour != 12:
        hour += 12
    elif match.group("meridiem").lower() == "a" and hour == 12:
        hour = 0
    return time(hour=hour, minute=minute)


def _event_in_range(
    event: EconomicCalendarEvent,
    *,
    start: datetime,
    end: datetime,
) -> bool:
    return start <= event.scheduled_at < end


def parse_fred_release_calendar(
    payload_text: str,
    *,
    start: datetime,
    end: datetime,
    expected_release_id: int | None = None,
) -> list[EconomicCalendarEvent]:
    try:
        payload = json.loads(payload_text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise EconomicCalendarSourceError("FRED_CALENDAR_JSON_INVALID", str(exc)) from exc
    pager = payload.get("pager") if isinstance(payload, dict) else None
    if not isinstance(pager, str):
        raise EconomicCalendarSourceError(
            "FRED_CALENDAR_PAYLOAD_INVALID",
            "FRED release calendar pager is missing",
        )
    root = _parse_html(pager)
    chicago = ZoneInfo("America/Chicago")
    current_date: date | None = None
    current_clock: time | None = None
    events: list[EconomicCalendarEvent] = []
    for row in (node for node in _iter_nodes(root) if node.tag == "tr"):
        row_text = _node_text(row)
        date_match = _FRED_DATE_PATTERN.fullmatch(row_text)
        if date_match:
            current_date = datetime.strptime(date_match.group(0), "%A %B %d, %Y").date()
            current_clock = None
            continue
        cells = [
            child for child in row.children if isinstance(child, _HtmlNode) and child.tag == "td"
        ]
        if len(cells) < 2 or current_date is None:
            continue
        parsed_clock = _parse_clock(_node_text(cells[0]))
        if parsed_clock is not None:
            current_clock = parsed_clock
        if current_clock is None:
            continue
        anchor = _first_node(
            cells[1],
            lambda node: node.tag == "a" and "/release" in node.attrs.get("href", ""),
        )
        if anchor is None:
            continue
        query = parse_qs(urlsplit(anchor.attrs.get("href", "")).query)
        try:
            release_id = int(query.get("rid", [""])[0])
        except TypeError, ValueError:
            continue
        if expected_release_id is not None and release_id != expected_release_id:
            continue
        policy = _FRED_RELEASE_POLICY.get(release_id)
        title = _node_text(anchor)
        if policy is None or not title:
            continue
        scheduled = datetime.combine(current_date, current_clock, tzinfo=chicago).astimezone(UTC)
        event_type, impact = policy
        event = EconomicCalendarEvent(
            provider="fred",
            provider_event_id=f"fred:{release_id}:{_iso_utc(scheduled)}",
            scheduled_at=scheduled,
            title=title,
            event_type=event_type,
            category="US macro release",
            impact=impact,
            source_name="FRED release calendar",
            source_url=f"https://fred.stlouisfed.org/release?rid={release_id}",
            source_timezone="America/Chicago",
        )
        if _event_in_range(event, start=start, end=end):
            events.append(event)
    return events


def parse_federal_reserve_calendar(
    page_html: str,
    *,
    page_url: str,
    start: datetime,
    end: datetime,
) -> list[EconomicCalendarEvent]:
    root = _parse_html(page_html)
    article = _first_node(
        root,
        lambda node: (
            node.tag == "div"
            and node.attrs.get("id") == "article"
            and "cal-nojs" in _node_classes(node)
        ),
    )
    if article is None:
        raise EconomicCalendarSourceError(
            "FED_CALENDAR_PAYLOAD_INVALID",
            "Federal Reserve calendar article is missing",
        )
    month_heading = _first_node(
        article,
        lambda node: node.tag == "h4" and "text-center" in _node_classes(node),
    )
    try:
        if month_heading is None:
            raise ValueError("month heading is missing")
        calendar_month = datetime.strptime(_node_text(month_heading), "%B %Y")
    except ValueError as exc:
        raise EconomicCalendarSourceError(
            "FED_CALENDAR_MONTH_INVALID",
            "Federal Reserve calendar month is missing",
        ) from exc

    new_york = ZoneInfo("America/New_York")
    current_category = ""
    events: list[EconomicCalendarEvent] = []
    for node in _iter_nodes(article):
        if node.tag == "h4" and "col-md-12" in _node_classes(node):
            current_category = _node_text(node)
            continue
        policy = _FED_CATEGORY_POLICY.get(current_category)
        node_classes = _node_classes(node)
        if (
            node.tag != "div"
            or "panel" not in node_classes
            or not node_classes.intersection({"panel-default", "panel-unstyled"})
        ):
            continue
        if policy is None:
            continue
        columns = [
            candidate
            for candidate in _iter_nodes(node)
            if candidate.tag == "div"
            and _node_classes(candidate).intersection({"col-xs-2", "col-xs-7", "col-xs-3"})
        ]
        time_column = next(
            (candidate for candidate in columns if "col-xs-2" in _node_classes(candidate)),
            None,
        )
        title_column = next(
            (candidate for candidate in columns if "col-xs-7" in _node_classes(candidate)),
            None,
        )
        date_column = next(
            (candidate for candidate in columns if "col-xs-3" in _node_classes(candidate)),
            None,
        )
        if time_column is None or title_column is None or date_column is None:
            continue
        scheduled_clock = _parse_clock(_node_text(time_column))
        day_match = re.search(r"\b([0-3]?\d)\b", _node_text(date_column))
        if scheduled_clock is None or day_match is None:
            continue
        try:
            local_date = date(calendar_month.year, calendar_month.month, int(day_match.group(1)))
        except ValueError:
            continue
        paragraphs = [
            candidate
            for candidate in _iter_nodes(title_column)
            if candidate.tag == "p" and bool(_node_text(candidate))
        ]
        title_node = paragraphs[0] if paragraphs else None
        title = _node_text(title_node) if title_node is not None else ""
        topic_node = next(
            (
                paragraph
                for paragraph in paragraphs[1:]
                if "calendar__title" in _node_classes(paragraph)
            ),
            None,
        )
        topic = _node_text(topic_node) if topic_node is not None else ""
        if topic and topic.casefold() not in title.casefold():
            title = f"{title}: {topic}"
        anchor = (
            _first_node(title_node, lambda candidate: candidate.tag == "a")
            if title_node is not None
            else None
        )
        if not title:
            continue
        scheduled = datetime.combine(local_date, scheduled_clock, tzinfo=new_york).astimezone(UTC)
        event_type, impact = policy
        identity_text = "|".join(
            (
                current_category,
                title,
                _iso_utc(scheduled),
                anchor.attrs.get("href", "") if anchor else "",
            )
        )
        digest = hashlib.sha256(identity_text.encode("utf-8")).hexdigest()[:24]
        event = EconomicCalendarEvent(
            provider="federal_reserve",
            provider_event_id=f"federal-reserve:{digest}",
            scheduled_at=scheduled,
            title=title,
            event_type=event_type,
            category=current_category,
            impact=impact,
            source_name="Federal Reserve calendar",
            source_url=urljoin(page_url, anchor.attrs.get("href", "")) if anchor else page_url,
            source_timezone="America/New_York",
        )
        if _event_in_range(event, start=start, end=end):
            events.append(event)
    return events


def parse_bea_release_calendar(
    payload_text: str,
    *,
    start: datetime,
    end: datetime,
) -> list[EconomicCalendarEvent]:
    try:
        payload = json.loads(payload_text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise EconomicCalendarSourceError("BEA_CALENDAR_JSON_INVALID", str(exc)) from exc
    if not isinstance(payload, dict):
        raise EconomicCalendarSourceError(
            "BEA_CALENDAR_PAYLOAD_INVALID",
            "BEA release calendar must be an object",
        )
    events_by_id: dict[str, EconomicCalendarEvent] = {}
    for title, (event_type, impact) in _BEA_RELEASE_POLICY.items():
        release = payload.get(title)
        release_dates = release.get("release_dates") if isinstance(release, dict) else None
        if not isinstance(release_dates, list):
            raise EconomicCalendarSourceError(
                "BEA_CALENDAR_PAYLOAD_INVALID",
                f"BEA release dates are missing for {title}",
            )
        for value in release_dates:
            if not isinstance(value, str):
                raise EconomicCalendarSourceError(
                    "BEA_CALENDAR_TIME_INVALID",
                    f"BEA release time is invalid for {title}",
                )
            try:
                scheduled = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise EconomicCalendarSourceError("BEA_CALENDAR_TIME_INVALID", value) from exc
            if scheduled.tzinfo is None or scheduled.utcoffset() is None:
                raise EconomicCalendarSourceError("BEA_CALENDAR_TIME_INVALID", value)
            scheduled = scheduled.astimezone(UTC)
            event = EconomicCalendarEvent(
                provider="bea",
                provider_event_id=f"bea:{event_type}:{_iso_utc(scheduled)}",
                scheduled_at=scheduled,
                title=title,
                event_type=event_type,
                category="US macro release",
                impact=impact,
                source_name="U.S. Bureau of Economic Analysis release schedule",
                source_url="https://www.bea.gov/news/schedule",
                source_timezone="UTC",
            )
            if _event_in_range(event, start=start, end=end):
                events_by_id[event.provider_event_id] = event
    return sorted(
        events_by_id.values(),
        key=lambda event: (event.scheduled_at, event.provider_event_id),
    )


def _http_get_text(url: str) -> str:
    request = Request(
        url,
        headers={
            "Accept": "application/json,text/html;q=0.9",
            "User-Agent": "MartinCall-Economic-Calendar/1.0",
        },
        method="GET",
    )
    with urlopen(request, timeout=_HTTP_TIMEOUT_SECONDS) as response:  # noqa: S310
        declared_size = response.headers.get("Content-Length")
        if declared_size and int(declared_size) > _MAX_SOURCE_BYTES:
            raise EconomicCalendarSourceError(
                "ECONOMIC_CALENDAR_SOURCE_TOO_LARGE",
                "economic calendar response exceeds the size limit",
            )
        payload = response.read(_MAX_SOURCE_BYTES + 1)
        if len(payload) > _MAX_SOURCE_BYTES:
            raise EconomicCalendarSourceError(
                "ECONOMIC_CALENDAR_SOURCE_TOO_LARGE",
                "economic calendar response exceeds the size limit",
            )
        charset = response.headers.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="strict")


def _source_error_code(provider: str, exc: BaseException) -> str:
    if isinstance(exc, EconomicCalendarSourceError):
        return exc.code
    return f"{provider.upper()}_CALENDAR_UNAVAILABLE"


def load_fred_calendar(
    *,
    start: datetime,
    end: datetime,
    fetched_at: datetime,
    http_get: Callable[[str], str] = _http_get_text,
) -> EconomicCalendarSourceResult:
    local_start = start.astimezone(ZoneInfo("America/Chicago")).date()
    local_end = end.astimezone(ZoneInfo("America/Chicago")).date()
    events: list[EconomicCalendarEvent] = []
    failed: list[str] = []
    error_codes: list[str] = []

    def load_release(release_id: int) -> list[EconomicCalendarEvent]:
        query = urlencode(
            {
                "po": 1,
                "ptic": 0,
                "vs": local_start.isoformat(),
                "ve": local_end.isoformat(),
                "rid": release_id,
            }
        )
        return parse_fred_release_calendar(
            http_get(f"{_FRED_CALENDAR_URL}?{query}"),
            start=start,
            end=end,
            expected_release_id=release_id,
        )

    release_ids = sorted(_FRED_RELEASE_POLICY)
    release_results: dict[int, list[EconomicCalendarEvent]] = {}
    with ThreadPoolExecutor(
        max_workers=min(_MAX_SOURCE_WORKERS, len(release_ids)),
        thread_name_prefix="economic-calendar-fred",
    ) as executor:
        futures = {
            executor.submit(load_release, release_id): release_id for release_id in release_ids
        }
        for future in as_completed(futures):
            release_id = futures[future]
            try:
                release_results[release_id] = future.result()
            except Exception as exc:
                failed.append(str(release_id))
                error_codes.append(_source_error_code("fred", exc))
    for release_id in release_ids:
        events.extend(release_results.get(release_id, ()))
    status = "ok" if not failed else "partial" if events else "unavailable"
    return EconomicCalendarSourceResult(
        provider="fred",
        source_name="FRED release calendar",
        source_url=_FRED_CALENDAR_URL,
        status=status,
        fetched_at=fetched_at,
        events=tuple(events),
        error_code=error_codes[0] if error_codes else None,
        failed_scopes=tuple(failed),
    )


def _month_starts(start: date, end: date) -> Iterator[date]:
    current = date(start.year, start.month, 1)
    terminal = date(end.year, end.month, 1)
    while current <= terminal:
        yield current
        current = date(current.year + (1 if current.month == 12 else 0), current.month % 12 + 1, 1)


def load_federal_reserve_calendar(
    *,
    start: datetime,
    end: datetime,
    fetched_at: datetime,
    http_get: Callable[[str], str] = _http_get_text,
) -> EconomicCalendarSourceResult:
    local_start = start.astimezone(ZoneInfo("America/New_York")).date()
    local_end = end.astimezone(ZoneInfo("America/New_York")).date()
    events: list[EconomicCalendarEvent] = []
    failed: list[str] = []
    error_codes: list[str] = []
    months = list(_month_starts(local_start, local_end))

    def load_month(month: date) -> list[EconomicCalendarEvent]:
        page_url = f"{_FED_CALENDAR_ROOT}{month.year}-{month.strftime('%B').lower()}.htm"
        return parse_federal_reserve_calendar(
            http_get(page_url),
            page_url=page_url,
            start=start,
            end=end,
        )

    month_results: dict[str, list[EconomicCalendarEvent]] = {}
    with ThreadPoolExecutor(
        max_workers=min(_MAX_SOURCE_WORKERS, len(months)),
        thread_name_prefix="economic-calendar-fed",
    ) as executor:
        futures = {executor.submit(load_month, month): month for month in months}
        for future in as_completed(futures):
            month = futures[future]
            scope = f"{month.year:04d}-{month.month:02d}"
            try:
                month_results[scope] = future.result()
            except Exception as exc:
                failed.append(scope)
                error_codes.append(_source_error_code("federal_reserve", exc))
    for month in months:
        scope = f"{month.year:04d}-{month.month:02d}"
        events.extend(month_results.get(scope, ()))
    status = "ok" if not failed else "partial" if events else "unavailable"
    return EconomicCalendarSourceResult(
        provider="federal_reserve",
        source_name="Federal Reserve calendar",
        source_url=_FED_CALENDAR_ROOT,
        status=status,
        fetched_at=fetched_at,
        events=tuple(events),
        error_code=error_codes[0] if error_codes else None,
        failed_scopes=tuple(failed),
    )


def load_bea_calendar(
    *,
    start: datetime,
    end: datetime,
    fetched_at: datetime,
    http_get: Callable[[str], str] = _http_get_text,
) -> EconomicCalendarSourceResult:
    try:
        events = tuple(
            parse_bea_release_calendar(
                http_get(_BEA_CALENDAR_URL),
                start=start,
                end=end,
            )
        )
    except Exception as exc:
        return EconomicCalendarSourceResult(
            provider="bea",
            source_name="U.S. Bureau of Economic Analysis release schedule",
            source_url=_BEA_CALENDAR_URL,
            status="unavailable",
            fetched_at=fetched_at,
            error_code=_source_error_code("bea", exc),
            failed_scopes=("release_dates",),
        )
    return EconomicCalendarSourceResult(
        provider="bea",
        source_name="U.S. Bureau of Economic Analysis release schedule",
        source_url=_BEA_CALENDAR_URL,
        status="ok",
        fetched_at=fetched_at,
        events=events,
    )


def load_economic_calendar_snapshot(
    *,
    start: datetime,
    end: datetime,
    now: datetime,
    http_get: Callable[[str], str] = _http_get_text,
) -> dict[str, Any]:
    for field_name, value in {"start": start, "end": end, "now": now}.items():
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"economic calendar {field_name} must be timezone-aware")
    start = start.astimezone(UTC)
    end = end.astimezone(UTC)
    now = now.astimezone(UTC)
    if end <= start:
        raise ValueError("economic calendar range must be increasing")

    with ThreadPoolExecutor(
        max_workers=3,
        thread_name_prefix="economic-calendar-source",
    ) as executor:
        fred_future = executor.submit(
            load_fred_calendar,
            start=start,
            end=end,
            fetched_at=now,
            http_get=http_get,
        )
        fed_future = executor.submit(
            load_federal_reserve_calendar,
            start=start,
            end=end,
            fetched_at=now,
            http_get=http_get,
        )
        bea_future = executor.submit(
            load_bea_calendar,
            start=start,
            end=end,
            fetched_at=now,
            http_get=http_get,
        )
        sources = (fred_future.result(), fed_future.result(), bea_future.result())
    provider_priority = {"fred": 1, "federal_reserve": 2, "bea": 2}
    events_by_semantic_key: dict[tuple[str, datetime], EconomicCalendarEvent] = {}
    for source in sources:
        for event in source.events:
            event_key = (event.event_type, event.scheduled_at)
            existing = events_by_semantic_key.get(event_key)
            if existing is not None:
                existing_priority = provider_priority.get(existing.provider, 0)
                incoming_priority = provider_priority.get(event.provider, 0)
                if incoming_priority <= existing_priority:
                    continue
            events_by_semantic_key[event_key] = event
    events = sorted(
        events_by_semantic_key.values(),
        key=lambda event: (event.scheduled_at, event.provider, event.provider_event_id),
    )
    if all(source.status == "ok" for source in sources):
        status = "ok"
    elif events or any(source.status in {"ok", "partial"} for source in sources):
        status = "partial"
    else:
        status = "unavailable"
    return {
        "schema": ECONOMIC_CALENDAR_SCHEMA,
        "status": status,
        "generated_at": _iso_utc(now),
        "range": {"from": _iso_utc(start), "to": _iso_utc(end)},
        "events": [event.as_payload() for event in events],
        "sources": [source.as_payload() for source in sources],
    }
