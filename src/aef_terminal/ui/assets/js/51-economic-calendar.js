const ECONOMIC_CALENDAR_SCHEMA = "economic-calendar-v1";
const ECONOMIC_CALENDAR_IMPACT_POLICY = "martincall-us-macro-v1";
const ECONOMIC_CALENDAR_REFRESH_MS = 30 * 60 * 1000;
const ECONOMIC_CALENDAR_SOURCE = "economic-calendar";
const ECONOMIC_CALENDAR_PROVIDERS = new Set(["bea", "federal_reserve", "fred"]);

function normalizedEconomicCalendarText(value, field, maximum = 320) {
  if (typeof value !== "string") throw new Error(`ECONOMIC_CALENDAR_${field.toUpperCase()}_INVALID`);
  const normalized = value.replace(/\s+/g, " ").trim();
  if (!normalized || normalized.length > maximum) {
    throw new Error(`ECONOMIC_CALENDAR_${field.toUpperCase()}_INVALID`);
  }
  return normalized;
}

function normalizeEconomicCalendarPayload(payload) {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new Error("ECONOMIC_CALENDAR_PAYLOAD_INVALID");
  }
  if (payload.schema !== ECONOMIC_CALENDAR_SCHEMA) {
    throw new Error("ECONOMIC_CALENDAR_SCHEMA_INVALID");
  }
  if (!["ok", "partial", "unavailable"].includes(payload.status)) {
    throw new Error("ECONOMIC_CALENDAR_STATUS_INVALID");
  }
  if (!Array.isArray(payload.events) || !Array.isArray(payload.sources)) {
    throw new Error("ECONOMIC_CALENDAR_COLLECTION_INVALID");
  }
  const identities = new Set();
  const events = payload.events.map(item => {
    if (!item || typeof item !== "object" || Array.isArray(item)) {
      throw new Error("ECONOMIC_CALENDAR_EVENT_INVALID");
    }
    const provider = normalizedEconomicCalendarText(item.provider, "provider", 48);
    if (!ECONOMIC_CALENDAR_PROVIDERS.has(provider)) {
      throw new Error("ECONOMIC_CALENDAR_PROVIDER_INVALID");
    }
    const providerEventId = normalizedEconomicCalendarText(item.provider_event_id, "event_id", 160);
    const identity = `${provider}|${providerEventId}`;
    if (identities.has(identity)) throw new Error("ECONOMIC_CALENDAR_EVENT_DUPLICATE");
    identities.add(identity);
    const scheduledMs = Date.parse(item.scheduled_at || "");
    if (!Number.isFinite(scheduledMs)) throw new Error("ECONOMIC_CALENDAR_TIME_INVALID");
    const impact = normalizedEconomicCalendarText(item.impact, "impact", 16);
    if (!["high", "medium"].includes(impact)) {
      throw new Error("ECONOMIC_CALENDAR_IMPACT_INVALID");
    }
    if (item.time_precision !== "exact") {
      throw new Error("ECONOMIC_CALENDAR_PRECISION_INVALID");
    }
    const sourceUrl = normalizedEconomicCalendarText(item.source_url, "source_url", 500);
    if (!sourceUrl.startsWith("https://")) {
      throw new Error("ECONOMIC_CALENDAR_SOURCE_URL_INVALID");
    }
    const impactPolicy = normalizedEconomicCalendarText(item.impact_policy, "impact_policy", 80);
    if (impactPolicy !== ECONOMIC_CALENDAR_IMPACT_POLICY) {
      throw new Error("ECONOMIC_CALENDAR_IMPACT_POLICY_INVALID");
    }
    const sourceTimezone = normalizedEconomicCalendarText(item.source_timezone, "source_timezone", 80);
    try {
      new Intl.DateTimeFormat("en-US", { timeZone: sourceTimezone }).format(new Date(0));
    } catch (_error) {
      throw new Error("ECONOMIC_CALENDAR_SOURCE_TIMEZONE_INVALID");
    }
    if (item.country !== "US") throw new Error("ECONOMIC_CALENDAR_COUNTRY_INVALID");
    return Object.freeze({
      provider,
      providerEventId,
      scheduledAt: new Date(scheduledMs).toISOString(),
      title: normalizedEconomicCalendarText(item.title, "title", 320),
      eventType: normalizedEconomicCalendarText(item.event_type, "event_type", 80),
      category: normalizedEconomicCalendarText(item.category, "category", 120),
      impact,
      impactPolicy,
      sourceName: normalizedEconomicCalendarText(item.source_name, "source_name", 120),
      sourceUrl,
      sourceTimezone,
      country: "US",
      timePrecision: "exact",
    });
  }).sort((left, right) => (
    Date.parse(left.scheduledAt) - Date.parse(right.scheduledAt)
    || left.provider.localeCompare(right.provider)
    || left.providerEventId.localeCompare(right.providerEventId)
  ));
  const generatedMs = Date.parse(payload.generated_at || "");
  if (!Number.isFinite(generatedMs)) throw new Error("ECONOMIC_CALENDAR_GENERATED_AT_INVALID");
  const sources = payload.sources.map(source => {
    if (!source || typeof source !== "object" || Array.isArray(source)) {
      throw new Error("ECONOMIC_CALENDAR_SOURCE_INVALID");
    }
    const provider = normalizedEconomicCalendarText(source.provider, "source_provider", 48);
    const status = normalizedEconomicCalendarText(source.status, "source_status", 16);
    const fetchedMs = Date.parse(source.fetched_at || "");
    const eventCount = Number(source.event_count);
    if (
      !ECONOMIC_CALENDAR_PROVIDERS.has(provider)
      || !["ok", "partial", "unavailable"].includes(status)
      || !Number.isFinite(fetchedMs)
      || !Number.isSafeInteger(eventCount)
      || eventCount < 0
      || !Array.isArray(source.failed_scopes)
    ) throw new Error("ECONOMIC_CALENDAR_SOURCE_INVALID");
    const sourceUrl = normalizedEconomicCalendarText(source.source_url, "source_url", 500);
    if (!sourceUrl.startsWith("https://")) throw new Error("ECONOMIC_CALENDAR_SOURCE_URL_INVALID");
    return Object.freeze({
      provider,
      status,
      fetchedAt: new Date(fetchedMs).toISOString(),
      eventCount,
      sourceName: normalizedEconomicCalendarText(source.source_name, "source_name", 160),
      sourceUrl,
      errorCode: source.error_code === null
        ? null
        : normalizedEconomicCalendarText(source.error_code, "source_error_code", 120),
      failedScopes: Object.freeze(source.failed_scopes.map(scope => (
        normalizedEconomicCalendarText(scope, "source_scope", 80)
      ))),
    });
  });
  return Object.freeze({
    schema: ECONOMIC_CALENDAR_SCHEMA,
    status: payload.status,
    generatedAt: new Date(generatedMs).toISOString(),
    events: Object.freeze(events),
    sources: Object.freeze(sources),
  });
}

function clearEconomicCalendarRefreshTimer() {
  const calendar = state.economicCalendar;
  if (!calendar?.refreshTimer) return;
  clearTimeout(calendar.refreshTimer);
  calendar.refreshTimer = null;
}

function scheduleEconomicCalendarRefresh() {
  const calendar = state.economicCalendar;
  clearEconomicCalendarRefreshTimer();
  if (!calendar?.started || !state.settings.economicCalendarEnabled) return;
  calendar.refreshTimer = setTimeout(() => {
    calendar.refreshTimer = null;
    void loadEconomicCalendar({ force: true });
  }, ECONOMIC_CALENDAR_REFRESH_MS);
}

function redrawEconomicCalendarLayer(reason) {
  if (typeof clearCanvasTooltipsBySource === "function") {
    clearCanvasTooltipsBySource("volume", ECONOMIC_CALENDAR_SOURCE);
  }
  if (state.snapshot?.bars?.length) {
    renderCharts(state.snapshot, {
      force: true,
      overlayImmediate: true,
      reason: reason || "economic-calendar",
    });
  }
}

async function loadEconomicCalendar(options = {}) {
  const calendar = state.economicCalendar;
  if (!calendar?.started || !state.settings.economicCalendarEnabled) return null;
  if (calendar.loading && !options.force) return null;
  if (calendar.abort) calendar.abort.abort();
  const controller = new AbortController();
  const sequence = Number(calendar.sequence || 0) + 1;
  calendar.sequence = sequence;
  calendar.abort = controller;
  calendar.loading = true;
  try {
    const payload = await fetchJson("/api/economic-calendar", {
      cache: "no-store",
      sharedTtlMs: 0,
      timeoutMs: 30000,
      signal: controller.signal,
    });
    if (sequence !== calendar.sequence) return null;
    const normalized = normalizeEconomicCalendarPayload(payload);
    calendar.schema = normalized.schema;
    calendar.status = normalized.status;
    calendar.generatedAt = normalized.generatedAt;
    calendar.events = normalized.events;
    calendar.sources = normalized.sources;
    redrawEconomicCalendarLayer("economic-calendar-loaded");
    return normalized;
  } catch (error) {
    if (sequence !== calendar.sequence || controller.signal.aborted) return null;
    calendar.status = calendar.events.length ? "stale" : "unavailable";
    debugStep(
      "economic calendar",
      requestErrorMessage(error, "calendar unavailable"),
    );
    return null;
  } finally {
    if (sequence === calendar.sequence) {
      calendar.loading = false;
      calendar.abort = null;
      scheduleEconomicCalendarRefresh();
    }
  }
}

function disableEconomicCalendarProjection() {
  const calendar = state.economicCalendar;
  const changed = Boolean(calendar.events.length || calendar.status !== "disabled");
  calendar.sequence = Number(calendar.sequence || 0) + 1;
  if (calendar.abort) calendar.abort.abort();
  calendar.abort = null;
  calendar.loading = false;
  clearEconomicCalendarRefreshTimer();
  calendar.schema = "";
  calendar.status = "disabled";
  calendar.events = [];
  calendar.sources = [];
  calendar.generatedAt = "";
  if (changed) redrawEconomicCalendarLayer("economic-calendar-disabled");
}

function syncEconomicCalendarRuntime(options = {}) {
  const calendar = state.economicCalendar;
  if (!calendar?.started) return Promise.resolve(null);
  if (!state.settings.economicCalendarEnabled) {
    disableEconomicCalendarProjection();
    return Promise.resolve(null);
  }
  const generatedMs = Date.parse(calendar.generatedAt || "");
  const refreshDue = !Number.isFinite(generatedMs) || Date.now() - generatedMs >= ECONOMIC_CALENDAR_REFRESH_MS;
  if (options.forceRefresh || calendar.status === "idle" || calendar.status === "disabled") {
    return loadEconomicCalendar({ force: Boolean(options.forceRefresh) });
  }
  if (options.refresh && refreshDue) return loadEconomicCalendar();
  scheduleEconomicCalendarRefresh();
  return Promise.resolve(null);
}

function startEconomicCalendarRuntime() {
  state.economicCalendar.started = true;
  return syncEconomicCalendarRuntime({ forceRefresh: true });
}

function stopEconomicCalendarRuntime() {
  const calendar = state.economicCalendar;
  calendar.started = false;
  calendar.sequence = Number(calendar.sequence || 0) + 1;
  if (calendar.abort) calendar.abort.abort();
  calendar.abort = null;
  calendar.loading = false;
  clearEconomicCalendarRefreshTimer();
}

function economicCalendarAbsoluteIndex(event, snapshot, geometry) {
  const allBars = geometry?.visible?.allBars;
  if (!Array.isArray(allBars) || !allBars.length) return null;
  const timestampMs = Date.parse(event?.scheduledAt || "");
  if (!Number.isFinite(timestampMs)) return null;
  const historicalIndex = chartBarIndexForEventTimestamp(allBars, timestampMs);
  if (historicalIndex >= 0) return historicalIndex;
  const futureSlots = providerFutureAxisIndex({
    allBars,
    snapshot,
    timeframe: state.timeframe,
    futureAxis: snapshot?.future_axis,
    slotStep: intervalMinutesFromState(),
  }).slots;
  const futureIndex = chartBarIndexForEventTimestamp(futureSlots, timestampMs);
  return futureIndex >= 0 ? futureSlots[futureIndex]?.absoluteIndex ?? null : null;
}

function economicCalendarMarkerGroups(snapshot, geometry, width) {
  if (!state.settings.economicCalendarEnabled) return [];
  const events = state.economicCalendar?.events;
  if (!Array.isArray(events) || !events.length) return [];
  const start = Number(geometry?.visible?.start) || 0;
  const groups = new Map();
  events.forEach(event => {
    const absoluteIndex = economicCalendarAbsoluteIndex(event, snapshot, geometry);
    if (!Number.isSafeInteger(absoluteIndex)) return;
    const screenIndex = absoluteIndex - start;
    const x = geometry.x(screenIndex);
    if (!isChartPlotXVisible(x, geometry.pad, width, 2)) return;
    const key = String(absoluteIndex);
    const group = groups.get(key) || { absoluteIndex, screenIndex, x, events: [] };
    group.events.push(event);
    groups.set(key, group);
  });
  return [...groups.values()]
    .map(group => ({
      ...group,
      events: group.events.sort((left, right) => (
        Date.parse(left.scheduledAt) - Date.parse(right.scheduledAt)
        || left.providerEventId.localeCompare(right.providerEventId)
      )),
    }))
    .sort((left, right) => left.absoluteIndex - right.absoluteIndex);
}

function economicCalendarEventTime(event) {
  const scheduled = new Date(event.scheduledAt);
  const local = new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    timeZoneName: "short",
  }).format(scheduled);
  const source = new Intl.DateTimeFormat("en-US", {
    hour: "numeric",
    minute: "2-digit",
    timeZone: event.sourceTimezone,
    timeZoneName: "short",
  }).format(scheduled);
  return `${local} · source ${source}`;
}

function economicCalendarTooltip(events) {
  const lines = [];
  if (events.length > 1) {
    lines.push({ role: "title", text: `${events.length} economic events` });
  }
  events.forEach((event, index) => {
    if (index > 0) lines.push({ role: "separator", text: "---" });
    lines.push({ role: "title", text: event.title });
    lines.push({ role: "meta", text: economicCalendarEventTime(event) });
    lines.push({
      role: "meta",
      text: `${event.impact === "high" ? "High" : "Medium"} impact · ${event.category}`,
    });
    lines.push({ role: "meta", text: `Source: ${event.sourceName}` });
  });
  return { lines, max_lines: 32 };
}

function drawEconomicCalendarMarkers(ctx, snapshot, geometry, width, options = {}) {
  const groups = economicCalendarMarkerGroups(snapshot, geometry, width);
  if (!groups.length) return;
  const bottom = geometry.pad.top + geometry.chartH - 2;
  groups.forEach(group => {
    const highImpact = group.events.some(event => event.impact === "high");
    const color = css(highImpact ? "--red" : "--gold");
    const markerY = bottom - 12;
    const radius = 8;
    ctx.save();
    ctx.strokeStyle = color;
    ctx.fillStyle = color;
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.moveTo(group.x, bottom);
    ctx.lineTo(group.x, markerY + radius - 1);
    ctx.stroke();
    ctx.beginPath();
    ctx.arc(group.x, markerY, radius, 0, Math.PI * 2);
    ctx.fill();
    ctx.strokeStyle = css("--chart-bg");
    ctx.stroke();
    ctx.fillStyle = highImpact ? "#ffffff" : "#18202a";
    ctx.font = `bold 9px ${CANVAS_MONO_FONT}`;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(group.events.length > 1 ? String(Math.min(group.events.length, 9)) : "E", group.x, markerY + 0.5);
    ctx.restore();
    if (options.registerTooltips === true) {
      registerCanvasTooltip(
        "volume",
        group.x,
        markerY,
        Math.max(20, Math.min(Number(geometry.xStep) || 20, 36)),
        28,
        () => economicCalendarTooltip(group.events),
        null,
        canvasLayerZIndex("tooltips"),
        ECONOMIC_CALENDAR_SOURCE,
      );
    }
  });
}
