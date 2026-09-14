let tickVisibleDeltaCacheKey = "";
let tickVisibleDeltaCache = null;

function tickVisibleBarsKey(bars, rows) {
      const firstBar = bars[0];
      const middleBar = bars[Math.floor((bars.length - 1) / 2)];
      const lastBar = bars[bars.length - 1];
      const firstRow = rows[0] || {};
      const lastRow = rows[rows.length - 1] || {};
      const rowKey = row => [
        row.ts || "",
        row.total_volume ?? "",
        row.net_delta ?? "",
        row.trade_count ?? "",
      ].join(":");
      return [
        bars.length,
        typeof chartBarRenderKey === "function" ? chartBarRenderKey(firstBar) : firstBar?.ts || "",
        typeof chartBarRenderKey === "function" ? chartBarRenderKey(middleBar) : middleBar?.ts || "",
        typeof chartBarRenderKey === "function" ? chartBarRenderKey(lastBar) : lastBar?.ts || "",
        rows.length,
        rowKey(firstRow),
        rowKey(lastRow),
      ].join("|");
    }

function tickDeltaByVisibleBars(bars) {
      const rows = Array.isArray(state.tickContext?.delta) ? state.tickContext.delta : [];
      if (!bars.length || !rows.length) return [];
      const cacheKey = tickVisibleBarsKey(bars, rows);
      if (cacheKey && cacheKey === tickVisibleDeltaCacheKey && tickVisibleDeltaCache) return tickVisibleDeltaCache;
      const times = genericOverlayLookupForVisible({ start: 0, end: bars.length }, bars).times;
      const step = Math.max(intervalMsFromState(), 60_000);
      const buckets = new Array(bars.length);
      for (let i = 0; i < bars.length; i += 1) {
        buckets[i] = { total_volume: 0, net_delta: 0, trade_count: 0 };
      }
      let index = 0;
      for (const row of rows) {
        const ts = Date.parse(row.ts || "");
        if (!Number.isFinite(ts) || ts < times[0] || ts >= times[times.length - 1] + step) continue;
        while (index < times.length - 1 && times[index + 1] <= ts) index += 1;
        const end = times[index] + step;
        if (ts < times[index] || ts >= end) continue;
        buckets[index].total_volume += Number(row.total_volume || 0);
        buckets[index].net_delta += Number(row.net_delta || 0);
        buckets[index].trade_count += Number(row.trade_count || 0);
      }
      tickVisibleDeltaCacheKey = cacheKey;
      tickVisibleDeltaCache = buckets;
      return buckets;
    }

    function recentTickDeltaSummary() {
      const cfg = state.indicators.tickFlow || {};
      if (!cfg.enabled || !cfg.live || !cfg.recentDelta) return null;
      if (state.tickContext?.stats?.source !== "tick") return null;
      const seconds = [15, 30, 60].includes(Number(cfg.recentDeltaSeconds)) ? Number(cfg.recentDeltaSeconds) : 30;
      const backendRecent = state.tickContext?.aggregates?.recent_delta?.[String(seconds)];
      if (!backendRecent) return null;
      const lastTs = Date.parse(backendRecent.lastTs || "");
      const staleMs = Math.max(seconds * 2000, 30000);
      if (Date.now() - lastTs > staleMs) return null;
      return Number.isFinite(lastTs) ? backendRecent : null;
    }

    function drawRecentTickDeltaOnPriceLine(ctx, priceY, anchorX, axisX, pad, priceH) {
      const recent = recentTickDeltaSummary();
      if (!recent) return;
      const net = Number(recent.netDelta || 0);
      const label = `Δ${recent.seconds}s: ${signed(Math.round(net))}`;
      const yy = clamp(priceY - 7, pad.top + 11, pad.top + priceH - 4);
      ctx.save();
      ctx.font = `800 10px ${CANVAS_MONO_FONT}`;
      const labelW = Math.max(72, ctx.measureText(label).width + 12);
      const x0 = clamp(anchorX + 9 + labelW, pad.left + labelW + 8, axisX - 12);
      const color = net > 0 ? themeColor("up", 0.92) : net < 0 ? themeColor("down", 0.92) : themeColor("axis", 0.78);
      ctx.textAlign = "right";
      ctx.fillStyle = color;
      drawCanvasText(ctx, label, x0, yy, undefined, { outlineWidth: 3 });
      registerCanvasTooltip(
        "price",
        x0 - Math.max(52, labelW / 2),
        yy - 4,
        Math.max(104, labelW),
        16,
        () => [
          `Recent tick delta ${recent.seconds}s`,
          `Net delta ${signed(Math.round(net))}`,
          `Volume ${Math.round(recent.totalVolume).toLocaleString("en-US")}`,
          `Trades ${Number(recent.tradeCount || 0).toLocaleString("en-US")}`,
          `Ratio ${Number(recent.deltaRatio || 0).toFixed(2)}`,
          `Last tick bucket ${recent.lastTs}`,
        ].join("\\n"),
      );
      ctx.restore();
    }

    function activeVolumeProfileRows() {
      const tickRows = Array.isArray(state.tickContext?.profile) ? state.tickContext.profile : [];
      const stats = state.tickContext?.stats || {};
      if (tickRows.length && Number(stats.profile_total_volume || stats.total_volume || 0) > 0) {
        return { rows: tickRows, source: "tick", stats, aggregates: state.tickContext?.aggregates || {} };
      }
      return { rows: [], source: "empty", stats, aggregates: {} };
    }

    function drawTickVolumeProfile(ctx, snapshot, pad, priceH, width, y) {
      const perfToken = window.mcPerfStart ? window.mcPerfStart("tickProfile") : null;
      let perfDetail = "disabled";
      if (gexFocusMode()) return;
      const cfg = state.indicators.tickFlow;
      const bars = visibleBars(snapshot).bars || [];
      ctx.save();
      try {
        const { rows, source, stats, aggregates } = activeVolumeProfileRows();
        perfDetail = `${source || "none"} ${rows.length} rows ${bars.length} bars`;
        if (!cfg.enabled || !cfg.profile || !rows.length) return;
        const plotLeft = pad.left;
        const plotRight = width - pad.right;
        const right = plotRight - 8;
        const maxW = clamp((plotRight - plotLeft) * 0.055, 27, 59);
        const profileLeft = right - maxW - 10;
        const profileLabelLeft = right - maxW - 8;
        const poc = aggregates?.poc || null;
        const valueArea = aggregates?.value_area || null;
        const levelDeltas = Array.isArray(aggregates?.level_deltas) ? aggregates.level_deltas : [];
        const serverImbalanceGroups = Array.isArray(aggregates?.imbalance_groups)
          ? aggregates.imbalance_groups.filter(group => Math.max(Number(group.dominant || 0), Number(group.passive || 0)) >= Math.max(Number(cfg.imbalanceMinVolume) || 25, 1))
          : [];
        const imbalanceGroups = cfg.imbalance ? serverImbalanceGroups : [];
        const highVolumeSet = new Set(
          Array.isArray(aggregates?.high_volume_prices)
            ? aggregates.high_volume_prices.map(Number)
            : [],
        );
        for (const row of rows) {
        const price = Number(row.price);
        const volume = Number(row.total_volume || 0);
        if (!Number.isFinite(price) || volume <= 0) continue;
        const yy = y(price);
        if (yy < pad.top - 4 || yy > pad.top + priceH + 4) continue;
        const norm = clamp(Number(row.normalized) || 0, 0, 1);
        const barW = Math.max(2, maxW * norm);
        const deltaRatio = Number(row.delta_ratio || 0);
        const buyVolume = Math.max(Number(row.buy_volume || 0), 0);
        const sellVolume = Math.max(Number(row.sell_volume || 0), 0);
        const totalSide = Math.max(buyVolume + sellVolume, volume, 1);
        const buyW = barW * (buyVolume / totalSide);
        const sellW = barW * (sellVolume / totalSide);
        const barH = clamp(priceH * 0.010, 3, highVolumeSet.has(price) ? 7 : 5);
        const isPoc = Number(poc?.price) === price;
        const neutralAlpha = 0.18;
        ctx.fillStyle = rgbaFromCssColor(css("--axis"), neutralAlpha);
        ctx.fillRect(right - barW, yy - barH / 2, barW, barH);
        ctx.fillStyle = themeColor("down", 0.48);
        ctx.fillRect(right - barW, yy - barH / 2, sellW, barH);
        ctx.fillStyle = themeColor("up", 0.52);
        ctx.fillRect(right - buyW, yy - barH / 2, buyW, barH);
        if (isPoc) {
          ctx.strokeStyle = themeColor("gold", 0.94);
          ctx.lineWidth = 1.35;
          ctx.beginPath();
          ctx.moveTo(profileLeft, yy);
          ctx.lineTo(right - barW - 4, yy);
          ctx.stroke();
        }
        registerCanvasTooltip(
          "price",
          right - barW / 2,
          yy,
          barW,
          Math.max(barH + 4, 8),
          () => [
            `Tick volume cluster ${fmt(price)}`,
            `Vol ${Math.round(volume).toLocaleString("en-US")}`,
            `Buy ${Math.round(buyVolume).toLocaleString("en-US")} / Sell ${Math.round(sellVolume).toLocaleString("en-US")}`,
            `Delta ${signed(row.net_delta)}`,
            `Trades ${Number(row.trade_count || 0).toLocaleString("en-US")}`,
          ].join("\\n"),
        );
      }
      for (const group of imbalanceGroups) {
        const yLow = y(group.low);
        const yHigh = y(group.high);
        if (!Number.isFinite(yLow) || !Number.isFinite(yHigh)) continue;
        const top = clamp(Math.min(yLow, yHigh) - 5, pad.top, pad.top + priceH);
        const bottom = clamp(Math.max(yLow, yHigh) + 5, pad.top, pad.top + priceH);
        if (bottom < pad.top || top > pad.top + priceH) continue;
        const mid = (top + bottom) / 2;
        const label = `${group.label}${group.count > 1 ? ` x${group.count}` : ""}`;
        const color = group.side === "buy" ? themeColor("up", 0.95) : themeColor("down", 0.95);
        const labelX = profileLabelLeft;
        ctx.strokeStyle = color;
        ctx.fillStyle = color;
        ctx.lineWidth = 1.2;
        ctx.beginPath();
        ctx.moveTo(right + 4, top);
        ctx.lineTo(right + 4, bottom);
        ctx.stroke();
        ctx.beginPath();
        ctx.moveTo(labelX + 4, mid);
        ctx.lineTo(right + 2, mid);
        ctx.stroke();
        ctx.font = "800 9px -apple-system, BlinkMacSystemFont, sans-serif";
        ctx.textAlign = "right";
        ctx.fillText(label, labelX, mid + 3);
        registerCanvasTooltip(
          "price",
          (labelX + right) / 2,
          mid,
          Math.max(right - labelX, 60),
          Math.max(bottom - top + 8, 16),
          () => [
            `${label} footprint imbalance`,
            `Zone ${fmt(group.low)} - ${fmt(group.high)}`,
            `Dominant ${Math.round(group.dominant).toLocaleString("en-US")} / diagonal ${Math.max(Math.round(group.passive), 1).toLocaleString("en-US")}`,
            "Backend tick aggregate; pressure context, not an entry by itself.",
          ].join("\\n"),
        );
      }
      const pocY = y(Number(poc?.price));
      if (Number.isFinite(pocY) && pocY >= pad.top && pocY <= pad.top + priceH) {
        ctx.font = "800 9px -apple-system, BlinkMacSystemFont, sans-serif";
        ctx.fillStyle = themeColor("gold", 0.90);
        ctx.textAlign = "right";
        ctx.fillText(`TPOC ${fmt(poc.price)}`, right - maxW - 5, pocY + 3);
        registerCanvasTooltip(
          "price",
          right - maxW - 52,
          pocY,
          96,
          16,
          () => [
            "Classified tick volume profile",
            `Last tick bucket ${stats?.last_delta_ts || "-"}`,
            `Total ${Math.round(stats?.profile_total_volume || stats?.total_volume || poc.total_volume || 0).toLocaleString("en-US")}`,
            `Net delta ${signed(stats?.net_delta || 0)}`,
          ].join("\\n"),
        );
      }
      for (const [label, row] of [["VAH", valueArea?.vah], ["VAL", valueArea?.val]]) {
        const price = Number(row?.price);
        const yy = y(price);
        if (!Number.isFinite(yy) || yy < pad.top || yy > pad.top + priceH) continue;
        ctx.strokeStyle = themeColor("axis", 0.34);
        ctx.setLineDash([4, 4]);
        ctx.lineWidth = 0.8;
        ctx.beginPath();
        ctx.moveTo(profileLeft, yy);
        ctx.lineTo(profileLabelLeft, yy);
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.font = "800 9px -apple-system, BlinkMacSystemFont, sans-serif";
        ctx.fillStyle = themeColor("axis", 0.82);
        ctx.textAlign = "right";
        ctx.fillText(`${label} ${fmt(price)}`, right - maxW - 5, yy + 3);
        registerCanvasTooltip(
          "price",
          (profileLeft + profileLabelLeft) / 2,
          yy,
          Math.max(profileLabelLeft - profileLeft, 40),
          14,
          () => [`Value Area ${label}`, `Price ${fmt(price)}`, `Coverage ${Math.round(Number(valueArea?.coverage || 0) * 100)}%`, `POC ${fmt(valueArea?.poc?.price)}`].join("\\n"),
        );
      }
      if (cfg.deltaLabels !== false) {
        for (const item of levelDeltas) {
          const yy = y(item.price);
          if (!Number.isFinite(yy) || yy < pad.top || yy > pad.top + priceH) continue;
          const bullish = Number(item.delta || 0) >= 0;
          const label = `${bullish ? "+" : ""}${Math.round(item.delta).toLocaleString("en-US")}`;
          const x0 = profileLeft - 8;
          ctx.fillStyle = bullish ? themeColor("up", 0.82) : themeColor("down", 0.82);
          ctx.font = "800 9px -apple-system, BlinkMacSystemFont, sans-serif";
          ctx.textAlign = "right";
          ctx.fillText(`Δ ${label}`, x0, yy + 3);
          registerCanvasTooltip(
            "price",
            x0 - 24,
            yy,
            56,
            14,
            () => [
              `Delta at ${item.name}`,
              `Level ${fmt(item.price)}`,
              `Volume ${Math.round(item.volume).toLocaleString("en-US")}`,
              `Net delta ${signed(item.delta)}`,
              `Delta ratio ${Number(item.delta_ratio || 0).toFixed(2)}`,
              `Rows ${item.row_count}`,
            ].join("\\n"),
          );
        }
      }
      } finally {
        const elapsed = window.mcPerfEnd ? window.mcPerfEnd(perfToken, perfDetail, 10) : 0;
        window.mcTickProfileLast = {
          ts: new Date().toISOString(),
          detail: perfDetail,
          ms: Math.round(Number(elapsed || 0) * 10) / 10,
        };
        ctx.restore();
      }
    }

    function drawTickDelta(ctx, bars, pad, chartH, xStep) {
      const cfg = state.indicators.tickFlow;
      if (!cfg.enabled || !cfg.delta || !bars.length) return;
      const buckets = tickDeltaByVisibleBars(bars);
      if (!buckets.length) return;
      let maxAbs = 0;
      for (const row of buckets) {
        const value = Math.abs(Number(row.net_delta || 0));
        if (value > maxAbs) maxAbs = value;
      }
      if (maxAbs <= 0) return;
      const bandH = clamp(chartH * 0.30, 28, 62);
      const bandTop = pad.top;
      const bandBottom = Math.min(pad.top + bandH, pad.top + chartH);
      const baseY = bandTop + bandH * 0.52;
      const maxBarH = bandH * 0.38;
      const barW = Math.max(2, xStep * 0.44);
      const x = typeof chartX === "function" ? chartX(pad, xStep) : index => pad.left + index * xStep + xStep * 0.5;
      ctx.save();
      ctx.fillStyle = rgbaFromCssColor(css("--chart-bg"), isLightTheme() ? 0.92 : 0.84);
      ctx.fillRect(pad.left, bandTop, ctx.canvas.width / (window.devicePixelRatio || 1) - pad.left - pad.right, bandBottom - bandTop + 2);
      ctx.strokeStyle = rgbaFromCssColor(css("--axis"), 0.16);
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(pad.left, bandBottom + 2);
      ctx.lineTo(ctx.canvas.width / (window.devicePixelRatio || 1) - pad.right, bandBottom + 2);
      ctx.stroke();
      ctx.strokeStyle = rgbaFromCssColor(css("--axis"), 0.28);
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(pad.left, baseY);
      ctx.lineTo(ctx.canvas.width / (window.devicePixelRatio || 1) - pad.right, baseY);
      ctx.stroke();
      buckets.forEach((row, index) => {
        const net = Number(row.net_delta || 0);
        if (!net) return;
        const h = Math.max(1, Math.abs(net) / maxAbs * maxBarH);
        const cx = x(index);
        const y0 = net >= 0 ? baseY - h : baseY;
        ctx.fillStyle = net >= 0 ? themeColor("up", 0.62) : themeColor("down", 0.62);
        ctx.fillRect(cx - barW / 2, y0, barW, h);
        registerCanvasTooltip(
          "volume",
          cx,
          baseY + (net >= 0 ? -h / 2 : h / 2),
          barW,
          h + 4,
          () => [
            `Tick delta ${signed(net)}`,
            `Tick vol ${Math.round(row.total_volume || 0).toLocaleString("en-US")}`,
            `Trades ${Number(row.trade_count || 0).toLocaleString("en-US")}`,
          ].join("\\n"),
        );
      });
      if (cfg.labels) {
        const total = buckets.reduce((sum, row) => sum + Number(row.net_delta || 0), 0);
        ctx.font = "800 10px -apple-system, BlinkMacSystemFont, sans-serif";
        ctx.textAlign = "left";
        ctx.fillStyle = total >= 0 ? themeColor("up", 0.86) : themeColor("down", 0.86);
        ctx.fillText(`TΔ ${signed(total)}`, pad.left + 6, bandTop + 11);
      }
      ctx.restore();
    }

registerIndicatorCanvasHook(
  "tick_flow",
  "price_profile",
  ({ ctx, snapshot, pad, priceH, width, y }) => {
    drawTickVolumeProfile(ctx, snapshot, pad, priceH, width, y);
  },
  {
    stateKey: () => [
      state.tickContextLoadedAt || 0,
      state.indicators.tickFlow?.profile !== false ? 1 : 0,
      state.indicators.tickFlow?.imbalance === true ? 1 : 0,
      state.indicators.tickFlow?.imbalanceMinVolume || 25,
    ].join(":"),
  },
);

registerIndicatorCanvasHook(
  "tick_flow",
  "volume_overlay",
  ({ ctx, bars, pad, chartH, xStep }) => {
    drawTickDelta(ctx, bars, pad, chartH, xStep);
  },
  {
    stateKey: () => [
      state.tickContextLoadedAt || 0,
      state.indicators.tickFlow?.delta !== false ? 1 : 0,
      state.indicators.tickFlow?.labels === true ? 1 : 0,
    ].join(":"),
  },
);

registerIndicatorCanvasHook(
  "tick_flow",
  "current_price_adornment",
  ({ ctx, priceY, anchorX, axisX, pad, priceH }) => {
    drawRecentTickDeltaOnPriceLine(
      ctx,
      priceY,
      anchorX,
      axisX,
      pad,
      priceH,
    );
  },
  {
    stateKey: () => [
      state.tickContextLoadedAt || 0,
      state.indicators.tickFlow?.live === true ? 1 : 0,
      state.indicators.tickFlow?.recentDelta === true ? 1 : 0,
      state.indicators.tickFlow?.recentDeltaSeconds || 30,
    ].join(":"),
  },
);
