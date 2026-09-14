    const genericOverlayLookupCache = new WeakMap();

    const INDICATOR_OVERLAY_ROLE_TONES = Object.freeze({
      entry: "warning",
      entry_fill: "warning",
      stop: "negative",
      stop_hit: "negative",
      target: "positive",
      target_hit: "positive",
      trail: "info",
      trail_stop_hit: "info",
      plan_cancel: "warning",
      futures_roll: "info",
      futures_roll_from: "info",
      futures_roll_to: "info",
    });

    function indicatorOverlayColor(item, alpha = 1, fallback = "neutral") {
      const explicitColor = String(item?.color || "").trim();
      if (explicitColor) return rgbaFromCssColor(explicitColor, clamp(Number(alpha), 0, 1));
      const role = String(item?.role || "").trim().toLowerCase();
      const semanticTone = String(item?.tone || INDICATOR_OVERLAY_ROLE_TONES[role] || item?.direction || fallback).trim().toLowerCase();
      const themeTone = ["positive", "long", "up", "bullish", "ok", "clear"].includes(semanticTone)
        ? "up"
        : ["negative", "short", "down", "bearish", "danger", "bad"].includes(semanticTone)
          ? "down"
          : ["warning", "caution", "flat"].includes(semanticTone)
            ? "gold"
            : semanticTone === "info"
              ? "blue"
              : "neutral";
      return themeColor(themeTone, alpha);
    }

    function indicatorBoxPresentation(item) {
      const setupState = String(item?.setup || item?.metrics?.state || "").trim().toUpperCase();
      const setupFillAlpha = setupState === "ARMED" ? 0.34 : setupState === "WATCH" ? 0.24 : setupState === "GO" ? 0.18 : 0.12;
      const explicitOpacity = item?.opacity === null || item?.opacity === undefined
        ? Number.NaN
        : Number(item.opacity);
      const fillAlpha = Number.isFinite(explicitOpacity)
        ? clamp(explicitOpacity, 0, 1)
        : setupFillAlpha;
      const explicitBorderOpacity = Number(item?.border_opacity);
      const borderAlpha = Number.isFinite(explicitBorderOpacity)
        ? clamp(explicitBorderOpacity, 0, 1)
        : setupState ? 0.52 : 0.84;
      const explicitPatternOpacity = Number(item?.pattern_opacity);
      const patternAlpha = Number.isFinite(explicitPatternOpacity)
        ? clamp(explicitPatternOpacity, 0, 1)
        : 0.22;
      return {
        fillAlpha,
        borderAlpha,
        patternAlpha,
        style: item?.style || (["ARMED", "GO"].includes(setupState) ? "solid" : "dotted"),
        width: Number(item?.width) || 1,
        animatedBorder: typeof item?.animated_border === "boolean"
          ? item.animated_border
          : false,
        pattern: String(item?.pattern || ""),
      };
    }

    function indicatorOverlayBadgeText(value) {
      if (!value || typeof value !== "object") return String(value || "");
      const code = String(value.code || "").toLowerCase();
      if (code === "setup_zone") {
        const action = String(value.action || value.state || "").toUpperCase();
        const state = action === "ARM"
          ? "ARMED"
          : action === "BLOCK"
            ? "BLOCKED"
            : action;
        const arrow = indicatorDirectionArrow({ direction: value.direction });
        return [value.mode, state, arrow].filter(Boolean).map(part => String(part).toUpperCase()).join(" ");
      }
      if (code === "trend_context") {
        const alignment = String(value.alignment || "neutral").toLowerCase();
        const label = alignment === "aligned"
          ? "TREND+"
          : alignment === "pullback"
            ? "PULLBACK"
            : alignment === "countertrend"
              ? "COUNTER"
              : "RANGE";
        return label;
      }
      if (code === "smc_zone") {
        const kind = String(value.kind || "smc").toLowerCase();
        const label = kind === "fvg" ? "FVG" : kind === "order_block" ? "OB" : "SMC";
        const arrow = String(value.direction || "").toLowerCase() === "long" ? "↑" : String(value.direction || "").toLowerCase() === "short" ? "↓" : "";
        return `${label}${arrow}${value.retained === true ? "✓" : ""}`;
      }
      if (code === "smc_rebound") {
        const arrow = String(value.direction || "").toLowerCase() === "long" ? "↑" : String(value.direction || "").toLowerCase() === "short" ? "↓" : "";
        return `SMC${arrow}`;
      }
      if (code === "impulse_pullback") {
        const direction = String(value.direction || "").toLowerCase();
        return direction === "short" ? "PB-S" : direction === "long" ? "PB-L" : "PB";
      }
      return code.replace(/_/g, " ").toUpperCase();
    }

    function genericOverlayLookupForVisible(visible, bars, allBars = bars) {
      const axisBars = Array.isArray(allBars) && allBars.length ? allBars : bars;
      const indexOffset = Number.isFinite(Number(visible?.start)) ? Number(visible.start) : 0;
      const axisVersion = Number(chartRenderVersions.bars);
      const firstTimestamp = axisBars[0]?.ts || "";
      const lastTimestamp = axisBars[axisBars.length - 1]?.ts || "";
      let cached = genericOverlayLookupCache.get(axisBars);
      if (
        !cached
        || cached.axisVersion !== axisVersion
        || cached.length !== axisBars.length
        || cached.firstTimestamp !== firstTimestamp
        || cached.lastTimestamp !== lastTimestamp
      ) {
        const times = new Array(axisBars.length);
        const absoluteByTs = new Map();
        const absoluteByTimestamp = new Map();
        for (let index = 0; index < axisBars.length; index += 1) {
          const ts = axisBars[index]?.ts;
          const timestamp = Date.parse(ts || "");
          times[index] = timestamp;
          absoluteByTs.set(timestampKey(ts), index);
          if (Number.isFinite(timestamp)) absoluteByTimestamp.set(timestamp, index);
        }
        cached = {
          times,
          absoluteByTs,
          absoluteByTimestamp,
          axisVersion,
          length: axisBars.length,
          firstTimestamp,
          lastTimestamp,
        };
        genericOverlayLookupCache.set(axisBars, cached);
      }
      const byTs = {
        get(timestamp) {
          const absoluteIndex = cached.absoluteByTs.get(timestamp);
          return absoluteIndex === undefined ? undefined : absoluteIndex - indexOffset;
        },
        has(timestamp) {
          return cached.absoluteByTs.has(timestamp);
        },
      };
      return {
        times: cached.times,
        absoluteByTimestamp: cached.absoluteByTimestamp,
        byTs,
        indexOffset,
      };
    }

    function xForOverlayTime(lookup, timestamp, xStep, x) {
      const times = lookup?.times || [];
      if (!times.length) return null;
      const indexOffset = Number.isFinite(Number(lookup?.indexOffset)) ? Number(lookup.indexOffset) : 0;
      const xAt = index => x(index - indexOffset);
      const exactIndex = lookup?.absoluteByTimestamp?.get(timestamp);
      return exactIndex === undefined ? null : xAt(exactIndex);
    }

    function clipOverlayLineToPlotX(x1, y1, x2, y2, minX, maxX) {
      const dx = x2 - x1;
      if (Math.abs(dx) < 1e-9) {
        return x1 >= minX && x1 <= maxX ? { x1, y1, x2, y2 } : null;
      }
      const atMin = (minX - x1) / dx;
      const atMax = (maxX - x1) / dx;
      const startRatio = Math.max(0, Math.min(atMin, atMax));
      const endRatio = Math.min(1, Math.max(atMin, atMax));
      if (startRatio > endRatio) return null;
      return {
        x1: x1 + dx * startRatio,
        y1: y1 + (y2 - y1) * startRatio,
        x2: x1 + dx * endRatio,
        y2: y1 + (y2 - y1) * endRatio,
      };
    }

    function xForOverlayEnd(item, lookup, xStep, x) {
      const hasProjectedEndpoint = Object.prototype.hasOwnProperty.call(item || {}, "end_anchor_ts")
        || Object.prototype.hasOwnProperty.call(item || {}, "end_bar_offset");
      if (hasProjectedEndpoint) {
        const anchorTs = Date.parse(item?.end_anchor_ts || "");
        const barOffset = Number(item?.end_bar_offset);
        if (!Number.isFinite(anchorTs) || !Number.isInteger(barOffset) || barOffset < 1) return null;
        const anchorX = xForOverlayTime(lookup, anchorTs, xStep, x);
        return anchorX === null ? null : anchorX + barOffset * xStep;
      }
      const endTs = Date.parse(item?.end_ts || item?.ts || "");
      return Number.isFinite(endTs) ? xForOverlayTime(lookup, endTs, xStep, x) : null;
    }

    function clipOverlayBoxToPlotX(leftRaw, rightRaw, minX, maxX, xStep) {
      let left = clamp(Math.min(leftRaw, rightRaw), minX, maxX);
      let right = clamp(Math.max(leftRaw, rightRaw), minX, maxX);
      if (right - left >= 2) return { left, right };
      const center = clamp((leftRaw + rightRaw) / 2, minX, maxX);
      const halfWidth = Math.max(Math.min(Math.abs(Number(xStep) || 0) * 0.45, 8), 2);
      left = clamp(center - halfWidth, minX, maxX);
      right = clamp(center + halfWidth, minX, maxX);
      if (right - left < 2) {
        if (center <= minX) right = Math.min(minX + 2, maxX);
        else if (center >= maxX) left = Math.max(maxX - 2, minX);
      }
      return right - left >= 2 ? { left, right } : null;
    }

    function drawGenericOverlayBox(ctx, item, timeLookup, pad, priceH, width, xStep, x, y) {
      const startTs = Date.parse(item.start_ts || item.ts);
      const topPrice = Number(item.top);
      const bottomPrice = Number(item.bottom);
      if (![startTs, topPrice, bottomPrice].every(Number.isFinite)) return;
      const leftRaw = xForOverlayTime(timeLookup, startTs, xStep, x);
      const rightRaw = xForOverlayEnd(item, timeLookup, xStep, x);
      if (leftRaw === null || rightRaw === null) return;
      if (overlayRangeOutsidePlot(leftRaw, rightRaw, pad, width)) return;
      const clippedRange = clipOverlayBoxToPlotX(leftRaw, rightRaw, pad.left, width - pad.right, xStep);
      if (!clippedRange) return;
      const { left, right } = clippedRange;
      const top = y(Math.max(topPrice, bottomPrice));
      const bottom = y(Math.min(topPrice, bottomPrice));
      if (bottom < pad.top || top > pad.top + priceH) return;
      ctx.save();
      const presentation = indicatorBoxPresentation(item);
      const boxFill = item.bg || indicatorOverlayColor(item, presentation.fillAlpha, "info");
      const boxBorder = item.border || indicatorOverlayColor(item, presentation.borderAlpha, "info");
      ctx.fillStyle = boxFill;
      ctx.strokeStyle = boxBorder;
      ctx.lineWidth = presentation.width;
      const boxStyle = presentation.style;
      ctx.setLineDash(lineDashForStyle(boxStyle));
      if (presentation.animatedBorder) ctx.lineDashOffset = -((Date.now() / 80) % 24);
      ctx.fillRect(left, top, right - left, Math.max(bottom - top, 1));
      const boxPattern = presentation.pattern;
      if (boxPattern === "hatch") {
        const boxH = Math.max(bottom - top, 1);
        ctx.save();
        ctx.beginPath();
        ctx.rect(left, top, right - left, boxH);
        ctx.clip();
        ctx.setLineDash([]);
        ctx.strokeStyle = presentation.pattern === "hatch"
          ? item.pattern_color || indicatorOverlayColor(item, presentation.patternAlpha, "info")
          : rgbaFromCssColor(boxBorder, isLightTheme() ? 0.18 : 0.22);
        ctx.lineWidth = 0.65;
        for (let xx = left - boxH; xx < right + boxH; xx += 8) {
          ctx.beginPath();
          ctx.moveTo(xx, bottom);
          ctx.lineTo(xx + boxH, top);
          ctx.stroke();
        }
        ctx.restore();
      }
      ctx.strokeRect(left, top, right - left, Math.max(bottom - top, 1));
      const labelPosition = item.label_position || "";
      const labelFontSize = clamp(Number(item.label_font_size) || 9, 5, 14);
      const badgeLines = Array.isArray(item.badge_facts)
        ? item.badge_facts.map(indicatorOverlayBadgeText).filter(Boolean).slice(0, 2)
        : Array.isArray(item.badge_lines)
          ? item.badge_lines.filter(Boolean).slice(0, 2)
        : labelPosition === "center" && item.label
          ? [item.label]
          : [];
      if (badgeLines.length) {
        const boxW = right - left;
        const boxH = Math.max(bottom - top, 1);
        if (boxH >= 10 && boxW >= 18) {
          ctx.save();
          ctx.beginPath();
          ctx.rect(left, top, boxW, boxH);
          ctx.clip();
          ctx.setLineDash([]);
          ctx.font = `800 ${labelFontSize}px -apple-system, BlinkMacSystemFont, sans-serif`;
          ctx.textAlign = "center";
          ctx.textBaseline = "middle";
          const lineH = Math.min(labelFontSize + 1, Math.max(8, boxH / Math.max(badgeLines.length, 1)));
          const startY = (top + bottom) / 2 - ((badgeLines.length - 1) * lineH) / 2;
          const maxTextW = Math.max(boxW - 6, 8);
          badgeLines.forEach((line, index) => {
            let text = String(line);
            while (text.length > 1 && measureCanvasText(ctx, text) > maxTextW) text = text.slice(0, -1);
            if (text.length < String(line).length) text = `${text.slice(0, Math.max(text.length - 1, 0))}…`;
            const lineY = startY + index * lineH;
            ctx.lineWidth = canvasTextOutlineWidth(2.5);
            ctx.strokeStyle = canvasTextOutlineColor();
            ctx.fillStyle = boxBorder;
            drawCanvasText(ctx, text, (left + right) / 2, lineY, undefined, { outlineWidth: 2.5 });
          });
          ctx.restore();
        }
      }
      if (item.label && item.role !== "setup_zone" && labelPosition !== "center") {
        ctx.setLineDash([]);
        ctx.font = `800 ${labelFontSize}px -apple-system, BlinkMacSystemFont, sans-serif`;
        ctx.textAlign = "center";
        ctx.textBaseline = "bottom";
        ctx.lineWidth = canvasTextOutlineWidth(3);
        const label = compactLineLabel(item.label);
        const labelX = clamp((left + right) / 2, pad.left + 18, width - pad.right - 18);
        const labelY = clamp(top - 3, pad.top + 10, pad.top + priceH - 4);
        ctx.strokeStyle = canvasTextOutlineColor();
        ctx.fillStyle = boxBorder;
        drawCanvasText(ctx, label, labelX, labelY, undefined, { outlineWidth: 3 });
      }
      ctx.restore();
      registerCanvasTooltip(
        "price",
        (left + right) / 2,
        (top + bottom) / 2,
        right - left,
        Math.max(bottom - top, 14),
        () => String(item.role || "").toLowerCase() === "setup_zone"
          ? standardSignalTooltip(item, { source: item.source, force: true })
          : standardOverlayTooltip(item, { source: item.source }),
      );
    }

    function visibleTimeWindowForBars(bars, marginBars = 2) {
      const firstMs = Date.parse(bars?.[0]?.ts || "");
      const lastMs = Date.parse(bars?.[bars.length - 1]?.ts || "");
      if (!Number.isFinite(firstMs) || !Number.isFinite(lastMs)) return null;
      const stepMs = bars.length > 1
        ? Math.max(Date.parse(bars[1].ts || "") - firstMs, 60_000)
        : Math.max(intervalMinutesFromState() * 60_000, 60_000);
      const marginMs = Math.max(stepMs * Math.max(Number(marginBars) || 0, 0), 0);
      return { start: firstMs - marginMs, end: lastMs + marginMs };
    }

    function overlayTimeRangeMs(item) {
      const start = Date.parse(item?.start_ts || item?.range_start_ts || item?.ts || "");
      const end = Date.parse(item?.end_ts || item?.valid_until || item?.ts || item?.start_ts || "");
      if (!Number.isFinite(start) && !Number.isFinite(end)) return null;
      return {
        start: Number.isFinite(start) ? start : end,
        end: Number.isFinite(end) ? end : start,
      };
    }

    function overlayIntersectsVisibleTime(item, timeWindow) {
      if (!timeWindow) return true;
      if (
        Object.prototype.hasOwnProperty.call(item || {}, "end_anchor_ts")
        || Object.prototype.hasOwnProperty.call(item || {}, "end_bar_offset")
      ) return true;
      const range = overlayTimeRangeMs(item);
      if (!range) return true;
      return Math.max(range.start, range.end) >= timeWindow.start && Math.min(range.start, range.end) <= timeWindow.end;
    }

    function overlayIntersectsPricePane(item, y, pad, priceH) {
      let minY = Infinity;
      let maxY = -Infinity;
      const keys = ["top", "bottom", "y1", "y2", "price", "level", "trigger", "entry", "stop", "target"];
      for (const key of keys) {
        const value = Number(item?.[key]);
        if (!Number.isFinite(value)) continue;
        const py = y(value);
        if (!Number.isFinite(py)) continue;
        if (py < minY) minY = py;
        if (py > maxY) maxY = py;
      }
      if (minY === Infinity) return true;
      return maxY >= pad.top - 24 && minY <= pad.top + priceH + 24;
    }

    function overlayIntersectsVisible(item, bars, byTs, y, pad, priceH, timeWindow) {
      if (!item || item.type === "table") return true;
      if (item.type === "label" || item.type === "marker") {
        const index = byTs.get(item._ts_key || timestampKey(item.ts));
        return Number.isInteger(index) && index >= 0 && index < bars.length;
      }
      return overlayIntersectsVisibleTime(item, timeWindow) && overlayIntersectsPricePane(item, y, pad, priceH);
    }

    function drawGenericOverlayLine(ctx, item, timeLookup, pad, priceH, width, xStep, x, y) {
      const startTs = Date.parse(item.start_ts || item.ts);
      const y1Value = Number(item.y1 ?? item.price);
      const y2Value = Number(item.y2 ?? item.y1 ?? item.price);
      if (![startTs, y1Value, y2Value].every(Number.isFinite)) return;
      const x1Raw = xForOverlayTime(timeLookup, startTs, xStep, x);
      const x2Raw = xForOverlayEnd(item, timeLookup, xStep, x);
      if (x1Raw === null || x2Raw === null) return;
      if (overlayRangeOutsidePlot(x1Raw, x2Raw, pad, width)) return;
      const clipped = clipOverlayLineToPlotX(
        x1Raw,
        y(y1Value),
        x2Raw,
        y(y2Value),
        pad.left,
        width - pad.right,
      );
      if (!clipped) return;
      const { x1, y1, x2, y2 } = clipped;
      if ((y1 < pad.top && y2 < pad.top) || (y1 > pad.top + priceH && y2 > pad.top + priceH)) return;
      ctx.save();
      const lineOpacity = Number.isFinite(Number(item.opacity)) ? clamp(Number(item.opacity), 0.10, 1) : 0.82;
      const lineColor = calibratedCanvasColor(indicatorOverlayColor(item, lineOpacity, "warning"), { minRatio: 2.35 });
      ctx.strokeStyle = lineColor;
      ctx.lineWidth = clamp(Number(item.width) || 1, 0.75, 6);
      const overlayLineStyle = item.style || "solid";
      const wavyLine = isWavyLineStyle(overlayLineStyle);
      ctx.lineCap = lineCapForStyle(overlayLineStyle);
      ctx.lineDashOffset = 0;
      ctx.setLineDash(wavyLine ? [] : lineDashForStyle(overlayLineStyle));
      ctx.beginPath();
      drawLinePathForStyle(ctx, x1, y1, x2, y2, overlayLineStyle);
      ctx.stroke();
      if (item.arrow_head) {
        const angle = Math.atan2(y2 - y1, x2 - x1);
        const head = clamp(Number(item.arrow_size) || 9, 5, 18);
        ctx.save();
        ctx.setLineDash([]);
        ctx.fillStyle = lineColor;
        ctx.beginPath();
        ctx.moveTo(x2, y2);
        ctx.lineTo(x2 - Math.cos(angle - Math.PI / 6) * head, y2 - Math.sin(angle - Math.PI / 6) * head);
        ctx.lineTo(x2 - Math.cos(angle + Math.PI / 6) * head, y2 - Math.sin(angle + Math.PI / 6) * head);
        ctx.closePath();
        ctx.fill();
        ctx.restore();
      }
      if (item.line_variant === "double") {
        ctx.beginPath();
        drawLinePathForStyle(ctx, x1, y1 + 3, x2, y2 + 3, overlayLineStyle);
        ctx.stroke();
      }
      ctx.setLineDash([]);
      if (item.label) {
        const lineRole = String(item.role || item.kind || "").toLowerCase();
        const lineDirection = String(item.direction || item.side || "").toLowerCase();
        const labelPosition = item.label_position || "";
        const labelSide = item.label_side || "";
        const labelText = compactLineLabel(lineRole === "stop" ? "STOP" : item.label);
        const labelColor = calibratedCanvasColor(indicatorOverlayColor(item, Math.max(lineOpacity, 0.82), "warning"), { minRatio: 3.6 });
        const labelFontSize = clamp(Number(item.label_font_size) || 9, 5, 14);
        const labelGapPx = Number.isFinite(Number(item.label_gap_px)) ? Math.max(Number(item.label_gap_px), 0) : null;
        ctx.font = `700 ${labelFontSize}px -apple-system, BlinkMacSystemFont, sans-serif`;
        ctx.fillStyle = labelColor;
        if (labelPosition === "center") {
          const labelX = clamp((x1 + x2) / 2, pad.left + 18, width - pad.right - 18);
          const labelAbove = labelSide === "above" || (labelSide !== "below" && y2 <= y1);
          const labelOffset = labelGapPx ?? (labelAbove ? 7 : 12);
          const labelY = clamp((y1 + y2) / 2 + (labelAbove ? -labelOffset : labelOffset), pad.top + 8, pad.top + priceH - 4);
          ctx.textAlign = "center";
          drawCanvasText(ctx, labelText, labelX, labelY, undefined, { outlineWidth: 3 });
        } else if (labelPosition === "left") {
          const labelX = clamp(Math.min(x1, x2) + 4, pad.left + 6, width - pad.right - 18);
          const labelAbove = labelSide === "above";
          const labelOffset = labelGapPx ?? (labelAbove ? 7 : 12);
          const labelY = clamp((y1 + y2) / 2 + (labelAbove ? -labelOffset : labelOffset), pad.top + 8, pad.top + priceH - 4);
          ctx.textAlign = "left";
          drawCanvasText(ctx, labelText, labelX, labelY, undefined, { outlineWidth: 3 });
        } else if (labelPosition === "line_right") {
          const rightEndIsSecond = x2 >= x1;
          const anchorX = rightEndIsSecond ? x2 : x1;
          const anchorY = rightEndIsSecond ? y2 : y1;
          const labelAbove = labelSide === "above"
            || (labelSide !== "below" && lineDirection === "short")
            || (labelSide !== "below" && lineDirection !== "long" && y2 > y1);
          const labelOffset = labelGapPx ?? 1;
          const horizontalGap = labelGapPx ?? 1;
          const labelW = measureCanvasText(ctx, labelText);
          const labelX = clamp(anchorX - horizontalGap, pad.left + labelW + 4, width - pad.right - 4);
          const labelY = clamp(anchorY + (labelAbove ? -labelOffset : labelOffset), pad.top + 8, pad.top + priceH - 4);
          ctx.textAlign = "right";
          ctx.textBaseline = labelAbove ? "bottom" : "top";
          drawCanvasText(ctx, labelText, labelX, labelY, undefined, { outlineWidth: 2.5 });
        } else if (labelPosition === "right" || item.label_anchor === "price_axis") {
          ctx.textAlign = "right";
          const labelX = typeof chartLineLabelRightX === "function" ? chartLineLabelRightX(pad, width) : width - pad.right - 4;
          drawCanvasText(ctx, labelText, labelX, clamp(y2 - 4, pad.top + 8, pad.top + priceH - 4), undefined, { outlineWidth: 3 });
        } else {
          const labelX = clamp((x1 + x2) / 2, pad.left + 18, width - pad.right - 18);
          const labelAbove = labelSide === "above" || (labelSide !== "below" && y2 <= y1);
          const labelOffset = labelGapPx ?? (labelAbove ? 7 : 12);
          const labelY = clamp((y1 + y2) / 2 + (labelAbove ? -labelOffset : labelOffset), pad.top + 8, pad.top + priceH - 4);
          ctx.textAlign = "center";
          drawCanvasText(ctx, labelText, labelX, labelY, undefined, { outlineWidth: 3 });
        }
      }
      ctx.restore();
      const hitX = (x1 + x2) / 2;
      const hitY = (y1 + y2) / 2;
      registerCanvasTooltip("price", hitX, hitY, Math.max(Math.abs(x2 - x1), 24), Math.max(Math.abs(y2 - y1), 14), () => isPlanRoleItem(item)
        ? standardSignalTooltip(item, { force: true })
        : standardOverlayTooltip(item, { source: item.source }));
    }

    function drawLifecycleMarker(ctx, item, px, py, lines, role, pad, width) {
      if (Number.isFinite(pad?.left) && Number.isFinite(width) && !isChartPlotXVisible(px, pad, width, 8)) return;
      const markerRole = role || String(item?.role || item?.kind || "").toLowerCase();
      const text = markerRole === "entry_fill" ? signalIntentMark(item, markerRole) : lines.join("");
      const isStopGlyph = markerRole === "stop_hit";
      const lifecycleGlyphScale = ["target_hit", "stop_hit", "trail_stop_hit"].includes(markerRole) ? 0.70 : 1;
      const markerFontScale = clamp(Number(item?.marker_font_scale) || 1, 0.35, 2);
      const baseFontSize = isStopGlyph ? 36 : text.length > 1 ? 13 : 18;
      const fontSize = baseFontSize * lifecycleGlyphScale * markerFontScale;
      const hitSize = Math.max(20, fontSize + 8);
      const direction = normalizedOverlaySignal(item).direction;
      const mirrorGo = shouldMirrorGoMark([text], { direction });
      const markerTextAnchor = String(item?.marker_text_anchor || "center").toLowerCase();
      const lowerLeftAnchor = markerTextAnchor === "lower_left";
      const upperLeftAnchor = markerTextAnchor === "upper_left";
      const leftAnchor = lowerLeftAnchor || upperLeftAnchor;
      const markerAnchorDot = item?.marker_anchor_dot === true;
      const markerTextGap = markerAnchorDot ? Math.max(3, fontSize * 0.45) : 0;
      const markerTextX = leftAnchor ? px + markerTextGap : px;
      ctx.save();
      ctx.font = `900 ${fontSize}px -apple-system, BlinkMacSystemFont, sans-serif`;
      ctx.textAlign = leftAnchor ? "left" : "center";
      ctx.textBaseline = lowerLeftAnchor ? "bottom" : upperLeftAnchor ? "top" : "middle";
      const textWidth = ctx.measureText(text).width;
      ctx.lineWidth = canvasTextOutlineWidth((isStopGlyph ? 8 : text.length > 1 ? 4 : 5) * lifecycleGlyphScale * markerFontScale);
      ctx.strokeStyle = markerRole === "trail_stop_hit"
        ? (isTvFlatUiShape() ? canvasTextOutlineColor() : isLightTheme() ? "rgba(255,255,255,0.96)" : "rgba(235,242,255,0.94)")
        : (isTvFlatUiShape() ? canvasTextOutlineColor() : isLightTheme() ? "rgba(255,255,255,0.96)" : "rgba(0,0,0,0.86)");
      const markerBg = indicatorOverlayColor(item, 0.90, "warning");
      const markerColorPolicy = String(item?.marker_color_policy || "").toLowerCase();
      const markerGlyphColorPolicy = String(item?.marker_glyph_color_policy || "").toLowerCase();
      const markerToneColor = markerColorPolicy === "tone" || markerGlyphColorPolicy === "tone" || markerAnchorDot
        ? calibratedCanvasColor(markerBg, { minRatio: 3.2 })
        : markerBg;
      const markerThemeTextColor = isLightTheme() ? "#111827" : "#f8fafc";
      const resolvedMarkerThemeTextColor = isLightTheme()
        ? String(item?.marker_theme_text_light || markerThemeTextColor)
        : String(item?.marker_theme_text_dark || markerThemeTextColor);
      const leadingGlyph = markerGlyphColorPolicy === "tone" ? (Array.from(text)[0] || "") : "";
      const trailingText = leadingGlyph ? text.slice(leadingGlyph.length) : "";
      const leadingGlyphWidth = leadingGlyph ? ctx.measureText(leadingGlyph).width : 0;
      const splitTextX = leftAnchor ? markerTextX : markerTextX - textWidth * 0.5;
      ctx.shadowColor = markerColorPolicy === "theme"
        ? (isLightTheme() ? "rgba(255,255,255,0.96)" : "rgba(0,0,0,0.86)")
        : (isLightTheme() ? "rgba(255,255,255,0.96)" : rgbaFromCssColor(markerBg, 0.78));
      ctx.shadowBlur = canvasShadowBlur(isLightTheme() ? 2 : 10);
      if (markerAnchorDot) {
        ctx.beginPath();
        ctx.arc(px, py, clamp(fontSize * 0.24, 1.5, 3), 0, Math.PI * 2);
        ctx.fillStyle = markerToneColor;
        ctx.fill();
      }
      if (isStopGlyph) {
        ctx.beginPath();
        ctx.arc(px, py, fontSize * 0.43, 0, Math.PI * 2);
        ctx.fillStyle = isLightTheme() ? "rgba(255,255,255,0.90)" : "rgba(0,0,0,0.78)";
        ctx.fill();
      } else if (shouldStrokeCanvasText()) {
        if (mirrorGo) {
          ctx.textBaseline = "top";
          drawLabelTextLine(ctx, text, markerTextX, py - fontSize * 0.5, true, {
            stroke: true,
            fill: false,
          });
        } else if (leadingGlyph) {
          ctx.textAlign = "left";
          ctx.strokeText(leadingGlyph, splitTextX, py);
          if (trailingText) ctx.strokeText(trailingText, splitTextX + leadingGlyphWidth, py);
        } else {
          ctx.strokeText(text, markerTextX, py);
        }
      }
      ctx.shadowBlur = 0;
      ctx.fillStyle = markerRole === "entry_fill"
        ? "#f3d35b"
        : markerRole === "target_hit"
        ? "#00d18f"
        : markerRole === "stop_hit"
        ? "#ff5c70"
        : markerRole === "trail_stop_hit"
        ? "#05070a"
        : markerRole === "plan_cancel"
        ? (isLightTheme() ? "#061a2f" : "#f8fafc")
        : markerColorPolicy === "theme"
        ? resolvedMarkerThemeTextColor
        : markerColorPolicy === "tone"
        ? markerToneColor
        : readableTextForBg(markerBg, null);
      if (mirrorGo) {
        ctx.textBaseline = "top";
        drawLabelTextLine(ctx, text, markerTextX, py - fontSize * 0.5, true);
      } else if (leadingGlyph) {
        ctx.textAlign = "left";
        ctx.fillStyle = markerToneColor;
        ctx.fillText(leadingGlyph, splitTextX, py);
        if (trailingText) {
          ctx.fillStyle = resolvedMarkerThemeTextColor;
          ctx.fillText(trailingText, splitTextX + leadingGlyphWidth, py);
        }
      } else {
        ctx.fillText(text, markerTextX, py);
      }
      ctx.restore();
      const hitWidth = Math.max(hitSize, markerTextGap + textWidth + 8);
      const hitX = leftAnchor ? px + (markerTextGap + textWidth) * 0.5 : px;
      const hitY = lowerLeftAnchor
        ? py - fontSize * 0.5
        : upperLeftAnchor
          ? py + fontSize * 0.5
          : py;
      registerCanvasTooltip("price", hitX, hitY, hitWidth, hitSize, () => standardSignalTooltip(item, { force: true }));
    }

    function drawRoundPivotMarker(ctx, item, px, py, lines, pad, width) {
      if (Number.isFinite(pad?.left) && Number.isFinite(width) && !isChartPlotXVisible(px, pad, width, 8)) return;
      const text = String(lines[0] || item.label || "").replace(/^W/i, "").slice(0, 2);
      if (!text) return;
      const radius = clamp(Number(item.marker_radius) || 9, 5, 18);
      const fill = rgbaFromCssColor(indicatorOverlayColor(item, 0.88, "info"), isLightTheme() ? 0.72 : 0.54);
      const stroke = calibratedCanvasColor(indicatorOverlayColor(item, 0.92, "info"), { minRatio: 2.2 });
      const hitSize = radius * 2 + 8;
      ctx.save();
      ctx.shadowColor = rgbaFromCssColor(stroke, isLightTheme() ? 0.24 : 0.44);
      ctx.shadowBlur = canvasShadowBlur(isLightTheme() ? 3 : 9);
      ctx.beginPath();
      ctx.arc(px, py, radius, 0, Math.PI * 2);
      ctx.fillStyle = fill;
      ctx.fill();
      ctx.shadowBlur = 0;
      ctx.lineWidth = 1.6;
      ctx.strokeStyle = stroke;
      ctx.stroke();
      ctx.font = `900 ${clamp(Number(item.label_font_size) || radius + 2, 8, 18)}px -apple-system, BlinkMacSystemFont, sans-serif`;
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillStyle = readableTextForBg(fill, null);
      drawCanvasText(ctx, text, px, py + 0.5, undefined, { outlineWidth: 2 });
      ctx.restore();
      registerCanvasTooltip("price", px, py, hitSize, hitSize, () => standardOverlayTooltip(item, { source: item.source }));
    }

    function drawGenericOverlayMarker(ctx, item, byTs, x, y, pad, width) {
      const index = byTs.get(item._ts_key || timestampKey(item.ts));
      const price = Number(item.price);
      if (index === undefined || !Number.isFinite(price)) return;
      const rawPx = x(index) + (Number.isFinite(Number(item.x_offset_px)) ? Number(item.x_offset_px) : 0);
      const px = clamp(rawPx, pad.left + 2, width - pad.right - 2);
      if (!isChartPlotXVisible(px, pad, width, 8)) return;
      const markerRole = String(item.role || item.kind || "").toLowerCase();
      const pivotNumber = Number(item.pivot_number);
      const markerLines = markerRole === "structure_pivot" && Number.isInteger(pivotNumber)
        ? [String(pivotNumber)]
        : Array.isArray(item.lines) ? item.lines : [item.lines || item.glyph || item.label || item.event || item.code || signalIntentMark(item)];
      const lines = markerLines
        .map(line => String(line ?? "").trim())
        .filter(Boolean)
        .slice(0, 2);
      if (!lines.length) return;
      if (markerRole === "structure_pivot") {
        drawRoundPivotMarker(ctx, item, px, y(price), lines, pad, width);
        return;
      }
      drawLifecycleMarker(ctx, item, px, y(price), lines, String(item.role || item.kind || "").toLowerCase(), pad, width);
    }

    function labelAnchorPrice(item, bar, side, fallbackPrice) {
      if (!bar || side === "center") return fallbackPrice;
      const role = String(item?.role || item?.kind || "").toLowerCase();
      if (
        item?.anchor_price_mode === "overlay"
        || ["entry", "stop", "target", "trail", "track", "pullback_zone"].includes(role)
        || item?.entry !== undefined
        || item?.trigger !== undefined
        || item?.stop !== undefined
        || item?.target !== undefined
      ) {
        return fallbackPrice;
      }
      const value = side === "below" ? Number(bar.low) : Number(bar.high);
      return Number.isFinite(value) ? value : fallbackPrice;
    }

    function drawGenericOverlayLabel(ctx, item, byTs, pad, priceH, width, x, y, labelStacks = null, bars = []) {
      const index = byTs.get(item._ts_key || timestampKey(item.ts));
      const price = Number(item.price);
      if (index === undefined || !Number.isFinite(price)) return;
      const lines = item.glyph_only
        ? (Array.isArray(item.lines) ? item.lines : [item.lines || item.glyph || signalIntentMark(item)]).map(line => String(line ?? "").trim()).filter(Boolean).slice(0, 2)
        : Array.isArray(item._label_lines) ? item._label_lines : buildIndicatorLabelLines(item);
      if (!lines.length) return;
      const anchor = item.anchor || item.side;
      const side = anchor === "below" ? "below" : anchor === "center" ? "center" : "above";
      const px = x(index);
      const py = y(labelAnchorPrice(item, bars[index], side, price));
      if (!isChartPlotXVisible(px, pad, width, 8)) return;
      const role = String(item.role || item.kind || "").toLowerCase();
      const noTick = item.no_tick || ["entry_fill", "target_hit", "stop_hit", "trail_stop_hit", "plan_cancel"].includes(role);
      const introKey = `indicator:${item.source || ""}:${item.ts || ""}:${item.code || item.state || item.label || item.event || ""}:${price}`;
      const absorptionWall = String(item.glyph_kind || "").toLowerCase() === "absorption_wall";
      const wallDirection = String(item.wall_direction || item.direction || "").toLowerCase();
      const direction = normalizedOverlaySignal(item).direction;
      const labelColor = indicatorOverlayColor(item, 0.90, "warning");
      ctx.save();
      applyChartMarkerIntro(ctx, introKey, px, py, 680);
      if (!noTick) drawSignalTick(ctx, px, py, labelColor, side, "horizontal");
      if (item.glyph_only) {
        drawLifecycleMarker(ctx, item, px, py, lines, role, pad, width);
        ctx.restore();
        return;
      }
      if (item.label_style === "text") {
        const labelFontSize = clamp(Number(item.label_font_size) || 9, 5, 14);
        const labelLineH = Number.isFinite(Number(item.label_font_size)) ? Math.max(8, labelFontSize + 1) : 10;
        const hit = drawIndicatorTextLabel(ctx, px, py, lines, {
          side,
          color: labelColor,
          offsetPx: Number.isFinite(Number(item._stack_offset_px)) ? item._stack_offset_px : stackedLabelOffset(labelStacks, index, side, lines, SIGNAL_LABEL_OFFSET_PX),
          lineH: labelLineH,
          font: `900 ${labelFontSize}px -apple-system, BlinkMacSystemFont, sans-serif`,
          minX: pad.left + 2,
          maxX: width - pad.right - 2,
          minY: pad.top + 2,
          maxY: pad.top + priceH - 2,
          absorptionWall,
          wallDirection,
          direction,
        });
        if (hit) registerCanvasTooltip("price", hit.x, hit.y, hit.width, hit.height, () => (isPlanRoleItem(item) || isLifecycleExecutionMarker(item))
          ? standardSignalTooltip(item, { force: true })
          : standardOverlayTooltip(item, { source: item.source }));
        ctx.restore();
        return;
      }
      const labelFontSize = clamp(Number(item.label_font_size) || 9, 5, 14);
      const labelLineH = Number.isFinite(Number(item.label_font_size)) ? Math.max(7, labelFontSize) : 9;
      const hit = drawIndicatorTextLabel(ctx, px, py, lines, {
        side,
        color: labelColor,
        offsetPx: Number.isFinite(Number(item._stack_offset_px)) ? Number(item._stack_offset_px) : stackedLabelOffset(labelStacks, index, side, lines, SIGNAL_LABEL_OFFSET_PX),
        lineH: labelLineH,
        font: `900 ${labelFontSize}px -apple-system, BlinkMacSystemFont, sans-serif`,
        minX: pad.left + 2,
        maxX: width - pad.right - 2,
        minY: pad.top + 2,
        maxY: pad.top + priceH - 2,
        absorptionWall,
        wallDirection,
        direction,
      });
      if (hit) registerCanvasTooltip("price", hit.x, hit.y, hit.width, hit.height, () => (isPlanRoleItem(item) || isLifecycleExecutionMarker(item))
        ? standardSignalTooltip(item, { force: true })
        : standardOverlayTooltip(item, { source: item.source }));
      ctx.restore();
    }

    const INDICATOR_TABLE_INVALID_CELL_TEXT = "N/A";

    function indicatorTableCellScalarText(value) {
      if (value === null || value === undefined) return "";
      if (typeof value === "string") return value;
      if (typeof value === "number" && Number.isFinite(value)) return String(value);
      if (typeof value === "boolean") return value ? "true" : "false";
      return null;
    }

    function indicatorTableCellText(cell) {
      if (cell && typeof cell === "object") {
        if (Array.isArray(cell)) return INDICATOR_TABLE_INVALID_CELL_TEXT;
        if (Object.hasOwn(cell, "parts") && !Array.isArray(cell.parts)) {
          return INDICATOR_TABLE_INVALID_CELL_TEXT;
        }
        if (Array.isArray(cell.parts)) {
          const separator = cell.separator === undefined
            ? " "
            : indicatorTableCellScalarText(cell.separator);
          if (separator === null) return INDICATOR_TABLE_INVALID_CELL_TEXT;
          const parts = cell.parts.map(part => indicatorTableCellText(part)).filter(Boolean);
          if (parts.includes(INDICATOR_TABLE_INVALID_CELL_TEXT)) {
            return INDICATOR_TABLE_INVALID_CELL_TEXT;
          }
          const empty = indicatorTableCellScalarText(cell.empty ?? "-");
          if (empty === null) return INDICATOR_TABLE_INVALID_CELL_TEXT;
          return parts.length
            ? parts.join(separator)
            : empty;
        }
        if (Object.hasOwn(cell, "text")) {
          return indicatorTableCellScalarText(cell.text) ?? INDICATOR_TABLE_INVALID_CELL_TEXT;
        }
        if (!Object.hasOwn(cell, "value")) {
          return INDICATOR_TABLE_INVALID_CELL_TEXT;
        }
        const rawKind = indicatorTableCellScalarText(cell.kind ?? "text");
        if (rawKind === null) return INDICATOR_TABLE_INVALID_CELL_TEXT;
        const kind = rawKind.toLowerCase();
        if (!["text", "integer", "percent", "fixed", "price", "domain_fact"].includes(kind)) {
          return INDICATOR_TABLE_INVALID_CELL_TEXT;
        }
        const empty = indicatorTableCellScalarText(cell.empty ?? "-");
        const value = cell.value;
        const prefix = indicatorTableCellScalarText(cell.prefix ?? "");
        const suffix = indicatorTableCellScalarText(cell.suffix ?? "");
        if ([empty, prefix, suffix].includes(null)) return INDICATOR_TABLE_INVALID_CELL_TEXT;
        if (kind === "domain_fact") {
          const rendered = typeof domainFactTableCellText === "function"
            ? domainFactTableCellText(value, cell.part)
            : null;
          if (rendered === null) return INDICATOR_TABLE_INVALID_CELL_TEXT;
          return rendered || empty;
        }
        const valueMissing = value === null || value === undefined || value === "";
        let body = "";
        if (kind === "integer") {
          const numeric = valueMissing || typeof value !== "number" ? NaN : value;
          if (!valueMissing && !Number.isFinite(numeric)) return INDICATOR_TABLE_INVALID_CELL_TEXT;
          body = Number.isFinite(numeric) ? String(Math.round(numeric)) : empty;
        } else if (kind === "percent") {
          const numeric = valueMissing || typeof value !== "number" ? NaN : value;
          if (!valueMissing && !Number.isFinite(numeric)) return INDICATOR_TABLE_INVALID_CELL_TEXT;
          const rawDigits = cell.digits ?? cell.precision;
          if (rawDigits !== undefined && !Number.isFinite(rawDigits)) {
            return INDICATOR_TABLE_INVALID_CELL_TEXT;
          }
          const digits = Number.isFinite(rawDigits) ? Math.max(0, Math.min(4, Math.trunc(rawDigits))) : 0;
          if (cell.scale !== undefined && !Number.isFinite(cell.scale)) {
            return INDICATOR_TABLE_INVALID_CELL_TEXT;
          }
          const scale = Number.isFinite(cell.scale) ? cell.scale : 100;
          body = Number.isFinite(numeric) ? `${(numeric * scale).toFixed(digits)}%` : empty;
        } else if (kind === "fixed" || kind === "price") {
          const numeric = valueMissing || typeof value !== "number" ? NaN : value;
          if (!valueMissing && !Number.isFinite(numeric)) return INDICATOR_TABLE_INVALID_CELL_TEXT;
          const rawDigits = cell.digits ?? cell.precision;
          if (rawDigits !== undefined && !Number.isFinite(rawDigits)) {
            return INDICATOR_TABLE_INVALID_CELL_TEXT;
          }
          const defaultDigits = kind === "price" ? 4 : 1;
          const digits = Number.isFinite(rawDigits) ? Math.max(0, Math.min(8, Math.trunc(rawDigits))) : defaultDigits;
          body = Number.isFinite(numeric) ? numeric.toFixed(digits) : empty;
        } else {
          body = valueMissing ? empty : indicatorTableCellScalarText(value);
          if (body === null) return INDICATOR_TABLE_INVALID_CELL_TEXT;
        }
        return `${prefix}${body}${suffix}`;
      }
      return indicatorTableCellScalarText(cell) ?? INDICATOR_TABLE_INVALID_CELL_TEXT;
    }

    function drawGenericIndicatorCornerTable(
      ctx,
      overlay,
      columns,
      pad,
      width,
      priceH,
      stackOffset,
      position,
      presentation,
    ) {
      const table = overlay.table || {};
      const { tableBg, tableText, accent, opacity, tableContract } = presentation;
      const cardW = indicatorTableWidthLayout(overlay, position, pad, width, columns).width;
      const headerH = 24;
      const rowH = 28;
      const cardH = headerH + columns.length * rowH;
      const right = position.endsWith("right");
      const topAligned = position.startsWith("top");
      const left = right
        ? width - pad.right - cardW - 4
        : pad.left + 4;
      const top = topAligned
        ? pad.top + 4 + stackOffset
        : pad.top + priceH - cardH - 4 - stackOffset;
      const label = indicatorTableLabel(overlay.source, table);
      const headerBg = accent || tableBg;
      const headerText = readableTextForBg(headerBg, tableText);
      const iconEntry = indicatorTableHeaderIconRenderers.get(String(tableContract.icon || "").trim());

      ctx.save();
      ctx.fillStyle = headerBg;
      ctx.globalAlpha = opacity;
      ctx.fillRect(left, top, cardW, headerH);
      ctx.globalAlpha = 1;
      ctx.strokeStyle = rgbaFromCssColor(headerText, 0.24);
      ctx.strokeRect(left, top, cardW, headerH);
      let titleLeft = left + 7;
      if (iconEntry) {
        iconEntry.renderer({
          ctx,
          centerX: left + 12,
          centerY: top + headerH / 2,
          size: 18,
          color: headerText,
          background: headerBg,
        });
        titleLeft = left + 25;
      }
      ctx.fillStyle = headerText;
      ctx.font = "850 8.5px -apple-system, BlinkMacSystemFont, sans-serif";
      ctx.textAlign = "left";
      ctx.textBaseline = "middle";
      ctx.fillText(displayTableCell(String(label || "Indicator").toUpperCase(), 32), titleLeft, top + headerH / 2);
      registerCanvasTooltip(
        "price",
        left + cardW / 2,
        top + headerH / 2,
        cardW,
        headerH,
        () => standardTableTooltip(overlay),
        iconEntry?.action || null,
      );

      columns.forEach((column, index) => {
        const [head, line1, line2] = indicatorTableColumnCells(column, table, overlay);
        const semanticBg = column?.tone ? indicatorOverlayColor(column, 1) : null;
        const rowBg = column?.bg || semanticBg || (index === 0 && accent ? accent : tableBg);
        const rowText = readableTextForBg(rowBg, column?.text || tableText);
        const y0 = top + headerH + index * rowH;
        ctx.fillStyle = rowBg;
        ctx.globalAlpha = opacity;
        ctx.fillRect(left, y0, cardW, rowH);
        ctx.globalAlpha = 1;
        ctx.strokeStyle = rgbaFromCssColor(rowText, 0.20);
        ctx.strokeRect(left, y0, cardW, rowH);
        ctx.fillStyle = rgbaFromCssColor(rowText, 0.72);
        ctx.font = "800 7.5px -apple-system, BlinkMacSystemFont, sans-serif";
        ctx.textAlign = "left";
        ctx.fillText(displayTableCell(head, 11), left + 7, y0 + rowH / 2);
        ctx.fillStyle = rowText;
        ctx.font = tableValueFont(head, line1, 8.5);
        ctx.textAlign = "center";
        ctx.fillText(displayTableCell(line1, 18), left + cardW * 0.52, y0 + rowH / 2);
        ctx.fillStyle = rgbaFromCssColor(rowText, 0.78);
        ctx.font = tableValueFont(head, line2, 8.5);
        ctx.fillText(displayTableCell(line2, 18), left + cardW * 0.82, y0 + rowH / 2);
        registerCanvasTooltip(
          "price",
          left + cardW / 2,
          y0 + rowH / 2,
          cardW,
          rowH,
          () => standardTableTooltip(overlay, column),
          column?.action || null,
        );
      });
      ctx.restore();
      return cardH + 2;
    }

    function drawGenericIndicatorTable(ctx, overlay, pad, width, priceH, stackOffset = 0, position = "bottom") {
      pad = indicatorTablePlacementPad(pad, width);
      const table = overlay?.table;
      const columns = typeof indicatorTableModelColumns === "function"
        ? indicatorTableModelColumns(table, overlay)
        : Array.isArray(table?.columns) ? table.columns : [];
      if (!columns.length) return 0;
      const widthLayout = indicatorTableWidthLayout(overlay, position, pad, width, columns);
      if (!widthLayout.fits) return 0;
      const tableBg = table.bg || overlay.bg || rgbaFromCssColor(css("--panel-2"), 0.90);
      const tableText = table.text || overlay.text || css("--text");
      const semanticAccent = table.tone || overlay.tone ? indicatorOverlayColor(table.tone ? table : overlay, 1) : null;
      const accent = table.accent || overlay.accent || semanticAccent;
      const sourceId = String(overlay?.source || table?.id || "");
      const tableContract = indicatorRendererTableContract(sourceId);
      const opacity = tableOpacityForSource(sourceId);
      if (indicatorTableIsCornerPosition(position)) {
        return drawGenericIndicatorCornerTable(
          ctx,
          overlay,
          columns,
          pad,
          width,
          priceH,
          stackOffset,
          position,
          { tableBg, tableText, accent, opacity, tableContract },
        );
      }
      const LABEL_W = widthLayout.headerWidth;
      const cellW = widthLayout.cellWidth;
      const cellH = 34;
      const tableW = cellW * columns.length;
      const left = (pad.left + width - pad.right - tableW - LABEL_W) / 2 + LABEL_W;
      const top = tableTopForPosition(position, stackOffset, pad, priceH, cellH);
      const tableGradient = table?.gradient && typeof canvasLinearGradient === "function"
        ? canvasLinearGradient(
            ctx,
            left,
            top,
            left,
            top + cellH,
            table.gradient.stops,
          )
        : null;
      const tableGradientStops = tableGradient
        ? table.gradient.stops.map(stop => String(stop?.color || "").trim()).filter(Boolean)
        : [];
      const tableThinking = tableContract.animation_ref === "table_thinking"
        && typeof indicatorTableThinkingActive === "function"
        && indicatorTableThinkingActive(table, overlay);
      ctx.save();
      ctx.font = "700 8.5px -apple-system, BlinkMacSystemFont, sans-serif";
      ctx.textAlign = "center";
      const label = indicatorTableLabel(overlay.source, table);
      if (tableThinking && typeof drawIndicatorTableThinkingBackdrop === "function") {
        drawIndicatorTableThinkingBackdrop(ctx, left, top, tableW, cellH, LABEL_W);
        if (typeof ensureIndicatorTableThinkingAnimationLoop === "function") ensureIndicatorTableThinkingAnimationLoop(table, overlay);
      }
      if (tableContract.animation_ref) {
        drawIndicatorTableAnimation(tableContract.animation_ref, {
          ctx,
          table,
          overlay,
          left,
          top,
          tableW,
          cellW,
          cellH,
          headerW: LABEL_W,
        });
      }
      drawIndicatorTableHeader(
        ctx,
        label,
        left,
        top,
        tableW,
        cellH,
        accent || tableBg,
        tableText,
        () => standardTableTooltip(overlay),
        {
          icon: tableContract.icon || "",
          headerWidth: LABEL_W,
          backgroundOpacity: opacity,
        },
      );
      columns.forEach((col, index) => {
        const [head, line1, line2] = indicatorTableColumnCells(col, table, overlay);
        const thinkingColumnHead = String(table?.thinking_column || "").toUpperCase();
        const isThinkingColumn = tableThinking && thinkingColumnHead && String(head || "").toUpperCase() === thinkingColumnHead;
        const isActionColumn = Boolean(col?.action);
        const semanticBg = col?.tone ? rgbaFromCssColor(indicatorOverlayColor(col, 1), 0.88) : null;
        const colBg = col?.bg || semanticBg || (isThinkingColumn ? rgbaFromCssColor(themeColor("blue"), 0.32) : isActionColumn ? rgbaFromCssColor(themeColor("blue"), 0.40) : (index === 0 && accent ? rgbaFromCssColor(accent, 0.82) : tableGradient || tableBg));
        const colText = readableTextForBg(
          tableGradient && colBg === tableGradient ? tableGradientStops : colBg,
          col?.text || tableText,
        );
        const x0 = left + index * cellW;
        ctx.fillStyle = colBg;
        ctx.globalAlpha = opacity;
        ctx.fillRect(x0, top, cellW, cellH);
        ctx.globalAlpha = 1;
        ctx.strokeStyle = rgbaFromCssColor(colText, 0.20);
        ctx.strokeRect(x0, top, cellW, cellH);
        ctx.fillStyle = rgbaFromCssColor(colText, 0.70);
        ctx.fillText(displayTableCell(head, 11), x0 + cellW / 2, top + 8);
        ctx.fillStyle = colText;
        ctx.font = tableValueFont(head, line1);
        ctx.fillText(displayTableCell(line1, 18), x0 + cellW / 2, top + 18);
	        ctx.fillStyle = rgbaFromCssColor(colText, 0.78);
        ctx.font = tableValueFont(head, line2);
        ctx.fillText(displayTableCell(line2, 18), x0 + cellW / 2, top + 28);
        if (isThinkingColumn && typeof drawIndicatorTableThinkingDots === "function") {
          drawIndicatorTableThinkingDots(ctx, x0, top, cellW, cellH, colText);
        }
        ctx.font = "700 8.5px -apple-system, BlinkMacSystemFont, sans-serif";
        registerCanvasTooltip("price", x0 + cellW / 2, top + cellH / 2, cellW, cellH, () => standardTableTooltip(overlay, col), col?.action || null);
      });
      ctx.restore();
      return cellH + 1;
    }

    const indicatorCustomOverlayRendererRegistry = new Map();

    const indicatorCandleStyleProviderRegistry = new Map();

    function registerIndicatorCandleStyleProvider(indicatorId, provider, options = {}) {
      const key = String(indicatorId || "").trim();
      if (!indicatorSpec(key)?.id || typeof provider !== "function") {
        throw new Error(`Invalid indicator candle style provider: ${key || "missing"}`);
      }
      if (indicatorCandleStyleProviderRegistry.has(key)) {
        throw new Error(`Duplicate indicator candle style provider: ${key}`);
      }
      indicatorCandleStyleProviderRegistry.set(key, {
        provider,
        priority: clamp(Number(options.priority) || 0, -1000, 1000),
      });
    }

    function indicatorCandleStyleSeries(snapshot, bars) {
      const resolved = Array.from({ length: Array.isArray(bars) ? bars.length : 0 }, () => null);
      const providers = Array.from(indicatorCandleStyleProviderRegistry.entries())
        .sort((left, right) => (
          left[1].priority - right[1].priority
          || numericValueOrFallback(indicatorUiSpec(left[0])?.runtime_order, 500)
            - numericValueOrFallback(indicatorUiSpec(right[0])?.runtime_order, 500)
          || left[0].localeCompare(right[0])
        ));
      for (const [indicatorId, entry] of providers) {
        if (!indicatorCalcForId(indicatorId) || indicatorVisibleForId(indicatorId) === false) continue;
        try {
          const styles = entry.provider({ snapshot, bars });
          if (!Array.isArray(styles)) continue;
          for (let index = 0; index < resolved.length; index += 1) {
            const color = String(styles[index]?.color || "").trim();
            if (color) resolved[index] = { mode: "solid", color };
          }
        } catch (error) {
          console.error(`Indicator candle style provider failed: ${indicatorId}`, error);
        }
      }
      return resolved;
    }

    const indicatorStateSeriesPresentationRegistry = new Map();

    const indicatorTextSizeScales = Object.freeze({
      small: 0.5,
      medium: 0.6,
      large: 0.7,
    });

    const indicatorStateSeriesThemePalettes = Object.freeze({
      Classic: Object.freeze({
        bull: "#5CF0D7",
        bear: "#B32AC3",
        bg: "#0D0D0D",
        frame: "#1E1E2E",
        neutral: "#888888",
        text: "#FFFFFF",
      }),
      "Cyber Aqua": Object.freeze({
        bull: "#00E5FF",
        bear: "#FF2E63",
        bg: "#060B10",
        frame: "#1A2A33",
        neutral: "#7FAEC2",
        text: "#FFFFFF",
      }),
      "Crimson Pulse": Object.freeze({
        bull: "#00CFFE",
        bear: "#E0F70E",
        bg: "#12080C",
        frame: "#2A0F18",
        neutral: "#9E7680",
        text: "#FFFFFF",
      }),
      "Royal Purple": Object.freeze({
        bull: "#C5AFF5",
        bear: "#D900FF",
        bg: "#0F0B1A",
        frame: "#241A3A",
        neutral: "#9C8FD9",
        text: "#FFFFFF",
      }),
      "Emerald Night": Object.freeze({
        bull: "#00E676",
        bear: "#FF5252",
        bg: "#07110B",
        frame: "#153322",
        neutral: "#7FAF9B",
        text: "#FFFFFF",
      }),
      "Minimal Mono": Object.freeze({
        bull: "#FFFFFF",
        bear: "#5C5C5C",
        bg: "#000000",
        frame: "#1C1C1C",
        neutral: "#777777",
        text: "#FFFFFF",
      }),
      "Classic Emerald": Object.freeze({
        bull: "#00FF00",
        bear: "#FF0000",
        bg: "#121212",
        frame: "#2A2A2A",
        neutral: "#9E9E9E",
        text: "#FFFFFF",
      }),
    });

    function registerIndicatorStateSeriesPresentation(indicatorId, contract) {
      const key = String(indicatorId || "").trim();
      if (
        !indicatorSpec(key)?.id
        || !contract
        || typeof contract !== "object"
        || contract.kind !== "indicator_state_series_v1"
      ) {
        throw new Error(`Invalid indicator state-series presentation: ${key || "missing"}`);
      }
      if (indicatorStateSeriesPresentationRegistry.has(key)) {
        throw new Error(`Duplicate indicator state-series presentation: ${key}`);
      }
      indicatorStateSeriesPresentationRegistry.set(key, Object.freeze({ ...contract }));
    }

    function indicatorStateSeriesContract(_snapshot, indicatorId) {
      return indicatorStateSeriesPresentationRegistry.get(String(indicatorId || "").trim()) || null;
    }

    function indicatorStateSeriesControl(contract, group, name, fallback = null) {
      const key = String(contract?.controls?.[name] || "").trim();
      return key && group && Object.prototype.hasOwnProperty.call(group, key)
        ? group[key]
        : fallback;
    }

    function indicatorStateSeriesPalette(contract, group) {
      const requested = String(indicatorStateSeriesControl(
        contract,
        group,
        "theme",
        contract?.default_theme || "",
      ) || "").trim();
      const exact = indicatorStateSeriesThemePalettes[requested];
      if (exact && typeof exact === "object") return exact;
      const normalized = requested.toLowerCase();
      const matched = Object.entries(indicatorStateSeriesThemePalettes)
        .find(([name]) => String(name).toLowerCase() === normalized)?.[1];
      return matched || indicatorStateSeriesThemePalettes.Classic;
    }

    function indicatorStateSeriesStateColor(contract, palette, stateCode, alpha = 1) {
      const normalized = String(stateCode || contract?.initial_state || "neutral").toLowerCase();
      const paletteKey = String(
        contract?.state_colors?.[normalized]
        || contract?.state_colors?.neutral
        || normalized,
      );
      const color = String(palette?.[paletteKey] || palette?.neutral || css("--text"));
      return rgbaFromCssColor(color, clamp(Number(alpha), 0, 1));
    }

    function indicatorStateSeriesDirectionLabel(direction) {
      return String(direction || "").toLowerCase() === "long" ? "𝐁𝐔𝐘" : "𝐒𝐄𝐋𝐋";
    }

    function indicatorStateSeriesTransitionPresentation(contract, group, palette, bullish, provisional = false) {
      const textSize = String(indicatorStateSeriesControl(
        contract,
        group,
        "text_size",
        "medium",
      ) || "medium").toLowerCase();
      const label = indicatorStateSeriesDirectionLabel(bullish ? "long" : "short");
      return {
        type: "marker",
        lines: [bullish ? "▲" : "▼", `${label}${provisional ? "?" : ""}`],
        color: indicatorStateSeriesStateColor(
          contract,
          palette,
          bullish ? "bullish" : "bearish",
          provisional ? 0.78 : 1,
        ),
        tone: bullish ? "positive" : "negative",
        marker_color_policy: "theme",
        marker_glyph_color_policy: "tone",
        marker_font_scale: indicatorTextSizeScales[textSize] || indicatorTextSizeScales.medium,
        marker_text_anchor: bullish ? "upper_left" : "lower_left",
        marker_anchor_dot: false,
        marker_theme_text_light: "#000000",
        marker_theme_text_dark: "#9CA3AF",
        no_tick: true,
      };
    }

    function indicatorStateSeriesField(contract, name, fallback = name) {
      return String(contract?.fields?.[name] || fallback);
    }

    function indicatorStateSeriesPoint(row, context, contract, provisional = false) {
      if (!row || typeof row !== "object") return null;
      const tsField = indicatorStateSeriesField(contract, "ts");
      const centerField = indicatorStateSeriesField(contract, "center");
      const upperField = indicatorStateSeriesField(contract, "upper");
      const lowerField = indicatorStateSeriesField(contract, "lower");
      const residualField = indicatorStateSeriesField(contract, "residual");
      const stateField = indicatorStateSeriesField(contract, "state");
      const ts = row[tsField];
      const index = context.timeLookup.byTs.get(timestampKey(ts));
      const center = Number(row[centerField]);
      const upper = Number(row[upperField]);
      const lower = Number(row[lowerField]);
      const residual = Number(row[residualField]);
      if (
        !Number.isFinite(index)
        || ![center, upper, lower, residual].every(Number.isFinite)
      ) return null;
      const px = context.x(index);
      if (
        px < context.pad.left - context.xStep
        || px > context.width - context.pad.right + context.xStep
      ) return null;
      return {
        ts,
        px,
        center,
        upper,
        lower,
        close: center + residual,
        state: String(row[stateField] || contract?.initial_state || "neutral").toLowerCase(),
        provisional,
      };
    }

    function indicatorStateSeriesPoints(context, contract, group) {
      const indicatorId = String(context.item?.source || "");
      const indicator = context.snapshot?.indicators?.[indicatorId] || {};
      const series = Array.isArray(indicator.series) ? indicator.series : [];
      const renderBars = clamp(
        Number(context.payload?.render_bars || contract?.render_bars) || 240,
        1,
        1000,
      );
      const points = series.slice(-renderBars)
        .map(row => indicatorStateSeriesPoint(row, context, contract, false))
        .filter(Boolean);
      const calculationMode = String(indicatorStateSeriesControl(
        contract,
        group,
        "calculation_mode",
        "confirmed",
      )).toLowerCase();
      if (
        calculationMode === String(contract?.preview?.mode || "provisional").toLowerCase()
        && indicator.preview?.confirmed === false
      ) {
        const previewPoint = indicatorStateSeriesPoint(indicator.preview, context, contract, true);
        if (previewPoint) {
          const existing = points.findIndex(point => point.ts === previewPoint.ts);
          if (existing >= 0) points.splice(existing, 1, previewPoint);
          else points.push(previewPoint);
        }
      }
      return points;
    }

    function indicatorStateSeriesMode(contract, group) {
      const requested = String(indicatorStateSeriesControl(
        contract,
        group,
        "mode",
        Object.keys(contract?.modes || {})[0] || "bands",
      ) || "").trim();
      const modes = contract?.modes && typeof contract.modes === "object"
        ? contract.modes
        : {};
      return String(
        modes[requested]
        || Object.entries(modes).find(([name]) => name.toLowerCase() === requested.toLowerCase())?.[1]
        || requested,
      ).toLowerCase();
    }

    function indicatorStateSeriesSegmentStyle(context, contract, palette, point, width, alpha = 1) {
      const previewAlpha = point.provisional
        ? clamp(Number(contract?.preview?.line_alpha) || 0.72, 0, 1)
        : 1;
      context.ctx.strokeStyle = indicatorStateSeriesStateColor(
        contract,
        palette,
        point.state,
        alpha * previewAlpha,
      );
      context.ctx.lineWidth = width;
      context.ctx.setLineDash(point.provisional && Array.isArray(contract?.preview?.dash)
        ? contract.preview.dash.map(value => Math.max(0, Number(value) || 0))
        : []);
      context.ctx.lineCap = "round";
      context.ctx.lineJoin = "round";
    }

    function drawIndicatorStateSeriesSegment(context, contract, palette, previous, current, field, width, alpha = 1) {
      indicatorStateSeriesSegmentStyle(context, contract, palette, current, width, alpha);
      context.ctx.beginPath();
      context.ctx.moveTo(previous.px, context.y(previous[field]));
      context.ctx.lineTo(current.px, context.y(current[field]));
      context.ctx.stroke();
    }

    function fillIndicatorStateSeriesQuad(context, contract, palette, previous, current, topField, bottomField, alpha) {
      const previewAlpha = current.provisional
        ? clamp(Number(contract?.preview?.fill_alpha) || 0.50, 0, 1)
        : 1;
      context.ctx.fillStyle = indicatorStateSeriesStateColor(
        contract,
        palette,
        current.state,
        alpha * previewAlpha,
      );
      context.ctx.beginPath();
      context.ctx.moveTo(previous.px, context.y(previous[topField]));
      context.ctx.lineTo(current.px, context.y(current[topField]));
      context.ctx.lineTo(current.px, context.y(current[bottomField]));
      context.ctx.lineTo(previous.px, context.y(previous[bottomField]));
      context.ctx.closePath();
      context.ctx.fill();
    }

    function fillIndicatorStateSeriesGradient(context, contract, palette, previous, current, trailField, alpha) {
      const previewAlpha = current.provisional
        ? clamp(Number(contract?.preview?.fill_alpha) || 0.50, 0, 1)
        : 1;
      const resolvedAlpha = alpha * previewAlpha;
      const trailY = (context.y(previous[trailField]) + context.y(current[trailField])) * 0.5;
      const priceY = (context.y(previous.close) + context.y(current.close)) * 0.5;
      const color = indicatorStateSeriesStateColor(contract, palette, current.state, 1);
      const gradient = Math.abs(trailY - priceY) < 1
        ? null
        : canvasLinearGradient(context.ctx, 0, trailY, 0, priceY, [
            { offset: 0, color: rgbaFromCssColor(color, resolvedAlpha) },
            { offset: 1, color: rgbaFromCssColor(color, 0) },
          ]);
      context.ctx.fillStyle = gradient || rgbaFromCssColor(color, resolvedAlpha);
      context.ctx.beginPath();
      context.ctx.moveTo(previous.px, context.y(previous[trailField]));
      context.ctx.lineTo(current.px, context.y(current[trailField]));
      context.ctx.lineTo(current.px, context.y(current.close));
      context.ctx.lineTo(previous.px, context.y(previous.close));
      context.ctx.closePath();
      context.ctx.fill();
    }

    function drawIndicatorStateSeriesPreviewLabel(context, contract, group, palette) {
      const indicatorId = String(context.item?.source || "");
      const indicator = context.snapshot?.indicators?.[indicatorId] || {};
      const hiddenWhenSettingTrue = String(
        contract?.labels?.hidden_when_setting_true || "",
      ).trim();
      if (hiddenWhenSettingTrue && indicator.settings?.[hiddenWhenSettingTrue] === true) return;
      const calculationMode = String(indicatorStateSeriesControl(
        contract,
        group,
        "calculation_mode",
        "confirmed",
      )).toLowerCase();
      if (calculationMode !== String(contract?.preview?.mode || "provisional").toLowerCase()) return;
      const events = Array.isArray(indicator.preview_events) ? indicator.preview_events : [];
      const event = events[events.length - 1];
      if (!event || event.confirmed !== false || event.previous_state === "neutral") return;
      const bullish = String(event.state || "").toLowerCase() === "bullish";
      const anchor = String(indicator.settings?.label_anchor || "main").toLowerCase();
      const basePrice = anchor === "high_low"
        ? Number(bullish ? event.bar_low : event.bar_high)
        : anchor === "bands"
          ? Number(bullish ? event.lower : event.upper)
          : Number(event.center);
      const labelAtr = Number(event.label_atr);
      const offsetMult = clamp(Number(indicator.settings?.label_offset_mult) || 0, 0, 100);
      if (!Number.isFinite(basePrice) || !Number.isFinite(labelAtr)) return;
      const presentation = indicatorStateSeriesTransitionPresentation(
        contract,
        group,
        palette,
        bullish,
        true,
      );
      context.primitives.marker(
        context.ctx,
        {
          ts: event.ts,
          price: basePrice + (bullish ? -1 : 1) * labelAtr * offsetMult,
          ...presentation,
          opacity: 0.78,
          interactive: true,
          role: contract?.labels?.role || "state_transition",
          direction: event.direction,
          event_code: event.event_code,
          code: event.code,
          source: indicatorId,
          layer: "signals",
        },
        context.timeLookup.byTs,
        context.x,
        context.y,
        context.pad,
        context.width,
      );
    }

    function drawGenericIndicatorStateSeries(context) {
      const indicatorId = String(context.item?.source || "");
      const contract = indicatorStateSeriesContract(context.snapshot, indicatorId);
      if (!contract || context.payload?.kind !== "indicator_state_series_v1") return;
      const group = indicatorStateForId(indicatorId) || {};
      const palette = indicatorStateSeriesPalette(contract, group);
      const points = indicatorStateSeriesPoints(context, contract, group);
      if (points.length < 2) {
        drawIndicatorStateSeriesPreviewLabel(context, contract, group, palette);
        return;
      }
      const line = contract.line || {};
      const fillAlpha = contract.fill_alpha || {};
      const showBandFill = indicatorStateSeriesControl(contract, group, "fill", true) !== false;
      const mode = indicatorStateSeriesMode(contract, group);
      context.ctx.save();
      context.ctx.beginPath();
      context.ctx.rect(
        context.pad.left,
        context.pad.top,
        Math.max(0, context.width - context.pad.left - context.pad.right),
        context.priceH,
      );
      context.ctx.clip();
      for (let index = 1; index < points.length; index += 1) {
        const previous = points[index - 1];
        const current = points[index];
        if (mode === "single") {
          fillIndicatorStateSeriesGradient(
            context,
            contract,
            palette,
            previous,
            current,
            "center",
            clamp(Number(fillAlpha.single) || 0.25, 0, 1),
          );
          drawIndicatorStateSeriesSegment(
            context,
            contract,
            palette,
            previous,
            current,
            "center",
            clamp(Number(line.single_width) || 2, 0.5, 12),
          );
        } else if (mode === "trail") {
          if (
            current.state !== previous.state
            || !["bullish", "bearish"].includes(current.state)
          ) continue;
          const trailField = current.state === "bullish" ? "lower" : "upper";
          fillIndicatorStateSeriesGradient(
            context,
            contract,
            palette,
            previous,
            current,
            trailField,
            clamp(Number(fillAlpha.trail) || 0.40, 0, 1),
          );
          drawIndicatorStateSeriesSegment(
            context,
            contract,
            palette,
            previous,
            current,
            trailField,
            clamp(Number(line.trail_width) || 5, 0.5, 12),
            clamp(Number(line.trail_alpha) || 0.40, 0, 1),
          );
        } else {
          if (showBandFill) {
            fillIndicatorStateSeriesQuad(
              context,
              contract,
              palette,
              previous,
              current,
              "upper",
              "lower",
              clamp(Number(fillAlpha.bands) || 0.20, 0, 1),
            );
          }
          for (const field of ["upper", "lower"]) {
            drawIndicatorStateSeriesSegment(
              context,
              contract,
              palette,
              previous,
              current,
              field,
              clamp(Number(line.band_width) || 1, 0.5, 12),
              clamp(Number(line.band_alpha) || 0.40, 0, 1),
            );
          }
        }
      }
      context.ctx.restore();
      drawIndicatorStateSeriesPreviewLabel(context, contract, group, palette);
    }

    function indicatorStateSeriesOverlayFilter(item, group, snapshot) {
      const admitted = genericIndicatorOverlayFilter(item, group);
      if (!admitted.length) return [];
      const overlay = admitted[0];
      const indicatorId = String(overlay.source || "");
      const contract = indicatorStateSeriesContract(snapshot, indicatorId);
      if (!contract) return admitted;
      const palette = indicatorStateSeriesPalette(contract, group);
      if (overlay.role === contract?.labels?.role) {
        const bullish = String(overlay.direction || "").toLowerCase() === "long";
        return [{
          ...overlay,
          ...indicatorStateSeriesTransitionPresentation(
            contract,
            group,
            palette,
            bullish,
            false,
          ),
        }];
      }
      if (overlay.type === "table" && overlay.table && typeof overlay.table === "object") {
        const latestState = snapshot?.indicators?.[indicatorId]?.latest?.state;
        return [{
          ...overlay,
          table: {
            ...overlay.table,
            accent: indicatorStateSeriesStateColor(contract, palette, latestState, 1),
            bg: rgbaFromCssColor(palette.bg || css("--panel-2"), 0.14),
            text: palette.text || css("--text"),
            gradient: {
              stops: [
                { offset: 0, color: rgbaFromCssColor(palette.bg || css("--panel-2"), 0.14) },
                { offset: 1, color: rgbaFromCssColor(palette.bg || css("--panel-2"), 0.05) },
              ],
            },
          },
        }];
      }
      return admitted;
    }

    function indicatorStateSeriesCandleStyles(indicatorId, { snapshot, bars }) {
      const contract = indicatorStateSeriesContract(snapshot, indicatorId);
      if (!contract) return [];
      const group = indicatorStateForId(indicatorId) || {};
      if (indicatorStateSeriesControl(contract, group, "candles", true) === false) return [];
      const palette = indicatorStateSeriesPalette(contract, group);
      const indicator = snapshot?.indicators?.[indicatorId] || {};
      const fields = contract.fields || {};
      const tsField = String(fields.ts || "ts");
      const stateField = String(fields.state || "state");
      const statesByTs = new Map((Array.isArray(indicator.series) ? indicator.series : [])
        .map(row => [timestampKey(row?.[tsField]), String(row?.[stateField] || "neutral").toLowerCase()]));
      const calculationMode = String(indicatorStateSeriesControl(
        contract,
        group,
        "calculation_mode",
        "confirmed",
      )).toLowerCase();
      if (
        calculationMode === String(contract?.preview?.mode || "provisional").toLowerCase()
        && indicator.preview?.confirmed === false
      ) {
        statesByTs.set(
          timestampKey(indicator.preview?.[tsField]),
          String(indicator.preview?.[stateField] || "neutral").toLowerCase(),
        );
      }
      let activeState = String(contract.initial_state || "neutral").toLowerCase();
      return (Array.isArray(bars) ? bars : []).map(bar => {
        const key = timestampKey(bar?.ts);
        if (statesByTs.has(key)) activeState = statesByTs.get(key);
        return { color: indicatorStateSeriesStateColor(contract, palette, activeState, 1) };
      });
    }

    function registerIndicatorStateSeriesCandleProvider(indicatorId, options = {}) {
      registerIndicatorCandleStyleProvider(
        indicatorId,
        context => indicatorStateSeriesCandleStyles(indicatorId, context),
        options,
      );
    }

    function registerIndicatorCustomOverlayRenderer(rendererRef, renderer) {
      const key = String(rendererRef || "").trim();
      if (!key || typeof renderer !== "function") {
        throw new Error(`Invalid indicator custom overlay renderer: ${key || "missing"}`);
      }
      if (indicatorCustomOverlayRendererRegistry.has(key)) {
        throw new Error(`Duplicate indicator custom overlay renderer: ${key}`);
      }
      indicatorCustomOverlayRendererRegistry.set(key, renderer);
    }

    function drawIndicatorCustomOverlay(rendererRef, context) {
      const key = String(rendererRef || "").trim();
      const renderer = indicatorCustomOverlayRendererRegistry.get(key);
      if (!renderer) {
        console.error(`Indicator custom overlay renderer is unavailable: ${key || "missing"}`);
        return false;
      }
      renderer(context);
      return true;
    }

    let indicatorOverlayPrimitiveCache = null;

    function indicatorRendererContract(indicatorId) {
      const spec = typeof indicatorSpec === "function" ? indicatorSpec(indicatorId) : (window.INDICATOR_REGISTRY_MANIFEST || {})[indicatorId] || {};
      return spec?.renderer_contract || {};
    }

    function indicatorRendererTableContract(indicatorId) {
      const table = indicatorRendererContract(indicatorId).table;
      return table && typeof table === "object" ? table : {};
    }

    function indicatorOverlayPrimitiveRegistry() {
      if (indicatorOverlayPrimitiveCache) return indicatorOverlayPrimitiveCache;
      indicatorOverlayPrimitiveCache = Object.freeze({
        lookupForVisible: genericOverlayLookupForVisible,
        xForTime: xForOverlayTime,
        box: drawGenericOverlayBox,
        line: drawGenericOverlayLine,
        marker: drawGenericOverlayMarker,
        label: drawGenericOverlayLabel,
        table: drawGenericIndicatorTable,
        textLabel: drawIndicatorTextLabel,
        verticalTextLabel: drawVerticalIndicatorTextLabel,
        signalTick: drawSignalTick,
        stackedLabelOffset,
        stackedLabels: stackedOverlayLabels,
        labelLines: buildIndicatorLabelLines,
        tablePositionForSource,
        linearGradient: canvasLinearGradient,
      });
      return indicatorOverlayPrimitiveCache;
    }

    function drawGenericIndicatorOverlays(ctx, snapshot, visible, pad, priceH, width, x, y, xStep, labelStacks = null) {
      const bars = visible.bars;
      if (!bars.length) {
        if (typeof updateIndicatorLensTables === "function") updateIndicatorLensTables([]);
        return;
      }
      const primitives = indicatorOverlayPrimitiveRegistry();
      const timeLookup = primitives.lookupForVisible(visible, bars, snapshot?.bars || bars);
      const { byTs } = timeLookup;
      const timeWindow = visibleTimeWindowForBars(bars, 2);
      const overlays = collectManagedIndicatorOverlays(snapshot, visible);
      const tableStackOffsets = {
        top: 0,
        bottom: 0,
        "top-left": 0,
        "top-right": 0,
        "bottom-left": 0,
        "bottom-right": 0,
      };
      const indicatorLensTables = [];
      const tablePad = indicatorTablePlacementPad(pad, width);
      const overlayBuckets = { box: [], custom: [], line: [], marker: [], label: [], table: [] };
      const glyphLabelOverlays = [];
      const boxedLabelOverlays = [];
      for (const overlay of overlays) {
        if (!overlayIntersectsVisible(overlay, bars, byTs, y, pad, priceH, timeWindow)) continue;
        if (overlay.type === "label") {
          if (overlay.glyph_only) glyphLabelOverlays.push(overlay);
          else boxedLabelOverlays.push(overlay);
        } else if (overlayBuckets[overlay.type]) {
          overlayBuckets[overlay.type].push(overlay);
        }
      }
      for (const item of overlayBuckets.box) {
        primitives.box(ctx, item, timeLookup, pad, priceH, width, xStep, x, y);
      }
      for (const item of overlayBuckets.custom) {
        drawIndicatorCustomOverlay(item.renderer_ref, {
          ctx,
          item,
          payload: item.payload,
          timeLookup,
          pad,
          priceH,
          width,
          xStep,
          x,
          y,
          bars,
          snapshot,
          visible,
          primitives,
        });
      }
      for (const item of overlayBuckets.line) {
        primitives.line(ctx, item, timeLookup, pad, priceH, width, xStep, x, y);
      }
      for (const item of overlayBuckets.marker) {
        primitives.marker(ctx, item, byTs, x, y, pad, width);
      }
      for (const item of primitives.stackedLabels(boxedLabelOverlays, byTs, labelStacks)) {
        primitives.label(ctx, item, byTs, pad, priceH, width, x, y, labelStacks, bars);
      }
      for (const item of overlayBuckets.table) {
        const position = primitives.tablePositionForSource(item.source);
        if (position === "off") continue;
        if (position === "dock") {
          indicatorLensTables.push({ overlay: item, reason: "dock" });
          continue;
        }
        const stackOffset = tableStackOffsets[position] || 0;
        const estimatedHeight = indicatorTableEstimatedHeight(item, position);
        const stackLimit = indicatorTableStackLimit(position, priceH);
        if (stackOffset + estimatedHeight > stackLimit || !indicatorTableWidthLayout(item, position, tablePad, width).fits) {
          indicatorLensTables.push({ overlay: item, reason: "overflow" });
          continue;
        }
        const consumedHeight = primitives.table(
          ctx,
          item,
          tablePad,
          width,
          priceH,
          stackOffset,
          position,
        );
        tableStackOffsets[position] = stackOffset + Math.max(
          Number(consumedHeight) || estimatedHeight,
          0,
        );
      }
      if (typeof updateIndicatorLensTables === "function") {
        updateIndicatorLensTables(indicatorLensTables);
      }
      for (const item of glyphLabelOverlays) {
        primitives.label(ctx, item, byTs, pad, priceH, width, x, y, null, bars);
      }
    }
    function drawDayLevels(ctx, pad, width, y) {
      const nyCfg = state.indicators.nyRange || {};
      const previousDay = state.marketContext?.previousDay || null;
      if (nyCfg.prevDayLevels && previousDay) {
        if (Number.isFinite(Number(previousDay.open))) {
          drawHorizontalLevel(ctx, pad, width, y(previousDay.open), "PD O", "rgba(148,163,184,0.92)", [5, 5], 1);
        }
        if (Number.isFinite(Number(previousDay.close))) {
          drawHorizontalLevel(ctx, pad, width, y(previousDay.close), "PD C", "rgba(148,163,184,0.84)", [2, 6], 1);
        }
      }
    }

    function drawOpeningRange(ctx, bars, pad, height, xStep, x, y, width) {
      const cfg = state.indicators.nyRange;
      const range = state.marketContext?.openingRange;
      if (!cfg.enabled || !range || !bars.length) return;
      const high = Number(range.high);
      const low = Number(range.low);
      const mid = Number(range.mid);
      if (![high, low, mid].every(Number.isFinite)) return;
      const color = css("--ny-range");
      const times = bars.map(bar => Date.parse(bar.ts));
      const rangeStart = Date.parse(range.startTs);
      const rangeEnd = Date.parse(range.endTs);
      const sessionEnd = Date.parse(range.sessionEndTs);
      if (![rangeStart, rangeEnd, sessionEnd].every(Number.isFinite)) return;
      const plotLeft = pad.left;
      const plotRight = width - pad.right;
      if (openingRangeIntersectsVisible(bars, range)) {
        const leftRaw = rangeStart <= times[0]
          ? plotLeft
          : xForTime(times, rangeStart, xStep, x);
        const rightRaw = rangeEnd >= times[times.length - 1]
          ? plotRight
          : xForTime(times, rangeEnd, xStep, x);
        if (leftRaw !== null && rightRaw !== null) {
          const left = clamp(leftRaw, plotLeft, plotRight);
          const right = clamp(rightRaw, plotLeft, plotRight);
          if (right > plotLeft && left < plotRight && Math.abs(right - left) >= 1) {
            const top = y(high);
            const bottom = y(low);
            ctx.save();
            ctx.fillStyle = rgbaFromCssColor(
              color,
              clamp(Number(cfg.opacity) || 14, 0, 60) / 100,
            );
            ctx.fillRect(left, top, right - left, Math.max(bottom - top, 1));
            ctx.setLineDash(lineDashForStyle(cfg.style));
            ctx.strokeStyle = rgbaFromCssColor(color, 0.85);
            ctx.lineWidth = 1;
            ctx.strokeRect(left, top, right - left, Math.max(bottom - top, 1));
            ctx.restore();
          }
        }
      }
      if (!cfg.priceLines) return;
      if (sessionEnd < times[0] || rangeStart > times[times.length - 1]) return;
      const lineStartRaw = rangeStart <= times[0]
        ? plotLeft
        : xForTime(times, rangeStart, xStep, x);
      const lineEndRaw = sessionEnd >= times[times.length - 1]
        ? plotRight
        : xForTime(times, sessionEnd, xStep, x);
      if (lineStartRaw === null || lineEndRaw === null) return;
      const lineStart = clamp(lineStartRaw, plotLeft, plotRight);
      const lineEnd = clamp(lineEndRaw, plotLeft, plotRight);
      if (lineEnd - lineStart < 1) return;
      const lineOptions = { startX: lineStart, endX: lineEnd, labelX: lineEnd - 4 };
      drawHorizontalLevel(ctx, pad, width, y(high), "OR H", color, lineDashForStyle(cfg.style), 1, lineOptions);
      drawHorizontalLevel(ctx, pad, width, y(low), "OR L", color, lineDashForStyle(cfg.style), 1, lineOptions);
      drawHorizontalLevel(ctx, pad, width, y(mid), "OR M", rgbaFromCssColor(color, 0.62), [2, 6], 1, lineOptions);
    }
