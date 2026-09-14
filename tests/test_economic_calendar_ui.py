from __future__ import annotations

import json
import subprocess
from pathlib import Path


CALENDAR_SOURCE = Path("src/aef_terminal/ui/assets/js/51-economic-calendar.js")


def test_browser_calendar_admission_and_axis_grouping(tmp_path: Path) -> None:
    source = CALENDAR_SOURCE.read_text(encoding="utf-8")
    script_path = tmp_path / "economic-calendar.js"
    script_path.write_text(
        "\n".join(
            (
                "const state = {",
                "  settings: { economicCalendarEnabled: true },",
                "  economicCalendar: { events: [], sources: [], status: 'idle', started: false, sequence: 0, abort: null, refreshTimer: null },",
                "  timeframe: '5m', snapshot: null,",
                "};",
                "function chartBarIndexForEventTimestamp(bars, timestampMs) {",
                "  let match = -1;",
                "  for (let index = 0; index < bars.length; index += 1) {",
                "    const start = Date.parse(bars[index].ts);",
                "    if (start <= timestampMs && timestampMs < start + 5 * 60 * 1000) match = index;",
                "  }",
                "  return match;",
                "}",
                "function providerFutureAxisIndex() { return { slots: [{ ts: '2026-09-16T10:10:00Z', absoluteIndex: 2 }] }; }",
                "function intervalMinutesFromState() { return 5; }",
                "function isChartPlotXVisible(x, _pad, width) { return x >= 0 && x <= width; }",
                source,
                "const payload = {",
                "  schema: 'economic-calendar-v1', status: 'ok', generated_at: '2026-09-16T09:00:00Z', sources: [],",
                "  events: [",
                "    { provider: 'bea', provider_event_id: 'bea:one', scheduled_at: '2026-09-16T10:02:00Z', title: 'GDP', event_type: 'gross_domestic_product', category: 'US macro release', impact: 'high', impact_policy: 'martincall-us-macro-v1', source_name: 'BEA', source_url: 'https://www.bea.gov/news/schedule', source_timezone: 'UTC', country: 'US', time_precision: 'exact' },",
                "    { provider: 'federal_reserve', provider_event_id: 'fed:one', scheduled_at: '2026-09-16T10:10:00Z', title: 'FOMC Meeting', event_type: 'fomc', category: 'FOMC Meetings', impact: 'high', impact_policy: 'martincall-us-macro-v1', source_name: 'Federal Reserve', source_url: 'https://www.federalreserve.gov/newsevents/', source_timezone: 'America/New_York', country: 'US', time_precision: 'exact' },",
                "    { provider: 'fred', provider_event_id: 'fred:one', scheduled_at: '2026-09-16T10:11:00Z', title: 'PPI', event_type: 'producer_price_index', category: 'US macro release', impact: 'medium', impact_policy: 'martincall-us-macro-v1', source_name: 'FRED', source_url: 'https://fred.stlouisfed.org/releases/calendar', source_timezone: 'America/Chicago', country: 'US', time_precision: 'exact' },",
                "  ],",
                "};",
                "const admitted = normalizeEconomicCalendarPayload(payload);",
                "if (!Object.isFrozen(admitted.events) || admitted.events.length !== 3) process.exit(2);",
                "state.economicCalendar.events = admitted.events;",
                "const geometry = { visible: { allBars: [{ ts: '2026-09-16T10:00:00Z' }, { ts: '2026-09-16T10:05:00Z' }], start: 0 }, pad: { left: 0, right: 0 }, x: index => 10 + index * 10 };",
                "const groups = economicCalendarMarkerGroups({ future_axis: {} }, geometry, 100);",
                "if (groups.length !== 2 || groups[0].events.length !== 1 || groups[1].events.length !== 2) process.exit(3);",
                "state.settings.economicCalendarEnabled = false;",
                "if (economicCalendarMarkerGroups({ future_axis: {} }, geometry, 100).length !== 0) process.exit(4);",
                "let invalidRejected = false;",
                "try { normalizeEconomicCalendarPayload({ ...payload, events: [{ ...payload.events[0], time_precision: 'estimated' }] }); } catch (error) { invalidRejected = error.message === 'ECONOMIC_CALENDAR_PRECISION_INVALID'; }",
                "if (!invalidRejected) process.exit(5);",
                "console.log(JSON.stringify(groups.map(group => group.events.length)));",
            )
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        ["node", str(script_path)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [1, 2]
