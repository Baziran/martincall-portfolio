    let observedPriceChartWidth = 0;
    let appViewportResizeFrame = 0;

    function syncAppViewportHeight() {
      const visualHeight = Number(window.visualViewport?.height);
      const layoutHeight = Number(window.innerHeight);
      const height = Math.round(
        Number.isFinite(visualHeight) && visualHeight > 0 ? visualHeight : layoutHeight,
      );
      if (!Number.isFinite(height) || height <= 0) return;
      const value = `${height}px`;
      if (document.documentElement.style.getPropertyValue("--app-viewport-height") === value) return;
      document.documentElement.style.setProperty("--app-viewport-height", value);
    }

    function scheduleAppViewportHeightSync() {
      if (appViewportResizeFrame) return;
      appViewportResizeFrame = requestAnimationFrame(() => {
        appViewportResizeFrame = 0;
        syncAppViewportHeight();
        if (state.snapshot) renderCharts(state.snapshot);
      });
    }

    function setupAppViewportHeight() {
      syncAppViewportHeight();
      window.visualViewport?.addEventListener("resize", scheduleAppViewportHeightSync);
      window.visualViewport?.addEventListener("scroll", scheduleAppViewportHeightSync);
    }

    let workspaceDockLayoutFrame = 0;

    function workspaceDockRequestedWidth(value) {
      return Math.round(clamp(Number(value) || 300, 220, 640));
    }

    function applyWorkspaceDockPresentation() {
      layout.workspaceDockWidth = workspaceDockRequestedWidth(layout.workspaceDockWidth);
      document.documentElement.style.setProperty("--workspace-dock-open-width", `${layout.workspaceDockWidth}px`);
      document.body.classList.toggle("workspace-dock-left", layout.workspaceDockSide === "left");
      const widthInput = document.getElementById("workspace-dock-width");
      if (widthInput) widthInput.value = String(layout.workspaceDockWidth);
      const widthValue = document.getElementById("workspace-dock-width-value");
      if (widthValue) widthValue.textContent = `${layout.workspaceDockWidth} px`;
      const sideInput = document.getElementById("workspace-dock-side");
      if (sideInput) sideInput.value = layout.workspaceDockSide;
      const resizer = document.getElementById("workspace-dock-resizer");
      resizer?.setAttribute("aria-valuenow", String(layout.workspaceDockWidth));
      resizer?.setAttribute("aria-valuetext", `${layout.workspaceDockWidth} px`);
      if (typeof syncGexSidebarHost === "function") syncGexSidebarHost();
    }

    function refreshWorkspaceDockLayout() {
      if (workspaceDockLayoutFrame) return;
      workspaceDockLayoutFrame = requestAnimationFrame(() => {
        workspaceDockLayoutFrame = 0;
        applyLayout();
        chartElementSizeCache.clear();
        if (state.snapshot) renderCharts(state.snapshot, {
          force: true,
          overlayImmediate: true,
          reason: "workspace dock layout",
        });
      });
    }

    function applyLayout() {
      const requestedGexSidebarWidth = typeof gexSidebarRequestedWidth === "function"
        ? gexSidebarRequestedWidth(layout.gexSidebarWidth)
        : Math.round(clamp(Number(layout.gexSidebarWidth) || 248, 154, 420));
      layout.gexSidebarWidth = requestedGexSidebarWidth;
      document.documentElement.style.setProperty("--side-width", `${layout.sideWidth}px`);
      document.documentElement.style.setProperty("--volume-height", `${layout.volumeHeight}px`);
      applyWorkspaceDockPresentation();
      const priceFrameWidth = Math.max(0, Number(observedPriceChartWidth) || 0);
      const gexSidebarGeometry = (
        priceFrameWidth > 0
        && typeof gexProfileGutterGeometry === "function"
      )
        ? gexProfileGutterGeometry(
            { left: CHART_LEFT_PAD, right: PRICE_AXIS_WIDTH },
            priceFrameWidth,
          )
        : {
            effectiveWidth: requestedGexSidebarWidth,
            panelRight: 2 + requestedGexSidebarWidth,
            requestedWidth: requestedGexSidebarWidth,
          };
      if (typeof applyGexSidebarGeometryToDom === "function") {
        applyGexSidebarGeometryToDom(gexSidebarGeometry);
      } else {
        const rootStyle = document.documentElement.style;
        const requestedCssWidth = `${requestedGexSidebarWidth}px`;
        if (rootStyle.getPropertyValue("--gex-sidebar-requested-width") !== requestedCssWidth) {
          rootStyle.setProperty("--gex-sidebar-requested-width", requestedCssWidth);
        }
        const effectiveCssWidth = `${Number(gexSidebarGeometry.effectiveWidth) || 0}px`;
        if (rootStyle.getPropertyValue("--gex-sidebar-effective-width") !== effectiveCssWidth) {
          rootStyle.setProperty("--gex-sidebar-effective-width", effectiveCssWidth);
        }
        const panelRightCss = `${Number(gexSidebarGeometry.panelRight) || 0}px`;
        if (rootStyle.getPropertyValue("--gex-sidebar-panel-right") !== panelRightCss) {
          rootStyle.setProperty("--gex-sidebar-panel-right", panelRightCss);
        }
        const widthInput = document.getElementById("gex-sidebar-width");
        if (widthInput && widthInput.value !== String(requestedGexSidebarWidth)) {
          widthInput.value = String(requestedGexSidebarWidth);
        }
      }
      document.getElementById("price-height-label").textContent = "auto";
      document.getElementById("volume-height-label").textContent = `${Math.round(Number(layout.volumeHeight) || 0)} px`;
    }

    let chartInteractionQualityUntil = 0;
    let chartInteractionQualityTimer = 0;

    function chartInteractionQualityActive() {
      return Date.now() < Number(chartInteractionQualityUntil || 0);
    }

    function chartCanvasPixelRatio(id) {
      const ratio = window.devicePixelRatio || 1;
      const chartLayer = /^(price-|volume-chart|volume-interaction)/.test(String(id || ""));
      return chartLayer && chartInteractionQualityActive() ? Math.min(ratio, 1.25) : ratio;
    }

    function beginChartInteractionQuality(durationMs = 180) {
      chartInteractionQualityUntil = Math.max(Number(chartInteractionQualityUntil || 0), Date.now() + Math.max(Number(durationMs) || 0, 0));
      document.body.classList.add("chart-interacting");
      if (chartInteractionQualityTimer) clearTimeout(chartInteractionQualityTimer);
      chartInteractionQualityTimer = setTimeout(() => {
        chartInteractionQualityTimer = 0;
        if (chartInteractionQualityActive()) {
          beginChartInteractionQuality(40);
          return;
        }
        document.body.classList.remove("chart-interacting");
        if (state.snapshot) renderCharts(state.snapshot, { overlayImmediate: true });
      }, Math.max(Number(durationMs) || 0, 0) + 24);
    }

    function endChartInteractionQuality() {
      chartInteractionQualityUntil = 0;
      if (chartInteractionQualityTimer) {
        clearTimeout(chartInteractionQualityTimer);
        chartInteractionQualityTimer = 0;
      }
      document.body.classList.remove("chart-interacting");
      if (state.snapshot) renderCharts(state.snapshot, { overlayImmediate: true });
    }

    const canvasContextCache = new Map();
    const opaqueCanvasIds = new Set(["price-chart", "volume-chart"]);

    function canvasContext(id) {
      const canvas = document.getElementById(id);
      const rect = canvas.getBoundingClientRect();
      if (id === "price-chart" && Number(rect.width) > 0) {
        observedPriceChartWidth = Number(rect.width);
      }
      const ratio = chartCanvasPixelRatio(id);
      const targetWidth = Math.floor(rect.width * ratio);
      const targetHeight = Math.floor(rect.height * ratio);
      if (canvas.width !== targetWidth) canvas.width = targetWidth;
      if (canvas.height !== targetHeight) canvas.height = targetHeight;
      let ctx = canvasContextCache.get(id);
      if (!ctx) {
        ctx = canvas.getContext("2d", { alpha: !opaqueCanvasIds.has(id) });
        canvasContextCache.set(id, ctx);
      }
      ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
      return { canvas, ctx, w: rect.width, h: rect.height };
    }

    let pendingChartSnapshot = null;
    let pendingChartOptions = {};
    let chartRenderFrame = 0;
    let chartRenderQueuePerf = null;
    let pendingObjectLayerReason = "";
    let pendingObjectLayerOptions = {};
    let objectLayerRedrawFrame = 0;
    let lastChartRenderKey = "";
    const chartGeometryCache = new Map();

    function chartStableJson(value, depth = 0) {
      if (value === null || value === undefined) return "";
      if (depth > 4) return String(value);
      if (typeof value !== "object") return String(value);
      if (Array.isArray(value)) return `[${value.slice(0, 120).map(item => chartStableJson(item, depth + 1)).join(",")}]`;
      return `{${Object.keys(value).sort().map(key => {
        const next = value[key];
        if (typeof next === "function") return "";
        return `${key}:${chartStableJson(next, depth + 1)}`;
      }).filter(Boolean).join(",")}}`;
    }

    const chartRenderVersions = {
      bars: 0,
      settings: 0,
      indicators: 0,
      objects: 0,
    };
    const chartIndicatorRenderKeyCache = new WeakMap();
    const chartElementSizeCache = new Map();
    let chartSizeObserver = null;

    function bumpChartRenderVersion(area) {
      if (area === "bars") chartRenderVersions.bars += 1;
      else if (area === "settings") chartRenderVersions.settings += 1;
      else if (area === "indicators") chartRenderVersions.indicators += 1;
      else if (area === "objects") chartRenderVersions.objects += 1;
    }

    function touchChartBarsVersion() {
      bumpChartRenderVersion("bars");
    }

    function touchChartUserObjectsVersion() {
      bumpChartRenderVersion("objects");
    }

    function chartElementSizeKey(id) {
      const cached = chartElementSizeCache.get(id);
      if (cached) return cached;
      const element = document.getElementById(id);
      if (!element) return "";
      const rect = element.getBoundingClientRect();
      return [
        Math.round(rect.width * 10) / 10,
        Math.round(rect.height * 10) / 10,
        element.width || 0,
        element.height || 0,
      ].join("x");
    }

    function ensureChartSizeObserver() {
      if (chartSizeObserver) return;
      if (typeof ResizeObserver !== "function") return;
      chartSizeObserver = new ResizeObserver(entries => {
        for (const entry of entries) {
          const id = entry.target?.id;
          if (!id) continue;
          const rect = entry.contentRect || entry.target.getBoundingClientRect();
          if (id === "price-chart" && Number(rect.width) > 0) {
            observedPriceChartWidth = Number(rect.width);
          }
          chartElementSizeCache.set(id, [
            Math.round(rect.width * 10) / 10,
            Math.round(rect.height * 10) / 10,
            entry.target.width || 0,
            entry.target.height || 0,
          ].join("x"));
          if (
            id === "price-chart"
            && typeof gexProfileGutterGeometry === "function"
            && typeof applyGexSidebarGeometryToDom === "function"
          ) {
            applyGexSidebarGeometryToDom(gexProfileGutterGeometry(
              { left: CHART_LEFT_PAD, right: PRICE_AXIS_WIDTH },
              Number(rect.width) || 0,
            ));
          }
        }
      });
      for (const id of ["price-chart", "volume-chart"]) {
        const element = document.getElementById(id);
        if (element) chartSizeObserver.observe(element);
      }
    }

    function chartBarRenderKey(bar) {
      if (!bar) return "";
      return [
        bar.ts || bar.time || "",
        bar.open,
        bar.high,
        bar.low,
        bar.close,
        bar.volume,
        bar.closed,
        bar.state || "",
        bar.bar_slot ?? "",
        bar.bar_slot_authoritative === true,
        bar.bar_slot_schedule_state || "",
      ].join(":");
    }

    function chartLiveQuoteRenderKey() {
      const quote = typeof currentChartQuoteDisplay === "function" ? currentChartQuoteDisplay(15000) : null;
      if (!quote) return "";
      return [
        quote.price,
        quote.bid,
        quote.ask,
        quote.last,
        quote.ts,
        quote.displayOnly ? "display_only" : "live",
      ].join(":");
    }

    function chartUserObjectsRenderKey() {
      return [
        chartRenderVersions.objects,
        state.drawing?.selectedId || "",
        state.alerts?.selectedId || "",
        state.optionTargets?.selectedId || "",
      ].join("|");
    }

    function chartHashParts(parts = []) {
      let hash = 2166136261;
      let count = 0;
      parts.forEach(value => {
        const text = String(value ?? "");
        if (!text) return;
        count += 1;
        for (let index = 0; index < text.length; index += 1) {
          hash ^= text.charCodeAt(index);
          hash = Math.imul(hash, 16777619) >>> 0;
        }
      });
      return `${count}:${hash.toString(36)}`;
    }

    function chartIndicatorVisualItemKey(item = null) {
      if (!item || typeof item !== "object") return "";
      return [
        item.ts || "",
        item.index ?? "",
        item.id || "",
        item.kind || "",
        item.type || "",
        item.source || "",
        item.code || "",
        item.base_code || "",
        item.event || "",
        item.event_type || "",
        item.state || "",
        item.action || "",
        item.raw_action || "",
        item.direction || "",
        item.label || "",
        item.text || "",
        item.price ?? "",
        item.level ?? "",
        item.upper ?? "",
        item.lower ?? "",
        item.mid ?? "",
        item.entry ?? "",
        item.stop ?? "",
        item.target ?? "",
        item.score ?? "",
        item.rvol ?? "",
        item.signal_overlay === true ? "signal-overlay" : "",
        item.important ? "important" : "",
        item.fuel ? "fuel" : "",
        item.terminal_climax ? "climax" : "",
      ].join(":");
    }

    function chartExtensionRenderKey(snapshot) {
      const providers = typeof window !== "undefined" && Array.isArray(window.chartRenderKeyProviders)
        ? window.chartRenderKeyProviders
        : [];
      return providers
        .map(provider => {
          if (!provider || typeof provider.renderKey !== "function") return "";
          const id = String(provider.id || "extension");
          try {
            return `${id}:${provider.renderKey(snapshot) || ""}`;
          } catch (error) {
            console.warn("chart render key provider failed", id, error);
            return `${id}:error`;
          }
        })
        .filter(Boolean)
        .join("|");
    }

    function chartIndicatorCollectionKey(items, limit = 160) {
      if (!Array.isArray(items) || !items.length) return "0";
      const rows = items.length > limit ? items.slice(-limit) : items;
      return `${items.length}:${chartHashParts(rows.map(chartIndicatorVisualItemKey))}`;
    }

    function chartIndicatorSeriesKey(indicator, limit = 12) {
      const series = Array.isArray(indicator?.series) ? indicator.series : [];
      if (!series.length) return "0";
      const rows = series.length > limit ? [series[0], ...series.slice(-(limit - 1))] : series;
      return `${series.length}:${chartHashParts(rows.map(chartIndicatorVisualItemKey))}`;
    }

    function chartIndicatorRenderKey(snapshot) {
      const indicators = snapshot?.indicators || {};
      if (!indicators || typeof indicators !== "object") return "";
      const cached = chartIndicatorRenderKeyCache.get(indicators);
      if (cached !== undefined) return cached;
      const key = Object.keys(indicators).sort().map(id => {
        const indicator = indicators[id] || {};
        const status = indicator.status || {};
        const statusKey = [
          status.state_code || "",
          status.health || "",
          status.reason_code || "",
          status.trigger_event?.code || "",
          status.trigger_event?.reason_code || "",
          status.trigger_event?.exit_reason || "",
          status.last_error || "",
          status.mode || "",
          status.bar_count ?? "",
          status.analysis_ts || "",
          status.calculated_at || "",
          status.event_count ?? "",
          status.series_count ?? "",
          status.last_event_ts || "",
          status.latest_state || "",
          status.params_hash || "",
          status.last_error || "",
        ].join(":");
        return [
          id,
          statusKey,
          chartIndicatorVisualItemKey(indicator.latest),
          chartIndicatorSeriesKey(indicator),
          chartIndicatorCollectionKey(indicator.events, 120),
          chartIndicatorCollectionKey(indicator.overlays, 160),
          chartIndicatorCollectionKey(indicator.levels, 80),
          chartIndicatorCollectionKey(indicator.signals, 80),
          chartIndicatorCollectionKey(indicator.pivots, 80),
          chartIndicatorCollectionKey(indicator.paths, 80),
          chartIndicatorCollectionKey(indicator.candidates, 80),
        ].join("^");
      }).join("||");
      chartIndicatorRenderKeyCache.set(indicators, key);
      return key;
    }

    function chartRenderCacheKey(snapshot, options = {}) {
      const bars = snapshot?.bars || [];
      const first = bars[0];
      const last = bars[bars.length - 1];
      return JSON.stringify([
        exactIdentityText(snapshot?.meta?.instrument_id ?? state.instrumentId),
        exactIdentityText(snapshot?.meta?.route_fingerprint ?? instrumentRouteFingerprint()),
        state.symbol,
        state.dataSource,
        state.timeframe,
        state.range,
        bars.length,
        chartBarRenderKey(first),
        chartBarRenderKey(last),
        snapshot?.meta?.symbol || "",
        snapshot?.meta?.timeframe || "",
        snapshot?.meta?.source || "",
        snapshot?.meta?.warning || "",
        snapshot?.meta?.analysis_updated_at || "",
        snapshot?.chart_guides?.meta?.generation || "",
        snapshot?.meta?.quote_ts || "",
        chartLiveQuoteRenderKey(),
        Math.round(Number(view.offset) || 0),
        Math.round(Number(view.barsVisible) || 0),
        Number(view.priceZoom || 0).toFixed(4),
        Number(view.priceShift || 0).toFixed(4),
        Number(view.rightGapBars || 0).toFixed(4),
        Number(view.smoothOffsetBars || 0).toFixed(4),
        Number(layout.gexSidebarWidth || 0),
        layout.gexSidebarPlacement,
        layout.workspaceDockSide,
        layout.workspaceDockWidth,
        Number(layout.sideWidth || 0),
        Number(layout.volumeHeight || 0),
        chartElementSizeKey("price-chart"),
        chartElementSizeKey("volume-chart"),
        options.overlayImmediate ? "overlay-now" : "overlay-raf",
        gexFocusMode() ? "gex-focus" : "normal",
        document.body?.className || "",
        document.documentElement?.className || "",
        chartRenderVersions.bars,
        chartRenderVersions.settings,
        chartRenderVersions.indicators,
        chartIndicatorRenderKey(snapshot),
        chartExtensionRenderKey(snapshot),
        chartUserObjectsRenderKey(),
      ]);
    }

    function chartRenderGuardBypassed(options = {}) {
      return Boolean(
        options.force
        || options.skipRenderGuard
        || chartInteractionQualityActive()
        || state.requests.market.loading
        || state.requests.history.loading
        || state.drawing?.draft
        || state.drawing?.drag
        || state.alerts?.drag
        || state.optionTargets?.draggingId
        || state.paperTrading?.dragOrder
      );
    }

    function chartRenderGuardState(snapshot, options = {}) {
      if (chartRenderGuardBypassed(options)) return { skip: false, key: "" };
      const key = chartRenderCacheKey(snapshot, options);
      return { skip: Boolean(key && key === lastChartRenderKey), key };
    }

    function resetChartRenderScope() {
      if (chartRenderFrame) {
        cancelAnimationFrame(chartRenderFrame);
        chartRenderFrame = 0;
      }
      if (objectLayerRedrawFrame) {
        cancelAnimationFrame(objectLayerRedrawFrame);
        objectLayerRedrawFrame = 0;
      }
      pendingChartSnapshot = null;
      pendingChartOptions = {};
      pendingObjectLayerReason = "";
      pendingObjectLayerOptions = {};
      lastChartRenderKey = "";
      chartGeometryCache.clear();
      if (chartRenderQueuePerf && window.mcPerfEnd) {
        window.mcPerfEnd(chartRenderQueuePerf, "scope reset", 20);
      }
      chartRenderQueuePerf = null;
      if (typeof resetDeferredMarketRenders === "function") {
        resetDeferredMarketRenders();
      }
      if (typeof resetDeferredChartLayerRenders === "function") {
        resetDeferredChartLayerRenders();
      }
    }

    function renderCharts(snapshot, options = {}) {
      if (window.mcTelemetryInc) window.mcTelemetryInc("render.requested");
      if (options.reason && window.mcTelemetrySet) window.mcTelemetrySet("render_reason", String(options.reason));
      if (options.reason && typeof debugStep === "function") debugStep("redraw requested", String(options.reason));
      pendingChartSnapshot = snapshot;
      pendingChartOptions = { ...pendingChartOptions, ...options };
      if (!chartRenderQueuePerf && window.mcPerfStart) {
        chartRenderQueuePerf = window.mcPerfStart("chartRenderQueue");
      }
      if (options.immediate) {
        if (window.mcTelemetryInc) window.mcTelemetryInc("render.immediate");
        if (chartRenderFrame) {
          cancelAnimationFrame(chartRenderFrame);
          chartRenderFrame = 0;
        }
        const target = pendingChartSnapshot;
        const targetOptions = pendingChartOptions;
        pendingChartSnapshot = null;
        pendingChartOptions = {};
        if (window.mcPerfEnd) window.mcPerfEnd(chartRenderQueuePerf, "immediate", 20);
        chartRenderQueuePerf = null;
        renderChartsNow(target, targetOptions);
        return;
      }
      if (chartRenderFrame) {
        if (window.mcTelemetryInc) window.mcTelemetryInc("render.coalesced");
        return;
      }
      chartRenderFrame = requestAnimationFrame(() => {
        chartRenderFrame = 0;
        const target = pendingChartSnapshot;
        const targetOptions = pendingChartOptions;
        pendingChartSnapshot = null;
        pendingChartOptions = {};
        renderChartsNow(target, targetOptions);
      });
    }

    function requestObjectLayerRedraw(reason = "objects", options = {}) {
      if (!state.snapshot) return false;
      if (window.mcTelemetryInc) window.mcTelemetryInc("render.objects.requested");
      if (reason && window.mcTelemetrySet) window.mcTelemetrySet("render_reason", String(reason));
      if (reason && typeof debugStep === "function") debugStep("redraw requested", String(reason));
      pendingObjectLayerReason = String(reason || pendingObjectLayerReason || "objects");
      pendingObjectLayerOptions = { ...pendingObjectLayerOptions, ...options };
      const draw = () => {
        objectLayerRedrawFrame = 0;
        const targetReason = pendingObjectLayerReason || "objects";
        const targetOptions = pendingObjectLayerOptions;
        pendingObjectLayerReason = "";
        pendingObjectLayerOptions = {};
        if (typeof renderPriceObjects !== "function") {
          const error = new Error("Object-layer renderer is unavailable");
          if (window.mcTelemetryInc) window.mcTelemetryInc("render.objects.error");
          if (typeof showRuntimeError === "function") showRuntimeError(error, "objectLayerRender");
          else console.error("object layer render failed", error);
          if (typeof debugStep === "function") debugStep("object redraw failed", String(targetReason));
          return;
        }
        const rendered = renderPriceObjects(state.snapshot, targetOptions) !== false;
        if (rendered && window.mcTelemetryInc) window.mcTelemetryInc("render.objects.rendered");
        if (targetReason && typeof debugStep === "function") {
          debugStep(rendered ? "redraw committed" : "redraw skipped", String(targetReason));
        }
      };
      if (options.immediate) {
        if (window.mcTelemetryInc) window.mcTelemetryInc("render.objects.immediate");
        if (objectLayerRedrawFrame) {
          cancelAnimationFrame(objectLayerRedrawFrame);
          objectLayerRedrawFrame = 0;
        }
        draw();
      } else if (!objectLayerRedrawFrame) {
        objectLayerRedrawFrame = requestAnimationFrame(draw);
      } else if (window.mcTelemetryInc) {
        window.mcTelemetryInc("render.objects.coalesced");
      }
      return true;
    }

    function renderChartsNow(snapshot, options = {}) {
      if (window.mcPerfEnd) window.mcPerfEnd(chartRenderQueuePerf, "rAF", 34);
      chartRenderQueuePerf = null;
      ensureChartSizeObserver();
      const guard = chartRenderGuardState(snapshot, options);
      if (guard.skip) {
        if (window.mcTelemetryInc) window.mcTelemetryInc("render.skipped");
        return;
      }
      const perfToken = window.mcPerfStart ? window.mcPerfStart("renderCharts") : null;
      const paintPerf = window.mcPerfStart ? window.mcPerfStart("chartRenderToPaint") : null;
      let rendered = false;
      try {
        chartGeometryCache.clear();
        if (!snapshot?.bars?.length) {
          updateMarketContext(snapshot || {});
          renderEmptyCanvas("price-chart", "NO HISTORY", snapshot?.meta?.warning || "Provider returned no bars for this request.");
          renderEmptyCanvas("volume-chart", "NO VOLUME HISTORY", "Try another timeframe or symbol.");
          rendered = true;
          return;
        }
        updateMarketContext(snapshot);
        renderPriceChart(snapshot, options);
        if (gexFocusMode()) {
          resetCanvasTooltips("volume");
          rendered = true;
          return;
        }
        renderVolumeChart(snapshot);
        rendered = true;
      } catch (error) {
        if (typeof showRuntimeError === "function") showRuntimeError(error, "renderCharts");
        else console.error("renderCharts failed", error);
      } finally {
        if (rendered && typeof updateAccessibleChartLoadStatus === "function") {
          updateAccessibleChartLoadStatus(snapshot);
        }
        if (rendered && guard.key) lastChartRenderKey = guard.key;
        if (rendered && options.reason && typeof debugStep === "function") debugStep("redraw committed", String(options.reason));
        if (rendered && window.mcTelemetryInc) window.mcTelemetryInc("render.rendered");
        if (rendered && state.pendingLiveCandlePaintAt) {
          const previewTs = state.pendingLiveCandlePaintAt;
          state.pendingLiveCandlePaintAt = "";
          requestAnimationFrame(() => {
            const latencyMs = Date.now() - Date.parse(previewTs || "");
            if (!Number.isFinite(latencyMs) || latencyMs < 0) return;
            if (window.mcRecordBackendTiming) {
              window.mcRecordBackendTiming(
                "liveCandleGatewayToPaint",
                latencyMs,
                `${state.symbol} ${state.timeframe}`,
              );
            }
            if (window.mcTelemetrySet) window.mcTelemetrySet("live_candle_gateway_to_paint_ms", Math.round(latencyMs));
            if (window.mcTelemetryMax) window.mcTelemetryMax("live_candle_gateway_to_paint_max_ms", latencyMs);
          });
        }
        if (window.mcPerfEnd) window.mcPerfEnd(perfToken, `${snapshot?.bars?.length || 0} bars`, 48);
        if (rendered && paintPerf && window.mcPerfEnd) {
          requestAnimationFrame(() => {
            window.mcPerfEnd(
              paintPerf,
              `${snapshot?.bars?.length || 0} bars`,
              80,
            );
          });
        }
      }
    }

    const CHART_VIEW_DAY_MS = 86_400_000;
    const INITIAL_CHART_VIEW_DURATION_MS = 3 * CHART_VIEW_DAY_MS;
    const INITIAL_CHART_VIEW_MAX_BARS = 600;

    function resetTimeViewportToLatest(snapshot, durationMs = INITIAL_CHART_VIEW_DURATION_MS) {
      const bars = Array.isArray(snapshot?.bars) ? snapshot.bars : [];
      if (!bars.length) return false;
      let count = bars.length;
      if (Number.isFinite(durationMs) && durationMs > 0) {
        const latestTs = Date.parse(bars[bars.length - 1]?.ts || "");
        if (Number.isFinite(latestTs)) {
          const cutoff = latestTs - durationMs;
          let start = bars.length - 1;
          while (start > 0) {
            const previousTs = Date.parse(bars[start - 1]?.ts || "");
            if (!Number.isFinite(previousTs) || previousTs < cutoff) break;
            start -= 1;
          }
          count = bars.length - start;
          count = Math.min(count, INITIAL_CHART_VIEW_MAX_BARS);
        }
      }
      view.barsVisible = clamp(count, 24, MAX_BARS_VISIBLE);
      view.offset = 0;
      view.historyPullOffsetBars = 0;
      view.rightGapBars = DEFAULT_RIGHT_GAP_BARS;
      view.rightGapManual = false;
      view.followLatest = true;
      view.smoothOffsetBars = 0;
      return true;
    }

    function visibleBars(snapshot) {
      const allBars = snapshot?.bars || [];
      if (!allBars.length) {
        return { bars: [], start: 0, end: 0, allBars };
      }
      const requestedCount = clamp(Math.round(Number(view.barsVisible) || DEFAULT_BARS_VISIBLE), 24, MAX_BARS_VISIBLE);
      const count = Math.min(requestedCount, allBars.length);
      view.barsVisible = requestedCount;
      const maxOffset = Math.max(allBars.length - count, 0);
      view.offset = clamp(Math.round(view.offset), 0, maxOffset);
      const end = allBars.length - view.offset;
      const start = Math.max(0, end - count);
      return { bars: allBars.slice(start, end), start, end, allBars };
    }

    function maxOffsetFor(snapshot) {
      const allBars = snapshot?.bars || [];
      if (!allBars.length) return 0;
      const requestedCount = clamp(Math.round(Number(view.barsVisible) || DEFAULT_BARS_VISIBLE), 24, MAX_BARS_VISIBLE);
      const count = Math.min(requestedCount, allBars.length);
      return Math.max(allBars.length - count, 0);
    }

    function smoothOffsetBars() {
      const value = Number(view.smoothOffsetBars) || 0;
      return clamp(value, -0.499, 0.499);
    }

    function authoritativeVisualBarSlot(bar) {
      if (
        !bar
        || barIsGapPlaceholder(bar)
        || bar.bar_slot_authoritative !== true
      ) return null;
      const value = Number(bar.bar_slot);
      return Number.isSafeInteger(value) ? value : null;
    }

    function chartVisualAxis(bars, slotStep = intervalMinutesFromState()) {
      const rows = Array.isArray(bars) ? bars : [];
      const step = Math.max(Math.round(Number(slotStep) || 0), 1);
      const positions = new Array(rows.length);
      const gaps = [];
      let position = 0;
      for (let index = 0; index < rows.length; index += 1) {
        if (index > 0) {
          let visualUnits = 1;
          const previousSlot = authoritativeVisualBarSlot(rows[index - 1]);
          const currentSlot = authoritativeVisualBarSlot(rows[index]);
          const slotDelta = currentSlot !== null && previousSlot !== null
            ? currentSlot - previousSlot
            : 0;
          if (slotDelta > step && slotDelta % step === 0) {
            visualUnits = slotDelta / step;
          }
          position += visualUnits;
          if (visualUnits > 1) {
            gaps.push({
              beforeIndex: index - 1,
              afterIndex: index,
              missingSlots: visualUnits - 1,
              fromTs: String(rows[index - 1]?.ts || ""),
              toTs: String(rows[index]?.ts || ""),
              startPosition: positions[index - 1] + 0.5,
              endPosition: position - 0.5,
            });
          }
        }
        positions[index] = position;
      }
      return {
        positions,
        gaps,
        span: rows.length ? positions[positions.length - 1] - positions[0] + 1 : 0,
      };
    }

    function chartVisualSpan(bars, slotStep = intervalMinutesFromState()) {
      return chartVisualAxis(bars, slotStep).span;
    }

    function chartXFromVisualAxis(pad, xStep, visualAxis) {
      const shift = smoothOffsetBars() * Number(xStep || 0);
      const positions = Array.isArray(visualAxis?.positions) ? visualAxis.positions : [];
      return index => {
        let visualPosition = Number(index);
        if (positions.length && Number.isFinite(visualPosition)) {
          const barIndex = Math.round(visualPosition);
          if (barIndex >= 0 && barIndex < positions.length) {
            visualPosition = positions[barIndex];
          } else if (barIndex < 0) {
            visualPosition = positions[0] + barIndex;
          } else {
            visualPosition = positions[positions.length - 1] + barIndex - positions.length + 1;
          }
        }
        return pad.left + visualPosition * xStep + xStep * 0.5 + shift;
      };
    }

    function chartX(pad, xStep, bars = null) {
      return chartXFromVisualAxis(pad, xStep, chartVisualAxis(bars));
    }

    function chartRenderDensityMode(xStep) {
      const step = Math.max(Number(xStep) || 0, 0);
      if (step >= CHART_CANDLE_DETAIL_MIN_STEP_PX) return "detail";
      if (step >= CHART_CANDLE_OVERVIEW_MAX_STEP_PX) return "thin";
      return "overview";
    }

    function newChartRenderColumn(bar, sourceIndex, centerX, pixelColumn, segment) {
      const volume = Number(bar?.volume);
      return {
        sourceStart: sourceIndex,
        sourceEnd: sourceIndex + 1,
        sourceCount: 1,
        firstSourceTs: String(bar?.ts || ""),
        lastSourceTs: String(bar?.ts || ""),
        firstSourceX: centerX,
        lastSourceX: centerX,
        centerX,
        pixelColumn,
        segment,
        renderOpen: Number(bar?.open),
        renderHigh: Number(bar?.high),
        renderLow: Number(bar?.low),
        renderClose: Number(bar?.close),
        renderVolume: Number.isFinite(volume) ? Math.max(volume, 0) : 0,
        renderVolumeKnown: Number.isFinite(volume),
        highSourceIndex: sourceIndex,
        lowSourceIndex: sourceIndex,
        provisional: bar?.closed === false || bar?.authoritative === false,
      };
    }

    function mergeChartRenderColumn(column, bar, sourceIndex, centerX) {
      const high = Number(bar?.high);
      const low = Number(bar?.low);
      const volume = Number(bar?.volume);
      column.sourceEnd = sourceIndex + 1;
      column.sourceCount += 1;
      column.lastSourceTs = String(bar?.ts || "");
      column.lastSourceX = centerX;
      column.centerX = (column.firstSourceX + centerX) / 2;
      column.renderClose = Number(bar?.close);
      if (high > column.renderHigh) {
        column.renderHigh = high;
        column.highSourceIndex = sourceIndex;
      }
      if (low < column.renderLow) {
        column.renderLow = low;
        column.lowSourceIndex = sourceIndex;
      }
      if (Number.isFinite(volume)) {
        column.renderVolume += Math.max(volume, 0);
        column.renderVolumeKnown = true;
      }
      return column;
    }

    function chartRenderProjection(bars, xStep, x, visualAxis = null) {
      const rows = Array.isArray(bars) ? bars : [];
      const mode = chartRenderDensityMode(xStep);
      if (mode !== "overview" || !rows.length || typeof x !== "function") {
        return {
          mode,
          columns: [],
          sourceBars: rows.length,
          maxSourceBarsPerColumn: rows.length ? 1 : 0,
        };
      }
      const axis = visualAxis && Array.isArray(visualAxis.gaps)
        ? visualAxis
        : chartVisualAxis(rows);
      const gapAfterIndexes = new Set(axis.gaps.map(gap => gap.afterIndex));
      const columns = [];
      const columnWidth = Math.max(Number(CHART_OVERVIEW_COLUMN_WIDTH_PX) || 1, 0.5);
      let active = null;
      let segment = 0;
      let maxSourceBarsPerColumn = 0;
      const flush = () => {
        if (!active) return;
        columns.push(active);
        maxSourceBarsPerColumn = Math.max(maxSourceBarsPerColumn, active.sourceCount);
        active = null;
      };
      rows.forEach((bar, sourceIndex) => {
        if (gapAfterIndexes.has(sourceIndex)) {
          flush();
          segment += 1;
        }
        if (!hasPriceBar(bar)) {
          flush();
          segment += 1;
          return;
        }
        const centerX = Number(x(sourceIndex));
        if (!Number.isFinite(centerX)) {
          flush();
          segment += 1;
          return;
        }
        const pixelColumn = Math.floor(centerX / columnWidth);
        const provisional = bar?.closed === false || bar?.authoritative === false;
        if (
          !active
          || active.pixelColumn !== pixelColumn
          || active.segment !== segment
          || active.provisional
          || provisional
        ) {
          flush();
          active = newChartRenderColumn(bar, sourceIndex, centerX, pixelColumn, segment);
          if (provisional) flush();
          return;
        }
        mergeChartRenderColumn(active, bar, sourceIndex, centerX);
      });
      flush();
      return {
        mode,
        columns,
        sourceBars: rows.length,
        maxSourceBarsPerColumn,
      };
    }

    function drawingMagnetCandidateRadiusBars(xStep, snapRadiusPx) {
      const step = Math.max(Number(xStep) || 0, 0.05);
      const radius = Math.max(Number(snapRadiusPx) || 0, 1);
      return clamp(
        Math.ceil(radius / step) + 2,
        2,
        512,
      );
    }

    function chartVisualHitForLocalX(bars, pad, xStep, localX) {
      const rows = Array.isArray(bars) ? bars : [];
      if (!rows.length || !Number.isFinite(Number(xStep)) || Number(xStep) <= 0) {
        return { index: null, gap: null };
      }
      const axis = chartVisualAxis(rows);
      const rawPosition = (
        localX
        - Number(pad?.left || 0)
        - Number(xStep) * 0.5
        - smoothOffsetBars() * Number(xStep)
      ) / Number(xStep);
      const gap = axis.gaps.find(item => (
        rawPosition >= item.startPosition
        && rawPosition <= item.endPosition
      ));
      if (gap) return { index: null, gap };
      if (rawPosition <= axis.positions[0]) return { index: 0, gap: null };
      const lastIndex = rows.length - 1;
      const lastPosition = axis.positions[lastIndex];
      if (rawPosition >= lastPosition) {
        return {
          index: lastIndex + Math.round(rawPosition - lastPosition),
          gap: null,
        };
      }
      let low = 0;
      let high = lastIndex;
      while (low + 1 < high) {
        const middle = Math.floor((low + high) / 2);
        if (axis.positions[middle] < rawPosition) low = middle;
        else high = middle;
      }
      const index = rawPosition - axis.positions[low] <= axis.positions[high] - rawPosition
        ? low
        : high;
      return { index, gap: null };
    }

    function drawProviderDataGaps(ctx, bars, pad, height, xStep, x, options = {}) {
      const gaps = chartVisualAxis(bars).gaps;
      if (!gaps.length || !ctx || typeof x !== "function") return;
      ctx.save();
      ctx.fillStyle = options.fill || "rgba(245, 158, 11, 0.075)";
      ctx.strokeStyle = options.stroke || "rgba(245, 158, 11, 0.58)";
      ctx.lineWidth = 1;
      ctx.setLineDash([3, 4]);
      gaps.forEach(gap => {
        const left = x(gap.beforeIndex) + xStep * 0.5;
        const right = x(gap.afterIndex) - xStep * 0.5;
        const width = Math.max(right - left, 0);
        if (width <= 0) return;
        ctx.fillRect(left, pad.top, width, height);
        ctx.beginPath();
        ctx.moveTo(left, pad.top);
        ctx.lineTo(left, pad.top + height);
        ctx.moveTo(right, pad.top);
        ctx.lineTo(right, pad.top + height);
        ctx.stroke();
        if (options.labels !== false && width >= 42) {
          ctx.save();
          ctx.setLineDash([]);
          ctx.fillStyle = options.text || "rgba(245, 158, 11, 0.92)";
          ctx.font = `10px ${CANVAS_MONO_FONT}`;
          ctx.textAlign = "center";
          ctx.fillText(
            `NO DATA · ${gap.missingSlots}`,
            left + width * 0.5,
            pad.top + 14,
            Math.max(width - 8, 1),
          );
          ctx.restore();
        }
      });
      ctx.restore();
    }

    function cachedChartGeometry(kind, snapshot, width, height, compute) {
      const bars = snapshot?.bars || [];
      const key = [
        kind,
        Math.round(Number(width) * 10) / 10,
        Math.round(Number(height) * 10) / 10,
        bars.length,
        chartBarRenderKey(bars[0]),
        chartBarRenderKey(bars[Math.floor((bars.length - 1) / 2)]),
        chartBarRenderKey(bars[bars.length - 1]),
        Math.round(Number(view.offset) || 0),
        Math.round(Number(view.barsVisible) || 0),
        Number(view.priceZoom || 0).toFixed(4),
        Number(view.priceShift || 0).toFixed(4),
        Number(view.rightGapBars || 0).toFixed(4),
        Number(view.smoothOffsetBars || 0).toFixed(4),
        gexFocusMode() ? "gex-focus" : "normal",
      ].join("~");
      const cached = chartGeometryCache.get(key);
      if (cached) return cached;
      if (chartGeometryCache.size > 24) chartGeometryCache.clear();
      const geometry = compute();
      chartGeometryCache.set(key, geometry);
      return geometry;
    }

    function hasPriceBar(bar) {
      const rawOhlc = [bar?.open, bar?.high, bar?.low, bar?.close];
      if (rawOhlc.some(value => typeof value !== "number" || !Number.isFinite(value))) return false;
      const [open, high, low, close] = rawOhlc;
      return [open, high, low, close].every(Number.isFinite)
        && high >= low
        && high >= Math.max(open, close)
        && low <= Math.min(open, close);
    }

    function finiteSeriesNumber(value) {
      return typeof value === "number" && Number.isFinite(value) ? value : null;
    }

    function latestFiniteValue(values) {
      for (let index = values.length - 1; index >= 0; index -= 1) {
        const value = finiteSeriesNumber(values[index]);
        if (value !== null) return { index, value };
      }
      return null;
    }

    function firstFiniteValue(values) {
      for (let index = 0; index < values.length; index += 1) {
        const value = finiteSeriesNumber(values[index]);
        if (value !== null) return { index, value };
      }
      return null;
    }

    function latestFiniteClose(bars, beforeIndex = null) {
      const lastIndex = (beforeIndex === null || beforeIndex === undefined)
        ? (bars?.length || 0) - 1
        : Math.min(Number(beforeIndex) || 0, (bars?.length || 1) - 1);
      for (let index = lastIndex; index >= 0; index -= 1) {
        const bar = bars[index];
        const close = finiteSeriesNumber(bar?.close);
        if (close !== null) return close;
      }
      return null;
    }

    function renderEmptyCanvas(id, title, detail, options = {}) {
      const { ctx, w, h } = canvasContext(id);
      if (id === "price-chart") {
        renderGexDockFromGeometry(null, null);
        for (const layerId of ["price-overlay", "price-gex-levels", "price-objects", "price-trading", "price-interaction", "price-gex-front"]) {
          const overlay = document.getElementById(layerId);
          if (!overlay) continue;
          const layer = canvasContext(layerId);
          layer.ctx.clearRect(0, 0, layer.w, layer.h);
        }
      } else if (id === "volume-chart") {
        const interaction = document.getElementById("volume-interaction");
        if (interaction) {
          const layer = canvasContext("volume-interaction");
          layer.ctx.clearRect(0, 0, layer.w, layer.h);
        }
      }
      ctx.clearRect(0, 0, w, h);
      ctx.fillStyle = css("--chart-bg");
      ctx.fillRect(0, 0, w, h);
      if (options.skeleton === true) {
        const left = Math.max(24, Math.round(w * 0.055));
        const right = Math.max(50, Math.round(w * 0.08));
        const top = Math.max(20, Math.round(h * 0.10));
        const bottom = Math.max(28, Math.round(h * 0.14));
        const innerW = Math.max(w - left - right, 1);
        const innerH = Math.max(h - top - bottom, 1);
        ctx.save();
        ctx.strokeStyle = css("--grid-line");
        ctx.lineWidth = 1;
        ctx.globalAlpha = 0.58;
        for (let row = 0; row <= 4; row += 1) {
          const py = top + innerH * row / 4;
          ctx.beginPath();
          ctx.moveTo(left, py);
          ctx.lineTo(left + innerW, py);
          ctx.stroke();
        }
        for (let column = 0; column <= 6; column += 1) {
          const px = left + innerW * column / 6;
          ctx.beginPath();
          ctx.moveTo(px, top);
          ctx.lineTo(px, top + innerH);
          ctx.stroke();
        }
        ctx.fillStyle = css("--axis");
        ctx.globalAlpha = 0.14;
        const stripH = Math.max(5, Math.min(9, Math.round(innerH * 0.045)));
        const stripY = [0.23, 0.47, 0.71];
        const stripW = [0.26, 0.18, 0.32];
        stripY.forEach((ratio, index) => {
          ctx.fillRect(
            left + innerW * (0.10 + index * 0.17),
            top + innerH * ratio,
            innerW * stripW[index],
            stripH,
          );
        });
        for (let row = 0; row < 4; row += 1) {
          ctx.fillRect(left + innerW + 8, top + innerH * row / 4 + 2, Math.max(right - 18, 18), 5);
        }
        for (let column = 0; column < 5; column += 1) {
          ctx.fillRect(left + innerW * column / 5 + 3, top + innerH + 9, Math.max(innerW * 0.08, 20), 5);
        }
        ctx.restore();
      }
      ctx.fillStyle = options.skeleton === true ? css("--axis") : css("--gold");
      ctx.font = "700 14px -apple-system, BlinkMacSystemFont, sans-serif";
      ctx.textAlign = "center";
      ctx.fillText(title, w / 2, h / 2 - 8);
      ctx.fillStyle = css("--axis");
      ctx.font = "12px -apple-system, BlinkMacSystemFont, sans-serif";
      ctx.fillText(detail, w / 2, h / 2 + 14);
      ctx.textAlign = "left";
    }

    function clampView(snapshot) {
      visibleBars(snapshot);
      view.priceZoom = clamp(Number(view.priceZoom) || 1, MIN_PRICE_ZOOM, MAX_PRICE_ZOOM);
      view.priceShift = clamp(Number(view.priceShift) || 0, -5, 5);
      rightGapBars();
    }

    function isVwapLevel(level) {
      return level?.role === "session_vwap";
    }

    function isLocalHighLowLevel(level) {
      return level?.role === "local_high" || level?.role === "local_low";
    }

    function activeChartLevels(snapshot) {
      return (snapshot.levels || []).filter(level => {
        if (isVwapLevel(level)) return false;
        if (isLocalHighLowLevel(level)) return false;
        return true;
      });
    }
    function activeVolumeSignalConfig() {
      if (state.indicators.vsaVolume.visible !== false) {
        return { ...state.indicators.vsaVolume, source: "vsa_volume", footer: "VSA Volume" };
      }
      return { visible: false, avg: false, labels: false, priceMarks: false, source: "", footer: "Relative volume" };
    }

    let indicatorSeriesCache = new WeakMap();

    function indicatorSeriesCacheKey(source, bars) {
      const first = typeof timestampKey === "function" ? timestampKey(bars[0]?.ts) : bars[0]?.ts || "";
      const last = typeof timestampKey === "function" ? timestampKey(bars[bars.length - 1]?.ts) : bars[bars.length - 1]?.ts || "";
      return `${source}|${bars.length}|${first}|${last}`;
    }

    function cachedIndicatorSeries(snapshot, source, series, bars) {
      if (!snapshot || !bars.length || !source) return [];
      let snapshotCache = indicatorSeriesCache.get(snapshot);
      if (!snapshotCache) {
        snapshotCache = new Map();
        indicatorSeriesCache.set(snapshot, snapshotCache);
      }
      const key = indicatorSeriesCacheKey(source, bars);
      const cached = snapshotCache.get(source);
      if (cached?.series === series && cached?.key === key) return cached.rows;
      const byTs = new Map((series || []).map(item => [
        typeof timestampKey === "function" ? timestampKey(item?.ts) : String(item?.ts || ""),
        item,
      ]));
      const rows = bars.map(bar => {
        const key = typeof timestampKey === "function" ? timestampKey(bar?.ts) : String(bar?.ts || "");
        return byTs.get(key) || null;
      });
      snapshotCache.set(source, { series, key, rows });
      return rows;
    }

    const VSA_VOLUME_RENDER_HOUR_OPTIONS = Object.freeze([2, 4, 6, 12]);
    const VSA_VOLUME_RENDER_DEFAULT_HOURS = 6;

    function vsaVolumeRenderHours(value = state.indicators?.vsaVolume?.renderHours) {
      const hours = Number(value);
      return VSA_VOLUME_RENDER_HOUR_OPTIONS.includes(hours)
        ? hours
        : VSA_VOLUME_RENDER_DEFAULT_HOURS;
    }

    function vsaSourceSeries(snapshot) {
      const candidate = snapshot?.vsa_volume?.series;
      return Array.isArray(candidate) ? candidate : null;
    }

    let vsaRenderSeriesCache = new WeakMap();

    function vsaRenderSourceSeries(snapshot) {
      const series = vsaSourceSeries(snapshot);
      if (!snapshot || !series?.length) return series;
      const hours = vsaVolumeRenderHours();
      const cached = vsaRenderSeriesCache.get(snapshot);
      if (cached?.series === series && cached?.hours === hours) return cached.rows;
      let latestMs = Number.NEGATIVE_INFINITY;
      for (const item of series) {
        const timestamp = Date.parse(item?.ts || "");
        if (Number.isFinite(timestamp) && timestamp > latestMs) latestMs = timestamp;
      }
      const cutoffMs = latestMs - hours * 60 * 60 * 1000;
      const rows = Number.isFinite(latestMs)
        ? series.filter(item => {
            const timestamp = Date.parse(item?.ts || "");
            return !Number.isFinite(timestamp) || timestamp > cutoffMs;
          })
        : series;
      vsaRenderSeriesCache.set(snapshot, { series, hours, rows });
      return rows;
    }

    function volumeStructureSeries(snapshot, bars) {
      if (!bars.length) return [];
      const cfg = activeVolumeSignalConfig();
      if (cfg.visible === false) return [];
      return vsaSeries(snapshot, bars);
    }

    function vsaSeries(snapshot, bars) {
      if (state.indicators.vsaVolume.visible === false || !bars.length) return [];
      const series = vsaRenderSourceSeries(snapshot);
      return cachedIndicatorSeries(snapshot, "vsa_volume", series, bars);
    }

    function vsaVolumeStructureText(code, item = null) {
      if (item?.terminal_climax) {
        const arrow = item?.terminal_direction === "short" ? "↓" : item?.terminal_direction === "long" ? "↑" : "";
        return `CLX★${arrow}`;
      }
      if (item?.fuel) {
        const arrow = item?.direction === "short" ? "↓" : item?.direction === "long" ? "↑" : "";
        return `FUEL${arrow}`;
      }
      const absRole = String(item?.absorption_role || "").toLowerCase();
      const absWall = absRole === "resistance"
        ? "ABSORB_WALL_DOWN"
        : absRole === "support"
        ? "ABSORB_WALL_UP"
        : item?.direction === "short"
        ? "ABSORB_WALL_DOWN"
        : item?.direction === "long"
        ? "ABSORB_WALL_UP"
        : "ABS";
      return {
        EXH_DN: "EX↓",
        EXH_UP: "EX↑",
        SPRING: "SPR↑",
        UPTHRUST: "UT↓",
        ABS_LVL: absWall,
        ABS: absWall,
        FUEL_UP: "FUEL↑",
        FUEL_DN: "FUEL↓",
        IMP_UP: "⚡↑",
        IMP_DN: "⚡↓",
      }[code] || "";
    }

    function vsaVolumeLabelModel(item = null) {
      const rendered = vsaVolumeStructureText(String(item?.code || ""), item);
      if (!rendered) return null;
      const renderedArrow = rendered.endsWith("↓") ? "↓" : rendered.endsWith("↑") ? "↑" : "";
      const direction = String(item?.terminal_climax ? item?.terminal_direction : item?.direction || "flat");
      const arrow = renderedArrow || (direction === "short" ? "↓" : direction === "long" ? "↑" : "");
      const renderedText = renderedArrow ? rendered.slice(0, -1) : rendered;
      const text = item?.terminal_climax
        ? "CLX"
        : item?.fuel
        ? "FUEL"
        : renderedText.startsWith("ABSORB_WALL")
        ? "ABS"
        : renderedText;
      return {
        orientation: "vertical",
        placement: "volume_bar",
        text,
        score: Math.min(99, Math.max(0, Math.round(Number(item?.score) || 0))),
        direction,
        arrow,
      };
    }

    function volumeStructureRole(item) {
      return String(item?.structure_text_role || "").trim();
    }

    function volumeStructureText(code, item = null) {
      const formatter = {
        vsa: vsaVolumeStructureText,
      }[volumeStructureRole(item)];
      return formatter ? formatter(code, item) : "";
    }

    function climaxCandleColors() {
      return isLightTheme()
        ? { fill: "#ffffff", stroke: "#000000" }
        : { fill: "#111111", stroke: "#f8fafc" };
    }

    function fuelCandleColors() {
      return isLightTheme()
        ? { fill: "#fff7ed", stroke: "#d97706" }
        : { fill: "#1c1408", stroke: "#f59e0b" };
    }

    function vsaVolumeColorLegendEntries() {
      const up = { close: 2, open: 1 };
      const down = { close: 1, open: 2 };
      return [
        { label: "CLX★", note: "Terminal VSA climax (two-tone fill + outline)", terminal: true },
        { label: "FUEL", note: "Fuel bar (amber two-tone fill + outline)", fuel: true },
        { code: "EXH_DN", label: "EX↓", note: "Exhaustion down", bar: down },
        { code: "EXH_UP", label: "EX↑", note: "Exhaustion up", bar: up },
        { code: "SPRING", label: "SPR↑", note: "Spring", bar: up },
        { code: "UPTHRUST", label: "UT↓", note: "Upthrust", bar: down },
        { code: "ABS", label: "ABSORB", note: "Absorption / wall", bar: up },
        { code: "FUEL_UP", label: "IMP↑", note: "Impulse up", bar: up },
        { code: "FUEL_DN", label: "IMP↓", note: "Impulse down", bar: down },
        { code: "IMP_UP", label: "⚡↑", note: "Strong impulse up", bar: up },
        { code: "IMP_DN", label: "⚡↓", note: "Strong impulse down", bar: down },
        { plainUp: true, label: "▲", note: "Plain bullish candle", bar: up },
        { plainDown: true, label: "▼", note: "Plain bearish candle", bar: down },
      ];
    }

    function renderVsaVolumeColorLegend() {
      const host = document.getElementById("vsa-volume-color-legend");
      if (!host) return;
      host.innerHTML = vsaVolumeColorLegendEntries().map(entry => {
        let swatchStyle = "";
        if (entry.terminal) {
          const colors = climaxCandleColors();
          swatchStyle = `background:${colors.fill};border:2px solid ${colors.stroke}`;
        } else if (entry.fuel) {
          const colors = fuelCandleColors();
          swatchStyle = `background:linear-gradient(to bottom, #000000 0 33%, ${colors.stroke} 33% 100%);border:2px solid ${colors.stroke}`;
        } else if (entry.plainUp || entry.plainDown) {
          const color = volumeStructureColor(null, entry.bar);
          swatchStyle = `background:${color}`;
        } else {
          const color = volumeStructureColor({ code: entry.code }, entry.bar);
          swatchStyle = `background:${color}`;
        }
        return `<div class="vsa-color-legend-item"><span class="vsa-color-swatch" style="${swatchStyle}"></span><span class="vsa-color-copy"><b>${escapeHtml(entry.label)}</b><span>${escapeHtml(entry.note)}</span></span></div>`;
      }).join("");
    }

    function volumeCandlePaint(vsaItem, bar) {
      if (vsaItem?.terminal_climax) {
        const colors = climaxCandleColors();
        return { mode: "terminal", fill: colors.fill, stroke: colors.stroke };
      }
      if (vsaItem?.fuel) {
        const colors = fuelCandleColors();
        return { mode: "fuel", fill: colors.stroke, stroke: colors.stroke };
      }
      const up = Number(bar?.close) >= Number(bar?.open);
      const color = vsaItem?.code
        ? volumeStructureColor(vsaItem, bar)
        : up
          ? css("--green")
          : css("--red");
      return { mode: "solid", color };
    }

    function volumeStructureColor(item, bar) {
      if (item?.terminal_climax) return climaxCandleColors().stroke;
      if (item?.fuel) return fuelCandleColors().stroke;
      if (!item?.code) {
        return bar.close >= bar.open ? themeColor("up", 0.68) : themeColor("down", 0.68);
      }
      const colors = {
        EXH_DN: themeColor("gold"),
        EXH_UP: themeColor("blue"),
        SPRING: themeColor("blue"),
        UPTHRUST: themeColor("gold"),
        ABS_LVL: themeColor("purple"),
        ABS: themeColor("purple"),
        FUEL_UP: themeColor("up"),
        FUEL_DN: themeColor("down"),
        IMP_UP: themeColor("up"),
        IMP_DN: themeColor("down"),
      };
      return colors[item.code] || css("--gold");
    }

    function volumeStructureTooltip(item) {
      if (!item) return "";
      const headline = item.terminal_climax
        ? `CLX★ ${Math.round(item.score || 0)}/100`
        : item.fuel
        ? `FUEL ${Math.round(item.score || 0)}/100`
        : `${volumeStructureText(item.code, item)} ${Math.round(item.score || 0)}/100`;
      return [
        headline,
        item.setup_summary || item.candidate_reason || "",
        `RVOL ${Number(item.rvol || 0).toFixed(2)} | rank ${Math.round(item.vol_rank || 0)}% | z ${Number(item.vol_z || 0).toFixed(2)}`,
        `Move ${Number(item.move_atr || 0).toFixed(2)} ATR | range ${Number(item.range_atr || 0).toFixed(2)} ATR`,
        `Spread ${Number(item.spread_rel || 0).toFixed(2)}`,
      ].filter(Boolean).join("\\n");
    }

    function quickSelectValue(values, targetIndex) {
      let left = 0;
      let right = values.length - 1;
      const target = clamp(Math.round(Number(targetIndex) || 0), 0, right);
      while (left < right) {
        const pivotValue = values[(left + right) >> 1];
        let i = left;
        let j = right;
        while (i <= j) {
          while (values[i] < pivotValue) i += 1;
          while (values[j] > pivotValue) j -= 1;
          if (i <= j) {
            const tmp = values[i];
            values[i] = values[j];
            values[j] = tmp;
            i += 1;
            j -= 1;
          }
        }
        if (target <= j) right = j;
        else if (target >= i) left = i;
        else return values[target];
      }
      return values[target];
    }

    function volumeCompressionCap(values) {
      const clean = values.filter(value => Number.isFinite(value) && value > 0);
      if (!clean.length) return 1;
      const p70 = quickSelectValue(
        clean,
        clamp(Math.round((clean.length - 1) * 0.70), 0, clean.length - 1),
      );
      const p90 = quickSelectValue(
        clean,
        clamp(Math.round((clean.length - 1) * 0.90), 0, clean.length - 1),
      );
      return Math.max(p90 * 1.25, p70 * 2.20, 1);
    }

    function compressVolumeForDisplay(value, cap) {
      const safeValue = Math.max(Number(value) || 0, 0);
      if (safeValue <= cap) return safeValue;
      const excess = safeValue - cap;
      return cap + Math.sqrt(excess / Math.max(cap, 1)) * cap * 0.42;
    }

    function volumeBarWidth(baseWidth, xStep, rawValue, cap, item) {
      const overflow = cap > 0 ? Math.max(rawValue / cap - 1, 0) : 0;
      const eventBoost = item?.important ? 0.28 : item?.code ? 0.16 : 0;
      const widthBoost = clamp(Math.sqrt(overflow) * 0.22 + eventBoost, 0, 0.72);
      return Math.min(baseWidth * (1 + widthBoost), Math.max(xStep * 0.92, baseWidth));
    }
    function openingRangeIntersectsVisible(bars, range = state.marketContext?.openingRange) {
      if (!range || !bars?.length) return false;
      const first = Date.parse(bars[0].ts);
      const last = Date.parse(bars[bars.length - 1].ts);
      const start = Date.parse(range.startTs);
      const end = Date.parse(range.endTs);
      return Number.isFinite(start) && Number.isFinite(end) && end >= first && start <= last;
    }
    function priceScale(snapshot, h, bars) {
      const pad = { left: CHART_LEFT_PAD, right: PRICE_AXIS_WIDTH, top: 10, bottom: 24 };
      const priceH = h - pad.top - pad.bottom;
      const latestClose = latestFiniteClose(bars);
      const ref = latestClose === null ? NaN : Number(latestClose);
      const priceBars = bars.filter(hasPriceBar);
      const signedOrCrossZero = Number.isFinite(ref) && (
        ref <= 0
        || priceBars.some(bar => Number(bar.low) <= 0)
      );
      const lowerPlausibleDistance = Number.isFinite(ref) ? Math.abs(ref) * 0.75 : Infinity;
      const upperPlausibleDistance = Number.isFinite(ref) ? Math.abs(ref) * 3.0 : Infinity;
      const points = [];
      priceBars.forEach(bar => {
        [Number(bar.high), Number(bar.low)].forEach(price => {
          if (
            Number.isFinite(price)
            && (
              signedOrCrossZero
              || !Number.isFinite(ref)
              || (price >= ref - lowerPlausibleDistance && price <= ref + upperPlausibleDistance)
            )
          ) points.push(price);
        });
      });
      if (!points.length) {
        priceBars.forEach(bar => {
          [Number(bar.high), Number(bar.low), Number(bar.close), Number(bar.open)].forEach(price => {
            if (Number.isFinite(price)) points.push(price);
          });
        });
      }
      const rawMax = points.length ? Math.max(...points) : (Number.isFinite(ref) ? ref : 1);
      const rawMin = points.length ? Math.min(...points) : rawMax - 1;
      const baseRange = Math.max(rawMax - rawMin, 1);
      const range = baseRange / view.priceZoom;
      const mid = (rawMax + rawMin) / 2 + view.priceShift * range;
      const maxP = mid + range / 2;
      const minP = mid - range / 2;
      return { pad, priceH, maxP, minP };
    }

    function priceChartGeometry() {
      if (!state.snapshot?.bars?.length) return null;
      const canvas = document.getElementById("price-chart");
      const rect = canvas.getBoundingClientRect();
      const visible = visibleBars(state.snapshot);
      const { bars, start, end, allBars } = visible;
      if (!bars.length) return null;
      const scale = priceScale(state.snapshot, rect.height, bars);
      const visualAxis = chartVisualAxis(bars);
      const visualSpan = visualAxis.span;
      const xStep = (rect.width - scale.pad.left - scale.pad.right) / (visualSpan + rightGapBars());
      const x = chartXFromVisualAxis(scale.pad, xStep, visualAxis);
      const y = price => scale.pad.top + (scale.maxP - price) / (scale.maxP - scale.minP) * scale.priceH;
      return {
        rect,
        bars,
        start,
        end,
        allBars,
        snapshot: state.snapshot,
        timeframe: state.timeframe,
        barsVersion: chartRenderVersions.bars,
        futureAxis: state.snapshot?.future_axis,
        slotStep: intervalMinutesFromState(),
        rightGapBars: rightGapBars(),
        ...scale,
        visualSpan,
        xStep,
        x,
        y,
      };
    }

    function isChartPlotXVisible(x, pad, width, margin = 6) {
      return Number.isFinite(x) && x >= pad.left - margin && x <= width - pad.right + margin;
    }

    function chartBarIndexForEventTimestamp(bars, timestampMs, stepMs = intervalMsFromState()) {
      const intervalMs = Number(stepMs);
      if (
        !Array.isArray(bars)
        || !bars.length
        || !Number.isFinite(timestampMs)
        || !Number.isFinite(intervalMs)
        || intervalMs <= 0
      ) return -1;
      let low = 0;
      let high = bars.length - 1;
      let match = -1;
      while (low <= high) {
        const middle = low + ((high - low) >> 1);
        const barTs = Date.parse(bars[middle]?.ts || "");
        if (!Number.isFinite(barTs)) return -1;
        if (barTs <= timestampMs) {
          match = middle;
          low = middle + 1;
        } else {
          high = middle - 1;
        }
      }
      if (match < 0) return -1;
      const barTs = Date.parse(bars[match]?.ts || "");
      return timestampMs < barTs + intervalMs ? match : -1;
    }

    function overlayRangeOutsidePlot(leftRaw, rightRaw, pad, width, margin = 1) {
      const plotLeft = pad.left - margin;
      const plotRight = width - pad.right + margin;
      return Math.max(leftRaw, rightRaw) < plotLeft || Math.min(leftRaw, rightRaw) > plotRight;
    }

    function drawingAxisFromGeo(geo) {
      if (!geo) return null;
      return {
        bars: geo.bars,
        allBars: geo.allBars,
        start: geo.start,
        xStep: geo.xStep,
        x: geo.x,
        y: geo.y,
        snapshot: Object.prototype.hasOwnProperty.call(geo, "snapshot")
          ? geo.snapshot
          : state.snapshot,
        timeframe: Object.prototype.hasOwnProperty.call(geo, "timeframe")
          ? String(geo.timeframe || "")
          : String(state.timeframe || ""),
        futureAxis: Object.prototype.hasOwnProperty.call(geo, "futureAxis")
          ? geo.futureAxis
          : state.snapshot?.future_axis,
        slotStep: geo.slotStep,
        ...(Object.prototype.hasOwnProperty.call(geo, "rightGapBars")
          ? { rightGapBars: geo.rightGapBars }
          : {}),
        width: geo.rect?.width,
        pad: geo.pad,
        barsVersion: Number.isFinite(Number(geo.barsVersion))
          ? Number(geo.barsVersion)
          : chartRenderVersions.bars,
      };
    }

    const confirmedDrawingAxisBaseCache = new WeakMap();

    function confirmedDrawingAxisBase(axis) {
      const allBars = Array.isArray(axis?.allBars) && axis.allBars.length
        ? axis.allBars
        : Array.isArray(axis?.bars) ? axis.bars : [];
      const barsVersion = Number(axis?.barsVersion) || 0;
      const slotStep = axisSlotStep(axis);
      const baseKey = `${barsVersion}:${allBars.length}:${slotStep}`;
      const shared = confirmedDrawingAxisBaseCache.get(allBars);
      if (shared?.baseKey === baseKey) {
        if (axis && typeof axis === "object") axis._confirmedDrawingAxisBase = shared;
        return shared;
      }
      const bars = [];
      const byTimestamp = new Map();
      const absoluteIndices = [];
      for (let absoluteIndex = 0; absoluteIndex < allBars.length; absoluteIndex += 1) {
        const bar = allBars[absoluteIndex];
        const key = drawingTimestampIdentity(bar?.ts);
        if (!key || !barIsConfirmedClosed(bar) || bar?.authoritative === false) continue;
        byTimestamp.set(key, bars.length);
        bars.push(bar);
        absoluteIndices.push(absoluteIndex);
      }
      const lineBreakpointLocalIndices = [];
      const visualPositions = chartVisualAxis(allBars, slotStep).positions;
      for (let localIndex = 1; localIndex < absoluteIndices.length; localIndex += 1) {
        // Screen spacing belongs to the candle axis, not to row adjacency.
        // Absent provider bars have no rows but may still leave an empty X span.
        const distance = visualPositions[absoluteIndices[localIndex]]
          - visualPositions[absoluteIndices[localIndex - 1]];
        if (distance === 1) continue;
        lineBreakpointLocalIndices.push(localIndex - 1, localIndex);
      }
      const base = {
        allBars,
        bars,
        byTimestamp,
        absoluteIndices,
        lineBreakpointLocalIndices: [...new Set(lineBreakpointLocalIndices)],
        projectionAxes: new Map(),
        localAxis: null,
        baseKey,
      };
      confirmedDrawingAxisBaseCache.set(allBars, base);
      if (axis && typeof axis === "object") axis._confirmedDrawingAxisBase = base;
      return base;
    }

    function confirmedDrawingAxis(axis, projection = null) {
      const base = confirmedDrawingAxisBase(axis);
      const projectionIndex = drawingProjectionIndex(projection);
      if (!projectionIndex && base.localAxis) return base.localAxis;
      const snapshot = axis && Object.prototype.hasOwnProperty.call(axis, "snapshot")
        ? axis.snapshot
        : state.snapshot;
      const snapshotKey = projectionIndex
        ? drawingProjectionSnapshotCacheKey(snapshot)
        : "local";
      const cached = projectionIndex ? base.projectionAxes.get(projectionIndex) : null;
      if (cached?.snapshotKey === snapshotKey) return cached.axis;
      const projectionOffset = (
        projectionIndex
        && drawingProjectionMatchesSnapshotScope(projectionIndex, snapshot)
      )
        ? drawingProjectionSnapshotOffset(
            projectionIndex,
            base.bars,
            base.byTimestamp,
          )
        : null;
      const result = {
        ...base,
        projection: projectionIndex,
        projectionOffset,
        lineLogicalIndicesByRange: new Map(),
        projectionKey: projectionIndex || "local",
      };
      if (projectionIndex) {
        base.projectionAxes.set(projectionIndex, { snapshotKey, axis: result });
      } else {
        base.localAxis = result;
      }
      return result;
    }

    function confirmedDrawingViewportLogicalRange(axis, projection = null) {
      const confirmedAxis = confirmedDrawingAxis(axis, projection);
      const absoluteIndices = confirmedAxis.absoluteIndices;
      if (!absoluteIndices.length) return null;
      const lowerBound = target => {
        let low = 0;
        let high = absoluteIndices.length;
        while (low < high) {
          const middle = low + Math.floor((high - low) * 0.5);
          if (absoluteIndices[middle] < target) low = middle + 1;
          else high = middle;
        }
        return low;
      };
      const viewportStart = Math.max(Number(axis?.start) || 0, 0);
      const viewportEnd = viewportStart + (Array.isArray(axis?.bars) ? axis.bars.length : 0);
      const firstLocalIndex = lowerBound(viewportStart);
      const endLocalIndex = lowerBound(viewportEnd) - 1;
      if (firstLocalIndex >= absoluteIndices.length || endLocalIndex < firstLocalIndex) return null;
      const origin = Number.isSafeInteger(confirmedAxis.projectionOffset)
        ? confirmedAxis.projectionOffset
        : 0;
      return {
        start: origin + firstLocalIndex,
        end: origin + endLocalIndex,
      };
    }

    function drawingLogicalIndexForPoint(point, axis, projection = null) {
      if (Number.isSafeInteger(point?.drawingLogicalIndex)) return point.drawingLogicalIndex;
      const confirmedAxis = confirmedDrawingAxis(axis, projection);
      const anchorTs = typeof point?.ts === "string" ? point.ts : point?.anchorTs;
      const key = drawingTimestampIdentity(anchorTs);
      if (!key) return null;
      const localIndex = confirmedAxis.byTimestamp.get(key);
      let logicalIndex = null;
      if (Number.isSafeInteger(localIndex)) {
        logicalIndex = Number.isSafeInteger(confirmedAxis.projectionOffset)
          ? localIndex + confirmedAxis.projectionOffset
          : localIndex;
      } else if (
        Number.isSafeInteger(confirmedAxis.projectionOffset)
        && confirmedAxis.projection?.byTimestamp.has(key)
      ) {
        logicalIndex = confirmedAxis.projection.byTimestamp.get(key);
      }
      if (!Number.isSafeInteger(logicalIndex)) return null;
      if (typeof point?.ts === "string") return logicalIndex;
      const barOffset = Number(point?.barOffset);
      return Number.isSafeInteger(barOffset) && barOffset > 0
        ? logicalIndex + barOffset
        : null;
    }

    function drawingScreenIndexForLogicalIndex(logicalIndex, axis, projection = null) {
      if (!Number.isSafeInteger(logicalIndex)) return null;
      const confirmedAxis = confirmedDrawingAxis(axis, projection);
      const origin = Number.isSafeInteger(confirmedAxis.projectionOffset)
        ? confirmedAxis.projectionOffset
        : 0;
      const localIndex = logicalIndex - origin;
      if (!confirmedAxis.absoluteIndices.length) return null;
      if (localIndex >= 0 && localIndex < confirmedAxis.absoluteIndices.length) {
        return confirmedAxis.absoluteIndices[localIndex] - (axis?.start || 0);
      }
      if (localIndex < 0) {
        return confirmedAxis.absoluteIndices[0] - (axis?.start || 0) + localIndex;
      }
      const latestLocalIndex = confirmedAxis.absoluteIndices.length - 1;
      return (
        confirmedAxis.absoluteIndices[latestLocalIndex]
        - (axis?.start || 0)
        + (localIndex - latestLocalIndex)
      );
    }

    function drawingPointAtLogicalIndex(logicalIndex, price) {
      return { drawingLogicalIndex: logicalIndex, price };
    }

    function latestDrawingLogicalIndex(axis, projection = null) {
      const confirmedAxis = confirmedDrawingAxis(axis, projection);
      if (!confirmedAxis.bars.length) return null;
      const localLatest = confirmedAxis.bars.length - 1;
      return Number.isSafeInteger(confirmedAxis.projectionOffset)
        ? localLatest + confirmedAxis.projectionOffset
        : localLatest;
    }

    function latestDrawingConfirmedBar(axis, projection = null) {
      const bars = confirmedDrawingAxis(axis, projection).bars;
      return bars.length ? bars[bars.length - 1] : null;
    }

    function drawingTimestampForLogicalIndex(logicalIndex, axis, projection = null) {
      const confirmedAxis = confirmedDrawingAxis(axis, projection);
      const origin = Number.isSafeInteger(confirmedAxis.projectionOffset)
        ? confirmedAxis.projectionOffset
        : 0;
      const localIndex = logicalIndex - origin;
      return Number.isSafeInteger(localIndex) && localIndex >= 0 && localIndex < confirmedAxis.bars.length
        ? confirmedAxis.bars[localIndex]?.ts || null
        : null;
    }

    function axisSlotStep(axis) {
      return Math.max(Number(axis?.slotStep) || intervalMinutesFromState(), 1);
    }

    const futureAxisIndexCache = new WeakMap();

    function providerFutureAxisIndex(axis) {
      const hasExplicitSnapshot = Boolean(
        axis && Object.prototype.hasOwnProperty.call(axis, "snapshot")
      );
      const snapshot = hasExplicitSnapshot
        ? axis.snapshot
        : (typeof state !== "undefined" ? state.snapshot : null);
      const payload = axis && Object.prototype.hasOwnProperty.call(axis, "futureAxis")
        ? axis.futureAxis
        : snapshot?.future_axis;
      const allBars = axis?.allBars?.length ? axis.allBars : axis?.bars;
      const last = allBars?.[allBars.length - 1];
      const step = axisSlotStep(axis);
      const activeTimeframe = axis && Object.prototype.hasOwnProperty.call(axis, "timeframe")
        ? String(axis.timeframe || "")
        : String(
            snapshot?.meta?.timeframe
            || (typeof state !== "undefined" ? state.timeframe : "")
            || "",
          );
      const anchorTimestamp = Date.parse(payload?.anchor_ts || "");
      let anchorIndex = -1;
      if (Array.isArray(allBars) && Number.isFinite(anchorTimestamp)) {
        for (let index = allBars.length - 1; index >= 0; index -= 1) {
          if (Date.parse(allBars[index]?.ts || "") === anchorTimestamp) {
            anchorIndex = index;
            break;
          }
        }
      }
      const anchorSlot = anchorIndex * step;
      const fingerprint = `${allBars?.length || 0}:${last?.ts || ""}:${payload?.anchor_ts || ""}:${anchorSlot}:${step}:${activeTimeframe}`;
      if (!payload || typeof payload !== "object" || !Array.isArray(payload.slots)) {
        return {
          slots: [],
          byAbsoluteIndex: new Map(),
          byTimestamp: new Map(),
        };
      }
      const cached = futureAxisIndexCache.get(payload);
      if (cached?.fingerprint === fingerprint) return cached;
      const hasExplicitTimeframe = Boolean(
        axis && Object.prototype.hasOwnProperty.call(axis, "timeframe")
      );
      const timeframeMatches = (
        !payload.timeframe
        || (!hasExplicitTimeframe && typeof state === "undefined")
        || String(payload.timeframe) === activeTimeframe
      );
      const scheduleState = String(payload.schedule_state || "");
      const slots = [];
      const byAbsoluteIndex = new Map();
      const byTimestamp = new Map();
      if (
        timeframeMatches
        && ["continuous", "verified"].includes(scheduleState)
        && Number.isFinite(anchorTimestamp)
        && anchorIndex >= 0
        && Number.isFinite(anchorSlot)
      ) {
        let previousTimestamp = anchorTimestamp;
        let previousOffset = 0;
        for (const item of payload.slots) {
          const timestamp = Date.parse(item?.ts || "");
          const barOffset = Number(item?.bar_offset);
          if (!Number.isFinite(timestamp) || !Number.isSafeInteger(barOffset)) continue;
          if (timestamp <= previousTimestamp || barOffset !== previousOffset + 1) break;
          const absoluteIndex = anchorIndex + barOffset;
          const barSlot = anchorSlot + barOffset * step;
          const normalized = {
            ts: String(item.ts),
            barOffset,
            barSlot,
            absoluteIndex,
          };
          slots.push(normalized);
          byAbsoluteIndex.set(absoluteIndex, normalized);
          byTimestamp.set(timestamp, normalized);
          previousTimestamp = timestamp;
          previousOffset = barOffset;
        }
      }
      const index = {
        fingerprint,
        slots,
        byAbsoluteIndex,
        byTimestamp,
      };
      futureAxisIndexCache.set(payload, index);
      return index;
    }

    function barSlotForAbsoluteIndex(axis, absoluteIndex) {
      const allBars = axis?.allBars?.length ? axis.allBars : axis?.bars;
      if (!allBars?.length || !Number.isFinite(absoluteIndex)) return null;
      const index = Math.round(absoluteIndex);
      const bar = index >= 0 && index < allBars.length ? allBars[index] : null;
      const stored = bar?.bar_slot;
      if (bar) {
        return bar?.bar_slot_authoritative === true && Number.isFinite(stored)
          ? Number(stored)
          : null;
      }
      if (index < allBars.length) return null;
      const future = providerFutureAxisIndex(axis).byAbsoluteIndex.get(index);
      return Number.isFinite(future?.barSlot) ? future.barSlot : null;
    }

    function screenIndexForTimestampPoint(point, axis) {
      const confirmedLogicalIndex = drawingLogicalIndexForPoint(point, axis);
      if (Number.isSafeInteger(confirmedLogicalIndex)) {
        return drawingScreenIndexForLogicalIndex(confirmedLogicalIndex, axis);
      }
      const timestamp = Date.parse(point?.ts || "");
      if (!Number.isFinite(timestamp)) return null;
      const future = providerFutureAxisIndex(axis).byTimestamp.get(timestamp);
      return Number.isSafeInteger(future?.absoluteIndex)
        ? future.absoluteIndex - Number(axis?.start || 0)
        : null;
    }

    function maxScreenIndexForAxis(axis) {
      const bars = axis?.bars || [];
      if (!bars.length) return 0;
      const fallback = Math.max(
        bars.length + drawingAxisRightGapBars(axis) - 1,
        bars.length - 1,
      );
      const width = Number(axis?.width);
      const xStep = Number(axis?.xStep);
      const pad = axis?.pad || {};
      const padRight = Number(pad.right);
      if (!Number.isFinite(width) || !Number.isFinite(xStep) || xStep <= 0 || typeof axis?.x !== "function") {
        return fallback;
      }
      const right = width - (Number.isFinite(padRight) ? padRight : PRICE_AXIS_WIDTH);
      const lastIndex = bars.length - 1;
      const lastCenter = axis.x(lastIndex);
      if (!Number.isFinite(right) || !Number.isFinite(lastCenter)) return fallback;
      const futureSlots = Math.max(Math.floor((right - lastCenter) / xStep), 0);
      return Math.max(lastIndex + futureSlots, lastIndex);
    }

    function chartPointFromLocal(localX, localY) {
      const geo = priceChartGeometry();
      if (!geo) return null;
      const rawPrice = geo.maxP - ((localY - geo.pad.top) / geo.priceH) * (geo.maxP - geo.minP);
      const axis = drawingAxisFromGeo(geo);
      const hit = chartVisualHitForLocalX(geo.bars, geo.pad, geo.xStep, localX);
      if (hit.gap || !Number.isSafeInteger(hit.index)) return null;
      let index = hit.index;
      const maxDrawingIndex = maxScreenIndexForAxis(axis);
      index = clamp(index, 0, maxDrawingIndex);
      const absoluteIndex = geo.start + index;
      const historicalBar = index < geo.bars.length ? geo.bars[index] : null;
      const future = historicalBar
        ? null
        : providerFutureAxisIndex(axis).byAbsoluteIndex.get(absoluteIndex);
      const storedBarSlot = historicalBar
        ? barSlotForAbsoluteIndex(geo, absoluteIndex)
        : future?.barSlot;
      const barSlot = Number.isFinite(storedBarSlot)
        ? Number(storedBarSlot)
        : historicalBar ? absoluteIndex * axisSlotStep(axis) : null;
      if (!Number.isFinite(barSlot)) return null;
      let point = {
        ts: historicalBar?.ts || future?.ts,
        barSlot,
        price: rawPrice,
        snapped: false,
        future: index >= geo.bars.length,
      };
      if (!state.drawing.magnet) return point;
      const candleStepPx = Math.max(Number(geo.xStep) || 0, 0.5);
      const snapRadiusPx = clamp(14 + Math.max(0, 4 - candleStepPx) * 4, 14, 28);
      const candidateRadiusBars = drawingMagnetCandidateRadiusBars(geo.xStep, snapRadiusPx);
      const candidateStart = Math.max(0, index - candidateRadiusBars);
      const candidateEnd = Math.min(geo.bars.length - 1, index + candidateRadiusBars);
      let best = null;
      for (let i = candidateStart; i <= candidateEnd; i += 1) {
        const bar = geo.bars[i];
        const candidateAbsoluteIndex = geo.start + i;
        const storedCandidateBarSlot = barSlotForAbsoluteIndex(
          geo,
          candidateAbsoluteIndex,
        );
        const candidateBarSlot = Number.isFinite(storedCandidateBarSlot)
          ? Number(storedCandidateBarSlot)
          : candidateAbsoluteIndex * axisSlotStep(axis);
        if (
          !hasPriceBar(bar)
          || !barIsConfirmedClosed(bar)
          || bar?.authoritative === false
          || !Number.isFinite(candidateBarSlot)
        ) continue;
        for (const [kind, price] of [["H", bar.high], ["L", bar.low]]) {
          const dx = geo.x(i) - localX;
          const dy = geo.y(price) - localY;
          const distance = Math.hypot(dx, dy);
          const temporalDistance = Math.abs(i - index);
          if (
            distance <= snapRadiusPx
            && (
              !best
              || distance < best.distance
              || (distance === best.distance && temporalDistance < best.temporalDistance)
            )
          ) {
            best = {
              distance,
              temporalDistance,
              ts: bar.ts,
              barSlot: candidateBarSlot,
              price,
              kind,
            };
          }
        }
      }
      return best ? { ts: best.ts, barSlot: best.barSlot, price: best.price, snapped: true, snapKind: best.kind } : point;
    }

    function drawingPointFromLocal(localX, localY) {
      const geo = priceChartGeometry();
      if (!geo) return null;
      const rawPrice = geo.maxP - ((localY - geo.pad.top) / geo.priceH) * (geo.maxP - geo.minP);
      const axis = drawingAxisFromGeo(geo);
      const hit = chartVisualHitForLocalX(geo.bars, geo.pad, geo.xStep, localX);
      if (hit.gap || !Number.isSafeInteger(hit.index)) return null;
      let index = hit.index;
      index = clamp(index, 0, maxScreenIndexForAxis(axis));
      const absoluteIndex = geo.start + index;
      const historicalBar = index < geo.bars.length ? geo.bars[index] : null;
      const historicalConfirmed = historicalBar
        && barIsConfirmedClosed(historicalBar)
        && historicalBar.authoritative !== false;
      let point = historicalConfirmed
        ? { ts: historicalBar.ts, price: rawPrice, snapped: false }
        : null;
      if (!point) {
        const allBars = Array.isArray(geo.allBars) ? geo.allBars : geo.bars;
        const laterConfirmed = allBars.slice(Math.max(absoluteIndex + 1, 0)).some(bar => (
          barIsConfirmedClosed(bar) && bar?.authoritative !== false
        ));
        if (laterConfirmed) return null;
        const latest = latestDrawingConfirmedBar(axis);
        const latestLogicalIndex = latestDrawingLogicalIndex(axis);
        const latestScreenIndex = drawingScreenIndexForLogicalIndex(latestLogicalIndex, axis);
        const barOffset = Math.round(index - latestScreenIndex);
        if (!latest || !Number.isSafeInteger(barOffset) || barOffset <= 0) return null;
        point = {
          anchorTs: latest.ts,
          barOffset,
          price: rawPrice,
          snapped: false,
        };
      }
      if (!state.drawing.magnet) return point;
      const candleStepPx = Math.max(Number(geo.xStep) || 0, 0.5);
      const snapRadiusPx = clamp(14 + Math.max(0, 4 - candleStepPx) * 4, 14, 28);
      const candidateRadiusBars = drawingMagnetCandidateRadiusBars(geo.xStep, snapRadiusPx);
      const candidateStart = Math.max(0, index - candidateRadiusBars);
      const candidateEnd = Math.min(geo.bars.length - 1, index + candidateRadiusBars);
      let best = null;
      for (let i = candidateStart; i <= candidateEnd; i += 1) {
        const bar = geo.bars[i];
        if (
          !hasPriceBar(bar)
          || !barIsConfirmedClosed(bar)
          || bar?.authoritative === false
        ) continue;
        for (const [kind, price] of [["H", bar.high], ["L", bar.low]]) {
          const dx = geo.x(i) - localX;
          const dy = geo.y(price) - localY;
          const distance = Math.hypot(dx, dy);
          const temporalDistance = Math.abs(i - index);
          if (
            distance <= snapRadiusPx
            && (
              !best
              || distance < best.distance
              || (distance === best.distance && temporalDistance < best.temporalDistance)
            )
          ) {
            best = { distance, temporalDistance, ts: bar.ts, price, kind };
          }
        }
      }
      return best
        ? { ts: best.ts, price: best.price, snapped: true, snapKind: best.kind }
        : point;
    }

    function lineDashForStyle(style) {
      if (Array.isArray(style)) return style.map(Number).filter(Number.isFinite);
      const normalized = String(style || "solid").trim().toLowerCase();
      if (["dash", "dashed", "--"].includes(normalized)) return [10, 7];
      if (["dot", "dotted", ":"].includes(normalized)) return [2, 7];
      return [];
    }

    function isWavyLineStyle(style) {
      return ["wave", "wavy", "sine"].includes(String(style || "").trim().toLowerCase());
    }

    function drawLinePathForStyle(ctx, x1, y1, x2, y2, style) {
      if (!isWavyLineStyle(style)) {
        ctx.moveTo(x1, y1);
        ctx.lineTo(x2, y2);
        return;
      }
      const dx = x2 - x1;
      const dy = y2 - y1;
      const length = Math.hypot(dx, dy);
      if (!Number.isFinite(length) || length < 2) {
        ctx.moveTo(x1, y1);
        ctx.lineTo(x2, y2);
        return;
      }
      const nx = -dy / length;
      const ny = dx / length;
      const amplitude = 4.5;
      const halfWave = 6;
      ctx.moveTo(x1, y1);
      for (let offset = 0, phase = 1; offset < length; offset += halfWave, phase *= -1) {
        const endOffset = Math.min(offset + halfWave, length);
        const controlOffset = (offset + endOffset) / 2;
        const controlX = x1 + (dx * controlOffset) / length + nx * amplitude * phase;
        const controlY = y1 + (dy * controlOffset) / length + ny * amplitude * phase;
        const endX = x1 + (dx * endOffset) / length;
        const endY = y1 + (dy * endOffset) / length;
        ctx.quadraticCurveTo(controlX, controlY, endX, endY);
      }
    }

    function lineCapForStyle(style) {
      if (isWavyLineStyle(style)) return "round";
      const dash = lineDashForStyle(style);
      return dash.length && dash[0] <= 2 ? "round" : "butt";
    }

    function chartGuideEmaValues(snapshot, length) {
      const guides = snapshot?.chart_guides?.ema;
      const values = guides?.[String(length)];
      const bars = snapshot?.bars || [];
      return Array.isArray(values) && values.length === bars.length ? values : null;
    }

    function chartGuideVwapSeries(snapshot) {
      const values = snapshot?.chart_guides?.vwap;
      const bars = snapshot?.bars || [];
      return Array.isArray(values) && values.length === bars.length ? values : null;
    }

    function globalEmaLength(key, fallback, low = 2, high = 1000) {
      const value = Number(state.indicators?.globalDefaults?.[key]);
      if (!Number.isFinite(value) || value < low || value > high) return fallback;
      return clamp(Math.round(value), low, high);
    }

    function emaPullbackLength() { return globalEmaLength("emaPullback", 20, 5, 300); }
    function emaTrendLength() { return globalEmaLength("emaSlow", 55, 10, 400); }
    function emaMagnetLength() { return globalEmaLength("emaMagnet", EMA233_LEN, 50, 1000); }

    function latestEmaValue(snapshot, length = emaMagnetLength()) {
      const guided = chartGuideEmaValues(snapshot, length);
      if (!guided) return null;
      const value = latestFiniteValue(guided)?.value;
      return Number.isFinite(value) ? value : null;
    }

    function alertDynamicLevel(alert, snapshot = state.snapshot) {
      const kind = String(alert?.kind || "").toLowerCase();
      if (kind === "ema233_touch") {
        return latestEmaValue(snapshot, emaMagnetLength());
      }
      if (kind === "vsa_fuel") {
        return currentSnapshotPrice(snapshot);
      }
      return Number(alert?.price);
    }

    function emaTouchState(alert, snapshot = state.snapshot) {
      const level = alertDynamicLevel(alert, snapshot);
      if (!Number.isFinite(level)) return null;
      const tolerance = Number(alert?.lastTolerance);
      return {
        level,
        tolerance: Number.isFinite(tolerance) ? tolerance : null,
      };
    }
    function drawEmaLine(ctx, snapshot, visible, x, y, length, options = {}) {
      if (!visible.bars.length) return;
      const allValues = chartGuideEmaValues(snapshot, length);
      if (!allValues) return;
      const values = allValues.slice(visible.start, visible.end);
      const lineColor = calibratedCanvasColor(options.color || css("--ema233"), { minRatio: 2.35 });
      ctx.save();
      ctx.strokeStyle = lineColor;
      ctx.lineWidth = clamp(Number(options.width) || 2, 1, 4);
      ctx.setLineDash(lineDashForStyle(options.style || "solid"));
      ctx.beginPath();
      let started = false;
      let hasSegment = false;
      values.forEach((value, index) => {
        const numericValue = finiteSeriesNumber(value);
        if (numericValue === null) {
          started = false;
          return;
        }
        const px = x(index);
        const py = y(numericValue);
        if (!started) {
          ctx.moveTo(px, py);
          started = true;
        } else {
          ctx.lineTo(px, py);
          hasSegment = true;
        }
      });
      if (hasSegment) ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = calibratedCanvasColor(lineColor, { minRatio: 3.6 });
      const firstValue = firstFiniteValue(values);
      if (firstValue) {
        ctx.font = "9px -apple-system, BlinkMacSystemFont, sans-serif";
        ctx.textAlign = "left";
        ctx.textBaseline = "middle";
        ctx.fillText(options.label || `EMA ${length}`, x(firstValue.index) + 8, y(firstValue.value) + (options.labelOffsetY || -5));
      }
      ctx.restore();
    }

    function drawEma233(ctx, snapshot, visible, pad, x, y) {
      const cfg = state.indicators.ema233;
      if (!visible.bars.length) return;
      if (!cfg.allEnabled) return;
      const pullbackLen = emaPullbackLength();
      const trendLen = emaTrendLength();
      const magnetLen = emaMagnetLength();
      if (cfg.ema20Enabled) {
        drawEmaLine(ctx, snapshot, visible, x, y, pullbackLen, {
          color: cfg.ema20Color || css("--ema20") || "#facc15",
          width: Math.max(1, Math.min(Number(cfg.width) || 2, 2)),
          style: cfg.style,
          label: `EMA ${pullbackLen}`,
          labelOffsetY: 11,
        });
      }
      if (cfg.ema50Enabled) {
        drawEmaLine(ctx, snapshot, visible, x, y, trendLen, {
          color: cfg.ema50Color || (isLightTheme() ? "#7c2d12" : "#22d3ee"),
          width: Math.max(1, Math.min(Number(cfg.width) || 2, 3)),
          style: cfg.style,
          label: `EMA ${trendLen}`,
          labelOffsetY: 18,
        });
      }
      if (cfg.enabled) {
        drawEmaLine(ctx, snapshot, visible, x, y, magnetLen, {
          color: cfg.color || css("--ema233") || "#38bdf8",
          width: cfg.width,
          style: cfg.style,
          label: `EMA ${magnetLen}`,
          labelOffsetY: -5,
        });
      }
    }

    function drawVwapCurve(ctx, snapshot, visible, x, y) {
      const cfg = state.indicators.vwap;
      if (!cfg.enabled || !visible.bars.length) return;
      const guided = chartGuideVwapSeries(snapshot);
      if (!guided) return;
      const series = guided.slice(visible.start, visible.end);
      const values = series.map(item => item.vwap);
      const lineColor = calibratedCanvasColor(cfg.color || css("--vwap") || "#22c55e", { minRatio: 2.35 });
      const bandColor = calibratedCanvasColor(cfg.bandColor || themeColor("blue"), { minRatio: 2.25 });
      ctx.save();
      if (cfg.bands) {
        const bandCount = clamp(Number(cfg.bandCount) || 2, 1, 3);
        ctx.lineWidth = 1;
        for (let band = bandCount; band >= 1; band -= 1) {
          const alpha = band === 1 ? 0.68 : band === 2 ? 0.48 : 0.32;
          ctx.strokeStyle = rgbaFromCssColor(bandColor, alpha);
          ctx.setLineDash([2, 5]);
          for (const side of [1, -1]) {
            ctx.beginPath();
            let bandStarted = false;
            let bandHasSegment = false;
            series.forEach((item, index) => {
              const vwap = finiteSeriesNumber(item.vwap);
              const sigma = finiteSeriesNumber(item.sigma);
              if (vwap === null || sigma === null) {
                bandStarted = false;
                return;
              }
              const px = x(index);
              const py = y(vwap + sigma * band * side);
              if (!bandStarted) {
                ctx.moveTo(px, py);
                bandStarted = true;
              } else {
                ctx.lineTo(px, py);
                bandHasSegment = true;
              }
            });
            if (bandHasSegment) ctx.stroke();
          }
        }
      }
      ctx.strokeStyle = lineColor;
      ctx.lineWidth = clamp(Number(cfg.width) || 1, 1, 3);
      ctx.setLineDash(lineDashForStyle(cfg.style));
      ctx.beginPath();
      let started = false;
      let hasSegment = false;
      values.forEach((value, index) => {
        const numericValue = finiteSeriesNumber(value);
        if (numericValue === null) {
          started = false;
          return;
        }
        const px = x(index);
        const py = y(numericValue);
        if (!started) {
          ctx.moveTo(px, py);
          started = true;
        } else {
          ctx.lineTo(px, py);
          hasSegment = true;
        }
      });
      if (hasSegment) ctx.stroke();
      ctx.setLineDash([]);
      const lastValue = latestFiniteValue(values);
      if (lastValue) {
        ctx.fillStyle = lineColor;
        ctx.font = "9px -apple-system, BlinkMacSystemFont, sans-serif";
        ctx.fillText("VWAP", x(lastValue.index) + 8, y(lastValue.value) - 5);
      }
      ctx.restore();
    }
