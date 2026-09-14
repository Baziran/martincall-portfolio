function adaptiveKernelConfig() {
  return state.indicators?.adaptiveKernelRegression || {};
}

function adaptiveKernelPlanStateText(value) {
  const stateCode = String(value || "").trim().toLowerCase();
  return ({
    armed: "ARMED",
    waiting_pullback: "WAIT PULLBACK",
    waiting_reclaim: "WAIT RECLAIM",
    ready_next_open: "READY NEXT OPEN",
    triggered: "TRIGGERED",
    expired: "EXPIRED",
    invalidated: "INVALIDATED",
    observing: "OBSERVING",
    unavailable: "UNAVAILABLE",
    unsupported_timeframe: "3m / 5m ONLY",
    preview_ignored: "CONFIRMED ONLY",
  })[stateCode] || stateCode.replace(/_/g, " ").toUpperCase();
}

function adaptiveKernelTriggerLineText(value) {
  const line = String(value || "").trim().toLowerCase();
  return line === "upper" ? "UPPER BAND" : line === "ema20" ? "EMA20" : "--";
}

function adaptiveKernelTableColumns(table) {
  const model = table?.model && typeof table.model === "object" ? table.model : {};
  const stateCode = String(model.state || "neutral").toLowerCase();
  const bullish = stateCode === "bullish";
  const bearish = stateCode === "bearish";
  const directionTone = bullish ? "positive" : bearish ? "negative" : "neutral";
  const signalText = bullish ? "▲  BULLISH" : bearish ? "▼  BEARISH" : "•  NEUTRAL";
  const columns = [
    {
      cells: ["Signal", signalText, ""],
      tone: directionTone,
      scenario: "Confirmed kernel regime",
    },
    {
      cells: [
        "Kernel MA",
        { kind: "price", value: model.center },
        {
          kind: "fixed",
          prefix: "h ",
          value: model.effective_bandwidth,
          digits: 2,
          suffix: model.adaptive_bandwidth ? " adaptive" : "",
        },
      ],
      scenario: "Kernel moving average and effective bandwidth",
    },
    {
      cells: [
        "Bands",
        { kind: "price", prefix: "U ", value: model.upper },
        { kind: "price", prefix: "L ", value: model.lower },
      ],
      scenario: "Upper and lower residual bands",
    },
    {
      cells: ["Band Width σ", { kind: "price", value: model.residual_sigma }, ""],
      scenario: "Residual standard deviation",
    },
  ];
  const plan = model.plan && typeof model.plan === "object" ? model.plan : {};
  const planRequested = adaptiveKernelConfig().entryPlan === true;
  const planEnabled = plan.enabled === true;
  if (!planRequested) return columns;
  if (!planEnabled) {
    columns.push(
      {
        cells: ["Plan", "REFRESHING", "PENDING"],
        tone: "warning",
        scenario: "Entry plan requested; awaiting refreshed analysis",
      },
      {
        cells: ["Trigger", "--", "WAIT"],
        tone: "warning",
        scenario: "Trigger unavailable until refreshed analysis arrives",
      },
    );
    return columns;
  }
  const planDirection = String(plan.direction || "flat").toLowerCase();
  const planTone = planDirection === "long"
    ? "positive"
    : planDirection === "short"
      ? "negative"
      : "warning";
  const timing = plan.entry_ready === true
    ? "NEXT OPEN"
    : plan.triggered === true && Number.isFinite(Number(plan.entry_price))
      ? { kind: "price", prefix: "E ", value: plan.entry_price }
      : `${Number(plan.wait_minutes || 60)}m`;
  columns.push(
    {
      cells: [
        "Plan",
        adaptiveKernelPlanStateText(plan.state),
        planDirection === "long" ? "LONG" : planDirection === "short" ? "SHORT" : "FLAT",
      ],
      tone: planTone,
      scenario: "Advisory entry-plan state",
    },
    {
      cells: [
        "Trigger",
        {
          kind: "price",
          prefix: `${adaptiveKernelTriggerLineText(plan.trigger_line)} `,
          value: plan.trigger_level,
          empty: "--",
        },
        timing,
      ],
      tone: planTone,
      scenario: "Confirmed-bar pullback and reclaim trigger",
    },
  );
  return columns;
}

registerIndicatorTableModel("adaptive_kernel_regression", adaptiveKernelTableColumns);

registerIndicatorStateSeriesPresentation("adaptive_kernel_regression", {
  kind: "indicator_state_series_v1",
  fields: {
    ts: "ts",
    center: "center",
    upper: "upper",
    lower: "lower",
    residual: "residual",
    state: "state",
  },
  controls: {
    mode: "visualMode",
    fill: "fill",
    theme: "theme",
    candles: "candles",
    text_size: "textSize",
    calculation_mode: "calculationMode",
  },
  modes: {
    bands: "bands",
    single: "single",
    trail: "trail",
  },
  initial_state: "bearish",
  state_colors: {
    bullish: "bull",
    bearish: "bear",
    neutral: "bear",
  },
  line: {
    single_width: 2,
    band_width: 1,
    trail_width: 5,
    band_alpha: 0.4,
    trail_alpha: 0.4,
  },
  fill_alpha: {
    bands: 0.2,
    single: 0.25,
    trail: 0.4,
  },
  preview: {
    mode: "provisional",
    line_alpha: 0.72,
    fill_alpha: 0.5,
    dash: [5, 4],
  },
  labels: {
    role: "kernel_regime_transition",
    hidden_when_setting_true: "entry_plan_enabled",
  },
  default_theme: "Classic",
});

registerIndicatorOverlayFilter("adaptive_kernel_regression_filter", indicatorStateSeriesOverlayFilter);

registerIndicatorStateSeriesCandleProvider(
  "adaptive_kernel_regression",
  { priority: 200 },
);

registerIndicatorCustomOverlayRenderer(
  "adaptive_kernel_regression_series",
  drawGenericIndicatorStateSeries,
);

const adaptiveKernelRefreshTracker = {
  activePreview: "",
  timer: null,
  lastRefreshAt: 0,
};

function resetAdaptiveKernelPreviewRefresh() {
  window.clearTimeout(adaptiveKernelRefreshTracker.timer);
  adaptiveKernelRefreshTracker.timer = null;
  adaptiveKernelRefreshTracker.activePreview = "";
}

function maybeScheduleAdaptiveKernelPreviewRefresh(reason = "chart-stream") {
  const config = adaptiveKernelConfig();
  if (document.visibilityState !== "visible") return false;
  if (
    !config.enabled
    || String(config.calculationMode || "confirmed").toLowerCase() !== "provisional"
  ) return false;
  if (
    state.serverSleeping
    || !providerCapability(state.dataSource, "chart_stream")
    || !state.snapshot?.bars?.length
    || !snapshotMatchesCurrentRoute()
  ) return false;
  if (effectiveDataQuality(state.snapshot?.meta || {}).market_closed) return false;
  const bars = state.snapshot.bars;
  const latest = bars[bars.length - 1];
  if (!barIsLivePreview(latest)) return false;
  const confirmed = latestConfirmedIndicatorBar(state.snapshot);
  const confirmedKey = timestampKey(confirmed?.ts);
  const liveKey = timestampKey(latest?.ts);
  const livePrice = Number(latest?.close ?? state.snapshot?.meta?.price);
  if (!confirmedKey || !liveKey || !Number.isFinite(livePrice)) return false;
  const refreshIntervalMs = 1500;
  const refreshBucket = Math.floor(Date.now() / refreshIntervalMs);
  const scheduleKey = `${liveKey}|${refreshBucket}`;
  const analysisVersionKey = `${confirmedKey}|kernel-preview|${liveKey}|${livePrice}`;
  const elapsed = Date.now() - Number(adaptiveKernelRefreshTracker.lastRefreshAt || 0);
  const ownerKey = JSON.stringify([
    state.instrumentId,
    instrumentRouteFingerprint(),
    state.timeframe,
    state.range,
    "adaptive-kernel-preview",
    refreshBucket,
  ]);
  return scheduleAnalysisRefreshOnce({
    latestKey: scheduleKey,
    activeBarStateKey: "activePreview",
    timerStateKey: "timer",
    lastRefreshStateKey: "lastRefreshAt",
    delayMs: Math.max(0, refreshIntervalMs - elapsed),
    reason,
    debugLabel: "adaptive kernel provisional refresh",
    ownerKey,
    tracker: adaptiveKernelRefreshTracker,
    canRun: () => (
      adaptiveKernelConfig().enabled
      && String(adaptiveKernelConfig().calculationMode || "confirmed").toLowerCase()
        === "provisional"
    ),
    retry: () => maybeScheduleAdaptiveKernelPreviewRefresh("busy"),
    loadOptions: {
      force: true,
      splitAnalysis: true,
      queueAnalysis: true,
      analysisOnly: true,
      reuseAnalysisDemand: true,
      analysisVersionKey,
      analysisRequireBarTs: confirmedKey,
    },
  });
}

registerIndicatorAnalysisRefreshScheduler("adaptive_kernel_regression", {
  schedule: maybeScheduleAdaptiveKernelPreviewRefresh,
  reset: resetAdaptiveKernelPreviewRefresh,
});
