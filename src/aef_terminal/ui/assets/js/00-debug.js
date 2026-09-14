window.mcDebugStep = function (phase, detail) {
      var panel = document.getElementById("debug-panel");
      if (!panel || !document.body.classList.contains("debug-mode")) return;
      var phaseNode = document.getElementById("debug-phase");
      var logNode = document.getElementById("debug-log");
      var line = new Date().toLocaleTimeString() + " | " + phase + (detail ? " | " + detail : "");
      window.mcDebugLines = (window.mcDebugLines || []).concat(line).slice(-40);
      if (phaseNode) phaseNode.textContent = phase;
      if (logNode) logNode.textContent = window.mcDebugLines.join("\\n");
    };
    window.mcPerfConsoleEnabled = function () {
      var body = document.body;
      return window.mcPerfConsole === true || Boolean(body && body.classList.contains("debug-mode"));
    };
    window.mcPerfEnabled = function () {
      return window.mcPerfProfile === true || window.mcPerfConsoleEnabled();
    };
    function mcPerfEntryLimit() {
      return window.mcPerfBudgetHarness ? 2000 : 80;
    }
    function mcStorePerfEntry(entry, options) {
      window.mcPerfSamples = window.mcPerfSamples || {};
      var label = String(entry && entry.label || "work");
      var labelSamples = window.mcPerfSamples[label] || [];
      labelSamples.push(entry);
      if (labelSamples.length > 1000) labelSamples.splice(0, labelSamples.length - 1000);
      window.mcPerfSamples[label] = labelSamples;
      if (!options || options.summary !== false) {
        var entries = window.mcPerfEntries || [];
        entries.push(entry);
        var entryLimit = mcPerfEntryLimit();
        if (entries.length > entryLimit) entries.splice(0, entries.length - entryLimit);
        window.mcPerfEntries = entries;
      }
    }
    window.mcTelemetryInc = function (key, amount) {
      var name = String(key || "");
      if (!name) return 0;
      var value = Number(amount);
      var delta = Number.isFinite(value) ? value : 1;
      window.mcTelemetryCounters = window.mcTelemetryCounters || {};
      window.mcTelemetryCounters[name] = Number(window.mcTelemetryCounters[name] || 0) + delta;
      return window.mcTelemetryCounters[name];
    };
    window.mcTelemetrySet = function (key, value) {
      var name = String(key || "");
      if (!name) return value;
      window.mcTelemetryState = window.mcTelemetryState || {};
      window.mcTelemetryState[name] = value;
      return value;
    };
    window.mcTelemetryMax = function (key, value) {
      var name = String(key || "");
      var numeric = Number(value);
      if (!name || !Number.isFinite(numeric)) return value;
      window.mcTelemetryState = window.mcTelemetryState || {};
      var current = Number(window.mcTelemetryState[name] || 0);
      var next = Math.max(Number.isFinite(current) ? current : 0, numeric);
      window.mcTelemetryState[name] = next;
      return next;
    };
    window.mcTelemetrySummary = function () {
      return {
        counters: Object.assign({}, window.mcTelemetryCounters || {}),
        state: Object.assign({}, window.mcTelemetryState || {}),
      };
    };
    window.mcPerfStart = function (label) {
      if (!window.mcPerfEnabled()) return null;
      return {
        label: String(label || "work"),
        start: (window.performance && performance.now) ? performance.now() : Date.now(),
      };
    };
    window.mcPerfEnd = function (token, detail, warnMs) {
      if (!token) return 0;
      var now = (window.performance && performance.now) ? performance.now() : Date.now();
      var elapsed = now - Number(token.start || now);
      var label = String(token.label || "work");
      var entry = {
        ts: new Date().toISOString(),
        label: label,
        ms: Math.round(elapsed * 10) / 10,
        detail: String(detail || ""),
      };
      mcStorePerfEntry(entry);
      if (window.mcRenderPerfPanel) window.mcRenderPerfPanel();
      var threshold = Number.isFinite(Number(warnMs)) ? Number(warnMs) : 32;
      if (elapsed >= threshold) {
        var text = label + " " + entry.ms + "ms" + (entry.detail ? " | " + entry.detail : "");
        if (window.mcDebugStep) window.mcDebugStep("perf", text);
        var warnKey = label + "|" + entry.detail;
        window.mcPerfWarnAt = window.mcPerfWarnAt || {};
        if (now - Number(window.mcPerfWarnAt[warnKey] || 0) >= 5000) {
          window.mcPerfWarnAt[warnKey] = now;
          if (window.mcPerfConsoleEnabled() && window.console && console.warn) console.warn("MartinCall slow path " + text);
        }
      }
      return elapsed;
    };
    window.mcRecordBackendTiming = function (label, ms, detail) {
      if (!window.mcPerfEnabled()) return 0;
      var value = Number(ms);
      if (!Number.isFinite(value) || value < 0) return 0;
      var entry = {
        ts: new Date().toISOString(),
        label: "backend:" + String(label || "work"),
        ms: Math.round(value * 10) / 10,
        detail: String(detail || ""),
      };
      mcStorePerfEntry(entry);
      if (window.mcRenderPerfPanel) window.mcRenderPerfPanel();
      return entry.ms;
    };
    window.mcPerfSummary = function () {
      var rows = window.mcPerfEntries || [];
      var groups = {};
      rows.forEach(function (row) {
        var key = row.label || "work";
        if (!groups[key]) groups[key] = { label: key, count: 0, total: 0, max: 0, last: 0, values: [] };
        var ms = Number(row.ms) || 0;
        groups[key].count += 1;
        groups[key].total += ms;
        groups[key].max = Math.max(groups[key].max, ms);
        groups[key].last = ms;
        groups[key].values.push(ms);
      });
      return Object.values(groups)
        .map(function (row) {
          var values = row.values.slice().sort(function (left, right) { return left - right; });
          var percentile = function (pct) {
            if (!values.length) return 0;
            var index = Math.min(values.length - 1, Math.max(0, Math.ceil(values.length * pct) - 1));
            return Math.round(values[index] * 10) / 10;
          };
          return {
            label: row.label,
            count: row.count,
            avg: Math.round((row.total / Math.max(row.count, 1)) * 10) / 10,
            p95: percentile(0.95),
            p99: percentile(0.99),
            max: Math.round(row.max * 10) / 10,
            last: Math.round(row.last * 10) / 10,
          };
        })
        .sort(function (left, right) { return right.max - left.max; });
    };
    window.mcStartPerfBudgetHarness = function (options) {
      var config = options && typeof options === "object" ? options : {};
      var durationMs = Math.max(180000, Math.min(300000, Number(config.durationMs) || 180000));
      var budgets = {
        loadMarketP95Ms: 1200,
        renderChartsP95Ms: 120,
        fetchMarketP95Ms: 1500,
        fetchHeadersMarketP95Ms: 1000,
        fetchBodyMarketP95Ms: 250,
        fetchJsonParseMarketP95Ms: 80,
        marketNormalizeCompactP95Ms: 80,
        marketStateCommitP95Ms: 80,
        chartRenderQueueP95Ms: 34,
        chartRenderToPaintP95Ms: 80,
        liveCandleGatewayToPaintP95Ms: 300,
        quoteWsParseP95Ms: 4,
        quoteGatewayToPaintP95Ms: 400,
        chartWsParseP95Ms: 4,
        quoteMergeP95Ms: 8,
        renderWatchlistP95Ms: 8,
        watchlistRafQueueP95Ms: 20,
        quoteToCommitP95Ms: 16,
        quoteToPaintP95Ms: 34,
        crosshairPointerToCommitP95Ms: 8,
        crosshairRemoteToCommitP95Ms: 20,
        animationFrameGapP95Ms: 20,
        mainThreadLongTaskCount: 0,
        mainThreadLongTaskMaxMs: 50,
        marketFetchPerMinute: 18,
        screenerFetchPerMinute: 18,
        fetchTotalPerMinute: 80,
        storageFetchPerMinute: 2,
        paperFetchPerMinute: 2,
        paperOrdersFetchPerMinute: 2,
        paperTradesFetchPerMinute: 2,
        optionTargetsFetchPerMinute: 2,
        gexFetchPerMinute: 6,
        tickFetchPerMinute: 75,
        marketAnalysisFetchPerMinute: 20,
        optionPricingFetchPerMinute: 12,
        marketResponseMaxBytes: 8000000,
        quoteWsPayloadMaxBytes: 250000,
        chartWsPayloadMaxBytes: 500000,
      };
      var configuredBudgets = config.budgets && typeof config.budgets === "object"
        ? config.budgets
        : {};
      Object.keys(configuredBudgets).forEach(function (key) {
        if (configuredBudgets[key] === null) delete budgets[key];
        else budgets[key] = configuredBudgets[key];
      });
      var requiredSamples = Object.assign({
        quoteWsParseP95Ms: 5,
        chartWsParseP95Ms: 5,
        liveCandleGatewayToPaintP95Ms: 1,
        quoteGatewayToPaintP95Ms: 5,
        quoteMergeP95Ms: 1,
        renderWatchlistP95Ms: 1,
        watchlistRafQueueP95Ms: 1,
        quoteToCommitP95Ms: 1,
        quoteToPaintP95Ms: 1,
        crosshairPointerToCommitP95Ms: 1,
        crosshairRemoteToCommitP95Ms: 1,
        animationFrameGapP95Ms: 120,
        mainThreadLongTaskCount: 1,
        mainThreadLongTaskMaxMs: 1,
        quoteWsPayloadMaxBytes: 1,
        chartWsPayloadMaxBytes: 1,
      }, config.requiredSamples || {});
      window.mcPerfProfile = true;
      window.mcPerfEntries = [];
      window.mcPerfSamples = {};
      window.mcTelemetryCounters = {};
      window.mcTelemetryState = {};
      var startedAt = Date.now();
      var harness = {
        instrumentId: typeof config.instrumentId === "string" ? config.instrumentId : "",
        timeframe: String(config.timeframe || "5m"),
        range: String(config.range || "3d"),
        durationMs: durationMs,
        budgets: budgets,
        requiredSamples: requiredSamples,
        startedAt: new Date(startedAt).toISOString(),
        stop: function () {
          return window.mcFinishPerfBudgetHarness("manual");
        },
      };
      window.mcPerfBudgetHarness = harness;
      window.cancelAnimationFrame(window.mcPerfFrameMonitor || 0);
      if (window.mcPerfLongTaskObserver) window.mcPerfLongTaskObserver.disconnect();
      window.mcPerfLongTaskObserver = null;
      var previousFrameAt = 0;
      var frameWindowMax = 0;
      var frameWindowSize = 0;
      var sampleAnimationFrame = function (frameAt) {
        if (!window.mcPerfBudgetHarness) return;
        if (document.visibilityState !== "visible") {
          previousFrameAt = 0;
          frameWindowMax = 0;
          frameWindowSize = 0;
        } else {
          if (previousFrameAt > 0) {
            frameWindowMax = Math.max(frameWindowMax, frameAt - previousFrameAt);
            frameWindowSize += 1;
            if (frameWindowSize >= 30) {
              mcStorePerfEntry({
                ts: new Date().toISOString(),
                label: "animationFrameGap",
                ms: Math.round(frameWindowMax * 10) / 10,
                detail: "visible-window-max",
              }, { summary: false });
              frameWindowMax = 0;
              frameWindowSize = 0;
            }
          }
          previousFrameAt = frameAt;
        }
        window.mcPerfFrameMonitor = window.requestAnimationFrame(sampleAnimationFrame);
      };
      window.mcPerfFrameMonitor = window.requestAnimationFrame(sampleAnimationFrame);
      var longTaskSupported = Boolean(
        window.PerformanceObserver
        && Array.isArray(window.PerformanceObserver.supportedEntryTypes)
        && window.PerformanceObserver.supportedEntryTypes.indexOf("longtask") >= 0
      );
      window.mcTelemetrySet("main_thread.long_task_observer_supported", longTaskSupported ? 1 : 0);
      if (longTaskSupported) {
        try {
          window.mcPerfLongTaskObserver = new window.PerformanceObserver(function (entryList) {
            entryList.getEntries().forEach(function (entry) {
              var duration = Math.max(Number(entry.duration) || 0, 0);
              window.mcTelemetryInc("main_thread.long_task_count");
              window.mcTelemetryMax("main_thread.long_task_max_ms", duration);
              mcStorePerfEntry({
                ts: new Date().toISOString(),
                label: "mainThreadLongTask",
                ms: Math.round(duration * 10) / 10,
                detail: String(entry.name || "self"),
              });
            });
          });
          window.mcPerfLongTaskObserver.observe({ type: "longtask", buffered: false });
        } catch (_) {
          window.mcPerfLongTaskObserver = null;
          window.mcTelemetrySet("main_thread.long_task_observer_supported", 0);
        }
      }
      window.clearTimeout(window.mcPerfBudgetTimer);
      window.mcPerfBudgetTimer = window.setTimeout(function () {
        window.mcFinishPerfBudgetHarness("duration");
      }, durationMs);
      return harness;
    };
    window.mcFinishPerfBudgetHarness = function (reason) {
      var harness = window.mcPerfBudgetHarness;
      if (!harness) return window.mcPerfBudgetResult || null;
      window.clearTimeout(window.mcPerfBudgetTimer);
      window.mcPerfBudgetTimer = null;
      window.cancelAnimationFrame(window.mcPerfFrameMonitor || 0);
      window.mcPerfFrameMonitor = 0;
      if (window.mcPerfLongTaskObserver) window.mcPerfLongTaskObserver.disconnect();
      window.mcPerfLongTaskObserver = null;
      var endedAt = Date.now();
      var elapsedMinutes = Math.max((endedAt - Date.parse(harness.startedAt)) / 60000, 1 / 60);
      var samplesByLabel = window.mcPerfSamples || {};
      var samplesFor = function (label, detail) {
        return (samplesByLabel[label] || []).filter(function (row) {
          return !detail || row.detail === detail;
        });
      };
      var percentileFor = function (label, detail, pct) {
        var values = samplesFor(label, detail)
          .map(function (row) { return Number(row.ms) || 0; })
          .sort(function (left, right) { return left - right; });
        if (!values.length) return 0;
        var index = Math.min(values.length - 1, Math.max(0, Math.ceil(values.length * pct) - 1));
        return Math.round(values[index] * 10) / 10;
      };
      var counters = Object.assign({}, window.mcTelemetryCounters || {});
      var telemetryState = Object.assign({}, window.mcTelemetryState || {});
      var perMinute = function (keyPrefix) {
        return Object.keys(counters).reduce(function (total, key) {
          return key.indexOf(keyPrefix) === 0 ? total + Number(counters[key] || 0) : total;
        }, 0) / elapsedMinutes;
      };
      var routePerMinute = function (route) {
        return Number(counters["fetch.route:" + String(route || "")] || 0) / elapsedMinutes;
      };
      var metrics = {
        loadMarketP95Ms: percentileFor("loadMarket", "", 0.95),
        renderChartsP95Ms: percentileFor("renderCharts", "", 0.95),
        fetchMarketP95Ms: percentileFor("fetch", "/api/market", 0.95),
        fetchHeadersMarketP95Ms: percentileFor("fetchHeaders", "/api/market", 0.95),
        fetchBodyMarketP95Ms: percentileFor("fetchBody", "/api/market", 0.95),
        fetchJsonParseMarketP95Ms: percentileFor("fetchJsonParse", "/api/market", 0.95),
        marketNormalizeCompactP95Ms: percentileFor("marketNormalizeCompact", "", 0.95),
        marketStateCommitP95Ms: percentileFor("marketStateCommit", "", 0.95),
        chartRenderQueueP95Ms: percentileFor("chartRenderQueue", "", 0.95),
        chartRenderToPaintP95Ms: percentileFor("chartRenderToPaint", "", 0.95),
        liveCandleGatewayToPaintP95Ms: percentileFor("backend:liveCandleGatewayToPaint", "", 0.95),
        quoteWsParseP95Ms: percentileFor("quoteWsParse", "", 0.95),
        quoteGatewayToPaintP95Ms: percentileFor("backend:quoteGatewayToPaint", "", 0.95),
        chartWsParseP95Ms: percentileFor("chartWsParse", "", 0.95),
        quoteMergeP95Ms: percentileFor("quoteMerge", "", 0.95),
        renderWatchlistP95Ms: percentileFor("renderWatchlist", "", 0.95),
        watchlistRafQueueP95Ms: percentileFor("watchlistRafQueue", "", 0.95),
        quoteToCommitP95Ms: percentileFor("quoteToCommit", "", 0.95),
        quoteToPaintP95Ms: percentileFor("quoteToPaint", "", 0.95),
        crosshairPointerToCommitP95Ms: percentileFor("crosshairPointerToCommit", "", 0.95),
        crosshairRemoteToCommitP95Ms: percentileFor("crosshairRemoteToCommit", "", 0.95),
        animationFrameGapP95Ms: percentileFor("animationFrameGap", "visible-window-max", 0.95),
        mainThreadLongTaskCount: Number(counters["main_thread.long_task_count"] || 0),
        mainThreadLongTaskMaxMs: Number(telemetryState["main_thread.long_task_max_ms"] || 0),
        marketFetchPerMinute: routePerMinute("/api/market"),
        screenerFetchPerMinute: routePerMinute("/api/screener"),
        fetchTotalPerMinute: perMinute("fetch.route:"),
        storageFetchPerMinute: perMinute("fetch.route:/api/storage"),
        paperFetchPerMinute: perMinute("fetch.route:/api/paper/"),
        paperOrdersFetchPerMinute: routePerMinute("/api/paper/orders"),
        paperTradesFetchPerMinute: routePerMinute("/api/paper/trades"),
        optionTargetsFetchPerMinute: perMinute("fetch.route:/api/option-targets"),
        gexFetchPerMinute: perMinute("fetch.route:/api/gex"),
        tickFetchPerMinute: perMinute("fetch.route:/api/ticks/"),
        marketAnalysisFetchPerMinute: routePerMinute("/api/market/analysis"),
        optionPricingFetchPerMinute: perMinute("fetch.route:/api/options/"),
        marketResponseMaxBytes: Number(telemetryState["fetch.bytes.max:/api/market"] || 0),
        quoteWsPayloadMaxBytes: Number(telemetryState["quote_ws.payload_bytes_max"] || 0),
        chartWsPayloadMaxBytes: Number(telemetryState["chart_ws.payload_bytes_max"] || 0),
      };
      var metricSamples = {
        loadMarketP95Ms: samplesFor("loadMarket", "").length,
        renderChartsP95Ms: samplesFor("renderCharts", "").length,
        fetchMarketP95Ms: samplesFor("fetch", "/api/market").length,
        fetchHeadersMarketP95Ms: samplesFor("fetchHeaders", "/api/market").length,
        fetchBodyMarketP95Ms: samplesFor("fetchBody", "/api/market").length,
        fetchJsonParseMarketP95Ms: samplesFor("fetchJsonParse", "/api/market").length,
        marketNormalizeCompactP95Ms: samplesFor("marketNormalizeCompact", "").length,
        marketStateCommitP95Ms: samplesFor("marketStateCommit", "").length,
        chartRenderQueueP95Ms: samplesFor("chartRenderQueue", "").length,
        chartRenderToPaintP95Ms: samplesFor("chartRenderToPaint", "").length,
        liveCandleGatewayToPaintP95Ms: samplesFor("backend:liveCandleGatewayToPaint", "").length,
        quoteWsParseP95Ms: samplesFor("quoteWsParse", "").length,
        quoteGatewayToPaintP95Ms: samplesFor("backend:quoteGatewayToPaint", "").length,
        chartWsParseP95Ms: samplesFor("chartWsParse", "").length,
        quoteMergeP95Ms: samplesFor("quoteMerge", "").length,
        renderWatchlistP95Ms: samplesFor("renderWatchlist", "").length,
        watchlistRafQueueP95Ms: samplesFor("watchlistRafQueue", "").length,
        quoteToCommitP95Ms: samplesFor("quoteToCommit", "").length,
        quoteToPaintP95Ms: samplesFor("quoteToPaint", "").length,
        crosshairPointerToCommitP95Ms: samplesFor("crosshairPointerToCommit", "").length,
        crosshairRemoteToCommitP95Ms: samplesFor("crosshairRemoteToCommit", "").length,
        animationFrameGapP95Ms: samplesFor("animationFrameGap", "visible-window-max").length,
        mainThreadLongTaskCount: Number(telemetryState["main_thread.long_task_observer_supported"] || 0),
        mainThreadLongTaskMaxMs: Number(telemetryState["main_thread.long_task_observer_supported"] || 0),
        quoteWsPayloadMaxBytes: Number(counters["quote_ws.payload_samples"] || 0),
        chartWsPayloadMaxBytes: Number(counters["chart_ws.payload_samples"] || 0),
      };
      var checks = Object.keys(harness.budgets).map(function (key) {
        var actual = Number(metrics[key] || 0);
        var budget = Number(harness.budgets[key]);
        var samples = Number(metricSamples[key] || 0);
        var required = Number(harness.requiredSamples[key] || 0);
        return {
          key: key,
          actual: Math.round(actual * 10) / 10,
          budget: budget,
          samples: samples,
          requiredSamples: required,
          pass: actual <= budget && samples >= required,
        };
      });
      var result = {
        ok: checks.every(function (item) { return item.pass; }),
        reason: String(reason || "done"),
        startedAt: harness.startedAt,
        endedAt: new Date(endedAt).toISOString(),
        elapsedMinutes: Math.round(elapsedMinutes * 100) / 100,
        instrumentId: harness.instrumentId,
        timeframe: harness.timeframe,
        range: harness.range,
        metrics: metrics,
        metricSamples: metricSamples,
        checks: checks,
        summary: window.mcPerfSummary(),
        telemetry: window.mcTelemetrySummary(),
      };
      window.mcPerfBudgetResult = result;
      window.mcPerfBudgetHarness = null;
      if (window.console && console.table) console.table(checks);
      return result;
    };
    window.mcRenderPerfPanel = function () {
      var panel = document.getElementById("debug-perf");
      if (!panel || !window.mcPerfEnabled()) return;
      var rows = window.mcPerfSummary().slice(0, 8);
      var telemetry = window.mcTelemetrySummary ? window.mcTelemetrySummary() : { counters: {}, state: {} };
      var telemetryLine = "telemetry: render "
        + Number(telemetry.counters["render.requested"] || 0)
        + "/"
        + Number(telemetry.counters["render.rendered"] || 0)
        + " skip="
        + Number(telemetry.counters["render.skipped"] || 0)
        + " coalesce="
        + Number(telemetry.counters["render.coalesced"] || 0)
        + " reason="
        + String(telemetry.state.render_reason || "-")
        + " theme="
        + String(telemetry.state.theme || "-")
        + " source="
        + String(telemetry.state.theme_source || "-");
      panel.textContent = rows.length
        ? rows.map(function (row) {
            return row.label + " n=" + row.count + " avg=" + row.avg + " p95=" + row.p95 + " p99=" + row.p99 + " max=" + row.max + " last=" + row.last;
          }).concat(telemetryLine).join("\\n")
        : "perf: waiting\\n" + telemetryLine;
    };
    window.mcDebugStep("early script started");
