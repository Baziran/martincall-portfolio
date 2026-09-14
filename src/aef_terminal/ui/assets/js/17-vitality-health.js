    const VITALITY_QUOTE_WARN_RATIO = 0.55;
    const VITALITY_CANDLE_W = 2;
    const VITALITY_CANDLE_GAP = 1;
    const VITALITY_CANDLE_PITCH = VITALITY_CANDLE_W + VITALITY_CANDLE_GAP;
    const VITALITY_CANDLE_PAD = 2.5;
    const VITALITY_CANDLE_MIN_H = 2;
    const VITALITY_CANDLE_MAX_H = 10;
    const VITALITY_CANDLE_SPEED_ALIVE = 0.032;
    const VITALITY_CANDLE_SPEED_WARN = 0.072;
    const vitalityCandleRuntime = {
      frameId: 0,
      candles: [],
      lastState: "",
    };

    function randomVitalityCandleColor(warn = false) {
      const alpha = 0.78 + Math.random() * 0.22;
      const palette = warn ? ["gold", "green", "red"] : ["green", "red"];
      const pick = palette[Math.floor(Math.random() * palette.length)];
      if (typeof themeColor === "function") return themeColor(pick, alpha);
      if (pick === "red") return `rgba(214, 69, 80, ${alpha.toFixed(3)})`;
      if (pick === "gold") return `rgba(212, 175, 55, ${alpha.toFixed(3)})`;
      return `rgba(0, 166, 118, ${alpha.toFixed(3)})`;
    }

    function randomVitalityCandleHeight() {
      return VITALITY_CANDLE_MIN_H + Math.floor(Math.random() * (VITALITY_CANDLE_MAX_H - VITALITY_CANDLE_MIN_H + 1));
    }

    function createVitalityCandle(x, warn = false, height = randomVitalityCandleHeight()) {
      return { x, h: height, color: randomVitalityCandleColor(warn) };
    }

    function seedVitalityCandles(warn = false) {
      const candles = [];
      let x = VITALITY_CANDLE_PAD - VITALITY_CANDLE_PITCH * 4;
      const limit = 16 - VITALITY_CANDLE_PAD + VITALITY_CANDLE_PITCH * 2;
      while (x < limit) {
        candles.push(createVitalityCandle(x, warn));
        x += VITALITY_CANDLE_PITCH;
      }
      vitalityCandleRuntime.candles = candles;
    }

    function stopVitalityCandleLoop() {
      if (vitalityCandleRuntime.frameId) {
        window.cancelAnimationFrame(vitalityCandleRuntime.frameId);
        vitalityCandleRuntime.frameId = 0;
      }
    }

    function clearVitalityCandles(node) {
      const canvas = node?.querySelector(".vitality-candles");
      if (!canvas) return;
      const ctx = canvas.getContext("2d");
      if (ctx) ctx.clearRect(0, 0, canvas.width, canvas.height);
    }

    function vitalityVisualizerMode() {
      return normalizeHealthVisualizer(state.settings.healthVisualizer);
    }

    function vitalityHealthEnabled() {
      return vitalityVisualizerMode() !== "off";
    }

    function vitalityCandlesEnabled() {
      return vitalityHealthEnabled() && vitalityVisualizerMode() === "candles";
    }

    function drawVitalityCandlesFrame() {
      vitalityCandleRuntime.frameId = 0;
      const root = document.getElementById("vitality-health");
      if (!root || root.classList.contains("hidden") || !vitalityCandlesEnabled()) {
        stopVitalityCandleLoop();
        if (root && !vitalityCandlesEnabled()) clearVitalityCandles(root);
        return;
      }
      const healthState = ["alive", "warn"].find(name => root.classList.contains(name)) || "";
      if (!healthState || document.visibilityState !== "visible" || uiMotionReduced()) {
        stopVitalityCandleLoop();
        return;
      }
      const canvas = root.querySelector(".vitality-candles");
      if (!canvas) return;
      const ctx = canvas.getContext("2d");
      if (!ctx) return;
      if (vitalityCandleRuntime.lastState !== healthState) {
        vitalityCandleRuntime.lastState = healthState;
        seedVitalityCandles(healthState === "warn");
      }
      const warn = healthState === "warn";
      const speed = warn ? VITALITY_CANDLE_SPEED_WARN : VITALITY_CANDLE_SPEED_ALIVE;
      const candles = vitalityCandleRuntime.candles;
      for (let index = 0; index < candles.length; index += 1) {
        candles[index].x += speed;
      }
      const rightLimit = 16 - VITALITY_CANDLE_PAD + VITALITY_CANDLE_W;
      vitalityCandleRuntime.candles = candles.filter(item => item.x < rightLimit + VITALITY_CANDLE_PITCH);
      let leftEdge = vitalityCandleRuntime.candles.length
        ? Math.min(...vitalityCandleRuntime.candles.map(item => item.x))
        : VITALITY_CANDLE_PAD;
      while (leftEdge > VITALITY_CANDLE_PAD - VITALITY_CANDLE_PITCH) {
        leftEdge -= VITALITY_CANDLE_PITCH;
        vitalityCandleRuntime.candles.unshift(createVitalityCandle(leftEdge, warn));
      }
      const pad = VITALITY_CANDLE_PAD;
      const innerW = 16 - pad * 2;
      const innerH = 16 - pad * 2;
      const bottom = 16 - pad;
      ctx.clearRect(0, 0, 16, 16);
      ctx.save();
      ctx.beginPath();
      ctx.rect(pad, pad, innerW, innerH);
      ctx.clip();
      for (const candle of vitalityCandleRuntime.candles) {
        const x = Math.round(candle.x);
        const h = Math.max(VITALITY_CANDLE_MIN_H, Math.round(candle.h));
        ctx.fillStyle = candle.color || randomVitalityCandleColor(warn);
        ctx.fillRect(x, bottom - h, VITALITY_CANDLE_W, h);
      }
      ctx.restore();
      vitalityCandleRuntime.frameId = window.requestAnimationFrame(drawVitalityCandlesFrame);
    }

    function ensureVitalityCandleLoop() {
      if (!vitalityCandlesEnabled() || vitalityCandleRuntime.frameId || uiMotionReduced()) return;
      vitalityCandleRuntime.frameId = window.requestAnimationFrame(drawVitalityCandlesFrame);
    }

    function quoteStreamAgeMs() {
      const lastSeen = Number(state.transport.quote.lastStateAt || state.transport.quote.openedAt || 0);
      return lastSeen > 0 ? Date.now() - lastSeen : Number.POSITIVE_INFINITY;
    }

    function chartStreamAgeMs() {
      const lastSeen = Number(state.transport.chart.lastMessageAt || state.transport.chart.openedAt || 0);
      return lastSeen > 0 ? Date.now() - lastSeen : Number.POSITIVE_INFINITY;
    }

    function chartStreamStaleNow() {
      if (typeof chartStreamOpen === "function" && !chartStreamOpen()) return false;
      return chartStreamAgeMs() >= CHART_STREAM_STALE_MS;
    }

    function computeVitalityHealthState(snapshot = state.snapshot) {
      if (!vitalityHealthEnabled()) return { state: "hidden", title: "Health indicator disabled", lines: [] };
      if (state.serverSleeping) {
        return {
          state: "sleep",
          title: "Server sleeping",
          lines: [
            "IBKR refresh and live streams are paused.",
            state.systemHealth?.sleep?.reason ? `Reason: ${state.systemHealth.sleep.reason}` : "",
          ].filter(Boolean),
        };
      }
      if (!snapshot?.meta) {
        return {
          state: "boot",
          title: "Loading market data",
          lines: ["Waiting for the first market snapshot."],
        };
      }
      const meta = snapshot.meta || {};
      const quality = effectiveDataQuality(meta);
      const qualityStatus = String(quality.status || "").toLowerCase();
      const qualityCodes = new Set(qualityStatus.split("+").filter(Boolean));
      const effectiveWarning = marketSnapshotNotice(snapshot);
      const signalsBlocked = quality.signals_ok === false;
      const marketClosed = quality.market_closed === true;
      const sessionWarmup = quality.session_warmup === true;
      const sessionUnknown = quality.session_unknown === true;
      const freshness = meta.freshness && typeof meta.freshness === "object" ? meta.freshness : {};
      const barAge = Number(freshness.bar_age_seconds);
      const barThreshold = Number(freshness.stale_threshold_seconds);
      const rawBarStale = String(freshness.status || "").toLowerCase() === "stale"
        || (Number.isFinite(barAge) && Number.isFinite(barThreshold) && barAge > barThreshold);
      const barStale = !marketClosed && rawBarStale;
      const visible = document.visibilityState === "visible";
      const quoteTransportSupported = providerCapability(state.dataSource, "live_quote_stream")
        || providerCapability(state.dataSource, "live_quote_polling");
      const chartTransportSupported = providerCapability(state.dataSource, "chart_stream");
      const quoteTransportExpected = quoteTransportSupported && visible;
      const chartTransportExpected = chartTransportSupported && visible;
      const quoteAge = quoteTransportSupported
        ? quoteStreamAgeMs()
        : Number.POSITIVE_INFINITY;
      const quoteStale = quoteTransportExpected
        && typeof quoteStreamStale === "function"
        && quoteStreamStale();
      const quoteWarn = quoteTransportExpected
        && !quoteStale
        && Number.isFinite(quoteAge)
        && quoteAge > QUOTE_STREAM_STALE_MS * VITALITY_QUOTE_WARN_RATIO
        && quoteAge < QUOTE_STREAM_STALE_MS;
      const chartAge = chartTransportSupported
        ? chartStreamAgeMs()
        : Number.POSITIVE_INFINITY;
      const chartStale = chartTransportExpected && chartStreamStaleNow();
      const quoteConnected = quoteTransportSupported
        && typeof quoteStreamOpen === "function"
        && quoteStreamOpen();
      const chartConnected = chartTransportSupported
        && typeof chartStreamOpen === "function"
        && chartStreamOpen();
      const hadQuoteStream = quoteTransportSupported
        && Number(state.transport.quote.openedAt || state.transport.quote.lastMessageAt || 0) > 0;
      const hadChartStream = chartTransportSupported
        && Number(state.transport.chart.lastMessageAt || state.transport.chart.openedAt || 0) > 0;
      const chartReconnectPending = chartTransportSupported
        && Boolean(state.transport.chart.retryTimer);
      const qualityFailure = ["error", "bad", "fail", "failed"].some(token => qualityStatus.includes(token));
      const noData = quality.empty_history === true
        || qualityCodes.has("no_data")
        || qualityCodes.has("no_confirmed_bar");
      const openMarketStale = !marketClosed
        && !sessionWarmup
        && !sessionUnknown
        && (barStale || qualityCodes.has("stale"));
      const qualityCritical = qualityFailure || noData || openMarketStale;
      const quoteTransportCritical = quoteTransportExpected
        && (quoteStale || (hadQuoteStream && !quoteConnected));
      const chartTransportCritical = chartTransportExpected
        && (
          chartReconnectPending
          || (hadChartStream && (chartStale || !chartConnected))
        );
      const transportCritical = quoteTransportCritical || chartTransportCritical;
      const qualityWarning = String(quality.warning || effectiveWarning || "").trim();
      const lines = [
        `Data quality: ${qualityStatus ? qualityStatus.toUpperCase() : "OK"}`,
        signalsBlocked ? "Signals: blocked" : "Signals: allowed",
        Number.isFinite(barAge) ? `Bar age: ${barAge.toFixed(1)}s` : "",
        Number.isFinite(barThreshold) ? `Bar threshold: ${barThreshold.toFixed(1)}s` : "",
        quoteTransportSupported
          ? quoteAge < Number.POSITIVE_INFINITY
            ? `Quote stream age: ${Math.round(quoteAge / 1000)}s`
            : "Quote stream age: n/a"
          : "",
        chartTransportSupported
          ? chartAge < Number.POSITIVE_INFINITY
            ? `Chart stream age: ${Math.round(chartAge / 1000)}s`
            : "Chart stream age: n/a"
          : "",
        quoteTransportSupported
          ? quoteConnected ? "Quote transport: connected" : "Quote transport: disconnected"
          : "",
        chartTransportSupported
          ? chartConnected ? "Chart transport: connected" : "Chart transport: disconnected"
          : "",
        qualityWarning,
      ].filter(Boolean);
      if (qualityCritical || transportCritical) {
        return {
          state: "dead",
          title: "Critical health issue",
          lines,
          error: transportCritical
            ? "Live transport is stale or disconnected."
            : qualityWarning ? "" : "Market data quality is critical.",
        };
      }
      const qualityWarn = qualityStatus && qualityStatus !== "ok" && !qualityCritical;
      const quoteTransportPending = quoteTransportExpected && !hadQuoteStream && !quoteConnected;
      const chartTransportPending = chartTransportExpected && !hadChartStream && !chartConnected;
      const transportPending = quoteTransportPending || chartTransportPending;
      if (barStale || quoteWarn || qualityWarn || transportPending) {
        return {
          state: "warn",
          title: "Health warning",
          lines,
          error: qualityWarning ? "" : barStale ? "Latest bar is stale." : "",
        };
      }
      return {
        state: "alive",
        title: "System healthy",
        lines,
      };
    }

    function renderVitalityHealth(snapshot = state.snapshot) {
      const node = document.getElementById("vitality-health");
      if (!node) return;
      const mode = vitalityVisualizerMode();
      if (!vitalityHealthEnabled()) {
        node.className = "vitality-health hidden";
        stopVitalityCandleLoop();
        clearVitalityCandles(node);
        return;
      }
      const model = computeVitalityHealthState(snapshot);
      const nextClass = `vitality-health ${model.state} mode-${mode}`;
      if (node.className !== nextClass) {
        node.className = nextClass;
        vitalityCandleRuntime.lastState = "";
      }
      node.title = model.title || "System health";
      node.setAttribute("aria-label", model.title || "System health");
      if (typeof bindStatusTooltip === "function") {
        const vitalityTone = model.state === "alive"
          ? "ok"
          : model.state === "warn" || model.state === "sleep" || model.state === "boot"
            ? "warn"
            : "bad";
        bindStatusTooltip(node, {
          title: model.title || "System health",
          lines: (model.lines || []).map(text => ({ text, tone: vitalityTone })),
          error: model.error || "",
        });
      }
      if (mode === "candles" && (model.state === "alive" || model.state === "warn")) ensureVitalityCandleLoop();
      else {
        stopVitalityCandleLoop();
        if (mode !== "candles") clearVitalityCandles(node);
      }
    }

    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible" && vitalityCandlesEnabled()) ensureVitalityCandleLoop();
      else stopVitalityCandleLoop();
    });
