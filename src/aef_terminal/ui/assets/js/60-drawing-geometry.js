    function pointToScreen(point, axis, projection = null) {
      const logicalIndex = drawingLogicalIndexForPoint(point, axis, projection);
      const screenIndex = drawingScreenIndexForLogicalIndex(logicalIndex, axis, projection);
      if (Number.isFinite(screenIndex)) {
        return {
          x: axis.x(screenIndex),
          y: axis.y(point.price),
          price: point.price,
          ts: point.ts || drawingTimestampForLogicalIndex(logicalIndex, axis, projection),
          drawingLogicalIndex: logicalIndex,
        };
      }
      return null;
    }

    function drawingAxisRightGapBars(axis) {
      if (axis && Object.prototype.hasOwnProperty.call(axis, "rightGapBars")) {
        const explicit = Number(axis.rightGapBars);
        return Number.isFinite(explicit) ? Math.max(explicit, 0) : 0;
      }
      return typeof rightGapBars === "function"
        ? Math.max(Number(rightGapBars()) || 0, 0)
        : 0;
    }

    function linePriceAtLogicalIndex(p1, p2, logicalIndex, axis, projection = null) {
      const s1 = drawingLogicalIndexForPoint(p1, axis, projection);
      const s2 = drawingLogicalIndexForPoint(p2, axis, projection);
      const y1 = p1?.price;
      const y2 = p2?.price;
      if (![s1, s2, y1, y2, logicalIndex].every(Number.isFinite)) return null;
      const span = s2 === s1 ? 1 : s2 - s1;
      return Number(y1) + (Number(y2) - Number(y1)) * ((Number(logicalIndex) - s1) / span);
    }

    function durableDrawingPointReason(point) {
      if (!point || typeof point !== "object" || Array.isArray(point)) return "missing_point";
      if (!Number.isFinite(Number(point.price))) return "invalid_price";
      const hasTs = Object.hasOwn(point, "ts");
      const hasAnchorTs = Object.hasOwn(point, "anchorTs");
      const hasBarOffset = Object.hasOwn(point, "barOffset");
      if (hasTs === (hasAnchorTs || hasBarOffset)) return "invalid_anchor";
      if (hasTs) return drawingTimestampIdentity(point.ts) ? "" : "invalid_anchor_timestamp";
      if (!drawingTimestampIdentity(point.anchorTs)) return "invalid_anchor_timestamp";
      return Number.isSafeInteger(point.barOffset) && point.barOffset > 0 ? "" : "invalid_bar_offset";
    }

    function drawingGeometryStatus(object) {
      return String(object?.geometryIntegrity?.status || "");
    }

    function drawingGeometryRenderable(object) {
      return drawingGeometryStatus(object) !== "invalid";
    }

    function channelIntegrityReasons(object) {
      const reasons = [];
      const integrity = object?.geometryIntegrity && typeof object.geometryIntegrity === "object"
        ? object.geometryIntegrity
        : {};
      if (String(integrity.status || "") === "invalid") {
        reasons.push(...(Array.isArray(integrity.reasons) ? integrity.reasons : [integrity.status]));
      }
      const p1 = object?.points?.[0];
      const p2 = object?.points?.[1];
      const hasOffsetPoint = object?.offsetPoint && typeof object.offsetPoint === "object";
      if (!hasOffsetPoint) reasons.push("missing_offset_point");
      for (const point of [p1, p2, object?.offsetPoint].filter(Boolean)) {
        const reason = durableDrawingPointReason(point);
        if (reason) reasons.push(reason);
      }
      return [...new Set(reasons.filter(Boolean))];
    }

    function channelCompromised(object) {
      return channelIntegrityReasons(object).length > 0;
    }

    function drawingObjectIntegrityReasons(object) {
      const reasons = [];
      if (!object || typeof object !== "object") return ["invalid_object"];
      const geometry = object.geometryIntegrity && typeof object.geometryIntegrity === "object"
        ? object.geometryIntegrity
        : {};
      if (String(geometry.status || "") === "invalid") {
        reasons.push(...(Array.isArray(geometry.reasons) ? geometry.reasons : [geometry.status]));
      }
      const type = String(object.type || "");
      if (!DRAWING_POINT_COUNTS[type] && type !== "path") reasons.push("invalid_type");
      if (type === "channel") reasons.push(...channelIntegrityReasons(object));
      const points = Array.isArray(object.points) ? object.points : [];
      const required = type === "path" ? PATH_MIN_POINTS : Number(DRAWING_POINT_COUNTS[type] || 0);
      const requiredPointCount = type === "channel" ? 2 : required;
      if (requiredPointCount > 0 && points.length < requiredPointCount) reasons.push("missing_points");
      const requiredPoints = type === "channel"
        ? [...points.slice(0, 2), object.offsetPoint].filter(Boolean)
        : points.slice(0, required || points.length);
      for (const point of requiredPoints) {
        const reason = durableDrawingPointReason(point);
        if (reason) reasons.push(reason);
      }
      if (type === "text" && !String(object.text || "").trim()) reasons.push("missing_text");
      return [...new Set(reasons.filter(Boolean))];
    }

    function lineEndpointAtLogicalIndex(p1, p2, logicalIndex, axis, offset = 0, projection = null) {
      const price = linePriceAtLogicalIndex(p1, p2, logicalIndex, axis, projection);
      if (!Number.isFinite(price)) return null;
      return drawingPointAtLogicalIndex(logicalIndex, price + offset);
    }

    function visibleLineEndpoints(p1, p2, axis, extendRight, offset = 0, projection = null) {
      if (!axis?.bars?.length) return [null, null];
      const s1 = drawingLogicalIndexForPoint(p1, axis, projection);
      const s2 = drawingLogicalIndexForPoint(p2, axis, projection);
      if (s1 === null || s2 === null) return [null, null];
      let startIndex = s1;
      let endIndex = s2;
      if (!extendRight) {
        const numericOffset = Number(offset);
        const firstPrice = Number(p1?.price) + numericOffset;
        const secondPrice = Number(p2?.price) + numericOffset;
        if (![numericOffset, firstPrice, secondPrice].every(Number.isFinite)) return [null, null];
        return [
          drawingPointAtLogicalIndex(s1, firstPrice),
          drawingPointAtLogicalIndex(s2, secondPrice),
        ];
      }
      if (extendRight) {
        const viewportRange = confirmedDrawingViewportLogicalRange(axis, projection);
        if (!viewportRange) return [null, null];
        startIndex = Math.max(Math.min(s1, s2), viewportRange.start);
        endIndex = Math.max(
          s1,
          s2,
          viewportRange.end + drawingAxisRightGapBars(axis),
        );
      }
      return [
        lineEndpointAtLogicalIndex(p1, p2, startIndex, axis, offset, projection),
        lineEndpointAtLogicalIndex(p1, p2, endIndex, axis, offset, projection),
      ];
    }

    function visibleLineLogicalPoints(p1, p2, axis, extendRight, offset = 0, projection = null) {
      const endpoints = visibleLineEndpoints(p1, p2, axis, extendRight, offset, projection);
      const endpointIndices = endpoints.map(point => point?.drawingLogicalIndex);
      if (!endpointIndices.every(Number.isSafeInteger)) return [];
      const lower = Math.min(...endpointIndices);
      const upper = Math.max(...endpointIndices);
      const confirmedAxis = confirmedDrawingAxis(axis, projection);
      const origin = Number.isSafeInteger(confirmedAxis.projectionOffset)
        ? confirmedAxis.projectionOffset
        : 0;
      const viewport = confirmedDrawingViewportLogicalRange(axis, projection);
      const cacheKey = `${lower}:${upper}:${viewport?.start}:${viewport?.end}`;
      let indices = confirmedAxis.lineLogicalIndicesByRange.get(cacheKey);
      if (!indices) {
        const knots = new Set([lower, upper]);
        for (const localIndex of confirmedAxis.lineBreakpointLocalIndices) {
          const logicalIndex = origin + localIndex;
          if (logicalIndex > lower && logicalIndex < upper) knots.add(logicalIndex);
        }
        // Off-viewport X is extrapolated locally. Pin both visible boundaries so
        // an offscreen gap or anchor cannot bend the visible part of the line.
        for (const logicalIndex of [viewport?.start, viewport?.end]) {
          if (logicalIndex > lower && logicalIndex < upper) knots.add(logicalIndex);
        }
        indices = [...knots].sort((a, b) => a - b);
        confirmedAxis.lineLogicalIndicesByRange.set(cacheKey, indices);
      }
      return indices
        .map(logicalIndex => lineEndpointAtLogicalIndex(
          p1,
          p2,
          logicalIndex,
          axis,
          offset,
          projection,
        ))
        .filter(Boolean);
    }

    function clipLineToRect(a, b, rect) {
      let x1 = Number(a?.x);
      let y1 = Number(a?.y);
      let x2 = Number(b?.x);
      let y2 = Number(b?.y);
      const left = Number(rect?.left);
      const right = Number(rect?.right);
      const top = Number(rect?.top);
      const bottom = Number(rect?.bottom);
      if (![x1, y1, x2, y2, left, right, top, bottom].every(Number.isFinite)) return null;
      const dx = x2 - x1;
      const dy = y2 - y1;
      let t0 = 0;
      let t1 = 1;
      const clip = (p, q) => {
        if (Math.abs(p) < 0.000001) return q >= 0;
        const r = q / p;
        if (p < 0) {
          if (r > t1) return false;
          if (r > t0) t0 = r;
        } else {
          if (r < t0) return false;
          if (r < t1) t1 = r;
        }
        return true;
      };
      if (
        !clip(-dx, x1 - left)
        || !clip(dx, right - x1)
        || !clip(-dy, y1 - top)
        || !clip(dy, bottom - y1)
      ) return null;
      return {
        a: { x: x1 + t0 * dx, y: y1 + t0 * dy },
        b: { x: x1 + t1 * dx, y: y1 + t1 * dy },
      };
    }

    function screenPointInsideRect(point, rect) {
      const x = Number(point?.x);
      const y = Number(point?.y);
      return (
        Number.isFinite(x)
        && Number.isFinite(y)
        && x >= Number(rect?.left)
        && x <= Number(rect?.right)
        && y >= Number(rect?.top)
        && y <= Number(rect?.bottom)
      );
    }

    function screenPolylineIntersectsRect(points, rect) {
      if (!Array.isArray(points) || !points.length || !rect) return false;
      let previous = null;
      for (const point of points) {
        if (!point) {
          previous = null;
          continue;
        }
        if (screenPointInsideRect(point, rect)) return true;
        if (previous && clipLineToRect(previous, point, rect)) return true;
        previous = point;
      }
      return false;
    }

    function drawingClipRect(axis) {
      const pad = axis?.pad || {};
      const width = Number(axis?.width);
      const bars = axis?.bars || [];
      const firstX = typeof axis?.x === "function" && bars.length ? axis.x(0) : Number(pad.left) || 0;
      const lastX = typeof axis?.x === "function" && bars.length ? axis.x(bars.length - 1) : width;
      const xStep = Number(axis?.xStep) || 1;
      const left = Number.isFinite(Number(pad.left)) ? Number(pad.left) : Math.min(firstX, lastX);
      const rightPad = Number.isFinite(Number(pad.right)) ? Number(pad.right) : PRICE_AXIS_WIDTH;
      const right = Number.isFinite(width) ? width - rightPad : Math.max(firstX, lastX);
      const top = Number.isFinite(Number(pad.top)) ? Number(pad.top) : 0;
      const height = Number.isFinite(Number(axis?.priceH)) ? Number(axis.priceH) : Math.max(1, Number(pad.height) || 1000);
      const bottom = top + height;
      const expandX = Math.max(
        80,
        xStep * drawingAxisRightGapBars(axis),
        Math.abs(right - left) * 0.15,
      );
      const expandY = Math.max(80, height * 0.35);
      return { left: left - expandX, right: right + expandX, top: top - expandY, bottom: bottom + expandY };
    }

    function strokePriceLine(ctx, p1, p2, axis, toScreen, offset, extendRight, projection = null) {
      const points = visibleLineLogicalPoints(
        p1,
        p2,
        axis,
        extendRight,
        offset,
        projection,
      ).map(toScreen).filter(Boolean);
      if (points.length < 2) return false;
      const clipRect = drawingClipRect(axis);
      let drawn = false;
      ctx.beginPath();
      for (let index = 1; index < points.length; index += 1) {
        const clipped = clipLineToRect(points[index - 1], points[index], clipRect);
        if (!clipped) continue;
        ctx.moveTo(clipped.a.x, clipped.a.y);
        ctx.lineTo(clipped.b.x, clipped.b.y);
        drawn = true;
      }
      if (!drawn) return false;
      ctx.stroke();
      return true;
    }

    function drawingPriceChangePct(points) {
      if (!Array.isArray(points) || points.length !== 2) return null;
      const firstPrice = points[0]?.price;
      const secondPrice = points[1]?.price;
      if (!Number.isFinite(firstPrice) || !Number.isFinite(secondPrice) || firstPrice <= 0) return null;
      const calculatedPct = ((secondPrice - firstPrice) / firstPrice) * 100;
      return Number.isFinite(calculatedPct) ? calculatedPct : null;
    }

    function drawingPriceChangeLabel(points) {
      const priceChangePct = drawingPriceChangePct(points);
      return Number.isFinite(priceChangePct) ? signed(priceChangePct, "%") : "—";
    }

    function drawingLineMeasurementFacts(object, axis) {
      if (
        object?.type !== "line"
        || object.lineVariant !== "ruler"
        || object.extendRight !== false
        || !Array.isArray(object.points)
        || object.points.length !== 2
      ) return null;
      const integrityStatus = String(object.geometryIntegrity?.status || "");
      if (integrityStatus === "invalid") return null;
      const firstIndex = drawingLogicalIndexForPoint(object.points[0], axis, object.anchorProjection);
      const secondIndex = drawingLogicalIndexForPoint(object.points[1], axis, object.anchorProjection);
      if (!Number.isSafeInteger(firstIndex) || !Number.isSafeInteger(secondIndex)) return null;
      const barCount = Math.abs(secondIndex - firstIndex);
      return { barCount, priceChangePct: drawingPriceChangePct(object.points) };
    }

    function drawingLineMeasurementLabel(facts) {
      const barCount = facts?.barCount;
      if (!Number.isSafeInteger(barCount) || barCount < 0) return null;
      const percent = Number.isFinite(facts.priceChangePct)
        ? signed(facts.priceChangePct, "%")
        : "—";
      return `${barCount} ${barCount === 1 ? "bar" : "bars"} · ${percent}`;
    }

    function drawDrawingSegmentLabel(ctx, axis, segment, text, color, options = {}) {
      if (!ctx || !axis || !Array.isArray(segment) || segment.length < 2 || !text || !color) return null;
      const pad = axis.pad || {};
      const width = Number(axis.width);
      const priceH = Number(axis.priceH);
      const left = Number(pad.left) || 0;
      const rightPad = Number.isFinite(Number(pad.right)) ? Number(pad.right) : PRICE_AXIS_WIDTH;
      const right = Number.isFinite(width) ? width - rightPad : left + 1;
      const top = Number(pad.top) || 0;
      const bottom = top + (Number.isFinite(priceH) ? priceH : 1);
      const clipped = clipLineToRect(segment[0], segment[1], { left, right, top, bottom });
      if (!clipped) return null;
      let centerX = (clipped.a.x + clipped.b.x) * 0.5;
      let centerY = (clipped.a.y + clipped.b.y) * 0.5;
      const anchorX = Number(options.anchorPoint?.x);
      const anchorY = Number(options.anchorPoint?.y);
      if (Number.isFinite(anchorX) && Number.isFinite(anchorY)) {
        const dx = clipped.b.x - clipped.a.x;
        const dy = clipped.b.y - clipped.a.y;
        const lengthSquared = dx * dx + dy * dy;
        if (lengthSquared > 0.000001) {
          const ratio = clamp(
            ((anchorX - clipped.a.x) * dx + (anchorY - clipped.a.y) * dy) / lengthSquared,
            0,
            1,
          );
          centerX = clipped.a.x + dx * ratio;
          centerY = clipped.a.y + dy * ratio;
        }
      }
      if (!Number.isFinite(centerX) || !Number.isFinite(centerY)) return null;
      ctx.save();
      ctx.font = options.font || "800 9px -apple-system, BlinkMacSystemFont, sans-serif";
      ctx.textAlign = "left";
      ctx.textBaseline = "alphabetic";
      const labelW = ctx.measureText(text).width + 10;
      const labelH = 16;
      const labelX = clamp(centerX - labelW * 0.5, left + 3, Math.max(left + 3, right - labelW - 3));
      const labelAbove = centerY - labelH - 6 >= top + 3;
      const labelY = clamp(
        labelAbove ? centerY - labelH - 6 : centerY + 6,
        top + 3,
        Math.max(top + 3, bottom - labelH - 3),
      );
      ctx.fillStyle = options.background || (isLightTheme() ? "rgba(255,255,255,0.92)" : "rgba(16,21,27,0.90)");
      ctx.fillRect(labelX, labelY, labelW, labelH);
      ctx.strokeStyle = color;
      ctx.lineWidth = 1;
      ctx.setLineDash([]);
      ctx.strokeRect(labelX + 0.5, labelY + 0.5, labelW - 1, labelH - 1);
      ctx.fillStyle = color;
      ctx.fillText(text, labelX + 5, labelY + 11);
      ctx.restore();
      return {
        x0: clipped.a.x,
        y0: clipped.a.y,
        x1: clipped.b.x,
        y1: clipped.b.y,
        centerX,
        centerY,
        labelX,
        labelY,
        labelW,
        labelH,
      };
    }

    function getThemeAdjustedColor(color) {
      if (typeof calibratedCanvasColor !== "function") return color;
      return calibratedCanvasColor(color, { minRatio: 2.8 });
    }

    function channelGhostDirection(object, axis) {
      const latest = latestDrawingConfirmedBar(axis, object.anchorProjection);
      const latestIndex = latestDrawingLogicalIndex(axis, object.anchorProjection);
      if (!latest || !Number.isSafeInteger(latestIndex)) return 0;
      const base = linePriceAtLogicalIndex(
        object.points[0],
        object.points[1],
        latestIndex,
        axis,
        object.anchorProjection,
      );
      const currentPrice = Number(latest.close);
      if (!Number.isFinite(base) || !Number.isFinite(currentPrice)) return 0;
      const outer = base + object.offset;
      const top = Math.max(base, outer);
      const bottom = Math.min(base, outer);
      if (currentPrice > top) return 1;
      if (currentPrice < bottom) return -1;
      return currentPrice >= (top + bottom) * 0.5 ? 1 : -1;
    }

    function drawChannelBand(ctx, object, axis, toScreen, col, muted, shift = 0, ghost = false) {
      const p1 = object.points[0];
      const p2 = object.points[1];
      const offset = object.offset || 0;
      const levels = object.showGuides === false ? [0, 1] : CHANNEL_LEVELS;
      const guideColor = isLightTheme() ? rgbaFromCssColor(col, 0.72) : muted;
      ctx.save();
      ctx.globalAlpha = ghost ? (isLightTheme() ? 0.72 : 0.58) : 1;
      for (const level of levels) {
        const isEdge = level === 0 || level === 1;
        const isMid = level === 0.5;
        ctx.strokeStyle = isEdge || !ghost ? col : guideColor;
        ctx.lineWidth = isEdge ? clamp(Number(object.width) || 2, 1, 4) : 1;
        ctx.setLineDash(isEdge ? lineDashForStyle(object.style) : isMid ? [7, 6] : [2, 5]);
        const lineOffset = shift + offset * level;
        const drawn = strokePriceLine(
          ctx,
          p1,
          p2,
          axis,
          toScreen,
          lineOffset,
          object.extendRight !== false,
          object.anchorProjection,
        );
        if (drawn && axis?.drawingDecoratorFrame) {
          runIndicatorDrawingDecorators(axis.drawingDecoratorFrame, "channel_band", {
            ctx,
            object,
            axis,
            toScreen,
            level,
            shift,
            ghost,
            lineOffset,
          });
        }
      }
      ctx.restore();
    }
