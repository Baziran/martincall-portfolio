let priceOverlayRenderFrame = 0;
let priceOverlayRenderSeq = 0;
let lastPriceObjectsRenderKey = "";
let volumeInteractionRenderFrame = 0;
let volumeRenderDataCacheState = null;
let lastVolumeInteractionFrame = null;

function resetDeferredChartLayerRenders() {
  if (priceOverlayRenderFrame) {
    cancelAnimationFrame(priceOverlayRenderFrame);
    priceOverlayRenderFrame = 0;
  }
  priceOverlayRenderSeq += 1;
  if (volumeInteractionRenderFrame) {
    cancelAnimationFrame(volumeInteractionRenderFrame);
    volumeInteractionRenderFrame = 0;
  }
  lastPriceObjectsRenderKey = "";
  volumeRenderDataCacheState = null;
  lastVolumeInteractionFrame = null;
  if (typeof resetCanvasTooltips === "function") {
    resetCanvasTooltips("price");
    resetCanvasTooltips("volume");
  }
}

function cachedRenderGeometry(kind, snapshot, width, height, compute) {
  if (typeof cachedChartGeometry === "function") return cachedChartGeometry(kind, snapshot, width, height, compute);
  return compute();
}

function priceRenderGeometry(snapshot, width, height, ctx = null) {
  const base = cachedRenderGeometry("price", snapshot, width, height, () => {
    const visible = visibleBars(snapshot);
    const { bars } = visible;
    const { pad, priceH, maxP, minP } = priceScale(snapshot, height, bars);
    const visualAxis = chartVisualAxis(bars);
    const visualSpan = visualAxis.span;
    const xStep = (width - pad.left - pad.right) / (visualSpan + rightGapBars());
    const priceToY = priceH / Math.max(maxP - minP, 1e-9);
    const y = price => pad.top + (maxP - price) * priceToY;
    const x = chartXFromVisualAxis(pad, xStep, visualAxis);
    const renderProjection = chartRenderProjection(bars, xStep, x, visualAxis);
    const bodyW = renderProjection.mode === "detail"
      ? Math.max(3, xStep * 0.55)
      : renderProjection.mode === "thin"
        ? Math.max(1, Math.min(xStep * 0.70, 2))
        : 1;
    const gexOnly = gexFocusMode();
    return { visible, bars, pad, priceH, maxP, minP, visualSpan, xStep, x, y, bodyW, gexOnly, renderProjection };
  });
  return { ctx, w: width, h: height, ...base };
}

function volumeRenderGeometry(snapshot, width, height) {
  return cachedRenderGeometry("volume", snapshot, width, height, () => {
    const visible = visibleBars(snapshot);
    const { bars } = visible;
    const pad = { left: CHART_LEFT_PAD, right: PRICE_AXIS_WIDTH, top: 4, bottom: 4 };
    const chartH = height - pad.top - pad.bottom;
    const visualAxis = chartVisualAxis(bars);
    const visualSpan = visualAxis.span;
    const xStep = (width - pad.left - pad.right) / (visualSpan + rightGapBars());
    const x = chartXFromVisualAxis(pad, xStep, visualAxis);
    const renderProjection = chartRenderProjection(bars, xStep, x, visualAxis);
    const bodyW = renderProjection.mode === "detail"
      ? Math.max(3, xStep * 0.55)
      : renderProjection.mode === "thin"
        ? Math.max(1, Math.min(xStep * 0.70, 2))
        : 1;
    return { visible, bars, pad, chartH, visualSpan, xStep, x, bodyW, w: width, h: height, renderProjection };
  });
}

function priceObjectsItemKey(item, fields) {
  if (!item || typeof item !== "object") return "";
  return fields.map(field => item[field] ?? "").join(":");
}

function priceObjectsItemsKey(items, fields) {
  const rows = Array.isArray(items) ? items : [];
  if (!fields.length) return rows.map(item => String(item ?? "")).join(";");
  return rows.map(item => priceObjectsItemKey(item, fields)).join(";");
}

function priceObjectsCollectionKey(items) {
  const rows = Array.isArray(items) ? items : [];
  return rows.map(item => {
    if (typeof chartStableJson === "function") return chartStableJson(item);
    try {
      return JSON.stringify(item);
    } catch (_error) {
      return String(item?.id ?? "");
    }
  }).join(";");
}

function priceObjectsOptionsKey(options = {}) {
  return [
    options.clear === false ? "keep" : "clear",
    priceObjectsItemsKey(options.excludeDrawingIds, []),
    priceObjectsItemsKey(options.excludePriceAlertIds, []),
    priceObjectsItemsKey(options.excludeOptionTargetIds, []),
  ].join("|");
}

function priceObjectsStateKey() {
  const optionRows = typeof allOptionTargets === "function" ? allOptionTargets() : state.optionTargets?.items;
  return [
    typeof chartUserObjectsRenderKey === "function" ? chartUserObjectsRenderKey() : "",
    priceObjectsCollectionKey(state.drawing?.objects),
    priceObjectsCollectionKey(state.alerts?.rules),
    priceObjectsCollectionKey(optionRows),
    priceObjectsCollectionKey(state.paperOrders),
  ].join("~");
}

function priceObjectsInteractionActive() {
  return Boolean(
    (typeof chartInteractionQualityActive === "function" && chartInteractionQualityActive())
    || state.requests.market.loading
    || state.requests.history.loading
    || state.drawing?.draft
    || state.drawing?.drag
    || state.alerts?.drag
    || state.optionTargets?.draggingId
    || state.paperTrading?.dragOrder
    || state.paperTrading?.dragEntryLabel
  );
}

function priceObjectsRenderCacheKey(snapshot, geometry, options = {}) {
  const bars = geometry?.bars || [];
  const visible = geometry?.visible || {};
  const first = bars[0];
  const middle = bars[Math.floor((bars.length - 1) / 2)];
  const last = bars[bars.length - 1];
  const pad = geometry?.pad || {};
  const gexSidebarBoundary = typeof gexLeftCompanionRight === "function"
    ? gexLeftCompanionRight(pad, Number(geometry?.w) || 0, snapshot)
    : Number(pad.left) || 0;
  return [
    state.symbol,
    state.dataSource,
    state.timeframe,
    state.range,
    visible.start ?? "",
    visible.end ?? "",
    bars.length,
    typeof chartBarRenderKey === "function" ? chartBarRenderKey(first) : first?.ts || "",
    typeof chartBarRenderKey === "function" ? chartBarRenderKey(middle) : middle?.ts || "",
    typeof chartBarRenderKey === "function" ? chartBarRenderKey(last) : last?.ts || "",
    Math.round(Number(geometry?.w) || 0),
    Math.round(Number(geometry?.h) || 0),
    Math.round(Number(pad.left) || 0),
    Math.round(Number(pad.right) || 0),
    Math.round(Number(pad.top) || 0),
    Math.round(Number(pad.bottom) || 0),
    Math.round(Number(gexSidebarBoundary) || 0),
    Number(geometry?.priceH || 0).toFixed(3),
    Number(geometry?.minP || 0).toFixed(4),
    Number(geometry?.maxP || 0).toFixed(4),
    Number(geometry?.xStep || 0).toFixed(4),
    typeof smoothOffsetBars === "function" ? Number(smoothOffsetBars() || 0).toFixed(4) : "",
    geometry?.gexOnly ? "gex" : "normal",
    document.body?.className || "",
    document.documentElement?.className || "",
    priceObjectsStateKey(),
    priceObjectsOptionsKey(options),
  ].join("~");
}

function priceObjectsRenderGuardState(snapshot, geometry, options = {}) {
  const key = priceObjectsRenderCacheKey(snapshot, geometry, options);
  const bypass = Boolean(options.force || options.immediate || options.animate || options.overlayImmediate || priceObjectsInteractionActive());
  return { skip: Boolean(!bypass && key && key === lastPriceObjectsRenderKey), key, bypass };
}

function rememberPriceObjectsRender(snapshot, geometry, options = {}) {
  if (priceObjectsInteractionActive() || options.animate) return;
  const key = priceObjectsRenderCacheKey(snapshot, geometry, options);
  if (key) lastPriceObjectsRenderKey = key;
}

function volumeRenderDataFor(snapshot, geometry, volumeCfg) {
  const bars = geometry?.bars || [];
  const visible = geometry?.visible || {};
  const source = String(volumeCfg?.source || "");
  const sourceSeries = vsaRenderSourceSeries(snapshot);
  const barsVersion = Number(chartRenderVersions.bars || 0);
  const visibleStart = Number(visible.start ?? 0);
  const visibleEnd = Number(visible.end ?? bars.length);
  const visibleSetting = volumeCfg?.visible !== false;
  const averageSetting = Boolean(volumeCfg?.avg);
  const labelsSetting = Boolean(volumeCfg?.labels);
  const colorsSetting = Boolean(volumeCfg?.volumeColors);
  const cached = volumeRenderDataCacheState;
  if (
    cached
    && cached.snapshot === snapshot
    && cached.sourceSeries === sourceSeries
    && cached.barsVersion === barsVersion
    && cached.visibleStart === visibleStart
    && cached.visibleEnd === visibleEnd
    && cached.barsLength === bars.length
    && cached.source === source
    && cached.visibleSetting === visibleSetting
    && cached.averageSetting === averageSetting
    && cached.labelsSetting === labelsSetting
    && cached.colorsSetting === colorsSetting
  ) return cached.data;

  const vsSeries = volumeStructureSeries(snapshot, bars);
  const rawDisplayVolumes = new Float64Array(bars.length);
  const rawDisplayAverages = new Float64Array(bars.length);
  for (let index = 0; index < bars.length; index += 1) {
    const item = vsSeries[index];
    rawDisplayVolumes[index] = Number(item?.display_volume ?? bars[index]?.volume ?? 0);
    const avg = item?.display_avg;
    rawDisplayAverages[index] = avg === null || avg === undefined ? Number.NaN : Number(avg);
  }
  const compressionCap = volumeCompressionCap(rawDisplayVolumes);
  const displayVolumes = new Float64Array(rawDisplayVolumes.length);
  const displayAverages = new Float64Array(rawDisplayAverages.length);
  let maxV = 1;
  for (let index = 0; index < rawDisplayVolumes.length; index += 1) {
    const volume = compressVolumeForDisplay(rawDisplayVolumes[index], compressionCap);
    const avg = Number.isFinite(rawDisplayAverages[index])
      ? compressVolumeForDisplay(rawDisplayAverages[index], compressionCap)
      : Number.NaN;
    displayVolumes[index] = volume;
    displayAverages[index] = avg;
    if (volume > maxV) maxV = volume;
    if (Number.isFinite(avg) && avg > maxV) maxV = avg;
  }
  const data = { vsSeries, rawDisplayVolumes, rawDisplayAverages, displayVolumes, displayAverages, compressionCap, maxV };
  volumeRenderDataCacheState = {
    snapshot,
    sourceSeries,
    barsVersion,
    visibleStart,
    visibleEnd,
    barsLength: bars.length,
    source,
    visibleSetting,
    averageSetting,
    labelsSetting,
    colorsSetting,
    data,
  };
  return data;
}

function batchedPathFor(map, color) {
  const key = String(color || "");
  let path = map.get(key);
  if (!path) {
    path = new Path2D();
    map.set(key, path);
  }
  return path;
}

function flushStrokeBatches(ctx, batches) {
  batches.forEach((path, color) => {
    ctx.strokeStyle = color;
    ctx.stroke(path);
  });
}

function flushFillBatches(ctx, batches) {
  batches.forEach((path, color) => {
    ctx.fillStyle = color;
    ctx.fill(path);
  });
}

function drawClimaxDirectionBand(ctx, vsaItem, x, y, bodyW, bodyH) {
  const direction = String(vsaItem?.direction || "").toLowerCase();
  if (direction !== "long" && direction !== "short") return;
  const bandH = Math.max(bodyH / 3, 1);
  const previousFillStyle = ctx.fillStyle;
  if (direction === "long") {
    ctx.fillStyle = themeColor("up");
    ctx.fillRect(x, y, bodyW, bandH);
  } else {
    ctx.fillStyle = themeColor("down");
    ctx.fillRect(x, y + bodyH - bandH, bodyW, bandH);
  }
  ctx.fillStyle = previousFillStyle;
}

function drawFuelDirectionBand(ctx, vsaItem, x, y, bodyW, bodyH) {
  const direction = String(vsaItem?.direction || "").toLowerCase();
  if (direction !== "long" && direction !== "short") return;
  const bandH = Math.max(bodyH / 3, 1);
  const previousFillStyle = ctx.fillStyle;
  ctx.fillStyle = "#000000";
  if (direction === "long") {
    ctx.fillRect(x, y, bodyW, bandH);
  } else {
    ctx.fillRect(x, y + bodyH - bandH, bodyW, bandH);
  }
  ctx.fillStyle = previousFillStyle;
}

function drawClimaxCandlePart(ctx, paint, x, y, width, height, vsaItem = null) {
  const stroke = paint.stroke || "#000000";
  const fill = paint.fill || "#ffffff";
  const cx = x + width / 2;
  const lineWidth = isLightTheme() ? 1.25 : 1;
  ctx.lineWidth = lineWidth;
  if (width <= 1.01) {
    ctx.strokeStyle = stroke;
    ctx.beginPath();
    ctx.moveTo(cx, y);
    ctx.lineTo(cx, y + height);
    ctx.stroke();
    return;
  }
  const bodyW = Math.max(width, 1);
  const bodyH = Math.max(height, 1);
  ctx.fillStyle = fill;
  ctx.fillRect(x, y, bodyW, bodyH);
  if (vsaItem?.terminal_climax && bodyH > 2) {
    drawClimaxDirectionBand(ctx, { direction: vsaItem.terminal_direction || vsaItem.direction }, x, y, bodyW, bodyH);
  } else if (vsaItem?.fuel && bodyH > 2) {
    drawFuelDirectionBand(ctx, vsaItem, x, y, bodyW, bodyH);
  }
  ctx.strokeStyle = stroke;
  ctx.strokeRect(x + lineWidth * 0.5, y + lineWidth * 0.5, Math.max(bodyW - lineWidth, 1), Math.max(bodyH - lineWidth, 1));
}

function drawCandlePrimitives(snapshot, geometry, addRect) {
  const { visible, bars, pad, priceH, xStep, x, y, bodyW, gexOnly } = geometry;
  const latestVisibleIndex = (snapshot?.bars?.length || 0) - 1 - Number(visible?.start || 0);
  const vsaPriceSeries = !gexOnly && state.indicators.vsaVolume.visible !== false && state.indicators.vsaVolume.candleColors
    ? vsaSeries(snapshot, bars)
    : [];
  const indicatorCandleStyles = !gexOnly && typeof indicatorCandleStyleSeries === "function"
    ? indicatorCandleStyleSeries(snapshot, bars)
    : [];
  bars.forEach((bar, i) => {
    if (!hasPriceBar(bar)) {
      return;
    }
    const vsaItem = vsaPriceSeries[i];
    const basePaint = indicatorCandleStyles[i] || volumeCandlePaint(vsaItem, bar);
    const cx = x(i);
    const liveBar = bar.closed === false && i === latestVisibleIndex;
    const paint = liveBar
      ? (basePaint && typeof basePaint === "object"
        ? { ...basePaint, provisional: true }
        : { color: basePaint, provisional: true })
      : basePaint;
    let drawHigh = Number(bar.high);
    let drawLow = Number(bar.low);
    let drawOpen = Number(bar.open);
    let drawClose = Number(bar.close);
    if (liveBar) {
      const quote = typeof currentChartQuoteDisplay === "function" ? currentChartQuoteDisplay(15000) : null;
      const current = finiteSeriesNumber(quote?.price);
      const bid = finiteSeriesNumber(quote?.bid);
      const ask = finiteSeriesNumber(quote?.ask);
      const livePrices = [drawOpen, drawHigh, drawLow, drawClose, current]
        .filter(Number.isFinite);
      if (Number.isFinite(bid)) livePrices.push(bid);
      if (Number.isFinite(ask)) livePrices.push(ask);
      if (livePrices.length) {
        drawHigh = Math.max(...livePrices);
        drawLow = Math.min(...livePrices);
        if (Number.isFinite(current)) drawClose = current;
      }
    }
    const wickTop = y(drawHigh);
    const wickBottom = y(drawLow);
    addRect(paint, cx - 0.5, Math.min(wickTop, wickBottom), 1, Math.max(Math.abs(wickBottom - wickTop), 1));
    const top = y(Math.max(drawOpen, drawClose));
    const bottom = y(Math.min(drawOpen, drawClose));
    const bodyX = cx - bodyW / 2;
    const bodyY = top;
    const bodyH = Math.max(bottom - top, 2);
    addRect(paint, bodyX, bodyY, bodyW, bodyH, paint?.mode === "terminal" || paint?.mode === "fuel" ? vsaItem : null);
  });
}

function chartOverviewColumnPrices(snapshot, geometry, column) {
  let open = Number(column?.renderOpen);
  let high = Number(column?.renderHigh);
  let low = Number(column?.renderLow);
  let close = Number(column?.renderClose);
  const absoluteIndex = Number(geometry?.visible?.start || 0) + Number(column?.sourceStart || 0);
  const currentSourceIndex = Math.max((snapshot?.bars?.length || 0) - 1, 0);
  if (column?.provisional && absoluteIndex === currentSourceIndex) {
    const quote = typeof currentChartQuoteDisplay === "function" ? currentChartQuoteDisplay(15000) : null;
    const current = finiteSeriesNumber(quote?.price);
    const bid = finiteSeriesNumber(quote?.bid);
    const ask = finiteSeriesNumber(quote?.ask);
    const livePrices = [open, high, low, close, current].filter(Number.isFinite);
    if (Number.isFinite(bid)) livePrices.push(bid);
    if (Number.isFinite(ask)) livePrices.push(ask);
    if (livePrices.length) {
      high = Math.max(...livePrices);
      low = Math.min(...livePrices);
      if (Number.isFinite(current)) close = current;
    }
  }
  return { open, high, low, close };
}

function drawOverviewCandleColumns(ctx, snapshot, geometry) {
  const columns = geometry?.renderProjection?.columns || [];
  if (!columns.length) return;
  const batches = new Map();
  const provisional = [];
  columns.forEach(column => {
    const prices = chartOverviewColumnPrices(snapshot, geometry, column);
    if (![prices.open, prices.high, prices.low, prices.close].every(Number.isFinite)) return;
    const color = prices.close >= prices.open ? css("--green") : css("--red");
    const cx = Number(column.centerX);
    if (!Number.isFinite(cx)) return;
    const part = {
      color,
      cx,
      highY: geometry.y(prices.high),
      lowY: geometry.y(prices.low),
      openY: geometry.y(prices.open),
      closeY: geometry.y(prices.close),
    };
    if (column.provisional) {
      provisional.push(part);
      return;
    }
    const path = batchedPathFor(batches, color);
    path.moveTo(cx, part.highY);
    path.lineTo(cx, part.lowY);
    path.moveTo(cx - 0.75, part.openY);
    path.lineTo(cx, part.openY);
    path.moveTo(cx, part.closeY);
    path.lineTo(cx + 0.75, part.closeY);
  });
  ctx.save();
  ctx.lineWidth = 1;
  flushStrokeBatches(ctx, batches);
  provisional.forEach(part => {
    ctx.strokeStyle = part.color || css("--muted");
    ctx.globalAlpha = 0.82;
    ctx.setLineDash([3, 2]);
    ctx.beginPath();
    ctx.moveTo(part.cx, part.highY);
    ctx.lineTo(part.cx, part.lowY);
    ctx.moveTo(part.cx - 0.75, part.openY);
    ctx.lineTo(part.cx, part.openY);
    ctx.moveTo(part.cx, part.closeY);
    ctx.lineTo(part.cx + 0.75, part.closeY);
    ctx.stroke();
  });
  ctx.restore();
}

function drawChartOverviewBadge(ctx, geometry) {
  const projection = geometry?.renderProjection;
  if (projection?.mode !== "overview" || !projection.columns?.length) return;
  const label = `${String(state.timeframe || "")} · OVERVIEW · up to ${projection.maxSourceBarsPerColumn} bars/px`;
  ctx.save();
  ctx.font = `10px ${CANVAS_MONO_FONT}`;
  const width = Math.ceil(ctx.measureText(label).width) + 12;
  const x = Number(geometry.pad?.left || 0) + 5;
  const y = Number(geometry.pad?.top || 0) + 5;
  ctx.fillStyle = isLightTheme() ? "rgba(255,255,255,0.84)" : "rgba(2,6,23,0.78)";
  ctx.fillRect(x, y, width, 18);
  ctx.fillStyle = calibratedCanvasColor(css("--muted"), { minRatio: 3.2 });
  ctx.fillText(label, x + 6, y + 12);
  ctx.restore();
}

function drawBatchedCandles(ctx, snapshot, geometry) {
  if (geometry?.renderProjection?.mode === "overview") {
    drawOverviewCandleColumns(ctx, snapshot, geometry);
    return;
  }
  const wickBatches = new Map();
  const bodyBatches = new Map();
  const climaxParts = [];
  const provisionalParts = [];
  ctx.globalAlpha = 1;
  drawCandlePrimitives(snapshot, geometry, (paint, x, y, width, height, vsaItem = null) => {
    if (paint?.provisional) {
      provisionalParts.push({ paint, x, y, width, height });
      return;
    }
    if (paint?.mode === "terminal" || paint?.mode === "fuel") {
      climaxParts.push({ paint, vsaItem, x, y, width, height });
      return;
    }
    const color = paint?.color || paint;
    const cx = x + width / 2;
    if (width <= 1.01) {
      const wickPath = batchedPathFor(wickBatches, color);
      wickPath.moveTo(cx, y);
      wickPath.lineTo(cx, y + height);
      return;
    }
    batchedPathFor(bodyBatches, color).rect(x, y, width, height);
  });
  flushStrokeBatches(ctx, wickBatches);
  flushFillBatches(ctx, bodyBatches);
  climaxParts.forEach(part => drawClimaxCandlePart(ctx, part.paint, part.x, part.y, part.width, part.height, part.vsaItem));
  provisionalParts.forEach(part => {
    const color = part.paint.color || part.paint.stroke || part.paint.fill || css("--muted");
    const centerX = part.x + part.width / 2;
    ctx.save();
    ctx.strokeStyle = color;
    ctx.fillStyle = color;
    ctx.setLineDash([3, 2]);
    if (part.width <= 1.01) {
      ctx.globalAlpha = 0.78;
      ctx.beginPath();
      ctx.moveTo(centerX, part.y);
      ctx.lineTo(centerX, part.y + part.height);
      ctx.stroke();
    } else {
      ctx.globalAlpha = 0.28;
      ctx.fillRect(part.x, part.y, part.width, part.height);
      ctx.globalAlpha = 0.9;
      ctx.strokeRect(part.x + 0.5, part.y + 0.5, Math.max(part.width - 1, 1), Math.max(part.height - 1, 1));
    }
    ctx.restore();
  });
}

function drawVolumePrimitives(bars, geometry, volumeCfg, vsSeries, rawDisplayVolumes, displayVolumes, compressionCap, maxV, addRect) {
  const { pad, chartH, xStep, x, bodyW } = geometry;
  bars.forEach((bar, i) => {
    if (!hasPriceBar(bar)) return;
    const cx = x(i);
    const item = vsSeries[i];
    const volume = displayVolumes[i];
    const vH = (volume / maxV) * chartH;
    const highVolume = rawDisplayVolumes[i] >= compressionCap;
    let color = volumeCfg.volumeColors && item?.terminal_climax
      ? climaxCandleColors().stroke
      : volumeCfg.volumeColors && item?.fuel
      ? fuelCandleColors().stroke
      : volumeCfg.volumeColors && item?.code
        ? volumeStructureColor(item, bar)
        : highVolume
          ? css("--gold")
          : volumeStructureColor(null, bar);
    if (item?.close_auction && !item?.code) color = themeColor("neutral", 0.62);
    const width = volumeBarWidth(bodyW, xStep, rawDisplayVolumes[i], compressionCap, item);
    addRect(color, cx - width / 2, pad.top + chartH - vH, width, Math.max(vH, 1));
  });
}

function drawBatchedVolumeBars(ctx, bars, geometry, volumeCfg, vsSeries, rawDisplayVolumes, displayVolumes, compressionCap, maxV) {
  const batches = new Map();
  drawVolumePrimitives(bars, geometry, volumeCfg, vsSeries, rawDisplayVolumes, displayVolumes, compressionCap, maxV, (color, x, y, width, height) => {
    batchedPathFor(batches, color).rect(x, y, width, height);
  });
  flushFillBatches(ctx, batches);
}

function chartOverviewVolumeData(renderProjection) {
  const columns = renderProjection?.columns || [];
  const rawDisplayVolumes = columns.map(column => (
    column.renderVolumeKnown ? Math.max(Number(column.renderVolume) || 0, 0) : 0
  ));
  const compressionCap = volumeCompressionCap(rawDisplayVolumes);
  const displayVolumes = rawDisplayVolumes.map(value => compressVolumeForDisplay(value, compressionCap));
  return {
    rawDisplayVolumes,
    displayVolumes,
    compressionCap,
    maxV: Math.max(...displayVolumes, 1),
  };
}

function drawBatchedOverviewVolumeColumns(ctx, geometry, volumeData) {
  const columns = geometry?.renderProjection?.columns || [];
  const { rawDisplayVolumes, displayVolumes, compressionCap, maxV } = volumeData;
  const batches = new Map();
  columns.forEach((column, index) => {
    const volume = Number(displayVolumes[index]);
    if (!Number.isFinite(volume)) return;
    const cx = Number(column.centerX);
    if (!Number.isFinite(cx)) return;
    const highVolume = Number(rawDisplayVolumes[index]) >= compressionCap;
    const color = highVolume
      ? css("--gold")
      : Number(column.renderClose) >= Number(column.renderOpen)
        ? themeColor("up", 0.68)
        : themeColor("down", 0.68);
    const height = Math.max((volume / Math.max(maxV, 1)) * geometry.chartH, 1);
    batchedPathFor(batches, color).rect(
      cx - 0.5,
      geometry.pad.top + geometry.chartH - height,
      1,
      height,
    );
  });
  flushFillBatches(ctx, batches);
}

    function drawPriceOverlayLayer(snapshot, geometry) {
      const perfToken = window.mcPerfStart ? window.mcPerfStart("renderOverlay") : null;
      const { ctx, w, h, visible, bars, pad, priceH, maxP, minP, xStep, x, y, gexOnly } = geometry;
      try {
        ctx.clearRect(0, 0, w, h);
        const labelStacks = new Map();
        const env = { ctx, snapshot, visible, bars, pad, priceH, width: w, height: h, minP, maxP, xStep, x, y, gexOnly, labelStacks };
        drawManagedCanvasLayers("indicators", env);
        drawManagedCanvasLayers("profile", env);
      } finally {
        if (window.mcPerfEnd) window.mcPerfEnd(perfToken, `${bars?.length || 0} bars`, 24);
      }
    }

    function drawPriceGexLevelsLayer(snapshot, geometry, options = {}) {
      const { ctx, w, h, visible, bars, pad, priceH, maxP, minP, xStep, x, y, gexOnly } = geometry;
      if (options.clear !== false) ctx.clearRect(0, 0, w, h);
      drawManagedCanvasLayers("gex", {
        ctx,
        snapshot,
        visible,
        bars,
        pad,
        priceH,
        width: w,
        height: h,
        maxP,
        minP,
        xStep,
        x,
        y,
        gexOnly,
      });
    }

    function drawPriceObjectsLayer(snapshot, geometry, options = {}) {
      const perfToken = window.mcPerfStart ? window.mcPerfStart("renderObjects") : null;
      const { ctx, w, h, visible, bars, pad, priceH, maxP, minP, xStep, x, y, gexOnly } = geometry;
      try {
        if (options.clear !== false) ctx.clearRect(0, 0, w, h);
        drawManagedCanvasLayers("objects", { ctx, snapshot, visible, bars, pad, priceH, width: w, height: h, maxP, minP, xStep, x, y, gexOnly }, options);
        flushCanvasTooltipFront("price");
      } finally {
        if (window.mcPerfEnd) window.mcPerfEnd(perfToken, `${bars?.length || 0} bars`, 18);
      }
    }

    function drawPriceInteractionLayer(snapshot, geometry) {
      const { ctx, w, h, visible, bars, pad, priceH, maxP, minP, xStep, x, y, gexOnly } = geometry;
      ctx.clearRect(0, 0, w, h);
      drawManagedCanvasLayers("interaction", { ctx, snapshot, visible, bars, pad, priceH, width: w, height: h, maxP, minP, xStep, x, y, gexOnly });
    }

    function drawPriceGexFrontLayer(snapshot, geometry) {
      const { ctx, w, h, visible, bars, pad, priceH, maxP, minP, xStep, x, y, gexOnly } = geometry;
      ctx.clearRect(0, 0, w, h);
      drawManagedPriceOverlays("front", { ctx, snapshot, visible, bars, pad, priceH, width: w, height: h, minP, maxP, xStep, x, y, gexOnly });
    }

    function rememberPriceBaseTooltips() {
      state.tooltipBase = state.tooltipBase || {};
      const price = state.tooltip?.price || [];
      state.tooltipBase.price = price.length ? price.slice() : [];
    }

    function restorePriceBaseTooltips() {
      if (!state.tooltip) state.tooltip = { price: [], volume: [] };
      const base = state.tooltipBase?.price;
      state.tooltip.price = Array.isArray(base) && base.length ? base.slice() : [];
    }

    function rememberPriceOverlayTooltips() {
      state.tooltipOverlayBase = state.tooltipOverlayBase || {};
      const price = state.tooltip?.price || [];
      state.tooltipOverlayBase.price = price.length ? price.slice() : [];
    }

    function restorePriceOverlayTooltips() {
      if (!state.tooltip) state.tooltip = { price: [], volume: [] };
      const base = state.tooltipOverlayBase?.price || state.tooltipBase?.price;
      state.tooltip.price = Array.isArray(base) && base.length ? base.slice() : [];
    }

    function renderPriceOverlay(snapshot = state.snapshot) {
      if (!snapshot?.bars?.length) return;
      const overlayCanvas = document.getElementById("price-overlay");
      if (!overlayCanvas) {
        renderPriceChart(snapshot);
        return;
      }
      const { ctx, w, h } = canvasContext("price-overlay");
      restorePriceBaseTooltips();
      const geometry = priceRenderGeometry(snapshot, w, h, ctx);
      drawPriceOverlayLayer(snapshot, geometry);
      flushCanvasTooltipFront("price");
      renderPriceGexLevelsFromGeometry(snapshot, geometry);
      rememberPriceOverlayTooltips();
      const objectsCanvas = document.getElementById("price-objects");
      if (objectsCanvas) {
        renderPriceObjects(snapshot);
      } else {
        drawPriceObjectsLayer(snapshot, geometry, { clear: false });
      }
    }

    function renderPriceObjects(snapshot = state.snapshot, options = {}) {
      if (!snapshot?.bars?.length) return false;
      const objectsCanvas = document.getElementById("price-objects");
      if (!objectsCanvas) {
        renderPriceOverlay(snapshot);
        return true;
      }
      const { ctx, w, h } = canvasContext("price-objects");
      restorePriceOverlayTooltips();
      const geometry = priceRenderGeometry(snapshot, w, h, ctx);
      const guard = priceObjectsRenderGuardState(snapshot, geometry, options);
      if (guard.skip) {
        if (window.mcTelemetryInc) window.mcTelemetryInc("render.objects.skipped");
        flushCanvasTooltipFront("price");
        renderPriceGexFrontFromGeometry(snapshot, geometry);
        return false;
      }
      drawPriceObjectsLayer(snapshot, geometry, options);
      renderPriceTradingFromGeometry(snapshot, geometry);
      renderPriceInteractionFromGeometry(snapshot, geometry);
      renderPriceGexFrontFromGeometry(snapshot, geometry);
      if (guard.key && !guard.bypass) lastPriceObjectsRenderKey = guard.key;
      return true;
    }

    function renderPriceObjectsFromGeometry(snapshot, geometry, options = {}) {
      if (!snapshot?.bars?.length || !geometry) return false;
      const objectsCanvas = document.getElementById("price-objects");
      if (!objectsCanvas) {
        drawPriceObjectsLayer(snapshot, geometry, { ...options, clear: false });
        renderPriceTrading(snapshot);
        renderPriceInteraction(snapshot);
        return true;
      }
      const guard = priceObjectsRenderGuardState(snapshot, geometry, options);
      if (guard.skip) {
        if (window.mcTelemetryInc) window.mcTelemetryInc("render.objects.skipped");
        flushCanvasTooltipFront("price");
        renderPriceGexFrontFromGeometry(snapshot, geometry);
        return false;
      }
      const objectsLayer = canvasContext("price-objects");
      drawPriceObjectsLayer(
        snapshot,
        { ...geometry, ctx: objectsLayer.ctx, w: geometry.w, h: geometry.h },
        { ...options, clear: options.clear !== false },
      );
      renderPriceTradingFromGeometry(snapshot, geometry);
      renderPriceInteractionFromGeometry(snapshot, geometry);
      renderPriceGexFrontFromGeometry(snapshot, geometry);
      if (guard.key && !guard.bypass) lastPriceObjectsRenderKey = guard.key;
      return true;
    }

    function renderPriceTrading(snapshot = state.snapshot) {
      if (!snapshot?.bars?.length) return;
      const tradingCanvas = document.getElementById("price-trading");
      if (!tradingCanvas) {
        state.paperOrderActionHits = [];
        return;
      }
      const { w, h } = canvasContext("price-trading");
      renderPriceTradingFromGeometry(snapshot, priceRenderGeometry(snapshot, w, h));
    }

    function renderPriceTradingFromGeometry(snapshot = state.snapshot, geometry = null) {
      if (!snapshot?.bars?.length || !geometry) return;
      const tradingCanvas = document.getElementById("price-trading");
      if (!tradingCanvas) {
        state.paperOrderActionHits = [];
        return;
      }
      const { ctx, w, h } = canvasContext("price-trading");
      ctx.clearRect(0, 0, w, h);
      const { visible, bars, pad, priceH, maxP, minP, xStep, x, y, gexOnly } = geometry;
      drawManagedCanvasLayers("trading", { ctx, snapshot, visible, bars, pad, priceH, width: w, height: h, maxP, minP, xStep, x, y, gexOnly });
    }

    function renderPriceInteraction(snapshot = state.snapshot) {
      if (!snapshot?.bars?.length) return;
      const interactionCanvas = document.getElementById("price-interaction");
      if (!interactionCanvas) {
        return;
      }
      const { w, h } = canvasContext("price-interaction");
      renderPriceInteractionFromGeometry(snapshot, priceRenderGeometry(snapshot, w, h));
    }

    function renderPriceInteractionFromGeometry(snapshot = state.snapshot, geometry = null) {
      if (!snapshot?.bars?.length || !geometry) return;
      const interactionCanvas = document.getElementById("price-interaction");
      if (!interactionCanvas) {
        return;
      }
      const { ctx, w, h } = canvasContext("price-interaction");
      const { visible, bars, pad, priceH, maxP, minP, xStep, x, y, gexOnly } = geometry;
      drawPriceInteractionLayer(snapshot, { ctx, w, h, visible, bars, pad, priceH, maxP, minP, xStep, x, y, gexOnly });
    }

    function renderPriceGexLevelsFromGeometry(snapshot = state.snapshot, geometry = null) {
      if (!geometry) return;
      if (typeof clearCanvasTooltipsBySource === "function") {
        clearCanvasTooltipsBySource("price", GEX_TOOLTIP_SOURCE_LEVELS);
      }
      const levelsCanvas = document.getElementById("price-gex-levels");
      if (levelsCanvas) {
        const { ctx, w, h } = canvasContext("price-gex-levels");
        drawPriceGexLevelsLayer(
          snapshot,
          { ...geometry, ctx, w, h },
        );
      } else {
        drawPriceGexLevelsLayer(snapshot, geometry, { clear: false });
      }
      flushCanvasTooltipFront("price");
      if (Array.isArray(state.tooltipOverlayBase?.price)) {
        const currentLevelRows = (state.tooltip?.price || []).filter(
          item => item?.source === GEX_TOOLTIP_SOURCE_LEVELS,
        );
        state.tooltipOverlayBase.price = [
          ...state.tooltipOverlayBase.price.filter(
            item => item?.source !== GEX_TOOLTIP_SOURCE_LEVELS,
          ),
          ...currentLevelRows,
        ];
      }
    }

    function renderPriceGexFrontFromGeometry(snapshot = state.snapshot, geometry = null) {
      renderGexDockFromGeometry(snapshot, geometry);
      const frontCanvas = document.getElementById("price-gex-front");
      if (!frontCanvas || !geometry) return;
      if (typeof clearCanvasTooltipsBySource === "function") {
        clearCanvasTooltipsBySource("price", GEX_TOOLTIP_SOURCE_FRONT);
      }
      const { ctx, w, h } = canvasContext("price-gex-front");
      if (!snapshot?.bars?.length) {
        ctx.clearRect(0, 0, w, h);
        return;
      }
      const { visible, bars, pad, priceH, maxP, minP, xStep, x, y, gexOnly } = geometry;
      drawPriceGexFrontLayer(snapshot, { ctx, w, h, visible, bars, pad, priceH, maxP, minP, xStep, x, y, gexOnly });
      flushCanvasTooltipFront("price");
    }

    function renderPriceGexLayers(snapshot = state.snapshot) {
      const levelsCanvas = document.getElementById("price-gex-levels");
      const frontCanvas = document.getElementById("price-gex-front");
      const anchorCanvas = levelsCanvas || frontCanvas;
      if (!anchorCanvas) return;
      const anchorLayer = canvasContext(anchorCanvas.id);
      if (
        typeof gexProfileGutterGeometry === "function"
        && typeof applyGexSidebarGeometryToDom === "function"
      ) {
        applyGexSidebarGeometryToDom(gexProfileGutterGeometry(
          { left: CHART_LEFT_PAD, right: PRICE_AXIS_WIDTH },
          anchorLayer.w,
        ));
      }
      if (!snapshot?.bars?.length) {
        for (const canvasId of ["price-gex-levels", "price-gex-front"]) {
          if (!document.getElementById(canvasId)) continue;
          const layer = canvasContext(canvasId);
          layer.ctx.clearRect(0, 0, layer.w, layer.h);
        }
        if (typeof clearCanvasTooltipsBySource === "function") {
          clearCanvasTooltipsBySource(
            "price",
            [GEX_TOOLTIP_SOURCE_LEVELS, GEX_TOOLTIP_SOURCE_FRONT],
          );
        }
        return;
      }
      const { w, h } = anchorLayer;
      const geometry = priceRenderGeometry(snapshot, w, h);
      renderPriceGexLevelsFromGeometry(snapshot, geometry);
      renderPriceGexFrontFromGeometry(snapshot, geometry);
    }

    function schedulePriceOverlayRender(snapshot, geometry) {
      const seq = ++priceOverlayRenderSeq;
      if (priceOverlayRenderFrame) cancelAnimationFrame(priceOverlayRenderFrame);
      priceOverlayRenderFrame = requestAnimationFrame(() => {
        priceOverlayRenderFrame = 0;
        if (seq !== priceOverlayRenderSeq || snapshot !== state.snapshot) return;
        try {
          restorePriceBaseTooltips();
          drawPriceOverlayLayer(snapshot, geometry);
          flushCanvasTooltipFront("price");
          renderPriceGexLevelsFromGeometry(snapshot, geometry);
          rememberPriceOverlayTooltips();
          renderPriceObjectsFromGeometry(snapshot, geometry);
        } catch (error) {
          if (typeof showRuntimeError === "function") showRuntimeError(error, "priceOverlay");
          else console.error("price overlay render failed", error);
        }
      });
    }

    function renderPriceOverlayNow(snapshot, geometry) {
      if (priceOverlayRenderFrame) {
        cancelAnimationFrame(priceOverlayRenderFrame);
        priceOverlayRenderFrame = 0;
      }
      priceOverlayRenderSeq += 1;
      try {
        restorePriceBaseTooltips();
        drawPriceOverlayLayer(snapshot, geometry);
        flushCanvasTooltipFront("price");
        renderPriceGexLevelsFromGeometry(snapshot, geometry);
        rememberPriceOverlayTooltips();
        renderPriceObjectsFromGeometry(snapshot, geometry);
        if (typeof syncAdvisorTogglePosition === "function") {
          syncAdvisorTogglePosition({ pad: geometry.pad, width: geometry.w, snapshot });
        }
      } catch (error) {
        if (typeof showRuntimeError === "function") showRuntimeError(error, "priceOverlayNow");
        else console.error("price overlay render failed", error);
      }
    }

    function renderPriceChart(snapshot, options = {}) {
      const perfToken = window.mcPerfStart ? window.mcPerfStart("renderPrice") : null;
      const { ctx, w, h } = canvasContext("price-chart");
      const overlayLayer = document.getElementById("price-overlay") ? canvasContext("price-overlay") : { ctx, w, h };
      const hasObjectLayer = Boolean(document.getElementById("price-objects"));
      resetCanvasTooltips("price");
      const geometry = priceRenderGeometry(snapshot, w, h, ctx);
      const { visible, bars, pad, priceH, maxP, minP, xStep, x, y, gexOnly } = geometry;
      if (
        typeof gexProfileGutterGeometry === "function"
        && typeof applyGexSidebarGeometryToDom === "function"
      ) {
        applyGexSidebarGeometryToDom(gexProfileGutterGeometry(pad, w));
      }

      ctx.clearRect(0, 0, w, h);
      ctx.fillStyle = css("--chart-bg");
      ctx.fillRect(0, 0, w, h);
      if (!gexOnly) {
        drawSessionBackground(ctx, bars, pad, priceH, xStep, x, {
          opacity: state.settings.sessionPriceOpacity,
          labels: false,
        });
        drawProviderDataGaps(ctx, bars, pad, priceH, xStep, x, { labels: true });
        drawOpeningRange(ctx, bars, pad, priceH, xStep, x, y, w);
      }

      ctx.lineWidth = 1;
      ctx.fillStyle = css("--axis");
      ctx.font = `11px ${CANVAS_MONO_FONT}`;
      for (let i = 0; i <= 6; i++) {
        const py = pad.top + i * priceH / 6;
        const price = maxP - i * (maxP - minP) / 6;
        if (state.settings.showHorizontalGrid) {
          ctx.strokeStyle = css("--grid-line");
          ctx.beginPath();
          ctx.moveTo(pad.left, py);
          ctx.lineTo(w - pad.right, py);
          ctx.stroke();
        }
        ctx.fillText(price.toFixed(2), w - pad.right + 12, py + 4);
      }

      if (!gexOnly) {
        drawPriceLevels(ctx, snapshot, pad, w, y);
        drawDayLevels(ctx, pad, w, y);
        const paperCtx = typeof paperActiveTradeForChart === "function" ? paperActiveTradeForChart(snapshot) : null;
        if (!paperCtx?.active) drawTradePlanLines(ctx, snapshot, pad, w, y);
      }
      drawManagedCanvasLayers("background", { ctx, snapshot, visible, bars, pad, priceH, width: w, height: h, minP, maxP, xStep, x, y, gexOnly, labelStacks: new Map() });
      drawBatchedCandles(ctx, snapshot, geometry);
      drawChartOverviewBadge(ctx, geometry);

      rememberPriceBaseTooltips();
      const objectsLayer = hasObjectLayer ? canvasContext("price-objects") : overlayLayer;
      if (!options.overlayImmediate) {
        const objectGeometry = { w, h, visible, bars, pad, priceH, maxP, minP, xStep, x, y, gexOnly };
        drawPriceObjectsLayer(snapshot, { ctx: objectsLayer.ctx, ...objectGeometry }, { clear: hasObjectLayer });
        renderPriceTradingFromGeometry(snapshot, objectGeometry);
        renderPriceInteractionFromGeometry(snapshot, objectGeometry);
        if (hasObjectLayer) rememberPriceObjectsRender(snapshot, objectGeometry, { clear: hasObjectLayer });
      }
      const overlayGeometry = { ...geometry, ctx: overlayLayer.ctx, w, h };
      if (options.overlayImmediate) renderPriceOverlayNow(snapshot, overlayGeometry);
      else schedulePriceOverlayRender(snapshot, overlayGeometry);
      if (window.mcPerfEnd) window.mcPerfEnd(perfToken, `${bars.length}/${snapshot?.bars?.length || 0} bars`, 24);
    }

    function vsaVolumeLabelTextColor(item = null) {
      return isLightTheme() ? "#061a2f" : "#f8fafc";
    }

    function hasVerticalVolumeBarLabel(item = null) {
      const label = vsaVolumeLabelModel(item);
      return Boolean(
        label
        && label.orientation === "vertical"
        && label.placement === "volume_bar"
        && String(label.text || "").trim()
      );
    }

    function drawVsaVolumeBarLabel(ctx, item, index, geometry, rawDisplayVolumes, displayVolumes, compressionCap, maxV, width) {
      const label = vsaVolumeLabelModel(item);
      if (!hasVerticalVolumeBarLabel(item)) return null;
      const { pad, chartH, xStep, x, bodyW } = geometry;
      const volume = Number(displayVolumes[index]);
      if (!Number.isFinite(volume) || !Number.isFinite(maxV) || maxV <= 0) return null;
      const px = x(index);
      const barHeight = Math.max((volume / maxV) * chartH, 1);
      const barTop = pad.top + chartH - barHeight;
      const barWidth = volumeBarWidth(bodyW, xStep, rawDisplayVolumes[index], compressionCap, item);
      const minY = pad.top + 2;
      const maxY = pad.top + chartH - 2;
      const minFontSize = 5;
      const maxFontSize = 8;
      const labelChars = [...String(label.text || "").trim()].length + (String(label.arrow || "").trim() ? 1 : 0);
      const availableHeight = Math.max(maxY - minY, minFontSize);
      const fontSize = clamp(Math.floor(availableHeight / Math.max(labelChars, 1)), minFontSize, maxFontSize);
      const lineH = fontSize;
      const arrowFontSize = clamp(fontSize + 1, minFontSize, maxFontSize + 1);
      const centerY = barTop + barHeight / 2;
      return drawVerticalIndicatorTextLabel(ctx, px, centerY, label, {
        lineH,
        font: `900 ${fontSize}px -apple-system, BlinkMacSystemFont, sans-serif`,
        arrowFont: `900 ${arrowFontSize}px -apple-system, BlinkMacSystemFont, sans-serif`,
        includeScore: false,
        text: vsaVolumeLabelTextColor(item),
        calibrateText: false,
        hitWidth: Math.max(barWidth, 14),
        minX: pad.left + 2,
        maxX: width - pad.right - 2,
        minY,
        maxY,
      });
    }

    function drawVsaVolumeBarLabels(ctx, snapshot, geometry, volumeCfg, volumeData, width, options = {}) {
      if (volumeCfg.visible === false || !volumeCfg.labels) return;
      const {
        vsSeries,
        rawDisplayVolumes,
        displayVolumes,
        compressionCap,
        maxV,
      } = volumeData;
      if (!Array.isArray(vsSeries)) return;
      const registerTooltips = options.registerTooltips === true;
      const labelIndexes = vsSeries
        .map((item, index) => {
          if (!hasVerticalVolumeBarLabel(item)) return -1;
          return index;
        })
        .filter(index => index >= 0)
        .slice(-48);
      const labelSet = new Set(labelIndexes);
      vsSeries.forEach((item, index) => {
        if (!labelSet.has(index)) return;
        if (!hasVerticalVolumeBarLabel(item)) return;
        const hit = drawVsaVolumeBarLabel(ctx, item, index, geometry, rawDisplayVolumes, displayVolumes, compressionCap, maxV, width);
        if (registerTooltips && hit) registerCanvasTooltip("volume", hit.x, hit.y, hit.width, hit.height, volumeStructureTooltip(item));
      });
    }

    function drawVolumeIndicatorOverlay(ctx, snapshot, geometry, volumeCfg, volumeData, width, options = {}) {
      if (geometry?.renderProjection?.mode === "overview") return;
      const { pad, chartH, x } = geometry;
      const { displayAverages, maxV } = volumeData;
      if (
        volumeCfg.visible !== false
        && volumeCfg.avg
        && (Array.isArray(displayAverages) || ArrayBuffer.isView(displayAverages))
        && Number.isFinite(Number(maxV))
        && Number(maxV) > 0
      ) {
        ctx.save();
        ctx.strokeStyle = calibratedCanvasColor(isLightTheme() ? "#334155" : "#e5e7eb", { minRatio: 4.2 });
        ctx.lineWidth = 1.5;
        ctx.setLineDash([4, 5]);
        ctx.beginPath();
        let started = false;
        displayAverages.forEach((value, index) => {
          if (!Number.isFinite(value)) return;
          const px = x(index);
          const py = pad.top + chartH - (value / maxV) * chartH;
          if (!started) {
            ctx.moveTo(px, py);
            started = true;
          } else {
            ctx.lineTo(px, py);
          }
        });
        if (started) ctx.stroke();
        ctx.restore();
      }
      ctx.save();
      drawVsaVolumeBarLabels(ctx, snapshot, geometry, volumeCfg, volumeData, width, { registerTooltips: options.registerTooltips === true });
      ctx.restore();
    }

    function renderVolumeChart(snapshot) {
      const perfToken = window.mcPerfStart ? window.mcPerfStart("renderVolume") : null;
      const { ctx, w, h } = canvasContext("volume-chart");
      resetCanvasTooltips("volume");
      const geometry = volumeRenderGeometry(snapshot, w, h);
      const { bars, pad, chartH, xStep, x } = geometry;
      const volumeCfg = activeVolumeSignalConfig();
      const volumeData = volumeRenderDataFor(snapshot, geometry, volumeCfg);
      const overviewVolumeData = geometry.renderProjection?.mode === "overview"
        ? chartOverviewVolumeData(geometry.renderProjection)
        : null;
      const {
        vsSeries,
        rawDisplayVolumes,
        displayVolumes,
        compressionCap,
      } = volumeData;
      const maxV = overviewVolumeData?.maxV ?? volumeData.maxV;
      ctx.clearRect(0, 0, w, h);
      ctx.fillStyle = css("--chart-bg");
      ctx.fillRect(0, 0, w, h);
      drawSessionBackground(ctx, bars, pad, chartH, xStep, x, {
        opacity: state.settings.sessionVolumeOpacity,
        labels: true,
      });
      drawProviderDataGaps(ctx, bars, pad, chartH, xStep, x, { labels: false });

      ctx.fillStyle = css("--axis");
      ctx.font = `11px ${CANVAS_MONO_FONT}`;
      for (let i = 0; i <= 3; i++) {
        const py = pad.top + i * chartH / 3;
        const volume = maxV - i * maxV / 3;
        if (state.settings.showHorizontalGrid) {
          ctx.strokeStyle = css("--grid-line");
          ctx.beginPath();
          ctx.moveTo(pad.left, py);
          ctx.lineTo(w - pad.right, py);
          ctx.stroke();
        }
        ctx.fillText(Math.round(volume).toLocaleString("en-US"), w - pad.right + 10, py + 4);
      }

      if (overviewVolumeData) {
        drawBatchedOverviewVolumeColumns(ctx, geometry, overviewVolumeData);
      } else {
        drawBatchedVolumeBars(ctx, bars, geometry, volumeCfg, vsSeries, rawDisplayVolumes, displayVolumes, compressionCap, maxV);
      }
      runIndicatorCanvasHooks("volume_overlay", {
        ctx,
        snapshot,
        bars,
        pad,
        chartH,
        xStep,
      });
      drawSessionBoundaries(ctx, bars, pad, chartH, xStep, x, true);
      lastVolumeInteractionFrame = { snapshot, geometry, volumeCfg, volumeData };
      renderVolumeInteractionFromGeometry(snapshot, geometry, volumeCfg, volumeData, { registerTooltips: true });
      if (window.mcPerfEnd) window.mcPerfEnd(perfToken, `${bars.length}/${snapshot?.bars?.length || 0} bars`, 24);
    }

    function renderVolumeInteraction(snapshot = state.snapshot, options = {}) {
      if (!snapshot?.bars?.length) return;
      const interactionCanvas = document.getElementById("volume-interaction");
      if (!interactionCanvas) return;
      if (lastVolumeInteractionFrame?.snapshot !== snapshot) return;
      renderVolumeInteractionFromGeometry(
        snapshot,
        lastVolumeInteractionFrame.geometry,
        lastVolumeInteractionFrame.volumeCfg,
        lastVolumeInteractionFrame.volumeData,
        options,
      );
    }

    function renderVolumeInteractionFromGeometry(snapshot = state.snapshot, geometry = null, volumeCfg = null, volumeData = null, options = {}) {
      if (!snapshot?.bars?.length || !geometry || !volumeCfg || !volumeData) return;
      const interactionCanvas = document.getElementById("volume-interaction");
      if (!interactionCanvas) return;
      const { ctx, w, h } = canvasContext("volume-interaction");
      const drawGeometry = { ...geometry, w, h };
      const { pad, chartH } = drawGeometry;
      ctx.clearRect(0, 0, w, h);
      drawVolumeIndicatorOverlay(ctx, snapshot, drawGeometry, volumeCfg, volumeData, w, { registerTooltips: options.registerTooltips === true });
      drawEconomicCalendarMarkers(ctx, snapshot, drawGeometry, w, { registerTooltips: options.registerTooltips === true });
      drawCrosshairVolume(ctx, pad, chartH, w);
    }

    function scheduleVolumeInteractionRender(snapshot = state.snapshot, options = {}) {
      if (!snapshot?.bars?.length) return;
      if (volumeInteractionRenderFrame) {
        cancelAnimationFrame(volumeInteractionRenderFrame);
        volumeInteractionRenderFrame = 0;
      }
      if (options.immediate) {
        renderVolumeInteraction(snapshot, options);
        return;
      }
      volumeInteractionRenderFrame = requestAnimationFrame(() => {
        volumeInteractionRenderFrame = 0;
        renderVolumeInteraction(snapshot, options);
      });
    }

    function drawCrosshairPrice(ctx, snapshot, visible, pad, priceH, maxP, minP, width) {
      if (!state.crosshair.visible || !["price", "time"].includes(state.crosshair.source)) return;
      const cx = clamp(state.crosshair.x, pad.left, width - pad.right);
      const cy = clamp(state.crosshair.y, pad.top, pad.top + priceH);
      const rawPrice = maxP - ((cy - pad.top) / priceH) * (maxP - minP);
      const crosshairPrice = finiteSeriesNumber(state.crosshair.price);
      const price = crosshairPrice !== null ? crosshairPrice : rawPrice;
      const axisX = width - pad.right;
      const labelX = axisX + 4;
      const labelW = pad.right - 8;
      ctx.save();
      ctx.setLineDash([3, 4]);
      ctx.strokeStyle = css("--crosshair");
      ctx.globalAlpha = 0.72;
      ctx.lineWidth = 1;
      ctx.beginPath();
      if (state.crosshair.source === "price") {
        ctx.moveTo(pad.left, cy);
        ctx.lineTo(axisX, cy);
      }
      ctx.moveTo(cx, pad.top);
      ctx.lineTo(cx, pad.top + priceH);
      ctx.stroke();
      if (state.crosshair.source === "time") {
        ctx.restore();
        return;
      }
      ctx.globalAlpha = 1;
      ctx.setLineDash([]);
      const labelBg = state.crosshair.snapKind ? themeColor("gold", 0.92) : css("--tooltip-bg");
      ctx.fillStyle = labelBg;
      ctx.fillRect(labelX, cy - 10, labelW, 20);
      ctx.fillStyle = readableTextForBg(labelBg, css("--tooltip-text"));
      ctx.font = `11px ${CANVAS_MONO_FONT}`;
      const snapPrefix = state.crosshair.snapKind ? `${state.crosshair.snapKind} ` : "";
      const percentReference = state.priceScale?.percentHover
        ? currentPricePercentScaleReference(snapshot, visible)
        : null;
      const percent = percentReference
        ? pricePercentScaleValue(price, percentReference.price)
        : null;
      if (percent !== null) {
        ctx.textAlign = "right";
        ctx.fillText(`${snapPrefix}${formatPricePercentScaleValue(percent)}`, width - 7, cy + 4);
      } else {
        ctx.fillText(`${snapPrefix}${fmt(price)}`, labelX + 5, cy + 4);
      }
      ctx.restore();
    }

    function drawCrosshairVolume(ctx, pad, chartH, width) {
      if (!state.crosshair.visible) return;
      const cx = clamp(state.crosshair.x, pad.left, width - pad.right);
      ctx.save();
      ctx.setLineDash([3, 4]);
      ctx.strokeStyle = css("--crosshair-muted");
      ctx.globalAlpha = 0.58;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(cx, pad.top);
      ctx.lineTo(cx, pad.top + chartH);
      ctx.stroke();
      ctx.restore();
    }

    function drawCrosshairTimeLabel(ctx, bars, pad, chartH, width) {
      if (!state.crosshair.visible || !bars.length) return;
      const ts = state.crosshair.ts;
      if (!ts) return;
      const label = axisTimeFormatter.format(new Date(ts));
      const cx = clamp(state.crosshair.x, pad.left, width - pad.right);
      ctx.save();
      ctx.font = `11px ${CANVAS_MONO_FONT}`;
      const labelW = Math.max(78, ctx.measureText(label).width + 14);
      const labelH = 20;
      const labelX = clamp(cx - labelW / 2, pad.left, width - pad.right - labelW);
      const labelY = pad.top + chartH + 4;
      ctx.fillStyle = css("--tooltip-bg");
      ctx.fillRect(labelX, labelY, labelW, labelH);
      ctx.fillStyle = css("--tooltip-text");
      ctx.fillText(label, labelX + 7, labelY + 14);
      ctx.restore();
    }

    function currentChartDisplayPrice(snapshot = state.snapshot, fallbackBar = null, quote = null) {
      const liveQuote = quote || (typeof currentChartQuoteDisplay === "function" ? currentChartQuoteDisplay(15000) : null);
      const latest = fallbackBar || snapshot?.bars?.[snapshot.bars.length - 1] || null;
      const metaQuoteMs = Date.parse(snapshot?.meta?.quote_ts || "");
      const metaQuoteFresh = (
        snapshot?.meta?.quote_status === "live"
        && snapshot?.meta?.quote_is_stale === false
        && Number.isFinite(metaQuoteMs)
        && Math.abs(Date.now() - metaQuoteMs) <= 15000
      );
      for (const value of [
        liveQuote?.price,
        metaQuoteFresh ? snapshot?.meta?.price : null,
        latest?.close,
      ]) {
        if (value === null || value === undefined || value === "") continue;
        const price = Number(value);
        if (Number.isFinite(price)) return price;
      }
      return null;
    }

    function pricePercentScaleValue(price, referencePrice) {
      const level = finiteSeriesNumber(price);
      const reference = finiteSeriesNumber(referencePrice);
      if (level === null || reference === null || reference === 0) return null;
      return ((level - reference) / Math.abs(reference)) * 100;
    }

    function formatPricePercentScaleValue(value) {
      const percent = finiteSeriesNumber(value);
      if (percent === null) return "";
      const normalized = Math.abs(percent) < 0.005 ? 0 : percent;
      const magnitude = Math.abs(normalized);
      const decimals = magnitude >= 1000 ? 0 : magnitude >= 100 ? 1 : 2;
      return `${normalized > 0 ? "+" : ""}${normalized.toFixed(decimals)}%`;
    }

    function currentPricePercentScaleReference(snapshot, visible) {
      const bars = Array.isArray(visible?.bars) ? visible.bars : [];
      const allBars = Array.isArray(visible?.allBars) && visible.allBars.length
        ? visible.allBars
        : Array.isArray(snapshot?.bars) ? snapshot.bars : bars;
      if (!bars.length || !allBars.length) return null;
      const latest = allBars[allBars.length - 1];
      const latestLocalIndex = allBars.length - 1 - Number(visible?.start || 0);
      if (latestLocalIndex < 0 || latestLocalIndex >= bars.length) return null;
      const quote = typeof currentChartQuoteDisplay === "function"
        ? currentChartQuoteDisplay(15000)
        : null;
      if (!quote) return null;
      const price = finiteSeriesNumber(currentChartDisplayPrice(snapshot, latest, quote));
      if (price === null || price === 0) return null;
      return {
        price,
        displayOnly: quote.displayOnly === true,
        status: String(quote.status || "").trim().toLowerCase(),
      };
    }

    function pricePercentScaleRows(maxP, minP, referencePrice, pad, priceH) {
      const maximum = finiteSeriesNumber(maxP);
      const minimum = finiteSeriesNumber(minP);
      const reference = finiteSeriesNumber(referencePrice);
      const height = Number(priceH);
      const top = Number(pad?.top) || 0;
      if (
        maximum === null
        || minimum === null
        || reference === null
        || reference === 0
        || !Number.isFinite(height)
        || height <= 0
        || maximum <= minimum
      ) return [];
      const priceToY = price => top + ((maximum - price) / (maximum - minimum)) * height;
      const candidates = [];
      const intervalCount = clamp(Math.floor(height / 22), 10, 28);
      for (let index = 0; index <= intervalCount; index += 1) {
        const price = maximum - (index / intervalCount) * (maximum - minimum);
        candidates.push({
          kind: "tick",
          price,
          percent: pricePercentScaleValue(price, reference),
          y: top + (index / intervalCount) * height,
          priority: 0,
        });
      }
      const referenceY = priceToY(reference);
      if (referenceY >= top && referenceY <= top + height) {
        candidates.push({
          kind: "reference",
          price: reference,
          percent: 0,
          y: referenceY,
          priority: 1,
        });
      }
      const selected = [];
      const minGap = 11;
      candidates
        .filter(item => item.percent !== null)
        .sort((left, right) => right.priority - left.priority || left.y - right.y)
        .forEach(item => {
          if (selected.some(existing => Math.abs(existing.y - item.y) < minGap)) return;
          selected.push(item);
        });
      return selected.sort((left, right) => left.y - right.y);
    }

    function drawHoveredPricePercentScale(ctx, snapshot, visible, pad, priceH, maxP, minP, width) {
      if (!state.priceScale?.percentHover) return;
      const reference = currentPricePercentScaleReference(snapshot, visible);
      if (!reference) return;
      const maximum = finiteSeriesNumber(maxP);
      const minimum = finiteSeriesNumber(minP);
      if (maximum === null || minimum === null || maximum <= minimum) return;
      const canvasWidth = Number(width);
      const plotLeft = Number(pad?.left) || 0;
      const axisWidth = Math.min(Number(pad?.right || 0), Math.max(canvasWidth - plotLeft, 0));
      if (!Number.isFinite(canvasWidth) || axisWidth < 48) return;
      const axisX = canvasWidth - axisWidth;
      const top = Number(pad?.top) || 0;
      const height = Number(priceH);
      const bottom = top + height;
      if (!Number.isFinite(height) || height <= 0) return;
      const referenceY = top + ((maximum - reference.price) / (maximum - minimum)) * height;
      const splitY = clamp(referenceY, top, bottom);
      const rows = pricePercentScaleRows(maxP, minP, reference.price, pad, height);
      const upColor = calibratedCanvasColor(css("--green"), { minRatio: 3.6 });
      const downColor = calibratedCanvasColor(css("--red"), { minRatio: 3.6 });
      const neutralColor = calibratedCanvasColor(css("--axis"), { minRatio: 3.6 });
      ctx.save();
      ctx.fillStyle = css("--chart-bg");
      ctx.fillRect(axisX, top - 6, axisWidth, height + 12);
      if (splitY > top) {
        ctx.fillStyle = rgbaFromCssColor(upColor, isLightTheme() ? 0.08 : 0.10);
        ctx.fillRect(axisX, top, axisWidth, splitY - top);
      }
      if (splitY < bottom) {
        ctx.fillStyle = rgbaFromCssColor(downColor, isLightTheme() ? 0.07 : 0.09);
        ctx.fillRect(axisX, splitY, axisWidth, bottom - splitY);
      }
      ctx.strokeStyle = rgbaFromCssColor(css("--axis"), 0.30);
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(axisX + 0.5, top);
      ctx.lineTo(axisX + 0.5, bottom);
      ctx.stroke();
      if (referenceY >= top && referenceY <= bottom) {
        ctx.strokeStyle = rgbaFromCssColor(neutralColor, 0.58);
        ctx.beginPath();
        ctx.moveTo(axisX, referenceY);
        ctx.lineTo(canvasWidth, referenceY);
        ctx.stroke();
      }
      ctx.textAlign = "right";
      ctx.textBaseline = "middle";
      rows.forEach(row => {
        const direction = row.price > reference.price ? 1 : row.price < reference.price ? -1 : 0;
        ctx.globalAlpha = reference.displayOnly ? 0.72 : 1;
        ctx.fillStyle = direction > 0 ? upColor : direction < 0 ? downColor : neutralColor;
        ctx.font = `${row.kind === "reference" ? 700 : 600} 9px ${CANVAS_MONO_FONT}`;
        ctx.fillText(formatPricePercentScaleValue(row.percent), canvasWidth - 6, row.y);
      });
      ctx.globalAlpha = 1;
      if (reference.displayOnly) {
        const status = reference.status === "delayed_frozen"
          ? "DELAYED"
          : reference.status ? reference.status.toUpperCase().replaceAll("_", " ") : "STALE";
        ctx.fillStyle = calibratedCanvasColor(css("--axis"), { minRatio: 3.6 });
        ctx.font = `700 8px ${CANVAS_MONO_FONT}`;
        ctx.fillText(`${status} REF`, canvasWidth - 6, bottom + 14);
      }
      ctx.restore();
    }

    function drawCurrentPriceMarker(ctx, snapshot, visible, pad, priceH, xStep, x, y, width) {
      const bars = visible?.bars || [];
      if (!bars.length) return;
      const allBars = visible?.allBars || snapshot?.bars || bars;
      const latest = allBars[allBars.length - 1] || bars[bars.length - 1];
      const latestLocalIndex = allBars.length - 1 - Number(visible?.start || 0);
      const latestVisible = latestLocalIndex >= 0 && latestLocalIndex < bars.length;
      if (!latestVisible) return;
      const quote = currentChartQuoteDisplay(15000);
      if (!quote) return;
      const bid = finiteSeriesNumber(quote?.bid);
      const ask = finiteSeriesNumber(quote?.ask);
      const quoteDisplayOnly = quote.displayOnly === true;
      const price = currentChartDisplayPrice(snapshot, latest, quote);
      if (!Number.isFinite(price)) return;
      const priceY = y(price);
      if (!Number.isFinite(priceY)) return;
      const axisX = width - pad.right;
      const lastX = x(latestLocalIndex);
      const lineY = clamp(priceY, pad.top, pad.top + priceH);
      const priceOnScale = priceY >= pad.top && priceY <= pad.top + priceH;
      const labelX = axisX + 4;
      const labelW = pad.right - 8;
      const showBidAsk = Boolean(
        priceOnScale
        && state.settings.showBidAskOnCandle
        && Number.isFinite(bid)
        && Number.isFinite(ask)
        && ask >= bid
      );
      const askY = showBidAsk ? y(ask) : NaN;
      const bidY = showBidAsk ? y(bid) : NaN;
      const spreadMidY = showBidAsk ? (askY + bidY) / 2 : priceY;
      const quoteBodyH = showBidAsk ? Math.max(Math.abs(bidY - askY) + 16, 28) : 0;
      const labelH = showBidAsk ? quoteBodyH + 26 : 34;
      const labelAnchorY = priceOnScale ? (showBidAsk ? spreadMidY : priceY) : lineY;
      const labelY = clamp((showBidAsk ? labelAnchorY - quoteBodyH / 2 : labelAnchorY - labelH / 2), pad.top, pad.top + priceH - labelH);
      const up = Number(snapshot.meta.change || 0) >= 0;
      const color = calibratedCanvasColor(up ? css("--green") : css("--red"), { minRatio: 2.35 });
      const sourceLabel = quote?.priceSource === "last"
        ? "Last"
        : quote?.priceSource === "bid_ask_mid"
          ? "Bid/ask midpoint"
          : "Provider price";
      const quoteDiagnostic = quoteDisplayOnly
        ? `Display only · ${String(quote?.status || "non-live").replaceAll("_", " ")}`
        : "Live";
      ctx.save();
      if (priceOnScale) {
        const lineStartX = Math.min(axisX - 12, lastX + xStep * 0.4);
        ctx.setLineDash([2, 4]);
        ctx.strokeStyle = rgbaFromCssColor(color, 1);
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(lineStartX, priceY);
        ctx.lineTo(axisX, priceY);
        ctx.stroke();
        ctx.setLineDash([]);
      }
      if (showBidAsk) {
        ctx.setLineDash([3, 3]);
        ctx.strokeStyle = rgbaFromCssColor(calibratedCanvasColor(css("--red"), { minRatio: 2.35 }), 0.54);
        ctx.beginPath();
        ctx.moveTo(Math.min(axisX - 12, lastX + xStep * 0.4), askY);
        ctx.lineTo(axisX, askY);
        ctx.stroke();
        ctx.strokeStyle = rgbaFromCssColor(calibratedCanvasColor(css("--green"), { minRatio: 2.35 }), 0.54);
        ctx.beginPath();
        ctx.moveTo(Math.min(axisX - 12, lastX + xStep * 0.4), bidY);
        ctx.lineTo(axisX, bidY);
        ctx.stroke();
        ctx.setLineDash([]);
      }
      ctx.shadowColor = rgbaFromCssColor(color, 0.34);
      ctx.shadowBlur = canvasShadowBlur(5);
      if (showBidAsk) {
        ctx.fillStyle = isLightTheme() ? "rgba(255,255,255,0.96)" : "rgba(15,23,42,0.94)";
        roundedRectPath(ctx, labelX, labelY, labelW, labelH, 4);
        ctx.fill();
        ctx.shadowBlur = 0;
        ctx.strokeStyle = rgbaFromCssColor(color, 0.62);
        ctx.lineWidth = 1;
        roundedRectPath(ctx, labelX, labelY, labelW, labelH, 4);
        ctx.stroke();
      } else {
        ctx.fillStyle = color;
        ctx.fillRect(labelX, labelY, labelW, labelH);
      }
      ctx.shadowBlur = 0;
      ctx.shadowColor = "transparent";
      ctx.globalAlpha = 1;
      ctx.font = `700 10px ${CANVAS_MONO_FONT}`;
      ctx.textAlign = "left";
      if (showBidAsk) {
        const quoteBodyTop = labelY;
        const quoteBodyBottom = labelY + quoteBodyH;
        let askTextY = clamp(askY, quoteBodyTop + 9, quoteBodyBottom - 6);
        let bidTextY = clamp(bidY, quoteBodyTop + 9, quoteBodyBottom - 6);
        if (Math.abs(bidTextY - askTextY) < 13) {
          const mid = (askTextY + bidTextY) / 2;
          askTextY = clamp(mid - 6.5, quoteBodyTop + 9, quoteBodyBottom - 6);
          bidTextY = clamp(mid + 6.5, quoteBodyTop + 9, quoteBodyBottom - 6);
        }
        ctx.fillStyle = calibratedCanvasColor(css("--red"), { minRatio: 3.6 });
        ctx.fillText(`A ${fmt(ask)}`, labelX + 5, askTextY + 3);
        ctx.fillStyle = calibratedCanvasColor(css("--green"), { minRatio: 3.6 });
        ctx.fillText(`B ${fmt(bid)}`, labelX + 5, bidTextY + 3);
        ctx.fillStyle = isLightTheme() ? "#475569" : "#cbd5e1";
        ctx.font = `10px ${CANVAS_MONO_FONT}`;
        ctx.fillText(fmt(price), labelX + 5, labelY + labelH - 14);
        ctx.fillText(candleCountdownText(), labelX + 5, labelY + labelH - 3);
        registerCanvasTooltip(
          "price",
          labelX + labelW / 2,
          labelY + labelH / 2,
          labelW,
          labelH,
          `Provider quote · ${quoteDiagnostic}\nAsk ${fmt(ask)}\nBid ${fmt(bid)}\n${sourceLabel} ${fmt(price)}${Number.isFinite(quote?.last) && quote?.priceSource === "bid_ask_mid" ? `\nLast ${fmt(quote.last)}` : ""}`,
        );
      } else {
        ctx.fillStyle = themeColor("label-light");
        ctx.font = `700 11px ${CANVAS_MONO_FONT}`;
        ctx.fillText(fmt(price), labelX + 5, labelY + 13);
        ctx.font = `11px ${CANVAS_MONO_FONT}`;
        ctx.fillText(candleCountdownText(), labelX + 5, labelY + 28);
        registerCanvasTooltip(
          "price",
          labelX + labelW / 2,
          labelY + labelH / 2,
          labelW,
          labelH,
          `Provider quote · ${quoteDiagnostic}\n${sourceLabel} ${fmt(price)}${Number.isFinite(quote?.last) && quote?.priceSource === "bid_ask_mid" ? `\nLast ${fmt(quote.last)}` : ""}`,
        );
      }
      if (priceOnScale && !quoteDisplayOnly) {
        runIndicatorCanvasHooks("current_price_adornment", {
          ctx,
          snapshot,
          priceY,
          anchorX: Math.min(axisX - 12, lastX + xStep * 0.45),
          axisX,
          pad,
          priceH,
        });
      }
      ctx.restore();
    }

    function snappedCrosshair(canvasId, localX, localY, rect) {
      const source = canvasId === "volume-chart" || canvasId === "volume-interaction" ? "volume" : "price";
      if (!state.snapshot?.bars?.length) {
        return { visible: true, source, x: localX, y: localY, index: null, ts: null, price: null };
      }
      const visible = visibleBars(state.snapshot);
      const { bars, allBars, start } = visible;
      if (!bars.length) {
        return { visible: true, source, x: localX, y: localY, index: null, ts: null, price: null };
      }
      const pad = { left: CHART_LEFT_PAD, right: PRICE_AXIS_WIDTH };
      const visualSpan = chartVisualSpan(bars);
      const xStep = (rect.width - pad.left - pad.right) / (visualSpan + rightGapBars());
      const maxIndex = Math.max(bars.length + rightGapBars() - 1, bars.length - 1);
      const hit = chartVisualHitForLocalX(bars, pad, xStep, localX);
      if (hit.gap) {
        return {
          visible: true,
          source,
          x: localX,
          y: localY,
          index: null,
          ts: null,
          price: null,
          dataGap: hit.gap,
        };
      }
      const index = clamp(
        Number.isSafeInteger(hit.index) ? hit.index : 0,
        0,
        maxIndex,
      );
      const snappedX = chartX(pad, xStep, bars)(index);
      const absoluteIndex = (start || 0) + index;
      const historicalBar = absoluteIndex < (allBars || []).length
        ? allBars[absoluteIndex]
        : null;
      const ts = historicalBar?.ts || providerFutureAxisIndex({
        allBars: allBars || state.snapshot.bars,
        futureAxis: state.snapshot?.future_axis,
        slotStep: intervalMinutesFromState(),
      }).byAbsoluteIndex.get(absoluteIndex)?.ts || null;
      let snappedY = localY;
      let snapPrice = null;
      let snapKind = null;
      if (source === "price") {
        const geo = priceChartGeometry();
        if (geo) {
          const rawY = clamp(localY, geo.pad.top, geo.pad.top + geo.priceH);
          snapPrice = geo.maxP - ((rawY - geo.pad.top) / geo.priceH) * (geo.maxP - geo.minP);
        }
        const bar = index < bars.length ? bars[index] : null;
        if (geo && bar) {
          const candidates = [
            { kind: "H", price: Number(bar.high), y: geo.y(Number(bar.high)) },
            { kind: "L", price: Number(bar.low), y: geo.y(Number(bar.low)) },
          ].filter(item => Number.isFinite(item.price) && Number.isFinite(item.y));
          const nearest = candidates
            .map(item => ({ ...item, distance: Math.abs(localY - item.y) }))
            .sort((a, b) => a.distance - b.distance)[0];
          const snapPx = clamp(xStep * 0.70, 7, 16);
          if (nearest && nearest.distance <= snapPx) {
            snappedY = nearest.y;
            snapPrice = nearest.price;
            snapKind = nearest.kind;
          }
        }
      }
      return {
        visible: true,
        source,
        x: snappedX,
        y: snappedY,
        index,
        ts,
        price: snapPrice,
        snapKind,
      };
    }
