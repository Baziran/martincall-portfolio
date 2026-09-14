    function forecastProjectionMode(forecast) {
      const mode = String(state.indicators?.martinCarlo?.mode || forecast?.display_mode || "both").toLowerCase();
      return ["heatmap", "fan", "both"].includes(mode) ? mode : "both";
    }

    function forecastProjectionMrDirection(forecast) {
      const bias = forecast?.bias || {};
      const warning = bias.reversal_warning || {};
      const traffic = bias.traffic || {};
      const option = bias.option || {};
      const metrics = forecast?.metrics || {};
      const anchorPrice = Number(forecast?.anchor_price);
      const meanPrice = Number(forecast?.anchors?.mean);
      let direction = "";
      const warningDir = String(warning.direction || "").toLowerCase();
      if (warning.enabled && (warningDir === "long" || warningDir === "short")) {
        direction = warningDir;
      } else {
        const trafficDir = String(traffic.direction || "").toLowerCase();
        const trafficKind = String(traffic.kind || "").toLowerCase();
        const trafficPhase = String(traffic.phase || "").toLowerCase();
        if (traffic.enabled && (trafficDir === "long" || trafficDir === "short") && (trafficKind === "fade" || trafficPhase === "reversal")) {
          direction = trafficDir;
        } else if (option.enabled) {
          const optionDir = String(option.direction || "").toLowerCase();
          if (optionDir === "long" || optionDir === "short") direction = optionDir;
        }
      }
      if (!direction && Number.isFinite(anchorPrice) && Number.isFinite(meanPrice) && meanPrice > 0) {
        const vol = Number(metrics.volatility) || 0.01;
        const gap = anchorPrice - meanPrice;
        const neutralBand = Math.max(anchorPrice * vol * 0.35, anchorPrice * 0.00025);
        if (Math.abs(gap) <= neutralBand) direction = "flat";
        else direction = gap > 0 ? "short" : "long";
      }
      if (direction === "long") return { direction, arrow: "↑", label: "revert up", color: themeColor("up", 1) };
      if (direction === "short") return { direction, arrow: "↓", label: "revert down", color: themeColor("down", 1) };
      return { direction: "flat", arrow: "↔", label: "at mean", color: themeColor("gold", 1) };
    }

    function forecastProjectionTooltip(forecast) {
      const metrics = forecast?.metrics || {};
      const terminal = forecast?.terminal_distribution || {};
      const advisory = forecast?.advisory || {};
      const mr = forecastProjectionMrDirection(forecast);
      const refreshMode = String(state.indicators?.martinCarlo?.refresh || "closed").toLowerCase();
      const pct = value => Number.isFinite(Number(value)) ? `${Math.round(Number(value) * 100)}%` : "-";
      const num = value => Number.isFinite(Number(value)) ? Number(value).toFixed(2) : "-";
      const mrLine = String(forecast?.engine || "").toLowerCase() === "mean_reversion"
        ? `MR path touch: ${pct(metrics.mean_reversion_probability)} | toward mean: ${mr.direction === "flat" ? `↔ ${mr.label}` : mr.arrow}`.trim()
        : `Mean reversion: ${pct(metrics.mean_reversion_probability)}`;
      const lines = [
        tooltipLine("MartinCarlo forecast", "title"),
        tooltipLine(`Refresh: ${refreshMode === "frozen" ? "Frozen snapshot" : refreshMode === "context" ? "Live advisory context; confirmed price anchor" : "Closed bar"}`, "meta"),
        tooltipLine(`Engine: ${String(forecast?.engine || "monte_carlo").replace(/_/g, " ")}${forecast?.requested_engine === "auto" ? " (auto)" : ""}`, "meta"),
        forecast?.engine_reason_code ? tooltipLine(`Reason: ${String(forecast.engine_reason_code).replace(/_/g, " ")}`, "meta") : "",
        tooltipLine(`Horizon: ${forecast?.horizon_bars || "-"} bars | Sims: ${forecast?.simulations || "-"}`, "meta"),
        tooltipLine(mrLine, "meta"),
        tooltipLine(`Bullish: ${pct(metrics.bullish_probability)} | Bearish: ${pct(metrics.bearish_probability)}`, "meta"),
        metrics.candle_nn_enabled ? tooltipLine(`CNN bull/bear: ${pct(metrics.candle_nn_bull)} / ${pct(metrics.candle_nn_bear)} | Edge: ${Number(metrics.candle_nn_edge || 0).toFixed(1)}`, "meta") : "",
        metrics.nn_bias_enabled ? tooltipLine(`NN regime: ${String(metrics.nn_regime_dominant || "-").replace(/_/g, " ")} | MR ${pct(metrics.nn_mean_reversion)} ${mr.arrow} | Vol ${pct(metrics.nn_vol_expansion)}`, "meta") : "",
        metrics.structural_bias_enabled ? tooltipLine(`SQZ: ${String(metrics.structural_dominant || "-").replace(/_/g, " ")} | ${metrics.structural_model_source || "-"} | Vol x${Number(metrics.structural_vol_multiplier || 1).toFixed(2)}`, "meta") : "",
        metrics.regime_filter_enabled ? tooltipLine(`Regime sample: ${metrics.regime_filter_strength || "-"} ${metrics.regime_filter_sample_size || 0}`, "meta") : "",
        tooltipLine(
          `P05: ${num(terminal.p05)} | P50: ${num(terminal.p50)} | P95: ${num(terminal.p95)}`,
          "plan",
          { P05: "percentile-low", P50: "percentile-mid", P95: "percentile-high" },
        ),
        advisory.title ? tooltipLine(`Zone: ${advisory.title}`, "meta") : "",
        advisory.note ? tooltipLine(`Hint: ${advisory.note}`, "meta") : "",
        advisory.target_note ? tooltipLine(`Target: ${advisory.target_note}`, "target") : "",
      ].filter(line => tooltipLineText(line).trim());
      return { lines, max_lines: 32 };
    }

    function forecastProjectionTooltipHitbox(forecast, anchorIndex, x, y, xStep, pad, priceH, width) {
      const percentiles = forecast?.percentiles || {};
      const p50 = Array.isArray(percentiles.p50) ? percentiles.p50 : [];
      if (!p50.length) return null;
      const p16 = Array.isArray(percentiles.p16) ? percentiles.p16 : [];
      const p84 = Array.isArray(percentiles.p84) ? percentiles.p84 : [];
      let minX = Infinity;
      let maxX = -Infinity;
      let minY = Infinity;
      let maxY = -Infinity;
      for (let index = 0; index < p50.length; index += 1) {
        const centerValue = Number(p50[index]);
        if (!Number.isFinite(centerValue)) continue;
        const px = x(anchorIndex + index + 1);
        if (px < pad.left - xStep || px > width - pad.right + xStep) continue;
        const centerY = clamp(y(centerValue), pad.top, pad.top + priceH);
        const lowY = Number.isFinite(Number(p16[index])) ? clamp(y(Number(p16[index])), pad.top, pad.top + priceH) : centerY;
        const highY = Number.isFinite(Number(p84[index])) ? clamp(y(Number(p84[index])), pad.top, pad.top + priceH) : centerY;
        const radius = clamp(Math.abs(highY - lowY) * 0.5, 8, 34);
        minX = Math.min(minX, px);
        maxX = Math.max(maxX, px);
        minY = Math.min(minY, centerY - radius);
        maxY = Math.max(maxY, centerY + radius);
      }
      if (![minX, maxX, minY, maxY].every(Number.isFinite)) return null;
      const left = clamp(minX - xStep * 0.45, pad.left, width - pad.right);
      const right = clamp(maxX + xStep * 0.45, pad.left, width - pad.right);
      const top = clamp(minY, pad.top, pad.top + priceH);
      const bottom = clamp(maxY, pad.top, pad.top + priceH);
      if (right <= left || bottom <= top) return null;
      return {
        x: (left + right) / 2,
        y: (top + bottom) / 2,
        width: Math.max(40, right - left),
        height: Math.max(18, bottom - top),
      };
    }

    function drawForecastProjectionHeatmap(ctx, forecast, anchorIndex, x, y, xStep, pad, priceH, width) {
      const heatmap = Array.isArray(forecast?.heatmap) ? forecast.heatmap : [];
      if (!heatmap.length) return;
      ctx.save();
      const base = themeColor("cyan", 1);
      for (const step of heatmap) {
        const stepNo = Number(step?.step);
        if (!Number.isFinite(stepNo)) continue;
        const effectiveStep = Math.max(1, Math.round(stepNo));
        const cx = x(anchorIndex + effectiveStep);
        const left = cx - Math.max(1.5, xStep * 0.42);
        const right = cx + Math.max(1.5, xStep * 0.42);
        if (right < pad.left || left > width - pad.right) continue;
        for (const bin of step.bins || []) {
          const low = Number(bin.low);
          const high = Number(bin.high);
          const probability = clamp(Number(bin.probability) || 0, 0, 1);
          if (!Number.isFinite(low) || !Number.isFinite(high) || high <= low || probability <= 0) continue;
          const top = clamp(y(high), pad.top, pad.top + priceH);
          const bottom = clamp(y(low), pad.top, pad.top + priceH);
          const h = Math.max(1, bottom - top);
          const alpha = clamp(0.05 + probability * 2.4, 0.05, 0.34);
          ctx.fillStyle = rgbaFromCssColor(base, alpha);
          ctx.fillRect(left, top, Math.max(1, right - left), h);
        }
      }
      ctx.restore();
    }

    function drawForecastProjectionDensityRidges(ctx, forecast, anchorIndex, x, y, xStep, pad, priceH, width) {
      if (state.indicators?.martinCarlo?.densityLines === false) return;
      const heatmap = Array.isArray(forecast?.heatmap) ? forecast.heatmap : [];
      if (!heatmap.length) return;
      const ridges = [[], []];
      for (const step of heatmap) {
        const stepNo = Number(step?.step);
        const bins = Array.isArray(step?.bins) ? [...step.bins] : [];
        if (!Number.isFinite(stepNo) || !bins.length) continue;
        bins.sort((a, b) => (Number(b?.probability) || 0) - (Number(a?.probability) || 0));
        const primary = bins[0];
        const primaryProb = Number(primary?.probability) || 0;
        const primaryCenter = (Number(primary?.low) + Number(primary?.high)) * 0.5;
        const effectiveStep = Math.max(1, Math.round(stepNo));
        if (primaryProb > 0 && Number.isFinite(primaryCenter)) ridges[0].push({ step: effectiveStep, price: primaryCenter, probability: primaryProb });
        const secondary = bins.find(bin => {
          const center = (Number(bin?.low) + Number(bin?.high)) * 0.5;
          const probability = Number(bin?.probability) || 0;
          return Number.isFinite(center)
            && probability >= primaryProb * 0.72
            && Math.abs(center - primaryCenter) > Math.max(Math.abs(Number(primary?.high) - Number(primary?.low)), 0.000001) * 1.2;
        });
        if (secondary) {
          const center = (Number(secondary.low) + Number(secondary.high)) * 0.5;
          ridges[1].push({ step: effectiveStep, price: center, probability: Number(secondary.probability) || 0 });
        }
      }
      const drawRidge = (points, alpha, widthPx, dash = []) => {
        if (points.length < 2) return;
        ctx.beginPath();
        ctx.setLineDash(dash);
        let started = false;
        for (const point of points) {
          const px = x(anchorIndex + point.step);
          if (px < pad.left - xStep || px > width - pad.right + xStep) continue;
          const py = clamp(y(point.price), pad.top, pad.top + priceH);
          if (!started) {
            ctx.moveTo(px, py);
            started = true;
          } else {
            ctx.lineTo(px, py);
          }
        }
        if (started) {
          ctx.strokeStyle = rgbaFromCssColor(themeColor("gold", 1), alpha);
          ctx.lineWidth = widthPx;
          ctx.stroke();
        }
        ctx.setLineDash([]);
      };
      ctx.save();
      ctx.rect(pad.left, pad.top, width - pad.left - pad.right, priceH);
      ctx.clip();
      drawRidge(ridges[0], 0.62, 1.8);
      drawRidge(ridges[1], 0.36, 1.2, [5, 5]);
      ctx.restore();
    }

    function drawForecastProjectionFan(ctx, forecast, anchorIndex, x, y, xStep, pad, priceH, width) {
      const percentiles = forecast?.percentiles || {};
      const p05 = Array.isArray(percentiles.p05) ? percentiles.p05 : [];
      const p50 = Array.isArray(percentiles.p50) ? percentiles.p50 : [];
      const p95 = Array.isArray(percentiles.p95) ? percentiles.p95 : [];
      const p10 = Array.isArray(percentiles.p10) ? percentiles.p10 : [];
      const p90 = Array.isArray(percentiles.p90) ? percentiles.p90 : [];
      if (!p50.length) return;
      const drawBand = (lowSeries, highSeries, color, alpha) => {
        const n = Math.min(lowSeries.length, highSeries.length);
        if (!n) return;
        ctx.beginPath();
        for (let i = 0; i < n; i += 1) {
          const px = x(anchorIndex + i + 1);
          const py = y(Number(highSeries[i]));
          if (i === 0) ctx.moveTo(px, py);
          else ctx.lineTo(px, py);
        }
        for (let i = n - 1; i >= 0; i -= 1) {
          ctx.lineTo(x(anchorIndex + i + 1), y(Number(lowSeries[i])));
        }
        ctx.closePath();
        ctx.fillStyle = rgbaFromCssColor(color, alpha);
        ctx.fill();
      };
      const drawLine = (series, color, widthPx, dash = []) => {
        ctx.beginPath();
        ctx.setLineDash(dash);
        let started = false;
        for (let i = 0; i < series.length; i += 1) {
          const value = Number(series[i]);
          const px = x(anchorIndex + i + 1);
          if (!Number.isFinite(value) || px < pad.left - xStep || px > width - pad.right + xStep) continue;
          const py = y(value);
          if (!started) {
            ctx.moveTo(px, py);
            started = true;
          } else {
            ctx.lineTo(px, py);
          }
        }
        if (started) {
          ctx.strokeStyle = color;
          ctx.lineWidth = widthPx;
          ctx.stroke();
        }
        ctx.setLineDash([]);
      };
      ctx.save();
      ctx.rect(pad.left, pad.top, width - pad.left - pad.right, priceH);
      ctx.clip();
      drawBand(p05, p95, themeColor("blue", 1), 0.07);
      drawBand(p10, p90, themeColor("cyan", 1), 0.10);
      drawLine(p05, themeColor("down", 0.62), 1, [2, 4]);
      drawLine(p50, themeColor("blue", 0.78), 1.5, [6, 4]);
      drawLine(p95, themeColor("up", 0.62), 1, [2, 4]);
      ctx.restore();
    }

    function drawForecastProjectionTerminalDistribution(ctx, forecast, anchorIndex, x, y, xStep, pad, priceH, width) {
      const dist = forecast?.terminal_distribution || {};
      const bins = Array.isArray(dist.bins) ? dist.bins : [];
      if (!bins.length) return;
      const offset = Number(dist.target_bar_offset || forecast?.horizon_bars || 0);
      if (!Number.isFinite(offset) || offset <= 0) return;
      const x0 = x(anchorIndex + offset) + Math.max(4, xStep * 0.45);
      const maxW = clamp(xStep * 5.5, 18, 54);
      if (x0 > width - pad.right + maxW || x0 < pad.left) return;
      const maxProb = Math.max(...bins.map(bin => Number(bin.probability) || 0), 0.000001);
      ctx.save();
      for (const bin of bins) {
        const low = Number(bin.low);
        const high = Number(bin.high);
        const probability = Number(bin.probability) || 0;
        if (!Number.isFinite(low) || !Number.isFinite(high) || high <= low || probability <= 0) continue;
        const top = clamp(y(high), pad.top, pad.top + priceH);
        const bottom = clamp(y(low), pad.top, pad.top + priceH);
        const w = Math.max(2, (probability / maxProb) * maxW);
        ctx.fillStyle = rgbaFromCssColor(themeColor("blue", 1), clamp(0.08 + probability * 1.8, 0.08, 0.34));
        ctx.fillRect(x0, top, w, Math.max(1, bottom - top));
      }
      const p50 = Number(dist.p50);
      if (Number.isFinite(p50)) {
        ctx.strokeStyle = themeColor("blue", 0.72);
        ctx.lineWidth = 1.2;
        ctx.beginPath();
        ctx.moveTo(x0 - 3, y(p50));
        ctx.lineTo(x0 + maxW + 3, y(p50));
        ctx.stroke();
      }
      ctx.restore();
    }

    function drawForecastProjectionImpulseArrow(ctx, forecast, anchorIndex, x, y, xStep, pad, priceH, width) {
      const arrow = forecast?.impulse_target_arrow;
      if (!arrow?.enabled) return;
      const startPrice = Number(arrow.start_price ?? forecast?.anchor_price);
      const targetPrice = Number(arrow.target_price);
      const horizon = Math.max(2, Number(forecast?.horizon_bars || 0));
      if (!Number.isFinite(startPrice) || !Number.isFinite(targetPrice) || targetPrice === startPrice) return;
      const direction = String(arrow.direction || "").toLowerCase();
      const color = direction === "short" ? themeColor("down", 1) : themeColor("up", 1);
      const x0 = clamp(x(anchorIndex), pad.left + 4, width - pad.right - 4);
      const x1 = clamp(x(anchorIndex + Math.max(2, Math.round(horizon * 0.42))), pad.left + 12, width - pad.right - 12);
      const y0 = clamp(y(startPrice), pad.top + 6, pad.top + priceH - 6);
      const y1 = clamp(y(targetPrice), pad.top + 6, pad.top + priceH - 6);
      const dx = x1 - x0;
      const dy = y1 - y0;
      const len = Math.hypot(dx, dy);
      if (len < 14) return;
      const ux = dx / len;
      const uy = dy / len;
      const head = clamp(xStep * 1.4, 12, 24);
      const wing = clamp(xStep * 0.55, 7, 14);
      const confidence = clamp(Number(arrow.confidence) || 0.5, 0.35, 1);
      ctx.save();
      ctx.lineCap = "round";
      ctx.lineJoin = "round";
      ctx.strokeStyle = rgbaFromCssColor(color, 0.15 + confidence * 0.20);
      ctx.lineWidth = clamp(xStep * 0.95, 10, 22);
      ctx.beginPath();
      ctx.moveTo(x0, y0);
      ctx.lineTo(x1 - ux * head * 0.45, y1 - uy * head * 0.45);
      ctx.stroke();
      ctx.strokeStyle = rgbaFromCssColor(color, 0.48 + confidence * 0.18);
      ctx.lineWidth = clamp(xStep * 0.32, 4, 8);
      ctx.beginPath();
      ctx.moveTo(x0, y0);
      ctx.lineTo(x1 - ux * head * 0.55, y1 - uy * head * 0.55);
      ctx.stroke();
      ctx.fillStyle = rgbaFromCssColor(color, 0.42 + confidence * 0.22);
      ctx.beginPath();
      ctx.moveTo(x1, y1);
      ctx.lineTo(x1 - ux * head - uy * wing, y1 - uy * head + ux * wing);
      ctx.lineTo(x1 - ux * head + uy * wing, y1 - uy * head - ux * wing);
      ctx.closePath();
      ctx.fill();
      ctx.font = "800 9px -apple-system, BlinkMacSystemFont, sans-serif";
      ctx.textAlign = "center";
      ctx.textBaseline = "bottom";
      const label = `${arrow.label || "MC IMP"} ${Math.round(confidence * 100)}`;
      ctx.fillStyle = color;
      drawCanvasText(ctx, label, (x0 + x1) / 2, Math.min(y0, y1) - 4, undefined, { outlineWidth: 3 });
      ctx.restore();
      registerCanvasTooltip("price", (x0 + x1) / 2, (y0 + y1) / 2, Math.max(36, Math.abs(x1 - x0)), Math.max(22, Math.abs(y1 - y0)), () => ({
        lines: [
          tooltipLine("MartinCarlo impulse target", "title"),
          tooltipLine(`Direction: ${direction || "-"}`, "meta"),
          tooltipLine(`Target: ${fmt(targetPrice)}`, "target"),
          tooltipLine(`Impulse score: ${arrow.impulse_score ?? "-"}`, "plan"),
          tooltipLine(`NN confidence: ${Math.round(confidence * 100)}%`, "meta"),
          tooltipLine(`Source: ${String(arrow.source || "-").replace(/_/g, " ")}`, "meta"),
        ].filter(line => tooltipLineText(line).trim()),
      }));
    }

    function drawForecastProjectionMeanReversionBadge(ctx, forecast, anchorIndex, x, y, pad, priceH, width) {
      if (state.indicators?.martinCarlo?.chartLabels === false) return;
      if (String(forecast?.engine || "").toLowerCase() !== "mean_reversion") return;
      const anchorPrice = Number(forecast?.anchor_price);
      if (!Number.isFinite(anchorPrice)) return;
      const metrics = forecast?.metrics || {};
      const mr = forecastProjectionMrDirection(forecast);
      const px = clamp(x(anchorIndex) + 8, pad.left + 4, width - pad.right - 58);
      const py = clamp(y(anchorPrice) - 34, pad.top + 6, pad.top + priceH - 24);
      const probability = Number(metrics.mean_reversion_probability);
      const pctVal = Number.isFinite(probability) ? Math.round(probability * 100) : null;
      const label = mr.direction === "flat"
        ? (pctVal != null ? `MC MR ${pctVal}%` : "MC MR")
        : (pctVal != null ? `MC MR${mr.arrow} ${pctVal}` : `MC MR${mr.arrow}`);
      ctx.save();
      ctx.font = "850 10px -apple-system, BlinkMacSystemFont, sans-serif";
      const tw = ctx.measureText(label).width;
      const w = Math.max(43, tw + 12);
      const h = 18;
      ctx.fillStyle = isLightTheme() ? "rgba(255,255,255,0.88)" : "rgba(15,23,42,0.82)";
      ctx.strokeStyle = mr.color;
      ctx.lineWidth = 1.2;
      roundedRectPath(ctx, px, py, w, h, 5);
      ctx.fill();
      ctx.stroke();
      ctx.fillStyle = mr.color;
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(label, px + w / 2, py + h / 2 + 0.5);
      ctx.restore();
      registerCanvasTooltip("price", px + w / 2, py + h / 2, w, h, () => ({
        lines: [
          tooltipLine("MartinCarlo mean reversion", "title"),
          forecast?.engine_reason_code ? tooltipLine(`Reason: ${String(forecast.engine_reason_code).replace(/_/g, " ")}`, "meta") : "",
          Number.isFinite(probability) ? tooltipLine(`MR path touch: ${Math.round(probability * 100)}%`, "meta") : "",
          tooltipLine(
            mr.direction === "flat"
              ? `Toward mean: ↔ ${mr.label}`
              : `Toward mean: ${mr.arrow} ${mr.label}`,
            "meta",
          ),
        ].filter(line => tooltipLineText(line).trim()),
      }));
    }

    function drawMartinCarloForecastOverlay(context) {
      const { ctx, item, timeLookup, pad, priceH, width, xStep, x, y, bars } = context;
      if (state.indicators?.martinCarlo?.visible === false) return;
      const forecast = item?.payload;
      if (!forecast || !Array.isArray(bars) || !bars.length) return;
      const anchorRaw = timeLookup.byTs.get(timestampKey(forecast.anchor_ts));
      if (anchorRaw === undefined) return;
      const anchorIndex = Math.max(anchorRaw, bars.length - 1);
      const mode = forecastProjectionMode(forecast);
      if (mode === "heatmap" || mode === "both") {
        drawForecastProjectionHeatmap(ctx, forecast, anchorIndex, x, y, xStep, pad, priceH, width);
        drawForecastProjectionDensityRidges(ctx, forecast, anchorIndex, x, y, xStep, pad, priceH, width);
        drawForecastProjectionTerminalDistribution(ctx, forecast, anchorIndex, x, y, xStep, pad, priceH, width);
      }
      if (mode === "fan" || mode === "both") {
        drawForecastProjectionFan(ctx, forecast, anchorIndex, x, y, xStep, pad, priceH, width);
      }
      drawForecastProjectionImpulseArrow(ctx, forecast, anchorIndex, x, y, xStep, pad, priceH, width);
      drawForecastProjectionMeanReversionBadge(ctx, forecast, anchorIndex, x, y, pad, priceH, width);
      const horizon = Number(forecast.horizon_bars || 0);
      if (horizon > 0) {
        const hit = forecastProjectionTooltipHitbox(forecast, anchorIndex, x, y, xStep, pad, priceH, width);
        if (hit) registerCanvasTooltip("price", hit.x, hit.y, hit.width, hit.height, () => forecastProjectionTooltip(forecast));
      }
    }

    function martinCarloForecastForRefresh(snapshot = state.snapshot) {
      const indicator = snapshot?.indicators?.martin_carlo || {};
      return indicator?.latest?.forecast
        || (Array.isArray(indicator?.overlays)
          ? indicator.overlays.find(item => item?.type === "custom" && item?.renderer_ref === "martin_carlo_forecast")?.payload
          : null)
        || null;
    }

    function martinCarloRefreshMode() {
      const mode = String(state.indicators?.martinCarlo?.refresh || "closed").toLowerCase();
      return ["closed", "context", "frozen"].includes(mode) ? mode : "closed";
    }

    function martinCarloContextMoveThreshold(snapshot = state.snapshot) {
      const bars = Array.isArray(snapshot?.bars) ? snapshot.bars : [];
      const lookback = bars.slice(-15).filter(
        bar => Number.isFinite(Number(bar?.high)) && Number.isFinite(Number(bar?.low))
      );
      const ranges = lookback
        .map(bar => Math.max(0, Number(bar.high) - Number(bar.low)))
        .filter(value => Number.isFinite(value) && value > 0);
      const avgRange = ranges.length
        ? ranges.reduce((sum, value) => sum + value, 0) / ranges.length
        : 0;
      const price = Number(snapshot?.meta?.price ?? bars[bars.length - 1]?.close ?? 0);
      return Math.max(
        avgRange * 0.35,
        Number.isFinite(price) ? Math.abs(price) * 0.00035 : 0,
      );
    }

    const martinCarloRefreshTracker = {
      activeBar: "",
      timer: null,
      lastRefreshAt: 0,
    };

    function resetMartinCarloAnalysisRefresh() {
      window.clearTimeout(martinCarloRefreshTracker.timer);
      martinCarloRefreshTracker.timer = null;
      martinCarloRefreshTracker.activeBar = "";
    }

    function maybeScheduleMartinCarloAnalysisRefresh(reason = "chart-stream") {
      if (document.visibilityState !== "visible") return false;
      if (!state.indicators?.martinCarlo?.enabled) return false;
      if (state.indicators?.martinCarlo?.visible === false) return false;
      if (
        state.serverSleeping
        || !providerCapability(state.dataSource, "chart_stream")
        || !state.snapshot?.bars?.length
        || !snapshotMatchesCurrentRoute()
      ) return false;
      if (effectiveDataQuality(state.snapshot?.meta || {}).market_closed) return false;
      const refreshMode = martinCarloRefreshMode();
      if (refreshMode === "frozen") return false;
      const bars = state.snapshot.bars;
      const latest = latestConfirmedIndicatorBar(state.snapshot) || bars[bars.length - 1];
      const latestKey = timestampKey(latest?.ts);
      if (!latestKey) return false;
      const forecast = martinCarloForecastForRefresh(state.snapshot);
      const anchorKey = timestampKey(forecast?.anchor_ts);
      const newAnchorBar = latestKey !== anchorKey;
      if (latestKey === anchorKey) {
        if (refreshMode !== "context") return false;
        const anchorPrice = Number(forecast?.anchor_price);
        const latestPrice = Number(state.snapshot.meta?.price ?? latest?.close);
        if (!Number.isFinite(anchorPrice) || !Number.isFinite(latestPrice)) return false;
        if (
          Math.abs(latestPrice - anchorPrice)
          < martinCarloContextMoveThreshold(state.snapshot)
        ) return false;
      }
      const elapsed = Date.now() - Number(martinCarloRefreshTracker.lastRefreshAt || 0);
      const delayMs = newAnchorBar && refreshMode === "context"
        ? 0
        : Math.max(0, (refreshMode === "context" ? 15000 : 12000) - elapsed);
      const latestPrice = Number(state.snapshot.meta?.price ?? latest?.close);
      const versionKey = refreshMode === "context" && Number.isFinite(latestPrice)
        ? `${latestKey}|${latestPrice}`
        : latestKey;
      return scheduleAnalysisRefreshOnce({
        latestKey: versionKey,
        activeBarStateKey: "activeBar",
        timerStateKey: "timer",
        lastRefreshStateKey: "lastRefreshAt",
        delayMs,
        reason,
        debugLabel: "martin carlo refresh",
        tracker: martinCarloRefreshTracker,
        canRun: () => (
          state.indicators?.martinCarlo?.enabled
          && state.indicators?.martinCarlo?.visible !== false
          && martinCarloRefreshMode() !== "frozen"
        ),
        retry: () => maybeScheduleMartinCarloAnalysisRefresh("busy"),
        loadOptions: {
          force: true,
          splitAnalysis: true,
          queueAnalysis: true,
          analysisOnly: true,
          reuseAnalysisDemand: true,
          analysisVersionKey: versionKey,
          analysisRequireBarTs: latestKey,
        },
      });
    }

    function martinCarloAttentionMessages(snapshot) {
      const forecast = martinCarloForecastForRefresh(snapshot);
      if (String(forecast?.engine || "").toLowerCase() !== "mean_reversion") return [];
      const metrics = forecast?.metrics || {};
      const mr = forecastProjectionMrDirection(forecast);
      const probability = Number(metrics.mean_reversion_probability);
      const reason = String(
        forecast?.engine_reason_code || "mean_reversion",
      ).replaceAll("_", " ");
      const tag = mr.direction === "flat" ? "MC MR" : `MC MR${mr.arrow}`;
      return [{
        tag,
        text: `MartinCarlo MR${Number.isFinite(probability) ? ` ${Math.round(probability * 100)}% path touch` : ""}${mr.direction === "flat" ? "" : ` ${mr.arrow}`}: ${reason}`,
        ts: forecast?.anchor_ts || snapshot?.meta?.analysis_ts,
        category: "forecast",
        severity: "warning",
        source: "martin_carlo",
        code: String(forecast?.engine_reason_code || "mean_reversion"),
        entityId: exactIdentityText(state.instrumentId),
        targetTab: "indicators",
      }];
    }

    registerIndicatorCustomOverlayRenderer(
      "martin_carlo_forecast",
      drawMartinCarloForecastOverlay,
    );
    registerIndicatorAnalysisRefreshScheduler("martin_carlo", {
      schedule: maybeScheduleMartinCarloAnalysisRefresh,
      reset: resetMartinCarloAnalysisRefresh,
    });
    registerIndicatorAttentionProvider(
      "martin_carlo",
      martinCarloAttentionMessages,
    );
    registerIndicatorProcessEffect("martin_carlo_refresh_reset", () => {
      resetMartinCarloAnalysisRefresh();
      return false;
    });
