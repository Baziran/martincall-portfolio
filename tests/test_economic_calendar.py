from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

from aef_terminal.data.economic_calendar import (
    ECONOMIC_CALENDAR_SCHEMA,
    load_economic_calendar_snapshot,
    parse_bea_release_calendar,
    parse_federal_reserve_calendar,
    parse_fred_release_calendar,
)
from aef_terminal.ui.economic_calendar_runtime import EconomicCalendarRuntime
from aef_terminal.ui.routers.economic_calendar import (
    EconomicCalendarRouterDeps,
    create_economic_calendar_router,
)


def _fred_payload(*rows: str) -> str:
    body = "".join(rows)
    return json.dumps(
        {
            "pager": (
                '<table><tbody><tr class="odd"><td colspan="2">'
                '<span style="font-weight: bold;">Friday September 04, 2026</span>'
                f"</td></tr>{body}</tbody></table>"
            ),
            "ptic": len(rows),
        }
    )


def _fred_event_row(release_id: int, title: str, scheduled_time: str = "7:30 am") -> str:
    return (
        f"<tr><td>{scheduled_time}</td><td>"
        f'<a href="/release?rid={release_id}">{title}</a>'
        "</td></tr>"
    )


def _fed_page() -> str:
    return """
    <div id="article" class="cal-nojs clearfix">
      <h4 class="text-center">September 2026</h4>
      <div class="row cal-nojs__rowTitle"><h4 class="col-md-12">FOMC Meetings </h4></div>
      <div class="panel panel-default"><div class="row">
        <div class="col-xs-2"><p>2:30 p.m.</p></div>
        <div class="col-xs-7"><p><a href="/live-broadcast.htm">FOMC Press Conference</a></p></div>
        <div class="col-xs-3"><p>16</p></div>
      </div></div>
      <div class="row cal-nojs__rowTitle"><h4 class="col-md-12">Speeches</h4></div>
      <div class="panel panel-default"><div class="row">
        <div class="col-xs-2"><p>10:10 a.m.</p></div>
        <div class="col-xs-7"><p>Governor speech on financial stability</p></div>
        <div class="col-xs-3"><p>21</p></div>
      </div></div>
      <div class="row cal-nojs__rowTitle"><h4 class="col-md-12">Statistical Releases</h4></div>
      <div class="panel panel-default"><div class="row">
        <div class="col-xs-2"><p>1:00 p.m.</p></div>
        <div class="col-xs-7"><p>Commercial Paper</p></div>
        <div class="col-xs-3"><p>22</p></div>
      </div></div>
    </div>
    """


def _empty_fed_page(month: str) -> str:
    return f'<div id="article" class="cal-nojs clearfix"><h4 class="text-center">{month}</h4></div>'


def _bea_payload() -> str:
    return json.dumps(
        {
            "U.S. International Trade in Goods and Services": {
                "release_dates": [
                    "2026-09-03T12:30:00+00:00",
                    "2026-09-03T12:30:00+00:00",
                ]
            },
            "Gross Domestic Product": {"release_dates": ["2026-09-30T12:30:00+00:00"]},
            "Personal Income and Outlays": {"release_dates": ["2026-09-30T12:30:00+00:00"]},
        }
    )


def _range() -> tuple[datetime, datetime]:
    return (
        datetime(2026, 9, 1, tzinfo=UTC),
        datetime(2026, 10, 1, tzinfo=UTC),
    )


def test_fred_parser_admits_curated_exact_releases_and_carries_repeated_time() -> None:
    start, end = _range()
    payload = _fred_payload(
        _fred_event_row(50, "Employment Situation"),
        _fred_event_row(46, "Producer Price Index", scheduled_time=""),
        _fred_event_row(999, "Uncurated release", scheduled_time="9:00 am"),
    )

    events = parse_fred_release_calendar(payload, start=start, end=end)

    assert [(event.event_type, event.impact) for event in events] == [
        ("employment_situation", "high"),
        ("producer_price_index", "medium"),
    ]
    assert [event.scheduled_at.isoformat() for event in events] == [
        "2026-09-04T12:30:00+00:00",
        "2026-09-04T12:30:00+00:00",
    ]
    assert all(event.source_timezone == "America/Chicago" for event in events)


def test_fred_parser_rejects_events_outside_the_requested_release_scope() -> None:
    start, end = _range()
    payload = _fred_payload(_fred_event_row(50, "Employment Situation"))

    events = parse_fred_release_calendar(
        payload,
        start=start,
        end=end,
        expected_release_id=10,
    )

    assert events == []


def test_federal_reserve_parser_uses_new_york_time_and_curated_categories() -> None:
    start, end = _range()

    events = parse_federal_reserve_calendar(
        _fed_page(),
        page_url="https://www.federalreserve.gov/newsevents/2026-september.htm",
        start=start,
        end=end,
    )

    assert [(event.title, event.event_type, event.impact) for event in events] == [
        ("FOMC Press Conference", "fomc", "high"),
        ("Governor speech on financial stability", "fed_speech", "medium"),
    ]
    assert events[0].scheduled_at.isoformat() == "2026-09-16T18:30:00+00:00"
    assert events[0].source_url == "https://www.federalreserve.gov/live-broadcast.htm"
    assert events[1].scheduled_at.isoformat() == "2026-09-21T14:10:00+00:00"
    assert (
        events[0].provider_event_id
        == parse_federal_reserve_calendar(
            _fed_page(),
            page_url="https://www.federalreserve.gov/newsevents/2026-september.htm",
            start=start,
            end=end,
        )[0].provider_event_id
    )


def test_bea_parser_admits_direct_macro_releases_and_deduplicates_dates() -> None:
    start, end = _range()

    events = parse_bea_release_calendar(_bea_payload(), start=start, end=end)

    assert [(event.event_type, event.impact) for event in events] == [
        ("international_trade", "medium"),
        ("gross_domestic_product", "high"),
        ("personal_income_and_outlays", "high"),
    ]
    assert all(event.provider == "bea" for event in events)
    assert len({event.provider_event_id for event in events}) == 3


def test_snapshot_exposes_partial_source_state_without_inventing_events() -> None:
    start, end = _range()

    def http_get(url: str) -> str:
        if "fred.stlouisfed.org" in url:
            raise OSError("blocked")
        if "apps.bea.gov" in url:
            return _bea_payload()
        return _fed_page() if "2026-september" in url else _empty_fed_page("August 2026")

    payload = load_economic_calendar_snapshot(
        start=start,
        end=end,
        now=datetime(2026, 9, 2, 12, tzinfo=UTC),
        http_get=http_get,
    )

    assert payload["schema"] == ECONOMIC_CALENDAR_SCHEMA
    assert payload["status"] == "partial"
    assert {event["provider"] for event in payload["events"]} == {
        "bea",
        "federal_reserve",
    }
    source_status = {source["provider"]: source["status"] for source in payload["sources"]}
    assert source_status == {
        "fred": "unavailable",
        "federal_reserve": "ok",
        "bea": "ok",
    }


def test_snapshot_deduplicates_provider_event_identity_and_sorts_by_time() -> None:
    start, end = _range()

    def http_get(url: str) -> str:
        if "apps.bea.gov" in url:
            return _bea_payload()
        if "fred.stlouisfed.org" not in url:
            return _fed_page() if "2026-september" in url else _empty_fed_page("August 2026")
        release_id = int(parse_qs(urlsplit(url).query)["rid"][0])
        if release_id == 50:
            return _fred_payload(_fred_event_row(50, "Employment Situation"))
        return _fred_payload()

    payload = load_economic_calendar_snapshot(
        start=start,
        end=end,
        now=datetime(2026, 9, 2, 12, tzinfo=UTC),
        http_get=http_get,
    )

    assert payload["status"] == "ok"
    assert [event["event_type"] for event in payload["events"]] == [
        "international_trade",
        "employment_situation",
        "fomc",
        "fed_speech",
        "gross_domestic_product",
        "personal_income_and_outlays",
    ]
    assert len(
        {(event["provider"], event["provider_event_id"]) for event in payload["events"]}
    ) == len(payload["events"])


def test_runtime_uses_a_bounded_utc_window_and_returns_cache_copies() -> None:
    loader_calls: list[tuple[datetime, datetime, datetime]] = []
    monotonic_now = [100.0]

    def loader(*, start: datetime, end: datetime, now: datetime) -> dict:
        loader_calls.append((start, end, now))
        return {
            "schema": ECONOMIC_CALENDAR_SCHEMA,
            "status": "ok",
            "generated_at": now.isoformat(),
            "range": {"from": start.isoformat(), "to": end.isoformat()},
            "events": [],
            "sources": [],
        }

    runtime = EconomicCalendarRuntime(
        loader=loader,
        clock=lambda: datetime(2026, 9, 10, 16, 45, tzinfo=UTC),
        monotonic_clock=lambda: monotonic_now[0],
    )

    first = runtime.snapshot()
    first["events"].append({"bad": True})
    second = runtime.snapshot()

    assert len(loader_calls) == 1
    assert second["events"] == []
    assert loader_calls[0][0] == datetime(2026, 9, 3, tzinfo=UTC)
    assert loader_calls[0][1] == datetime(2026, 10, 2, tzinfo=UTC)
    monotonic_now[0] += 6 * 60 * 60 + 1
    runtime.snapshot()
    assert len(loader_calls) == 2


def test_router_runs_calendar_snapshot_through_async_boundary() -> None:
    router = create_economic_calendar_router(
        EconomicCalendarRouterDeps(
            snapshot=lambda: {
                "schema": ECONOMIC_CALENDAR_SCHEMA,
                "status": "ok",
                "events": [],
                "sources": [],
            }
        )
    )
    endpoint = next(
        route.endpoint
        for route in router.routes
        if route.path == "/api/economic-calendar" and "GET" in route.methods
    )

    payload = asyncio.run(endpoint())

    assert payload["schema"] == ECONOMIC_CALENDAR_SCHEMA
