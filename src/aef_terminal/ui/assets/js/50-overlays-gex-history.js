const gexHistoryRenderLaneCache = new WeakMap();

function gexHistorySnapshotTs(item) {
      return gexHistoryTimestampMs(item);
    }

    function gexHistoryRenderLane(gex) {
      const historyView = gexHistoryView(state.indicators?.gexContext?.historyView);
      const captureMode = effectiveGexContextMode();
      const entitlement = gexMarketDataEntitlement(gex);
      if (!entitlement) return [];
      const history = Array.isArray(gex?.history) ? gex.history : [];
      const cacheable = Boolean(
        gex
        && typeof gex === "object"
        && Object.isFrozen(gex)
        && Object.isFrozen(history)
      );
      const cached = cacheable
        ? gexHistoryRenderLaneCache.get(gex)
        : null;
      if (
        cached
        && cached.history === history
        && cached.historyView === historyView
        && cached.captureMode === captureMode
        && cached.entitlement === entitlement
      ) return cached.lane;
      const matchingLane = history.filter(item => (
        item?.capture_mode === captureMode
        && gexMarketDataEntitlement(item) === entitlement
      ));
      const latestTimestampMs = matchingLane.reduce(
        (latest, item) => Math.max(latest, gexHistorySnapshotTs(item) || 0),
        0,
      );
      const historyWindowMs = (historyView === "2d" ? 2 : 1) * CHART_VIEW_DAY_MS;
      const timeWindow = {
        start: latestTimestampMs - historyWindowMs,
        end: latestTimestampMs,
      };
      const lane = latestTimestampMs <= 0
        ? []
        : historyView === "last"
          ? matchingLane.slice(-1)
          : matchingLane.filter(item => gexHistoryIntersectsVisible(item, timeWindow));
      if (cacheable) {
        gexHistoryRenderLaneCache.set(gex, {
          history,
          historyView,
          captureMode,
          entitlement,
          lane,
        });
      }
      return lane;
    }

    function gexHistorySnapshotEndTs(item) {
      const startMs = gexHistorySnapshotTs(item);
      if (item?.projection_mode === "sample") return startMs;
      const exactEnd = gexNullableNumber(item?.valid_until_unix_ms);
      return exactEnd;
    }

    function xForGexHistoryTime(lookup, timestamp, xStep, x) {
      const times = Array.isArray(lookup?.times) ? lookup.times : [];
      if (!times.length || !Number.isFinite(timestamp)) return null;
      const indexOffset = Number.isFinite(Number(lookup?.indexOffset))
        ? Number(lookup.indexOffset)
        : 0;
      const xAt = index => x(index - indexOffset);
      if (timestamp < times[0]) return null;
      if (timestamp >= times[times.length - 1]) return xAt(times.length - 1);
      let low = 0;
      let high = times.length - 1;
      while (low + 1 < high) {
        const middle = Math.floor((low + high) / 2);
        if (times[middle] <= timestamp) low = middle;
        else high = middle;
      }
      const leftTs = times[low];
      const rightTs = times[high];
      if (!Number.isFinite(leftTs) || !Number.isFinite(rightTs) || rightTs <= leftTs) {
        return null;
      }
      return xAt(low) + ((timestamp - leftTs) / (rightTs - leftTs)) * xStep;
    }

    function gexHistoryIntersectsVisible(item, timeWindow) {
      if (!timeWindow) return true;
      const startMs = gexHistorySnapshotTs(item);
      const endMs = gexHistorySnapshotEndTs(item);
      if (!Number.isFinite(startMs) || !Number.isFinite(endMs)) return true;
      return Math.max(startMs, endMs) >= timeWindow.start && Math.min(startMs, endMs) <= timeWindow.end;
    }

    function visibleGexHistoryCount(history, visible) {
      if (!Array.isArray(history) || !history.length) return 0;
      const timeWindow = visibleTimeWindowForBars(visible?.bars || [], 3);
      if (!timeWindow) return history.length;
      let count = 0;
      for (let index = 0; index < history.length; index += 1) {
        if (gexHistoryIntersectsVisible(history[index], timeWindow)) count += 1;
      }
      return count;
    }

    function gexTrailQuality(history, visible, xStep, cfg) {
      const requested = cfg?.displayLevels;
      const displayLimit = GEX_DISPLAY_LEVEL_COUNTS.includes(requested)
        ? requested
        : GEX_DEFAULT_DISPLAY_LEVEL_COUNT;
      let maxLevels = 1;
      // activeGexContext has already admitted every history row. The quality
      // budget needs only the bounded row count; re-sorting every strike here
      // would repeat canonical admission before every raster-cache lookup.
      for (const snapshot of history) {
        maxLevels = Math.max(
          maxLevels,
          Math.min(
            Array.isArray(snapshot?.levels) ? snapshot.levels.length : 0,
            displayLimit,
          ),
        );
      }
      const visibleHistoryCount = Math.max(visibleGexHistoryCount(history, visible), 1);
      const work = Math.max(visibleHistoryCount * maxLevels, 1);
      const visibleCount = visible?.bars?.length || 0;
      const zoomedOut = visibleCount > 220 || xStep < 5;
      const dense = work > 760 || visibleCount > 320 || xStep < 3.8;
      const targetSegments = dense ? 72 : zoomedOut ? 110 : 180;
      return {
        sampleStep: Math.max(1, Math.ceil(visibleHistoryCount / targetSegments)),
        labels: visibleCount <= 260 && xStep >= 5,
        shadows: canvasExpensiveEffectsEnabled() && !dense && visibleCount <= 340 && xStep >= 3.8,
      };
    }

    function gexTrailCacheKey(gex, snapshot, visible, pad, priceH, width, xStep, scaleInfo, quality, history) {
      const first = history[0] || {};
      const last = history[history.length - 1] || {};
      const cfg = state.indicators.gexContext;
      const ratio = window.devicePixelRatio || 1;
      const theme = state.settings.theme || "";
      const minuteBucket = Math.floor(Date.now() / 60000);
      return [
        gex?.instrument_id || "",
        gex?.route_fingerprint || "",
        effectiveGexContextMode(),
        gexMarketDataEntitlement(gex),
        history.length,
        gex?.history_revision || "",
        first.captured_at || "",
        first.capture_mode || "",
        first.projection_mode || "",
        first.projection_bucket_minutes ?? "",
        last.captured_at || "",
        last.capture_mode || "",
        last.projection_mode || "",
        last.projection_bucket_minutes ?? "",
        last.valid_until_unix_ms || "",
        visible.start,
        visible.end,
        visible.bars?.length || 0,
        Math.round(width),
        Math.round(priceH),
        Math.round(pad.left),
        Math.round(pad.right),
        Math.round(pad.top),
        Math.round(Number(scaleInfo?.minP || 0) * 100),
        Math.round(Number(scaleInfo?.maxP || 0) * 100),
        Math.round(xStep * 100),
        Math.round(smoothOffsetBars() * 1000),
        Math.round(ratio * 100),
        String(cfg.zones),
        String(gexZoneMode(cfg.zoneStyle)),
        String(gexHistoryView(cfg.historyView)),
        String(quality.labels),
        String(quality.shadows),
        quality.sampleStep,
        GEX_DISPLAY_LEVEL_COUNTS.includes(cfg.displayLevels) ? cfg.displayLevels : GEX_DEFAULT_DISPLAY_LEVEL_COUNT,
        theme,
        minuteBucket,
      ].join("|");
    }

    function drawCachedGexHistoryTrail(ctx, gex, snapshot, visible, pad, priceH, width, y, xStep, scaleInfo) {
      if (shouldHidePerIndicatorPlanDetail()) return;
      const history = gexHistoryRenderLane(gex);
      if (!history.length || !snapshot?.bars?.length) return;
      const cfg = state.indicators.gexContext;
      const visibleBars = Array.isArray(visible?.bars) ? visible.bars : [];
      if (!visibleBars.length) return;
      const quality = gexTrailQuality(history, visible, xStep, cfg);
      const key = gexTrailCacheKey(gex, snapshot, visible, pad, priceH, width, xStep, scaleInfo, quality, history);
      const ratio = window.devicePixelRatio || 1;
      const canvasHeight = Math.max(Math.ceil(Number(scaleInfo?.height) || pad.top + priceH), 1);
      const canvasWidth = Math.max(Math.ceil(width), 1);
      const pixelWidth = Math.floor(canvasWidth * ratio);
      const pixelHeight = Math.floor(canvasHeight * ratio);
      if (
        !gexTrailLayerCache
        || gexTrailLayerCache.key !== key
        || gexTrailLayerCache.canvas.width !== pixelWidth
        || gexTrailLayerCache.canvas.height !== pixelHeight
      ) {
        const layer = document.createElement("canvas");
        layer.width = pixelWidth;
        layer.height = pixelHeight;
        const layerCtx = layer.getContext("2d");
        layerCtx.scale(ratio, ratio);
        drawGexHistoryTrail(layerCtx, gex, snapshot, visible, pad, priceH, width, y, xStep, {
          labels: quality.labels,
          sampleStep: quality.sampleStep,
          shadows: quality.shadows,
          tooltips: false,
          history,
        });
        gexTrailLayerCache = { key, canvas: layer, width: canvasWidth, height: canvasHeight, quality };
      }
      ctx.drawImage(gexTrailLayerCache.canvas, 0, 0, gexTrailLayerCache.width, gexTrailLayerCache.height);
    }

    function drawGexHistoryTrail(ctx, gex, snapshot, visible, pad, priceH, width, y, xStep, options = {}) {
      const history = Array.isArray(options.history) ? options.history : gexHistoryRenderLane(gex);
      if (!history.length || !snapshot?.bars?.length) return;
      const bars = visible?.bars || [];
      if (!bars.length) return;
      const cfg = state.indicators.gexContext;
      const drawLabels = options.labels !== false;
      const drawShadows = options.shadows !== false;
      const allowTooltips = options.tooltips !== false;
      const sampleStep = Math.max(1, Math.round(Number(options.sampleStep) || 1));
      const plotLeft = pad.left;
      const plotRight = width - pad.right;
      const nowMs = Date.now();
      const ordered = history
        .map(item => ({ ...item, tsMs: gexHistoryTimestampMs(item) }))
        .filter(item => Number.isFinite(item.tsMs))
        .sort((a, b) => a.tsMs - b.tsMs);
      if (!ordered.length) return;
      const fadeSpanMs = Math.max(nowMs - ordered[0].tsMs, 60_000);
      const timeWindow = visibleTimeWindowForBars(bars, 3);
      const visibleOrdered = timeWindow
        ? ordered.filter(item => gexHistoryIntersectsVisible(item, timeWindow))
        : ordered;
      if (!visibleOrdered.length) return;
      const orderedForDraw = sampleStep <= 1
        ? visibleOrdered
        : visibleOrdered.filter((_, index) => index === visibleOrdered.length - 1 || (visibleOrdered.length - 1 - index) % sampleStep === 0);
      const currentLevels = Array.isArray(gex?.levels) ? gex.levels : [];
      ctx.save();
      ctx.lineCap = "butt";
      const x = chartX(pad, xStep);
      const timeLookup = genericOverlayLookupForVisible(visible, bars, snapshot.bars);
      for (let snapIndex = 0; snapIndex < orderedForDraw.length; snapIndex += 1) {
        const snap = orderedForDraw[snapIndex];
        const endMs = gexHistorySnapshotEndTs(snap);
        if (!Number.isFinite(endMs)) continue;
        const ageRatio = clamp((nowMs - snap.tsMs) / fadeSpanMs, 0, 1);
        const x0 = xForGexHistoryTime(timeLookup, snap.tsMs, xStep, x);
        const x1 = xForGexHistoryTime(timeLookup, endMs, xStep, x);
        if (x0 === null || x1 === null) continue;
        let left = clamp(Math.min(x0, x1), plotLeft, plotRight);
        let right = clamp(Math.max(x0, x1), plotLeft, plotRight);
        if (snap.projection_mode === "sample" || right - left < 1) {
          const anchor = clamp(x0, plotLeft, plotRight);
          left = Math.max(plotLeft, anchor - 1);
          right = Math.min(plotRight, anchor + 1);
          if (right - left < 1) {
            if (anchor <= plotLeft) right = Math.min(plotRight, plotLeft + 1);
            else left = Math.max(plotLeft, plotRight - 1);
          }
        }
        if (right <= left) continue;
        const levels = gexVisibleLevels(snap.levels || [], cfg);
        if (drawLabels && right - left > 34) {
          ctx.font = `10px ${CANVAS_MONO_FONT}`;
          ctx.textAlign = "left";
          ctx.textBaseline = "top";
          const stampColor = state.settings.theme === "light" ? "#475569" : "#94a3b8";
          ctx.fillStyle = rgbaFromCssColor(stampColor, clamp(0.20 + (1 - ageRatio) * 0.32, 0.20, 0.52));
          ctx.fillText(`GEX ${gexHistoryCaptureLabel(snap)} ${gexHistoryTimeLabel(snap)}`, left + 3, pad.top + 16 + (snapIndex % 3) * 12);
        }
        const gammaFlip = gexNullableNumber(snap.gamma_flip);
        if (gammaFlip !== null) {
          const flipY = y(gammaFlip);
          if (flipY >= pad.top && flipY <= pad.top + priceH) {
            const flipColor = themeColor("purple", 0.95);
            const flipAlpha = clamp(0.18 + (1 - ageRatio) * 0.34, 0.18, 0.52);
            ctx.shadowBlur = 0;
            ctx.strokeStyle = rgbaFromCssColor(flipColor, flipAlpha);
            ctx.lineWidth = 1.4;
            ctx.setLineDash([5, 5]);
            ctx.beginPath();
            ctx.moveTo(left, flipY);
            ctx.lineTo(right, flipY);
            ctx.stroke();
            if (drawLabels && right - left > 56) {
              ctx.setLineDash([]);
              ctx.font = `10px ${CANVAS_MONO_FONT}`;
              ctx.textAlign = "right";
              ctx.textBaseline = "middle";
              ctx.fillStyle = rgbaFromCssColor(flipColor, clamp(flipAlpha + 0.16, 0.28, 0.68));
              ctx.fillText(`ZERO G ${fmt(gammaFlip)}`, width - pad.right - 4, flipY - 6);
            }
            if (allowTooltips) {
              registerCanvasTooltip(
                "price",
                (left + right) / 2,
                flipY,
                Math.max(right - left, 12),
                14,
                () => [
                  `Estimated Zero Gamma ${fmt(gammaFlip)}`,
                  `Capture: ${gexHistoryCaptureLabel(snap)}`,
                  `Переоценённая граница режима · ${gexHistoryTimeLabel(snap)}`,
                  "Не entry и не support/resistance.",
                ].join("\n"),
                null,
                canvasLayerZIndex("tooltips"),
                GEX_TOOLTIP_SOURCE_HISTORY,
              );
            }
          }
        }
        for (const level of levels) {
          const price = gexNullableNumber(level.price);
          if (price === null) continue;
          const yy = y(price);
          if (yy < pad.top || yy > pad.top + priceH) continue;
          const strength = gexLevelStrengthValue(level);
          const renderStrength = strength ?? 0;
          const alpha = clamp((0.06 + renderStrength * 0.24) * (1 - ageRatio * 0.72), 0.035, 0.30);
          const color = gexKindColor(level, 0.9);
          const segmentHalf = gexNullableNumber(level.zone_half_width);
          if (segmentHalf === null) continue;
          const top = y(price + segmentHalf);
          const bottom = y(price - segmentHalf);
          if (cfg.zones && bottom >= pad.top && top <= pad.top + priceH) {
            const glowAlpha = clamp((0.025 + renderStrength * 0.16) * (1 - ageRatio * 0.65), 0.012, 0.18);
            const grad = ctx.createLinearGradient(0, top, 0, bottom);
            grad.addColorStop(0, rgbaFromCssColor(color, 0));
            grad.addColorStop(0.46, rgbaFromCssColor(color, glowAlpha));
            grad.addColorStop(0.54, rgbaFromCssColor(color, glowAlpha));
            grad.addColorStop(1, rgbaFromCssColor(color, 0));
            ctx.fillStyle = grad;
            ctx.fillRect(left, top, right - left, Math.max(bottom - top, 1));
          }
          ctx.shadowBlur = drawShadows ? clamp(2 + renderStrength * 16 * (1 - ageRatio * 0.45), 0, 18) : 0;
          ctx.shadowColor = rgbaFromCssColor(color, clamp(alpha + renderStrength * 0.22, 0.10, 0.46));
          ctx.strokeStyle = rgbaFromCssColor(color, alpha);
          ctx.lineWidth = clamp(1 + renderStrength * 1.6, 1, 2.4);
          ctx.setLineDash([10, 6]);
          ctx.beginPath();
          ctx.moveTo(left, yy);
          ctx.lineTo(right, yy);
          ctx.stroke();
          ctx.shadowBlur = 0;
          const intensity = gexLevelIntensity(level);
          if (drawLabels && right - left > 56 && (renderStrength >= 0.18 || intensity)) {
            ctx.setLineDash([]);
            ctx.font = `10px ${CANVAS_MONO_FONT}`;
            ctx.textAlign = "right";
            ctx.textBaseline = "middle";
            ctx.fillStyle = rgbaFromCssColor(color, clamp(alpha + 0.20, 0.22, 0.68));
            const label = [compactLineLabel(level.kind || "GEX"), gexLevelPowerLabel(level, strength), intensity].filter(Boolean).join(" ");
            ctx.fillText(label, width - pad.right - 4, yy - 6);
          }
          if (allowTooltips) {
            registerCanvasTooltip(
              "price",
              (left + right) / 2,
              yy,
              Math.max(right - left, 12),
              14,
              () => gexLevelTooltip(snap, level, {
                title: `GEX ${gexHistoryCaptureLabel(snap)} ${gexHistoryTimeLabel(snap)} · ${level.kind || "level"} ${fmt(price)}`,
                strength,
                temporalScope: "historical",
                snapshotLabel: gexHistoryTimeLabel(snap),
                currentLevel: gexLevelForExactStrike(currentLevels, price),
              }),
              null,
              canvasLayerZIndex("tooltips"),
              GEX_TOOLTIP_SOURCE_HISTORY,
            );
          }
        }
      }
      ctx.restore();
    }
