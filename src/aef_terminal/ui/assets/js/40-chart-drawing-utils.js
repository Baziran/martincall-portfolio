    function rgbaFromCssColor(color, alpha) {
      const cacheKey = `${String(color || "")}|${alpha}`;
      if (rgbaColorCache.has(cacheKey)) return rgbaColorCache.get(cacheKey);
      const hex = String(color || "").trim();
      let value = hex;
      if (/^#[0-9a-f]{6}([0-9a-f]{2})?$/i.test(hex)) {
        const r = parseInt(hex.slice(1, 3), 16);
        const g = parseInt(hex.slice(3, 5), 16);
        const b = parseInt(hex.slice(5, 7), 16);
        value = `rgba(${r}, ${g}, ${b}, ${alpha})`;
      } else {
        const rgba = hex.match(/^rgba?[(]([^)]+)[)]$/i);
        if (rgba) {
          const parts = rgba[1].split(",").map(part => part.trim());
          if (parts.length >= 3) value = `rgba(${parts[0]}, ${parts[1]}, ${parts[2]}, ${alpha})`;
        }
      }
      if (rgbaColorCache.size > 512) rgbaColorCache.clear();
      rgbaColorCache.set(cacheKey, value);
      return value;
    }

    function canvasLinearGradient(ctx, x0, y0, x1, y1, stops) {
      if (!ctx || typeof ctx.createLinearGradient !== "function") return null;
      const normalizedStops = (Array.isArray(stops) ? stops : [])
        .map(stop => ({
          offset: clamp(Number(stop?.offset), 0, 1),
          color: String(stop?.color || "").trim(),
        }))
        .filter(stop => Number.isFinite(stop.offset) && stop.color)
        .sort((left, right) => left.offset - right.offset);
      if (!normalizedStops.length) return null;
      const gradient = ctx.createLinearGradient(
        Number(x0) || 0,
        Number(y0) || 0,
        Number(x1) || 0,
        Number(y1) || 0,
      );
      normalizedStops.forEach(stop => gradient.addColorStop(stop.offset, stop.color));
      return gradient;
    }

    const chartMarkerIntroState = new Map();

    function chartMarkerIntro(key, durationMs = 680) {
      const id = String(key || "");
      if (!id || durationMs <= 0) return { alpha: 1, scale: 1 };
      const now = Date.now();
      if (!chartMarkerIntroState.has(id)) chartMarkerIntroState.set(id, now);
      const age = now - Number(chartMarkerIntroState.get(id) || now);
      if (chartMarkerIntroState.size > 700) {
        const cutoff = now - 120000;
        for (const [itemKey, startedAt] of chartMarkerIntroState.entries()) {
          if (startedAt < cutoff) chartMarkerIntroState.delete(itemKey);
        }
      }
      if (age >= durationMs) return { alpha: 1, scale: 1 };
      const t = clamp(age / durationMs, 0, 1);
      const eased = 1 - Math.pow(1 - t, 3);
      return {
        alpha: clamp(0.12 + eased * 0.88, 0.12, 1),
        scale: clamp(0.78 + eased * 0.22, 0.78, 1),
      };
    }

    function applyChartMarkerIntro(ctx, key, x, y, durationMs = 680) {
      const intro = chartMarkerIntro(key, durationMs);
      if (intro.alpha >= 0.999 && Math.abs(intro.scale - 1) < 0.001) return false;
      ctx.globalAlpha *= intro.alpha;
      ctx.translate(x, y);
      ctx.scale(intro.scale, intro.scale);
      ctx.translate(-x, -y);
      return true;
    }

    function parseCanvasColor(color) {
      const raw = String(color || "").trim();
      if (!raw) return null;
      if (mcThemeCache.parsed.has(raw)) return mcThemeCache.parsed.get(raw);
      let parsed = null;
      if (/^#[0-9a-f]{3}$/i.test(raw)) {
        parsed = {
          r: parseInt(raw[1] + raw[1], 16),
          g: parseInt(raw[2] + raw[2], 16),
          b: parseInt(raw[3] + raw[3], 16),
          a: 1,
        };
      } else if (/^#[0-9a-f]{6}([0-9a-f]{2})?$/i.test(raw)) {
        parsed = {
          r: parseInt(raw.slice(1, 3), 16),
          g: parseInt(raw.slice(3, 5), 16),
          b: parseInt(raw.slice(5, 7), 16),
          a: raw.length === 9 ? clamp(parseInt(raw.slice(7, 9), 16) / 255, 0, 1) : 1,
        };
      } else {
        const rgba = raw.match(/^rgba?[(]([^)]+)[)]$/i);
        if (rgba) {
          const parts = rgba[1].split(",").map(part => part.trim());
          if (parts.length >= 3) {
            const parseChannel = value => {
              if (String(value).endsWith("%")) return clamp(parseFloat(value) * 2.55, 0, 255);
              return clamp(parseFloat(value), 0, 255);
            };
            const alpha = parts.length >= 4 ? clamp(parseFloat(parts[3]), 0, 1) : 1;
            parsed = {
              r: parseChannel(parts[0]),
              g: parseChannel(parts[1]),
              b: parseChannel(parts[2]),
              a: Number.isFinite(alpha) ? alpha : 1,
            };
            if (![parsed.r, parsed.g, parsed.b].every(Number.isFinite)) parsed = null;
          }
        }
      }
      if (mcThemeCache.parsed.size > 512) mcThemeCache.parsed.clear();
      mcThemeCache.parsed.set(raw, parsed);
      return parsed;
    }

    function canvasColorToHex(color, fallback = "#123a63") {
      const parsed = parseCanvasColor(color) || parseCanvasColor(fallback);
      if (!parsed) return fallback;
      const toHex = value => Math.round(clamp(Number(value) || 0, 0, 255)).toString(16).padStart(2, "0");
      return `#${toHex(parsed.r)}${toHex(parsed.g)}${toHex(parsed.b)}`;
    }

    function canvasColorString(rgb, alpha = 1) {
      const r = Math.round(clamp(Number(rgb?.r) || 0, 0, 255));
      const g = Math.round(clamp(Number(rgb?.g) || 0, 0, 255));
      const b = Math.round(clamp(Number(rgb?.b) || 0, 0, 255));
      const a = clamp(Number(alpha), 0, 1);
      if (Number.isFinite(a) && a < 0.999) return `rgba(${r}, ${g}, ${b}, ${a})`;
      return `#${r.toString(16).padStart(2, "0")}${g.toString(16).padStart(2, "0")}${b.toString(16).padStart(2, "0")}`;
    }

    function mixCanvasRgb(from, to, amount) {
      const t = clamp(Number(amount) || 0, 0, 1);
      return {
        r: from.r + (to.r - from.r) * t,
        g: from.g + (to.g - from.g) * t,
        b: from.b + (to.b - from.b) * t,
        a: from.a,
      };
    }

    function blendCanvasColor(color, baseColor = css("--chart-bg")) {
      const cacheKey = `${String(color || "")}|${String(baseColor || "")}`;
      if (mcThemeCache.blended.has(cacheKey)) return mcThemeCache.blended.get(cacheKey);
      const fg = parseCanvasColor(color);
      if (!fg) return null;
      const bg = parseCanvasColor(baseColor) || { r: 15, g: 23, b: 32, a: 1 };
      const alpha = clamp(Number(fg.a), 0, 1);
      const value = {
        r: fg.r * alpha + bg.r * (1 - alpha),
        g: fg.g * alpha + bg.g * (1 - alpha),
        b: fg.b * alpha + bg.b * (1 - alpha),
        a: 1,
      };
      if (mcThemeCache.blended.size > 512) mcThemeCache.blended.clear();
      mcThemeCache.blended.set(cacheKey, value);
      return value;
    }

    function relativeLuminance(rgb) {
      const channel = value => {
        const normalized = clamp(Number(value) || 0, 0, 255) / 255;
        return normalized <= 0.03928 ? normalized / 12.92 : Math.pow((normalized + 0.055) / 1.055, 2.4);
      };
      return 0.2126 * channel(rgb.r) + 0.7152 * channel(rgb.g) + 0.0722 * channel(rgb.b);
    }

    function contrastRatio(fg, bg) {
      const a = relativeLuminance(fg);
      const b = relativeLuminance(bg);
      const light = Math.max(a, b);
      const dark = Math.min(a, b);
      return (light + 0.05) / (dark + 0.05);
    }

    function calibratedCanvasColor(color, options = {}) {
      const baseColor = options.baseColor || css("--chart-bg");
      const minRatio = clamp(Number(options.minRatio) || 2.35, 1, 7);
      const cacheKey = `cal:${String(color || "")}|${String(baseColor || "")}|${minRatio}`;
      if (mcThemeCache.readable.has(cacheKey)) return mcThemeCache.readable.get(cacheKey);
      const raw = parseCanvasColor(color);
      const bg = parseCanvasColor(baseColor) || parseCanvasColor(css("--chart-bg"));
      if (!raw || !bg) return color;
      const blended = blendCanvasColor(color, baseColor);
      if (!blended || contrastRatio(blended, bg) >= minRatio) {
        mcThemeCache.readable.set(cacheKey, color);
        return color;
      }
      const bgLum = relativeLuminance(bg);
      const target = bgLum >= 0.5
        ? { r: 0, g: 0, b: 0, a: raw.a }
        : { r: 255, g: 255, b: 255, a: raw.a };
      let best = raw;
      for (let step = 1; step <= 12; step += 1) {
        const mixed = mixCanvasRgb(raw, target, step / 12);
        const candidate = { ...mixed, a: raw.a };
        const visible = {
          r: candidate.r * raw.a + bg.r * (1 - raw.a),
          g: candidate.g * raw.a + bg.g * (1 - raw.a),
          b: candidate.b * raw.a + bg.b * (1 - raw.a),
          a: 1,
        };
        best = candidate;
        if (contrastRatio(visible, bg) >= minRatio) break;
      }
      const value = canvasColorString(best, raw.a);
      if (mcThemeCache.readable.size > 512) mcThemeCache.readable.clear();
      mcThemeCache.readable.set(cacheKey, value);
      return value;
    }

    function readableTextForBg(bgColor, preferred = null) {
      const rawBackgrounds = (Array.isArray(bgColor) ? bgColor : [bgColor])
        .map(color => String(color || "").trim())
        .filter(Boolean);
      const cacheKey = `${JSON.stringify(rawBackgrounds)}|${String(preferred || "")}`;
      if (mcThemeCache.readable.has(cacheKey)) return mcThemeCache.readable.get(cacheKey);
      const backgrounds = rawBackgrounds
        .map(color => blendCanvasColor(color))
        .filter(Boolean);
      if (!backgrounds.length) return preferred || css("--text");
      const lightColor = themeColor("label-light");
      const darkColor = themeColor("label-dark");
      const light = blendCanvasColor(lightColor, "#000000") || { r: 255, g: 255, b: 255, a: 1 };
      const dark = blendCanvasColor(darkColor, "#ffffff") || { r: 6, g: 26, b: 47, a: 1 };
      const preferredRgb = preferred ? blendCanvasColor(preferred, "#000000") : null;
      const minimumContrast = candidate => Math.min(
        ...backgrounds.map(background => contrastRatio(candidate, background)),
      );
      const value = preferredRgb && minimumContrast(preferredRgb) >= 4.5
        ? preferred
        : minimumContrast(light) >= minimumContrast(dark) ? lightColor : darkColor;
      if (mcThemeCache.readable.size > 512) mcThemeCache.readable.clear();
      mcThemeCache.readable.set(cacheKey, value);
      return value;
    }

    function themeColor(name, alpha = null) {
      const cacheKey = `${String(name || "")}|${alpha === null ? "base" : alpha}`;
      if (mcThemeCache.theme.has(cacheKey)) return mcThemeCache.theme.get(cacheKey);
      const table = themeColorTable();
      const raw = table[name] || css(`--canvas-${name}`) || css("--text");
      const value = String(name || "").startsWith("label-")
        ? raw
        : calibratedCanvasColor(raw, { minRatio: 2.25 });
      const out = alpha === null ? value : rgbaFromCssColor(value, alpha);
      if (mcThemeCache.theme.size > 512) mcThemeCache.theme.clear();
      mcThemeCache.theme.set(cacheKey, out);
      return out;
    }
    function isLightTheme() {
      refreshThemeColorCache();
      return mcThemeCache.key.startsWith("light");
    }

    function chartLineLabelRightX(pad, width, inset = 4) {
      return width - (Number(pad?.right) || 0) - inset;
    }

    function drawHorizontalLevel(ctx, pad, width, yy, label, color, dash = [5, 5], lineWidth = 1, options = {}) {
      const visibleColor = calibratedCanvasColor(color, { minRatio: 2.25 });
      const plotLeft = Number(pad?.left) || 0;
      const plotRight = width - (Number(pad?.right) || 0);
      const startX = Number.isFinite(Number(options.startX))
        ? clamp(Number(options.startX), plotLeft, plotRight)
        : plotLeft;
      const endX = Number.isFinite(Number(options.endX))
        ? clamp(Number(options.endX), plotLeft, plotRight)
        : plotRight;
      const labelRightX = Number.isFinite(Number(options.labelX))
        ? clamp(Number(options.labelX), plotLeft + 4, plotRight - 1)
        : chartLineLabelRightX(pad, width);
      ctx.save();
      ctx.strokeStyle = visibleColor;
      ctx.lineWidth = lineWidth;
      if (!options.axisOnly) {
        if (endX > startX) {
          ctx.setLineDash(dash);
          ctx.beginPath();
          ctx.moveTo(startX, yy);
          ctx.lineTo(endX, yy);
          ctx.stroke();
        }
      }
      ctx.setLineDash([]);
      if (options.axisLabel) {
        const tickLeft = width - pad.right + 3;
        const tickRight = Math.min(tickLeft + 14, width - 6);
        ctx.beginPath();
        ctx.moveTo(tickLeft, yy);
        ctx.lineTo(tickRight, yy);
        ctx.stroke();
      }
      if (label) {
        ctx.fillStyle = calibratedCanvasColor(color, { minRatio: 3.6 });
        ctx.font = "10px -apple-system, BlinkMacSystemFont, sans-serif";
        if (options.axisLabel) {
          ctx.textAlign = "right";
          ctx.textBaseline = "middle";
          ctx.fillText(label, width - 4, yy);
        } else {
          ctx.textAlign = "right";
          ctx.textBaseline = "bottom";
          ctx.fillText(label, labelRightX, yy - 3);
        }
      }
      ctx.restore();
      if (options.tooltipItem && typeof registerCanvasTooltip === "function") {
        const textWidth = String(label || "").length * 7 + 14;
        const axisWidth = Math.max(26, (Number(pad?.right) || 0) - 8);
        const hitW = options.axisLabel ? axisWidth : Math.max(26, textWidth);
        const hitX = options.axisLabel ? width - hitW / 2 - 4 : labelRightX - hitW / 2;
        registerCanvasTooltip("price", hitX, yy, hitW, 18, () => {
          if (typeof standardOverlayTooltip === "function") {
            return standardOverlayTooltip(options.tooltipItem, { source: options.tooltipItem.source });
          }
          return "";
        });
      }
    }

    function levelColor(level) {
      if (isVwapLevel(level)) return css("--vwap");
      return level.kind === "resistance" ? css("--level-high") : level.kind === "support" ? css("--level-low") : css("--blue");
    }

    function levelDash(level) {
      return isVwapLevel(level) ? lineDashForStyle(state.indicators.vwap.style) : [5, 5];
    }

    function levelWidth(level) {
      return isVwapLevel(level) ? clamp(Number(state.indicators.vwap.width) || 1, 1, 3) : 1;
    }

    function drawPriceLevels(ctx, snapshot, pad, width, y) {
      for (const level of activeChartLevels(snapshot)) {
        drawHorizontalLevel(
          ctx,
          pad,
          width,
          y(level.price),
          isVwapLevel(level) ? "VWAP" : "",
          levelColor(level),
          levelDash(level),
          levelWidth(level),
        );
      }
    }

    function priceAlertCanvasLabel(alert, selected = false) {
      return `${selected ? "SEL " : ""}${alert.kind === "ema233_touch" ? "EMA233" : alertDirectionMark(alert)} ${alert.fired ? "FIRED" : "ALERT"}`;
    }

    function priceAlertCanvasLeft(pad, width) {
      const companionRight = typeof gexLeftCompanionRight === "function"
        ? gexLeftCompanionRight(pad, width, state.snapshot)
        : pad.left;
      return Math.max(Number(pad.left) || 0, Number(companionRight) || 0);
    }

    function priceAlertCanvasLayout(pad, width, yy, labelW) {
      const leftEdge = priceAlertCanvasLeft(pad, width);
      const deleteSize = 20;
      const deleteCx = leftEdge + 14;
      const labelX = leftEdge + 32;
      const handleX1 = labelX - 5;
      const handleX2 = Math.min(width - pad.right - 8, labelX + labelW + 4);
      return {
        deleteSize,
        deleteCx,
        deleteCy: yy - 7,
        deleteX1: deleteCx - deleteSize / 2,
        deleteY1: yy - 17,
        deleteX2: deleteCx + deleteSize / 2,
        deleteY2: yy + 5,
        labelX,
        labelW,
        handleX1,
        handleX2,
        handleY: yy,
        hitX1: Math.max(leftEdge, handleX1 - 8),
        hitY1: yy - 16,
        hitX2: Math.min(width - pad.right, handleX2 + 16),
        hitY2: yy + 12,
      };
    }

    function drawPriceAlerts(ctx, pad, width, priceH, y, options = {}) {
      const excludeIds = new Set(options.excludeIds || []);
      const onlyIds = options.onlyIds ? new Set(options.onlyIds) : null;
      const alerts = state.alerts.rules.filter(alert =>
        alert.enabled
        && priceAlertMatchesScope(alert, state.instrumentId, state.timeframe)
        && !excludeIds.has(alert.id)
        && (!onlyIds || onlyIds.has(alert.id))
      );
      if (!alerts.length) return;
      ctx.save();
      ctx.font = "800 10px -apple-system, BlinkMacSystemFont, sans-serif";
      ctx.textAlign = "left";
      ctx.textBaseline = "bottom";
      for (const alert of alerts) {
        const dynamic = emaTouchState(alert, state.snapshot);
        const price = Number(dynamic?.level ?? alert.price);
        if (!Number.isFinite(price)) continue;
        const yy = y(price);
        if (yy < pad.top || yy > pad.top + priceH) continue;
        const selected = alert.id === state.alerts.selectedId;
        const lineAlpha = alert.fired ? 0.1 : selected ? 0.95 : 0.76;
        const textAlpha = alert.fired ? 0.55 : 0.96;
        const color = alert.fired ? themeColor("gold", 1) : themeColor("blue", 1);
        const lineColor = alert.fired ? themeColor("gold", lineAlpha) : themeColor("blue", lineAlpha);
        ctx.strokeStyle = lineColor;
        ctx.lineWidth = 0.35;
        ctx.setLineDash(alert.fired ? [2, 5] : [6, 4]);
        const leftEdge = priceAlertCanvasLeft(pad, width);
        ctx.beginPath();
        ctx.moveTo(leftEdge, yy);
        ctx.lineTo(alert.fired ? Math.min(width - pad.right, leftEdge + 168) : width - pad.right, yy);
        ctx.stroke();
        ctx.setLineDash([]);
        const label = priceAlertCanvasLabel(alert, selected);
        const labelW = ctx.measureText(label).width + 12;
        const layout = priceAlertCanvasLayout(pad, width, yy, labelW);
        if (selected) {
          ctx.globalAlpha = 0.14;
          ctx.fillStyle = color;
          ctx.fillRect(layout.hitX1, layout.hitY1, Math.max(1, layout.hitX2 - layout.hitX1), Math.max(1, layout.hitY2 - layout.hitY1));
          ctx.globalAlpha = 1;
        }
        ctx.strokeStyle = alert.fired ? themeColor("gold", 0.16) : themeColor("blue", selected ? 0.36 : 0.22);
        ctx.lineWidth = selected ? 6 : 4;
        ctx.lineCap = "round";
        ctx.beginPath();
        ctx.moveTo(layout.handleX1, layout.handleY);
        ctx.lineTo(layout.handleX2, layout.handleY);
        ctx.stroke();
        ctx.lineCap = "butt";
        ctx.fillStyle = alert.fired ? themeColor("gold", textAlpha) : themeColor("blue", textAlpha);
        ctx.fillText(label, layout.labelX, yy - 4);
        ctx.strokeStyle = alert.fired ? themeColor("gold", 0.72) : themeColor("blue", 0.86);
        ctx.lineWidth = 1.25;
        ctx.beginPath();
        ctx.moveTo(layout.deleteCx - 4, layout.deleteCy - 4);
        ctx.lineTo(layout.deleteCx + 4, layout.deleteCy + 4);
        ctx.moveTo(layout.deleteCx + 4, layout.deleteCy - 4);
        ctx.lineTo(layout.deleteCx - 4, layout.deleteCy + 4);
        ctx.stroke();
        ctx.beginPath();
        ctx.fillStyle = alert.fired ? themeColor("gold", 0.62) : themeColor("blue", 0.8);
        ctx.arc(leftEdge + 2, yy, selected ? 4.4 : 3.2, 0, Math.PI * 2);
        ctx.fill();
        const tolerance = dynamic ? `\\nTolerance ${fmt(dynamic.tolerance)}` : "";
        const dynamicLabel = dynamic ? "EMA 233 touch alert" : "price alert";
        const dragHint = dynamic ? "Click to select, DEL to delete." : "Drag only from the left handle, DEL to delete.";
        if (options.tooltips !== false) {
          registerCanvasTooltip("price", layout.deleteCx, layout.deleteCy, layout.deleteSize, layout.deleteSize, `Delete ${dynamicLabel}`);
          registerCanvasTooltip("price", (layout.hitX1 + layout.hitX2) / 2, (layout.hitY1 + layout.hitY2) / 2, layout.hitX2 - layout.hitX1, layout.hitY2 - layout.hitY1, `${dynamic ? "EMA 233 touch alert" : `Price alert ${alert.direction || "cross"}`}\\nLevel ${fmt(price)}${tolerance}\\n${alert.fired ? "Fired" : "Armed"}\\n${dragHint}`);
        }
      }
      ctx.restore();
    }

    function drawTradePlanLines(ctx, snapshot, pad, width, y) {
      if (!advisorTradePlanEnabled()) return;
      const widthPx = clamp(Number(state.indicators.tradePlan.width) || 1, 1, 3);
      const presentation = typeof isAdvisorChartMode === "function" && isAdvisorChartMode() && typeof advisorPlanPresentation === "function"
        ? advisorPlanPresentation(snapshot)
        : null;
      const candidatePlan = presentation && presentation.executable !== true;
      const plan = coherentDecisionPlan(snapshot?.decision);
      if (!plan) return;
      for (const [name, colorVar] of [["trigger", "--plan-trigger"], ["stop", "--plan-stop"], ["target", "--plan-target"]]) {
        const value = plan[name];
        if (value === null || value === undefined) continue;
        const label = name === "stop"
          ? STOP_MARK
          : name === "trigger" && candidatePlan
          ? "CAND trig"
          : name === "target"
          ? "tgt"
          : "trig";
        const tooltipItem = {
          type: "line",
          source: "trade_setup_card",
          scenario: "Trade setup plan",
          setup: "decision_plan",
          role: name,
          price: value,
          y1: value,
          y2: value,
          label,
          action: snapshot?.decision?.action || "PLAN",
          direction: snapshot?.decision?.direction || "",
          context_lines: [`Scenario source: ${snapshot?.decision?.kind || "decision"}`],
          metrics: {
            scenario_source: snapshot?.decision?.kind || "decision",
            role: name,
            price: value,
          },
        };
        drawHorizontalLevel(ctx, pad, width, y(value), label, css(colorVar), candidatePlan ? [3, 5] : [2, 4], widthPx, { axisLabel: true, axisOnly: true, tooltipItem });
      }
    }
    function normalizeCompactKey(value) {
      return String(value ?? "")
        .replace(/_/g, " ")
        .replace(/[·•]/g, "")
        .replace(/\s+/g, " ")
        .trim()
        .toUpperCase();
    }

    function compactLabelToken(value, limit = 6) {
      const text = String(value ?? "").replace(/_/g, " ").replace(/\s+/g, " ").trim();
      if (!text) return "";
      if (/^-?\d+(\.\d+)?$/.test(text)) return text;
      if (/^0(\.\d+)?$|^1(\.0+)?$/.test(text)) return text;
      const key = normalizeCompactKey(text);
      if (COMPACT_LABELS.has(key)) return COMPACT_LABELS.get(key);
      if (candidateIntentText(key)) return candidateCompactText(key);
      if (watchIntentText(key)) return watchCompactText(key);
      if (/^P5\\??/i.test(text)) return text.includes("?") ? "P5?" : "P5";
      if (/SFP|LIQ|LIQUIDITY/i.test(text)) return "LQ";
      if (/FAILED|FBO|FBD/i.test(text)) return "FB";
      if (/IMPULSE|IGNITION|FUEL/i.test(text)) return "⚡";
      if (/PULLBACK|\bPB\b/i.test(text)) return "PB";
      if (/COMPRESSION|SQUEEZE/i.test(text)) return "CMP";
      if (/CHURN/i.test(text)) return "CH";
      if (/EXHAUST/i.test(text)) return "EX";
      if (text.length <= limit) return text;
      const words = text.split(/\s+/).filter(Boolean);
      if (words.length >= 2) {
        const acronym = words.map(word => compactLabelToken(word, 2).replace(/[^\w↑↓▲▼▶⚑◇×✓⚡⟁●👁🔎🔋✹☠⏳]/g, "").slice(0, 2)).join("");
        if (acronym && acronym.length <= Math.max(limit, 4)) return acronym;
        return words.slice(0, 2).map(word => compactLabelToken(word, 3)).join("");
      }
      return text.slice(0, limit);
    }

    function watchIntentText(value) {
      const text = normalizeCompactKey(value);
      return /\b(WATCH|RISK WAIT|LOW RR|WAIT ENTRY)\b/.test(text);
    }

    function waitIntentText(value) {
      const text = normalizeCompactKey(value);
      if (continuationIntentText(text)) return false;
      if (/\b(NO SIGNAL|OFF|DISABLED|WAIT ENTRY|RISK WAIT|LOW RR)\b/.test(text)) return false;
      return /\bWAIT\b/.test(text) || /\b(RETEST|ACCEPTANCE|CONFIRMATION)\b/.test(text);
    }

    function continuationIntentText(value) {
      const text = normalizeCompactKey(value);
      return /\b(TRANSIT|CONTINUATION|BREAKOUT TRANSIT|ПРОДОЛЖ)\b/.test(text);
    }

    function readyIntentText(value) {
      const text = normalizeCompactKey(value);
      return /\b(ARM|ARMED|READY|PREPARE|SETUP READY)\b/.test(text);
    }

    function trailIntentText(value) {
      const text = normalizeCompactKey(value);
      return /\b(FOLLOW|RIDE|TRAIL|TRAILING)\b/.test(text);
    }

    function takeIntentText(value) {
      const text = normalizeCompactKey(value);
      return /\b(TAKE|TAKE PROFIT|TP|TARGET HIT|PROFIT)\b/.test(text) || String(value ?? "").toLowerCase() === "target";
    }

    function watchCompactText(value) {
      const text = normalizeCompactKey(value);
      if (/\bLOW RR\b/.test(text)) return "RR";
      if (/\bRISK WAIT\b/.test(text)) return "RW";
      if (/\bWAIT ENTRY\b/.test(text)) return "E?";
      return "WT";
    }

    function candidateIntentText(value) {
      const text = normalizeCompactKey(value);
      return /\bCANDIDATE\b/.test(text);
    }

    function goIntentText(value) {
      const text = normalizeCompactKey(value);
      return !/\b(NO GO|WAIT|WATCH|CANDIDATE|RISK WAIT|LOW RR)\b/.test(text) && /\b(GO|FIRE|SHOT)\b/.test(text);
    }

    function candidateCompactText(value) {
      const text = normalizeCompactKey(value);
      return "CND";
    }

    function signalIntentMark(item, role = "") {
      const normalized = normalizedOverlaySignal(item);
      const roleText = String(role || normalized.role || "").toLowerCase();
      if (roleText === "stop") return STOP_MARK;
      if (roleText === "target") return TAKE_MARK;
      if (roleText === "trail" || roleText === "track") return TRAIL_MARK;
      if (roleText === "entry_fill") return normalized.direction === "SHORT" ? "▼" : "▲";
      if (roleText === "target_hit") return TAKE_MARK;
      if (roleText === "stop_hit" || roleText === "trail_stop_hit") return STOP_MARK;
      if (roleText === "plan_cancel") return "×";
      if (normalized.glyph) return normalized.glyph;
      if (normalized.glyph_kind === "entry") return normalized.direction === "SHORT" ? "▼" : "▲";
      if (normalized.glyph_kind === "target") return TAKE_MARK;
      if (normalized.glyph_kind === "stop") return STOP_MARK;
      if (normalized.glyph_kind === "trail") return TRAIL_MARK;
      const actionCode = String(item?.action || "").trim().toUpperCase();
      if (actionCode === "GO") return GO_MARK;
      if (actionCode === "ARM" || actionCode === "ARMED") return READY_MARK;
      if (actionCode === "CANDIDATE") return CANDIDATE_MARK;
      if (actionCode === "FOLLOW" || actionCode === "RIDE") return TRAIL_MARK;
      if (actionCode === "TAKE" || actionCode === "TARGET_HIT") return TAKE_MARK;
      if (
        actionCode === "WAIT"
        || actionCode === "WATCH"
        || actionCode === "WAIT_ENTRY"
        || actionCode === "BLOCK"
      ) return WAIT_MARK;
      return "";
    }

    function displayTableCell(value, limit = 20) {
      const text = String(value ?? "-").replace(/_/g, " ").replace(/\s+/g, " ").trim();
      if (!text || text === "-") return "-";
      if (text.length <= limit) return text;
      return `${text.slice(0, Math.max(1, limit - 3)).trim()}...`;
    }

    function compactLineLabel(value) {
      return compactLabelToken(value, 5).slice(0, 6);
    }

    function normalizePointerLabelLines(lines, maxLines = 3) {
      const rawLines = (Array.isArray(lines) ? lines : [lines]).map(line => String(line ?? "").trim()).filter(Boolean);
      const out = [];
      for (const line of rawLines) {
        if (line === "ABSORB_WALL" || line === "ABSORB_WALL_UP" || line === "ABSORB_WALL_DOWN") {
          out.push(line);
        } else if (/^[A-Z0-9]+(?:_[A-Z0-9]+)+$/.test(line)) {
          out.push(compactLabelToken(line, 6));
        } else if (line.includes("_") || line.length > 12) {
          out.push(compactLabelToken(line, 7));
        } else {
          out.push(line);
        }
        if (out.length >= maxLines) break;
      }
      return out.slice(0, maxLines);
    }
    function isFlatUiShape() {
      return document.body?.classList?.contains("ui-flat") === true;
    }
    function isTvFlatUiShape() {
      return document.body?.classList?.contains("ui-tv-flat") === true;
    }
    function canvasTextOutlineColor() {
      if (isLightTheme()) return "rgba(255,255,255,0.96)";
      if (isTvFlatUiShape()) return "rgba(19,23,34,0.72)";
      return isLightTheme() ? "rgba(255,255,255,0.92)" : "rgba(0,0,0,0.78)";
    }
    function canvasTextOutlineWidth(width) {
      if (isLightTheme()) return 0;
      const value = Number(width);
      if (!Number.isFinite(value)) return isTvFlatUiShape() ? 1.4 : 3;
      return isTvFlatUiShape() ? Math.max(0.9, value * 0.46) : value;
    }
    function shouldStrokeCanvasText() {
      return canvasTextOutlineWidth(1) > 0;
    }
    const canvasTextMeasureCache = new Map();
    function measureCanvasText(ctx, text) {
      const value = String(text ?? "");
      const key = `${ctx.font || ""}|${value}`;
      const cached = canvasTextMeasureCache.get(key);
      if (cached !== undefined) return cached;
      const width = ctx.measureText(value).width;
      if (canvasTextMeasureCache.size > 2048) canvasTextMeasureCache.clear();
      canvasTextMeasureCache.set(key, width);
      return width;
    }
    function drawCanvasText(ctx, text, x, y, maxWidth = undefined, options = {}) {
      const value = String(text ?? "");
      const shouldOutline = options.outline !== false && shouldStrokeCanvasText();
      if (shouldOutline) {
        const prevLineWidth = ctx.lineWidth;
        const prevStrokeStyle = ctx.strokeStyle;
        ctx.lineWidth = canvasTextOutlineWidth(Number(options.outlineWidth) || prevLineWidth || 2);
        ctx.strokeStyle = options.outlineColor || canvasTextOutlineColor();
        if (Number.isFinite(maxWidth)) ctx.strokeText(value, x, y, maxWidth);
        else ctx.strokeText(value, x, y);
        ctx.lineWidth = prevLineWidth;
        ctx.strokeStyle = prevStrokeStyle;
      }
      if (Number.isFinite(maxWidth)) ctx.fillText(value, x, y, maxWidth);
      else ctx.fillText(value, x, y);
    }
    function roundedRectPath(ctx, x, y, width, height, radius = 4) {
      const r = isFlatUiShape() ? 0 : Math.min(radius, width / 2, height / 2);
      ctx.beginPath();
      ctx.moveTo(x + r, y);
      ctx.lineTo(x + width - r, y);
      ctx.quadraticCurveTo(x + width, y, x + width, y + r);
      ctx.lineTo(x + width, y + height - r);
      ctx.quadraticCurveTo(x + width, y + height, x + width - r, y + height);
      ctx.lineTo(x + r, y + height);
      ctx.quadraticCurveTo(x, y + height, x, y + height - r);
      ctx.lineTo(x, y + r);
      ctx.quadraticCurveTo(x, y, x + r, y);
      ctx.closePath();
    }

    function isPrioritySignalLines(lines) {
      const text = (Array.isArray(lines) ? lines : [lines]).map(line => String(line || "").toUpperCase()).join(" ");
      return /(^|\s)GO($|\s)|W✓|WW✓|FIRE|SHOT|TRIG/.test(text);
    }

    function shouldMirrorGoMark(lines, options = {}) {
      return normalizedTypedDirection(options.direction) === "SHORT";
    }

    function drawLabelTextLine(ctx, line, centerX, topY, mirrorGoMark = false, options = {}) {
      const text = String(line || "");
      const paintText = (value, x, y) => {
        if (options.stroke === true) ctx.strokeText(value, x, y);
        if (options.fill !== false) ctx.fillText(value, x, y);
      };
      if (!mirrorGoMark || !text.includes(GO_MARK)) {
        paintText(text, centerX, topY);
        return;
      }
      const parts = [];
      let cursor = 0;
      while (cursor < text.length) {
        const index = text.indexOf(GO_MARK, cursor);
        if (index < 0) {
          if (cursor < text.length) parts.push({ text: text.slice(cursor), rocket: false });
          break;
        }
        if (index > cursor) parts.push({ text: text.slice(cursor, index), rocket: false });
        parts.push({ text: GO_MARK, rocket: true });
        cursor = index + GO_MARK.length;
      }
      const widths = parts.map(part => measureCanvasText(ctx, part.text));
      let x0 = centerX - widths.reduce((sum, width) => sum + width, 0) / 2;
      const previousAlign = ctx.textAlign;
      ctx.textAlign = "left";
      parts.forEach((part, index) => {
        const width = widths[index];
        if (part.rocket) {
          ctx.save();
          ctx.translate(x0 + width / 2, topY + width * 0.5);
          ctx.scale(1, -1);
          ctx.textAlign = "center";
          paintText(part.text, 0, width * 0.5);
          ctx.restore();
        } else {
          paintText(part.text, x0, topY);
        }
        x0 += width;
      });
      ctx.textAlign = previousAlign;
    }

    function drawPointerLabel(ctx, anchorX, anchorY, lines, options = {}) {
      const textLines = normalizePointerLabelLines(lines);
      if (!textLines.length) return null;
      if (textLines.some(line => line === "ABSORB_WALL" || line === "ABSORB_WALL_UP" || line === "ABSORB_WALL_DOWN")) {
        return drawStackedTextMark(ctx, anchorX, anchorY, textLines, {
          ...options,
          offsetPx: options.offsetPx ?? 5,
          symbolSize: options.symbolSize ?? 11,
        });
      }
      const side = options.side === "below" ? "below" : options.side === "center" ? "center" : "above";
      const offset = Number.isFinite(Number(options.offsetPx)) ? Number(options.offsetPx) : 5;
      const pointer = Number.isFinite(Number(options.pointerPx)) ? Number(options.pointerPx) : 6;
      const pointerHalf = Number.isFinite(Number(options.pointerHalfPx)) ? Number(options.pointerHalfPx) : 5;
      const padX = Number.isFinite(Number(options.padX)) ? Number(options.padX) : 5;
      const padY = Number.isFinite(Number(options.padY)) ? Number(options.padY) : 4;
      const lineH = Number.isFinite(Number(options.lineH)) ? Number(options.lineH) : 10;
      const minWidth = Number.isFinite(Number(options.minWidth)) ? Number(options.minWidth) : 28;
      ctx.save();
      ctx.font = options.font || "700 9px -apple-system, BlinkMacSystemFont, sans-serif";
      ctx.textAlign = "center";
      ctx.textBaseline = "top";
      const textWidth = Math.max(...textLines.map(line => measureCanvasText(ctx, line)));
      const boxW = Math.max(minWidth, textWidth + padX * 2);
      const boxH = Math.max(14, textLines.length * lineH + padY * 2);
      const minX = Number.isFinite(Number(options.minX)) ? Number(options.minX) : 0;
      const maxX = Number.isFinite(Number(options.maxX)) ? Number(options.maxX) : Infinity;
      const minY = Number.isFinite(Number(options.minY)) ? Number(options.minY) : -Infinity;
      const maxY = Number.isFinite(Number(options.maxY)) ? Number(options.maxY) : Infinity;
      const boxX = clamp(anchorX - boxW / 2, minX, maxX - boxW);
      const targetBoxY = side === "center"
        ? anchorY - boxH / 2
        : side === "above"
        ? anchorY - offset - pointer - boxH
        : anchorY + offset + pointer;
      const boxY = clamp(targetBoxY, minY, maxY - boxH);
      const baseOffset = Number.isFinite(Number(options.baseOffsetPx)) ? Number(options.baseOffsetPx) : 5;
      const stackedAway = side !== "center" && offset > baseOffset + 1;
      const clippedTipY = side === "above"
        ? anchorY - baseOffset - pointer - boxH - 2
        : anchorY + baseOffset + pointer + boxH + 2;
      const pointerTipY = stackedAway ? clamp(clippedTipY, minY, maxY) : anchorY;
      const pointerBaseY = side === "above" ? boxY + boxH - 1 : boxY + 1;
      const pointerX = clamp(anchorX, boxX + pointerHalf + 2, boxX + boxW - pointerHalf - 2);
      const labelBg = options.bg || themeColor("gold", 0.90);
      const labelText = readableTextForBg(labelBg, options.text || null);
      const glow = options.glow === true || (options.glow !== false && isPrioritySignalLines(textLines));
      if (glow) {
        ctx.shadowColor = rgbaFromCssColor(labelBg, isLightTheme() ? 0.48 : 0.72);
        ctx.shadowBlur = canvasShadowBlur(isLightTheme() ? 10 : 14);
      }
      ctx.fillStyle = labelBg;
      roundedRectPath(ctx, boxX, boxY, boxW, boxH, Number(options.radius) || 4);
      ctx.fill();
      ctx.shadowBlur = 0;
      ctx.shadowColor = "transparent";
      if (side !== "center") {
        ctx.beginPath();
        ctx.moveTo(pointerX - pointerHalf, pointerBaseY);
        ctx.lineTo(pointerX + pointerHalf, pointerBaseY);
        ctx.lineTo(pointerX, pointerTipY);
        ctx.closePath();
        ctx.fill();
      }
      ctx.fillStyle = labelText;
      const mirrorGo = shouldMirrorGoMark(textLines, options);
      textLines.forEach((line, lineIndex) => {
        drawLabelTextLine(ctx, line, boxX + boxW / 2, boxY + padY + lineIndex * lineH, mirrorGo);
      });
      ctx.restore();
      return { x: boxX + boxW / 2, y: boxY + boxH / 2, width: boxW, height: side === "center" ? boxH : boxH + pointer + offset };
    }

    function drawSignalTick(ctx, anchorX, anchorY, color, side = "center", mode = "horizontal") {
      if (!Number.isFinite(anchorX) || !Number.isFinite(anchorY)) return;
      const half = 5;
      const stem = 6;
      const dir = side === "below" ? 1 : side === "above" ? -1 : 0;
      ctx.save();
      ctx.strokeStyle = color || themeColor("gold", 0.94);
      ctx.lineWidth = 1.35;
      ctx.setLineDash([]);
      ctx.beginPath();
      if (mode === "horizontal") {
        const markHalf = 3.5;
        const halo = isLightTheme() ? "rgba(255,255,255,0.96)" : "rgba(2,8,18,0.92)";
        ctx.strokeStyle = halo;
        ctx.lineWidth = 4;
        ctx.moveTo(anchorX - markHalf, anchorY);
        ctx.lineTo(anchorX + markHalf, anchorY);
        ctx.stroke();
        ctx.beginPath();
        ctx.strokeStyle = color || themeColor("gold", 0.94);
        ctx.lineWidth = 2;
        ctx.moveTo(anchorX - markHalf, anchorY);
        ctx.lineTo(anchorX + markHalf, anchorY);
      } else if (dir === 0) {
        ctx.moveTo(anchorX - half, anchorY);
        ctx.lineTo(anchorX + half, anchorY);
        ctx.moveTo(anchorX, anchorY - half);
        ctx.lineTo(anchorX, anchorY + half);
      } else {
        ctx.moveTo(anchorX, anchorY);
        ctx.lineTo(anchorX, anchorY + dir * stem);
      }
      ctx.stroke();
      ctx.restore();
    }
