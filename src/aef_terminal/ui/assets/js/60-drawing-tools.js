    function drawDrawingObject(ctx, object, toScreen, width, pad, preview = false, axis = null) {
      if (axis) toScreen = point => pointToScreen(point, axis, object.anchorProjection);
      const points = object.points.map(toScreen);
      if ((object.type === "fib" || object.type === "text") && points.some(point => point === null)) return;
      const clipRect = axis ? drawingClipRect(axis) : null;
      if (object.type === "path" && clipRect && !screenPolylineIntersectsRect(points, clipRect)) return;
      const selectedId = axis && Object.prototype.hasOwnProperty.call(axis, "selectedId")
        ? axis.selectedId
        : state.drawing.selectedId;
      const showHandles = axis && Object.prototype.hasOwnProperty.call(axis, "showHandles")
        ? axis.showHandles === true
        : true;
      const selected = !preview && selectedId !== null && object.id === selectedId;
      const objectColor = getThemeAdjustedColor(object.color || css("--drawing"));
      const col = preview ? css("--crosshair") : objectColor;
      const muted = preview ? css("--crosshair-muted") : object.color ? rgbaFromCssColor(objectColor, 0.34) : css("--drawing-muted");
      const ghostMuted = preview ? muted : rgbaFromCssColor(objectColor, 0.72);
      const handleColor = selected ? css("--blue") : col;
      ctx.save();
      ctx.lineWidth = selected ? clamp((Number(object.width) || 2) + 1, 2, 5) : preview ? 1 : clamp(Number(object.width) || 2, 1, 4);
      ctx.strokeStyle = col;
      ctx.fillStyle = col;
      ctx.font = "10px -apple-system, BlinkMacSystemFont, sans-serif";

      if (object.type === "line" && points.length >= 2) {
        ctx.setLineDash(lineDashForStyle(object.style));
        strokePriceLine(
          ctx,
          object.points[0],
          object.points[1],
          axis,
          toScreen,
          0,
          object.extendRight === true,
          object.anchorProjection,
        );
        ctx.setLineDash([]);
        if (object.lineVariant === "ruler") {
          const measurement = drawingLineMeasurementFacts(object, axis);
          const measurementLabel = drawingLineMeasurementLabel(measurement);
          const segment = screenLineEndpoints(
            object.points[0],
            object.points[1],
            axis,
            toScreen,
            false,
            0,
            object.anchorProjection,
          );
          if (segment && measurementLabel) {
            drawDrawingSegmentLabel(ctx, axis, segment, measurementLabel, col, { labelOnly: true });
          }
        }
      } else if (object.type === "zone" && points.length >= 2) {
        const p1 = points[0];
        const p2 = points[1];
        if (!p1 || !p2) {
          ctx.restore();
          return;
        }
        const left = Math.min(p1.x, p2.x);
        const right = object.extendRight === true ? width - pad.right : Math.max(p1.x, p2.x);
        const top = Math.min(p1.y, p2.y);
        const bottom = Math.max(p1.y, p2.y);
        const zoneWidth = Math.max(right - left, 1);
        const zoneHeight = Math.max(bottom - top, 1);
        ctx.setLineDash(lineDashForStyle(object.style));
        ctx.fillStyle = rgbaFromCssColor(objectColor, preview ? 0.08 : 0.12);
        ctx.fillRect(left, top, zoneWidth, zoneHeight);
        ctx.strokeRect(left, top, zoneWidth, zoneHeight);
        ctx.setLineDash([]);
        const label = String(object.label || "").trim();
        if (label) {
          ctx.fillStyle = col;
          ctx.fillText(label, left + 5, top + 12);
        }
      } else if (object.type === "channel" && points.length >= 2) {
        const channelOffset = resolvedChannelOffset(object, axis);
        const channelObject = { ...object, offset: channelOffset || 0 };
        if (!drawingGeometryRenderable(channelObject)) return;
        const ghostCopies = clamp(Number(object.ghostCopies ?? CHANNEL_GHOST_COPIES_DEFAULT), 0, CHANNEL_GHOST_COPIES_MAX);
        const widthPrice = Math.abs(channelObject.offset || 0);
        const direction = channelGhostDirection(channelObject, axis);
        if (widthPrice > 0 && direction !== 0) {
          for (let copy = ghostCopies; copy >= 1; copy -= 1) {
            drawChannelBand(ctx, channelObject, axis, toScreen, col, ghostMuted, direction * widthPrice * copy, true);
          }
        }
        drawChannelBand(ctx, channelObject, axis, toScreen, col, muted, 0, false);
      } else if (object.type === "fib" && points.length >= 2) {
        const left = Math.min(points[0].x, points[1].x);
        const right = object.extendRight === false
          ? Math.max(points[0].x, points[1].x)
          : width - pad.right;
        ctx.strokeStyle = muted;
        ctx.lineWidth = clamp(Number(object.width) || 1, 1, 4);
        ctx.beginPath();
        ctx.moveTo(points[0].x, points[0].y);
        ctx.lineTo(points[1].x, points[1].y);
        ctx.stroke();
        for (const level of FIB_LEVELS) {
          const price = object.points[1].price + (object.points[0].price - object.points[1].price) * level;
          const yy = toScreen({ ...object.points[0], price })?.y;
          if (yy === undefined) continue;
          ctx.setLineDash(level === 0 || level === 1 ? lineDashForStyle(object.style) : [4, 4]);
          ctx.strokeStyle = col;
          ctx.beginPath();
          ctx.moveTo(left, yy);
          ctx.lineTo(right, yy);
          ctx.stroke();
          ctx.setLineDash([]);
          ctx.fillText(`${Math.round(level * 1000) / 10}% ${fmt(price)}`, right - 92, yy - 4);
        }
        const priceChangeLabel = drawingPriceChangeLabel(object.points);
        if (priceChangeLabel !== "—") {
          drawDrawingSegmentLabel(
            ctx,
            axis,
            [points[0], points[1]],
            priceChangeLabel,
            col,
            { labelOnly: true },
          );
        }
      } else if (object.type === "path" && object.points.length >= 2) {
        ctx.setLineDash(lineDashForStyle(object.style));
        ctx.lineWidth = selected ? clamp((Number(object.width) || 2) + 1, 2, 5) : preview ? 1.4 : clamp(Number(object.width) || 2, 1, 4);
        ctx.beginPath();
        let started = false;
        for (const point of points) {
          if (!point) {
            started = false;
            continue;
          }
          if (!started) {
            ctx.moveTo(point.x, point.y);
            started = true;
          } else {
            ctx.lineTo(point.x, point.y);
          }
        }
        ctx.stroke();
        ctx.setLineDash([]);
      } else if (object.type === "text" && points.length >= 1) {
        const text = object.text || "Text";
        const lines = text.split("\n");
        const optionTargetText = Boolean(optionTargetDrawingPayload(object));
        const lineH = optionTargetText ? 11 : 9;
        const metrics = lines.map(l => ctx.measureText(l));
        const maxW = Math.max(44, ...metrics.map(m => m.width)) + 14;
        const boxH = (lines.length * lineH) + 6;
        const x0 = points[0].x + 6;
        const y0 = points[0].y - boxH + 2;

        const outline = isLightTheme() ? "rgba(50,50,50,0.95)" : "rgba(10,15,25,0.95)";
        const strokeTextOutline = !isLightTheme();
        ctx.save();
        if (optionTargetText) {
          ctx.font = "700 10.5px -apple-system, BlinkMacSystemFont, sans-serif";
        }
        if (object.optionTargetLive && optionTargetText) {
          const pulse = 0.5 + 0.5 * Math.sin(Date.now() / 260);
          const dotX = points[0].x;
          const dotY = points[0].y;
          ctx.fillStyle = themeColor("blue", 0.95);
          ctx.beginPath();
          ctx.arc(dotX, dotY, 3.5, 0, Math.PI * 2);
          ctx.fill();
          ctx.strokeStyle = themeColor("blue", 0.22 + pulse * 0.32);
          ctx.lineWidth = 2 + pulse * 3;
          ctx.beginPath();
          ctx.arc(dotX, dotY, 7 + pulse * 5, 0, Math.PI * 2);
          ctx.stroke();
        }
        ctx.strokeStyle = outline;
        ctx.lineWidth = 3;
        ctx.lineJoin = "round";
        ctx.fillStyle = col;
        lines.forEach((line, i) => {
          const ly = y0 + (i + 1) * lineH - 4;
          if (strokeTextOutline && !optionTargetText) ctx.strokeText(line, x0 + 7, ly);
          ctx.fillText(line, x0 + 7, ly);
        });
        ctx.restore();
      } else if (object.type === "ellipse" && points.length >= 2) {
        const p1 = points[0];
        const p2 = points[1];
        const p3 = points[2]; // Третья точка (ширина), может быть undefined в превью
        if (!p1 || !p2) {
          ctx.restore();
          return;
        }

        const cx = (p1.x + p2.x) / 2;
        const cy = (p1.y + p2.y) / 2;
        const rx = Math.hypot(p2.x - p1.x, p2.y - p1.y) / 2;
        const rotation = Math.atan2(p2.y - p1.y, p2.x - p1.x);

        if (points.length === 2 && preview) {
          // Фаза 1: Рисуем только осевую линию
          ctx.beginPath();
          ctx.moveTo(p1.x, p1.y);
          ctx.lineTo(p2.x, p2.y);
          ctx.setLineDash([2, 4]);
          ctx.stroke();
          ctx.setLineDash([]);
        } else if (p3) {
          // Фаза 2: Рисуем эллипс, используя проекцию p3 на нормаль оси
          const ry = Math.abs((p3.x - cx) * -Math.sin(rotation) + (p3.y - cy) * Math.cos(rotation));
          ctx.beginPath();
          ctx.ellipse(cx, cy, rx, Math.max(ry, 0.5), rotation, 0, 2 * Math.PI);
          ctx.stroke();
          ctx.fillStyle = rgbaFromCssColor(col, 0.05);
          ctx.fill();
        }
      }

      if (showHandles) {
        for (const point of points) {
          if (object.type === "path" && !selected && !preview) continue;
          if (!point) continue;
          if (clipRect && !screenPointInsideRect(point, clipRect)) continue;
          ctx.beginPath();
          ctx.arc(point.x, point.y, selected ? 4.6 : 3.5, 0, Math.PI * 2);
          ctx.fillStyle = handleColor;
          ctx.fill();
        }
      }
      if (showHandles && selected && object.type === "channel") {
        const offsetPoint = channelOffsetPoint(object, axis);
        const handle = offsetPoint ? toScreen(offsetPoint) : null;
        if (handle) {
          ctx.fillStyle = handleColor;
          ctx.fillRect(handle.x - 4.5, handle.y - 4.5, 9, 9);
          ctx.strokeStyle = css("--chart-bg");
          ctx.lineWidth = 1;
          ctx.strokeRect(handle.x - 5.5, handle.y - 5.5, 11, 11);
        }
      }
      ctx.restore();
    }

    function drawDrawingObjects(
      ctx,
      objects,
      visible,
      pad,
      priceH,
      xStep,
      x,
      y,
      width,
      options = {},
    ) {
      const bars = visible?.bars || [];
      if (!bars.length || !Array.isArray(objects)) return null;
      const excludeIds = new Set(options.excludeIds || []);
      const axis = {
        bars,
        allBars: visible.allBars || bars,
        start: visible.start || 0,
        xStep,
        x,
        y,
        width,
        pad,
        priceH,
        barsVersion: Number.isFinite(Number(options.barsVersion))
          ? Number(options.barsVersion)
          : 0,
        selectedId: Object.prototype.hasOwnProperty.call(options, "selectedId")
          ? options.selectedId
          : null,
        showHandles: options.showHandles === true,
      };
      for (const field of [
        "snapshot",
        "slotStep",
        "rightGapBars",
        "timeframe",
        "futureAxis",
      ]) {
        if (Object.prototype.hasOwnProperty.call(options, field)) axis[field] = options[field];
      }
      const toScreen = point => pointToScreen(point, axis);
      const drawingDecoratorFrame = options.decorators === true
        ? beginIndicatorDrawingDecoratorFrame({
            ctx,
            axis,
            toScreen,
            interaction: false,
          })
        : null;
      if (drawingDecoratorFrame) axis.drawingDecoratorFrame = drawingDecoratorFrame;
      try {
        for (const object of objects) {
          if (excludeIds.has(object.id)) continue;
          if (options.renderableOnly === true && !drawingGeometryRenderable(object)) continue;
          drawDrawingObject(ctx, object, toScreen, width, pad, false, axis);
        }
        if (typeof options.afterObjects === "function") options.afterObjects(axis);
      } finally {
        if (drawingDecoratorFrame) endIndicatorDrawingDecoratorFrame(drawingDecoratorFrame);
      }
      return axis;
    }

    function drawDrawings(ctx, visible, pad, priceH, xStep, x, y, width, options = {}) {
      const bars = visible?.bars || [];
      if (!bars.length) return;
      if (state.drawing.hidden) return;
      drawDrawingObjects(
        ctx,
        state.drawing.objects,
        visible,
        pad,
        priceH,
        xStep,
        x,
        y,
        width,
        {
          ...options,
          snapshot: state.snapshot,
          slotStep: intervalMinutesFromState(),
          rightGapBars: rightGapBars(),
          timeframe: String(state.timeframe || ""),
          futureAxis: state.snapshot?.future_axis,
          barsVersion: chartRenderVersions.bars,
          selectedId: state.drawing.selectedId,
          showHandles: true,
          decorators: true,
          renderableOnly: false,
          afterObjects: () => {
            if (!options.staticOnly) {
              drawDrawingInteraction(ctx, visible, pad, priceH, xStep, x, y, width);
            }
          },
        },
      );
    }

    function drawDrawingInteraction(ctx, visible, pad, priceH, xStep, x, y, width) {
      const bars = visible?.bars || [];
      if (!bars.length || state.drawing.hidden) return;
      const axis = { bars, allBars: visible.allBars || bars, start: visible.start || 0, xStep, x, y, width, pad, priceH, barsVersion: chartRenderVersions.bars };
      const toScreen = point => pointToScreen(point, axis);
      const draggingId = state.drawing.drag?.id;
      if (draggingId) {
        const draggingObject = state.drawing.objects.find(object => object.id === draggingId);
        if (draggingObject) drawDrawingObject(ctx, draggingObject, toScreen, width, pad, true, axis);
      }
      if (state.drawing.draft) {
        const draft = state.drawing.draft;
        const hoverPoint = state.drawing.hoverPoint;
        if (draft.type === "channel") {
          if (draft.points.length === 1 && hoverPoint) {
            drawDrawingObject(ctx, {
              ...draft,
              type: "line",
              points: [draft.points[0], hoverPoint],
              extendRight: false,
            }, toScreen, width, pad, true, axis);
          } else if (draft.points.length >= 2 && hoverPoint) {
            drawDrawingObject(ctx, {
              ...draft,
              points: draft.points.slice(0, 2),
              offsetPoint: hoverPoint,
              extendRight: true,
              showGuides: true,
              ghostCopies: CHANNEL_GHOST_COPIES_DEFAULT,
            }, toScreen, width, pad, true, axis);
          }
        } else {
          const points = [...draft.points];
          if (hoverPoint) points.push(hoverPoint);
          drawDrawingObject(ctx, { ...draft, points }, toScreen, width, pad, true, axis);
        }
      }
      if (state.drawing.hoverPoint?.snapped) {
        const snap = toScreen(state.drawing.hoverPoint);
        if (snap) {
          ctx.save();
          ctx.strokeStyle = css("--drawing");
          ctx.lineWidth = 1.4;
          ctx.beginPath();
          ctx.arc(snap.x, snap.y, 7, 0, Math.PI * 2);
          ctx.stroke();
          ctx.restore();
        }
      }
    }

    function screenLineEndpoints(p1, p2, axis, toScreen, extendRight, offset = 0, projection = null) {
      const points = screenLinePoints(p1, p2, axis, toScreen, extendRight, offset, projection);
      return points.length >= 2 ? [points[0], points[points.length - 1]] : null;
    }

    function screenLinePoints(p1, p2, axis, toScreen, extendRight, offset = 0, projection = null) {
      return visibleLineLogicalPoints(
        p1, p2, axis, extendRight, offset, projection,
      ).map(toScreen).filter(Boolean);
    }

    function distanceToPolyline(px, py, points) {
      let distance = Infinity;
      for (let index = 1; index < points.length; index += 1) {
        distance = Math.min(distance, distanceToSegment(px, py, points[index - 1], points[index]));
      }
      return distance;
    }

    function channelOffsetPoint(object, axis) {
      if (!object?.points?.[0] || !object?.points?.[1]) return null;
      const anchor = object.offsetPoint;
      if (anchor && Number.isFinite(Number(anchor.price))) return anchor;
      return null;
    }

    function resolvedChannelOffset(object, axis) {
      if (!object?.points?.[0] || !object?.points?.[1]) return 0;
      const anchor = object.offsetPoint;
      if (anchor && Number.isFinite(Number(anchor.price))) {
        const logicalIndex = drawingLogicalIndexForPoint(anchor, axis, object.anchorProjection);
        if (Number.isSafeInteger(logicalIndex)) {
          const base = linePriceAtLogicalIndex(
            object.points[0],
            object.points[1],
            logicalIndex,
            axis,
            object.anchorProjection,
          );
          return Number.isFinite(base) ? Number(anchor.price) - base : 0;
        }
      }
      return 0;
    }

    function drawingHandlePoints(object, axis) {
      if (!object?.points?.length) return [];
      if (object.type === "channel") {
        const offsetPoint = channelOffsetPoint(object, axis);
        return [
          { kind: "point", index: 0, point: object.points[0] },
          { kind: "point", index: 1, point: object.points[1] },
          ...(offsetPoint ? [{ kind: "offset", index: 2, point: offsetPoint }] : []),
        ];
      }
      return object.points.map((point, index) => ({ kind: "point", index, point }));
    }

    function distanceToSegment(px, py, a, b) {
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const lenSq = dx * dx + dy * dy;
      if (lenSq === 0) return Math.hypot(px - a.x, py - a.y);
      const t = clamp(((px - a.x) * dx + (py - a.y) * dy) / lenSq, 0, 1);
      const x = a.x + t * dx;
      const y = a.y + t * dy;
      return Math.hypot(px - x, py - y);
    }

    function hitTestDrawing(localX, localY) {
      if (state.drawing.hidden) return null;
      const geo = priceChartGeometry();
      if (!geo) return null;
      const axis = drawingAxisFromGeo(geo);
      for (let i = state.drawing.objects.length - 1; i >= 0; i -= 1) {
        const object = state.drawing.objects[i];
        const toScreen = point => pointToScreen(point, axis, object.anchorProjection);
        if (object.type === "text") {
          const p = toScreen(object.points[0]);
          if (p) {
            const lines = (object.text || "Text").split("\n");
            const boxH = (lines.length * 9) + 6;
            const maxLineLen = Math.max(...lines.map(l => l.length));
            const boxW = Math.max(44, maxLineLen * 5.5 + 14);
            const x0 = p.x + 6;
            const y0 = p.y - boxH + 2;
            if (localX >= x0 && localX <= x0 + boxW && localY >= y0 && localY <= y0 + boxH) return object.id;
          }
        } else if (object.type === "line") {
          const linePoints = screenLinePoints(
            object.points[0], object.points[1], axis, toScreen,
            object.extendRight === true, 0, object.anchorProjection,
          );
          if (linePoints.length >= 2 && distanceToPolyline(localX, localY, linePoints) <= 9) return object.id;
        } else if (object.type === "zone") {
          const p1 = toScreen(object.points[0]);
          const p2 = toScreen(object.points[1]);
          if (p1 && p2) {
            const left = Math.min(p1.x, p2.x);
            const right = object.extendRight === true ? geo.rect.width - geo.pad.right : Math.max(p1.x, p2.x);
            const top = Math.min(p1.y, p2.y);
            const bottom = Math.max(p1.y, p2.y);
            const nearVertical = Math.abs(localX - left) <= 8 || Math.abs(localX - right) <= 8;
            const nearHorizontal = Math.abs(localY - top) <= 8 || Math.abs(localY - bottom) <= 8;
            if (
              localX >= left - 8
              && localX <= right + 8
              && localY >= top - 8
              && localY <= bottom + 8
              && (nearVertical || nearHorizontal)
            ) return object.id;
          }
        } else if (object.type === "channel") {
          if (!drawingGeometryRenderable(object) || channelCompromised(object)) continue;
          const levels = [0, 1];
          const offset = resolvedChannelOffset(object, axis);
          for (const level of levels) {
            const linePoints = screenLinePoints(
              object.points[0], object.points[1], axis, toScreen,
              object.extendRight !== false, (offset || 0) * level, object.anchorProjection,
            );
            if (linePoints.length >= 2 && distanceToPolyline(localX, localY, linePoints) <= 9) return object.id;
          }
        } else if (object.type === "fib") {
          const p1 = toScreen(object.points[0]);
          const p2 = toScreen(object.points[1]);
          if (p1 && p2 && distanceToSegment(localX, localY, p1, p2) <= 9) return object.id;
          const left = p1 && p2 ? Math.min(p1.x, p2.x) : 0;
          const right = p1 && p2
            ? (object.extendRight === false ? Math.max(p1.x, p2.x) : geo.rect.width - geo.pad.right)
            : -1;
          for (const level of FIB_LEVELS) {
            const price = object.points[1].price + (object.points[0].price - object.points[1].price) * level;
            const yLine = toScreen({ ...object.points[0], price })?.y;
            if (
              yLine !== undefined
              && localX >= left - 6
              && localX <= right + 6
              && Math.abs(localY - yLine) <= 6
            ) return object.id;
          }
        } else if (object.type === "path") {
          const points = object.points.map(toScreen);
          for (let index = 1; index < points.length; index += 1) {
            const a = points[index - 1];
            const b = points[index];
            if (a && b && distanceToSegment(localX, localY, a, b) <= 9) return object.id;
          }
        } else if (object.type === "ellipse" && object.points.length >= 3) {
          const p1 = toScreen(object.points[0]);
          const p2 = toScreen(object.points[1]);
          const p3 = toScreen(object.points[2]);
          if (p1 && p2 && p3) {
            const cx = (p1.x + p2.x) / 2;
            const cy = (p1.y + p2.y) / 2;
            const rx = Math.hypot(p2.x - p1.x, p2.y - p1.y) / 2;
            const rotation = Math.atan2(p2.y - p1.y, p2.x - p1.x);
            const ry = Math.abs((p3.x - cx) * -Math.sin(rotation) + (p3.y - cy) * Math.cos(rotation));

            const dx = localX - cx;
            const dy = localY - cy;
            const tx = dx * Math.cos(rotation) + dy * Math.sin(rotation);
            const ty = dx * -Math.sin(rotation) + dy * Math.cos(rotation);

            if (rx > 0 && ry > 0) {
              if (Math.pow(tx / rx, 2) + Math.pow(ty / ry, 2) <= 1.1) return object.id;
            }
          }
        }
      }
      return null;
    }

    function hitTestDrawingHandle(localX, localY) {
      if (state.drawing.hidden) return null;
      const geo = priceChartGeometry();
      if (!geo) return null;
      const axis = drawingAxisFromGeo(geo);
      for (let i = state.drawing.objects.length - 1; i >= 0; i -= 1) {
        const object = state.drawing.objects[i];
        const toScreen = point => pointToScreen(point, axis, object.anchorProjection);
        for (const handle of drawingHandlePoints(object, axis)) {
          const p = toScreen(handle.point);
          if (p && Math.hypot(localX - p.x, localY - p.y) <= 10) {
            return { id: object.id, ...handle };
          }
        }
      }
      return null;
    }

    function selectedDrawing() {
      return state.drawing.objects.find(object => object.id === state.drawing.selectedId) || null;
    }

    let drawingOverlayFrame = 0;
    let drawingOverlayPending = false;

    function renderDrawingOverlay(options = {}) {
      if (!state.snapshot) return;
      drawingOverlayPending = true;
      if (options.immediate) {
        if (drawingOverlayFrame) {
          cancelAnimationFrame(drawingOverlayFrame);
          drawingOverlayFrame = 0;
        }
        drawingOverlayPending = false;
        renderPriceObjects(state.snapshot, options);
        return;
      }
      if (drawingOverlayFrame) return;
      drawingOverlayFrame = requestAnimationFrame(() => {
        drawingOverlayFrame = 0;
        if (!drawingOverlayPending || !state.snapshot) return;
        drawingOverlayPending = false;
        renderPriceObjects(state.snapshot, options);
      });
    }

    function renderInteractionOverlay(options = {}) {
      if (!state.snapshot) return;
      const draw = () => {
        if (typeof renderPriceInteraction === "function") {
          renderPriceInteraction(state.snapshot);
        } else {
          renderDrawingOverlay();
        }
      };
      if (options.immediate) {
        if (state.drawing.interactionFrame) {
          cancelAnimationFrame(state.drawing.interactionFrame);
          state.drawing.interactionFrame = 0;
        }
        draw();
        return;
      }
      if (state.drawing.interactionFrame) return;
      state.drawing.interactionFrame = requestAnimationFrame(() => {
        state.drawing.interactionFrame = 0;
        draw();
      });
    }

    function renderDrawingBaseWithout(id) {
      if (!state.snapshot) return;
      renderPriceObjects(state.snapshot, { excludeDrawingIds: id ? [id] : [] });
    }
    function updateSelectedDrawing(patch) {
      const object = selectedDrawing();
      if (!object) return;
      const historyEligible = drawingPatchIsHistoryEligible(patch);
      const historyBefore = historyEligible ? captureDrawingHistoryState() : null;
      Object.assign(object, patch);
      saveDrawings(historyEligible
        ? { historyBefore, historyLabel: "Drawing properties" }
        : { recordHistory: false });
      applyDrawingUi();
      requestObjectLayerRedraw("drawing updated", { force: true });
    }

    function deleteSelectedDrawing() {
      if (!state.drawing.selectedId) return;
      const historyBefore = captureDrawingHistoryState();
      const deletedId = state.drawing.selectedId;
      if (typeof markDrawingDeleted === "function") markDrawingDeleted(deletedId);
      state.drawing.objects = state.drawing.objects.filter(object => object.id !== state.drawing.selectedId);
      clearOptionTargetRuntime(deletedId);
      state.drawing.selectedId = null;
      state.drawing.draft = null;
      saveDrawings({
        historyBefore,
        historyLabel: "Delete drawing",
        deletedIds: [deletedId],
      });
      applyDrawingUi();
      requestObjectLayerRedraw("drawing deleted", { force: true });
    }

    function updateDrawingHandle(handle, localX, localY) {
      const object = state.drawing.objects.find(item => item.id === handle.id);
      const point = drawingPointFromLocal(localX, localY);
      if (!object || !point) return;
      if (object.type === "channel" && handle.kind === "offset") {
        object.offsetPoint = point;
      } else if (handle.kind === "point" && object.points[handle.index]) {
        object.points[handle.index] = point;
      }
      delete object.anchorResolution;
      delete object.anchorProjection;
      delete object.geometryIntegrity;
      resolveDrawingAnchorsAgainstSnapshot([object], state.snapshot);
      renderInteractionOverlay();
    }

    function startDrawingHandleDrag(canvas, event, handle) {
      setChartObjectSelection("drawing", handle.id);
      const historyBefore = captureDrawingHistoryState();
      state.drawing.drag = handle;
      state.drawing.localEditUntil = Date.now() + DRAWING_LOCAL_EDIT_GRACE_MS;
      applyDrawingUi();
      renderDrawingBaseWithout(handle.id);
      canvas.setPointerCapture(event.pointerId);
      canvas.classList.add("dragging");
      const rect = canvas.getBoundingClientRect();
      const onMove = move => {
        state.drawing.localEditUntil = Date.now() + DRAWING_LOCAL_EDIT_GRACE_MS;
        updateDrawingHandle(handle, move.clientX - rect.left, move.clientY - rect.top);
      };
      const cleanupDrag = save => {
        canvas.classList.remove("dragging");
        canvas.removeEventListener("pointermove", onMove);
        canvas.removeEventListener("pointerup", onUp);
        canvas.removeEventListener("pointercancel", onCancel);
        canvas.removeEventListener("lostpointercapture", onCancel);
        state.drawing.drag = null;
        state.drawing.localEditUntil = Date.now() + DRAWING_LOCAL_EDIT_GRACE_MS;
        if (save) saveDrawings({ historyBefore, historyLabel: "Move drawing handle" });
        applyDrawingUi();
        requestObjectLayerRedraw("drawing drag committed", { force: true });
      };
      const onUp = () => cleanupDrag(true);
      const onCancel = () => cleanupDrag(true);
      canvas.addEventListener("pointermove", onMove);
      canvas.addEventListener("pointerup", onUp);
      canvas.addEventListener("pointercancel", onCancel);
      canvas.addEventListener("lostpointercapture", onCancel);
    }

    function syncObjectControls() {
      const object = selectedDrawing();
      const textDraft = state.drawing.draft?.type === "text" ? state.drawing.draft : null;
      const activeObject = textDraft || object;
      const properties = document.getElementById("drawing-object-properties");
      const controls = [
        "object-extend-right",
        "object-style",
        "object-width",
        "object-color",
        "object-channel-guides",
        "object-ghost-copies",
      ].map(id => document.getElementById(id)).filter(Boolean);
      if (properties) {
        properties.hidden = !activeObject;
        properties.querySelectorAll("[data-object-types]").forEach(row => {
          const objectTypes = String(row.dataset.objectTypes || "").split(/\s+/).filter(Boolean);
          row.hidden = !activeObject || !objectTypes.includes(activeObject.type);
        });
      }
      const selectedLabel = object
        ? (object.type === "line" && object.lineVariant === "ruler" ? "RULER" : object.type.toUpperCase())
        : (textDraft ? "TEXT DRAFT" : "");
      document.getElementById("object-selected-label").textContent = selectedLabel;
      controls.forEach(control => { control.disabled = !object; });
      const textInput = document.getElementById("object-text-input");
      const textSource = activeObject?.type === "text" ? activeObject : null;
      if (textInput) {
        const contextKey = textDraft
          ? `draft:${textDraft.scopeKey || ""}`
          : object?.type === "text" ? `object:${object.id}` : "";
        if (textSource) {
          const sourceValue = String(textSource.text || "");
          const contextChanged = textInput.dataset.drawingTextContext !== contextKey;
          const sourceChanged = textInput.dataset.drawingTextValue !== sourceValue;
          const hasUnsavedInput = textInput.value !== (textInput.dataset.drawingTextValue || "");
          if (contextChanged || (sourceChanged && !hasUnsavedInput && document.activeElement !== textInput)) {
            textInput.value = sourceValue;
            textInput.dataset.drawingTextContext = contextKey;
            textInput.dataset.drawingTextValue = sourceValue;
            textInput.setCustomValidity("");
          }
        } else if (document.activeElement !== textInput) {
          textInput.value = "";
          textInput.dataset.drawingTextContext = "";
          textInput.dataset.drawingTextValue = "";
          textInput.setCustomValidity("");
        }
      }
      if (!object) {
        renderDrawingManager();
        return;
      }
      const isRuler = object.type === "line" && object.lineVariant === "ruler";
      const isLineLike = object.type === "line" || object.type === "channel";
      const canExtendRight = (isLineLike || object.type === "zone" || object.type === "fib") && !isRuler;
      const isChannel = object.type === "channel";
      document.getElementById("object-extend-right").disabled = !canExtendRight;
      document.getElementById("object-extend-right").checked = canExtendRight && (
        object.type === "fib" ? object.extendRight !== false : Boolean(object.extendRight)
      );
      document.getElementById("object-style").disabled = object.type === "text";
      document.getElementById("object-style").value = object.style || "solid";
      document.getElementById("object-width").disabled = object.type === "text";
      document.getElementById("object-width").value = String(clamp(Number(object.width) || 2, 1, 4));
      document.getElementById("object-color").disabled = false;
      document.getElementById("object-color").value = canvasColorToHex(object.color || css("--drawing"));
      document.getElementById("object-channel-guides").disabled = !isChannel;
      document.getElementById("object-channel-guides").checked = object.showGuides !== false;
      document.getElementById("object-ghost-copies").disabled = !isChannel;
      document.getElementById("object-ghost-copies").value = String(clamp(Number(object.ghostCopies ?? CHANNEL_GHOST_COPIES_DEFAULT), 0, CHANNEL_GHOST_COPIES_MAX));
      renderDrawingManager();
    }

    function drawingStatusText() {
      const selected = selectedDrawing();
      if (selected) {
        const selectedType = selected.type === "line" && selected.lineVariant === "ruler"
          ? "RULER"
          : selected.type.toUpperCase();
        return `${selectedType} selected`;
      }
      const optionTarget = selectedOptionTarget();
      if (optionTarget) return `OPTION ${String(optionTarget.symbol || state.symbol).toUpperCase()} selected`;
      const alert = selectedPriceAlert();
      if (alert) return `ALERT ${fmt(alertDynamicLevel(alert, state.snapshot) ?? alert.price)} selected`;
      if (state.drawing.hidden) return "objects hidden";
      if (state.drawing.tool === "cursor") {
        const mode = normalizeCursorMode(state.drawing.cursorMode) === "informative"
          ? " · INFO"
          : "";
        return `cursor${mode}${state.drawing.magnet ? " · MAG" : ""}`;
      }
      if (state.drawing.tool === "path") {
        const have = state.drawing.draft?.points?.length || 0;
        const snap = state.drawing.magnet ? " · MAG" : "";
        return `PLAN ${have ? `${have} pts · Enter/right/dbl/PLAN finish` : "click first point"}${snap}`;
      }
      const need = DRAWING_POINT_COUNTS[state.drawing.tool] || 1;
      const have = state.drawing.draft?.points?.length || 0;
      const prefix = state.drawing.tool === "line" && normalizeDrawingLineMode(state.drawing.lineMode) === "ruler"
        ? "RULER"
        : state.drawing.tool.toUpperCase();
      const snap = state.drawing.magnet ? " · MAG" : "";
      return `${prefix} ${Math.min(have + 1, need)}/${need}${snap}`;
    }

    function closeDrawingModeMenu(kind) {
      const lineMenu = kind === "line";
      const menu = document.getElementById(lineMenu ? "line-tool-menu" : "cursor-mode-menu");
      const toggle = document.getElementById(lineMenu ? "line-tool-toggle" : "cursor-mode-toggle");
      menu?.classList.add("hidden");
      toggle?.classList.remove("active");
      toggle?.setAttribute("aria-expanded", "false");
    }

    function normalizeDrawingLineMode(mode) {
      return mode === "ruler" ? "ruler" : "line";
    }

    function setDrawingLineMode(mode) {
      state.drawing.lineMode = normalizeDrawingLineMode(mode);
      closeDrawingModeMenu("line");
      setDrawingTool("line");
    }

    function setCursorMode(mode) {
      const normalized = normalizeCursorMode(mode);
      state.drawing.cursorMode = normalized;
      setServerSettingValue("aef:cursorMode", normalized);
      closeDrawingModeMenu("cursor");
      if (normalized !== "informative" && typeof hideCanvasTooltip === "function") {
        hideCanvasTooltip();
      }
      setDrawingTool("cursor");
    }

    function applyDrawingUi() {
      const buttons = document.querySelectorAll("[data-drawing-tool]");
      buttons.forEach(button => {
        button.classList.toggle("active", button.dataset.drawingTool === state.drawing.tool);
      });
      document.getElementById("drawing-magnet")?.classList.toggle("active", state.drawing.magnet);
      const visibilityButton = document.getElementById("drawing-visibility");
      if (visibilityButton) {
        visibilityButton.classList.toggle("active", !state.drawing.hidden);
        visibilityButton.textContent = state.drawing.hidden ? "HID" : "VIS";
      }
      document.getElementById("drawing-delete")?.classList.toggle("active", Boolean(selectedDrawing() || selectedPriceAlert() || selectedOptionTarget()));
      const cursorMode = normalizeCursorMode(state.drawing.cursorMode);
      state.drawing.cursorMode = cursorMode;
      const cursorControl = document.getElementById("cursor-mode-control");
      cursorControl?.classList.toggle("informative", cursorMode === "informative");
      const cursorButton = document.getElementById("drawing-cursor");
      if (cursorButton) {
        cursorButton.title = cursorMode === "informative"
          ? "Cursor / pan · informative candle card"
          : "Cursor / pan";
      }
      document.querySelectorAll("[data-cursor-mode]").forEach(button => {
        const active = button.dataset.cursorMode === cursorMode;
        button.classList.toggle("active", active);
        button.setAttribute("aria-checked", active ? "true" : "false");
      });
      const lineMode = normalizeDrawingLineMode(state.drawing.lineMode);
      state.drawing.lineMode = lineMode;
      const lineControl = document.getElementById("line-tool-control");
      lineControl?.classList.toggle("ruler", lineMode === "ruler");
      const lineButton = document.getElementById("drawing-line");
      if (lineButton) {
        const lineTitle = lineMode === "ruler"
          ? "Ruler · bars and percentage"
          : "Trend line";
        lineButton.title = lineTitle;
        lineButton.setAttribute("aria-label", lineTitle);
      }
      document.querySelectorAll("[data-line-tool-mode]").forEach(button => {
        const active = button.dataset.lineToolMode === lineMode;
        button.classList.toggle("active", active);
        button.setAttribute("aria-checked", active ? "true" : "false");
      });
      document.getElementById("draw-status").textContent = drawingStatusText();
      document.getElementById("price-chart")?.classList.toggle("drawing", state.drawing.tool !== "cursor");
      syncObjectControls();
    }

    function setDrawingTool(tool) {
      if (state.drawing.draft?.type === "text" && state.drawing.managerOpen) closeDrawingManager();
      closeDrawingModeMenu("cursor");
      closeDrawingModeMenu("line");
      state.drawing.tool = tool;
      state.drawing.draft = null;
      state.drawing.hoverPoint = null;
      if (tool !== "cursor") setChartObjectSelection("", null);
      if (tool !== "cursor" && typeof hideCanvasTooltip === "function") hideCanvasTooltip();
      applyDrawingUi();
      renderDrawingOverlay();
    }

    function addDrawingObject(object) {
      const historyBefore = captureDrawingHistoryState();
      const saved = {
        id: createBrowserUuidV4(),
        ...object,
      };
      state.drawing.objects.push(saved);
      setChartObjectSelection("drawing", saved.id);
      saveDrawings({ historyBefore, historyLabel: "Add drawing" });
      state.drawing.draft = null;
      state.drawing.hoverPoint = null;
      state.drawing.tool = "cursor";
      applyDrawingUi();
      requestObjectLayerRedraw("drawing added", { force: true });
      return saved;
    }

    function commitDrawingTextEditor() {
      const input = document.getElementById("object-text-input");
      const text = String(input?.value || "");
      if (!input || !text.trim()) {
        input?.setCustomValidity("Enter label text.");
        input?.reportValidity();
        return false;
      }
      input.setCustomValidity("");
      const draft = state.drawing.draft?.type === "text" ? state.drawing.draft : null;
      if (draft) {
        const point = draft.points?.[0];
        if (!point || draft.scopeKey !== drawingHistoryScopeKey()) {
          closeDrawingManager();
          return false;
        }
        addDrawingObject({ type: "text", text, points: [point] });
        closeDrawingManager();
        return true;
      }
      const object = selectedDrawing();
      if (object?.type !== "text") return false;
      if (String(object.text || "") !== text) {
        input.dataset.drawingTextValue = text;
        updateSelectedDrawing({ text });
      }
      return true;
    }

    function cancelDrawingTextEditor() {
      closeDrawingManager();
    }

    function finishPathDrawing() {
      const draft = state.drawing.draft;
      if (!draft || draft.type !== "path") return false;
      const points = draft.points.slice();
      if (points.length < PATH_MIN_POINTS) {
        state.drawing.draft = null;
        state.drawing.hoverPoint = null;
        applyDrawingUi();
        renderDrawingOverlay();
        return false;
      }
      addDrawingObject({ type: "path", points, style: "solid", width: 2 });
      return true;
    }

    function handleDrawingPointerDown(event) {
      const rect = event.currentTarget.getBoundingClientRect();
      const point = drawingPointFromLocal(event.clientX - rect.left, event.clientY - rect.top);
      if (!point) return;
      if (state.drawing.tool === "text") {
        state.drawing.draft = {
          type: "text",
          text: "Note",
          points: [point],
          scopeKey: drawingHistoryScopeKey(),
        };
        state.drawing.hoverPoint = null;
        openDrawingManager({ focusSelectedType: true });
        renderInteractionOverlay({ immediate: true });
        return;
      }
      if (state.drawing.tool === "path") {
        if (!state.drawing.draft || state.drawing.draft.type !== "path") {
          state.drawing.draft = { type: "path", points: [point], style: "solid", width: 2 };
        } else if (state.drawing.draft.points.length < DRAWING_MAX_ANCHOR_POINTS) {
          state.drawing.draft.points.push(point);
        }
        applyDrawingUi();
        renderInteractionOverlay();
        return;
      }
      const required = DRAWING_POINT_COUNTS[state.drawing.tool];
      if (!required) return;
      if (!state.drawing.draft || state.drawing.draft.type !== state.drawing.tool) {
        state.drawing.draft = { type: state.drawing.tool, points: [point] };
        if (state.drawing.tool === "line" && normalizeDrawingLineMode(state.drawing.lineMode) === "ruler") {
          state.drawing.draft.lineVariant = "ruler";
          state.drawing.draft.extendRight = false;
        }
      } else {
        state.drawing.draft.points.push(point);
      }
      if (state.drawing.draft.points.length >= required) {
        const points = state.drawing.draft.points.slice(0, required);
        if (state.drawing.tool === "channel") {
          addDrawingObject({
            type: "channel",
            points: points.slice(0, 2),
            offsetPoint: points[2],
            extendRight: true,
            showGuides: true,
            ghostCopies: CHANNEL_GHOST_COPIES_DEFAULT,
            style: "solid",
            width: 2,
          });
        } else if (state.drawing.tool === "line") {
          const ruler = state.drawing.draft.lineVariant === "ruler";
          addDrawingObject({
            type: "line",
            ...(ruler ? { lineVariant: "ruler" } : {}),
            points,
            extendRight: !ruler,
            style: "solid",
            width: 2,
          });
        } else if (state.drawing.tool === "zone") {
          addDrawingObject({ type: "zone", points, extendRight: true, style: "solid", width: 2, label: "Price zone" });
        } else if (state.drawing.tool === "fib") {
          addDrawingObject({ type: "fib", points, extendRight: true, style: "solid", width: 1 });
        } else if (state.drawing.tool === "ellipse") {
          addDrawingObject({ type: "ellipse", points, style: "solid", width: 1 });
        } else {
          addDrawingObject({ type: state.drawing.tool, points });
        }
      } else {
        applyDrawingUi();
        renderInteractionOverlay();
      }
    }

    function setupDrawingTools() {
      setupFloatingPanelDrag("drawing-dialog", {
        root: document.getElementById("drawing-dialog"),
        surface: document.querySelector("#drawing-dialog .modal-card"),
        handle: document.querySelector("#drawing-dialog .modal-head"),
      });
      document.querySelectorAll("[data-drawing-tool]").forEach(button => {
        button.addEventListener("click", () => {
          const tool = button.dataset.drawingTool;
          if (tool === "path" && state.drawing.tool === "path" && state.drawing.draft?.type === "path") {
            finishPathDrawing();
            return;
          }
          if (tool === "line") {
            setDrawingLineMode(state.drawing.lineMode);
            return;
          }
          setDrawingTool(tool);
        });
      });
      const cursorModeToggle = document.getElementById("cursor-mode-toggle");
      const cursorModeMenu = document.getElementById("cursor-mode-menu");
      const lineToolToggle = document.getElementById("line-tool-toggle");
      const lineToolMenu = document.getElementById("line-tool-menu");
      cursorModeToggle?.addEventListener("click", event => {
        event.preventDefault();
        event.stopPropagation();
        const open = cursorModeMenu?.classList.contains("hidden");
        if (open) closeDrawingModeMenu("line");
        cursorModeMenu?.classList.toggle("hidden", !open);
        cursorModeToggle.classList.toggle("active", Boolean(open));
        cursorModeToggle.setAttribute("aria-expanded", open ? "true" : "false");
        if (open) {
          cursorModeMenu?.querySelector('[aria-checked="true"]')?.focus();
        }
      });
      cursorModeMenu?.querySelectorAll("[data-cursor-mode]").forEach(button => {
        button.addEventListener("click", event => {
          event.preventDefault();
          event.stopPropagation();
          setCursorMode(button.dataset.cursorMode);
          document.getElementById("drawing-cursor")?.focus();
        });
      });
      cursorModeMenu?.addEventListener("keydown", event => {
        const choices = Array.from(cursorModeMenu.querySelectorAll("[data-cursor-mode]"));
        const index = choices.indexOf(document.activeElement);
        if (event.key === "Escape") {
          event.preventDefault();
          event.stopPropagation();
          closeDrawingModeMenu("cursor");
          cursorModeToggle?.focus();
        } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
          event.preventDefault();
          const delta = event.key === "ArrowDown" ? 1 : -1;
          choices[(index + delta + choices.length) % choices.length]?.focus();
        }
      });
      lineToolToggle?.addEventListener("click", event => {
        event.preventDefault();
        event.stopPropagation();
        const open = lineToolMenu?.classList.contains("hidden");
        if (open) closeDrawingModeMenu("cursor");
        lineToolMenu?.classList.toggle("hidden", !open);
        lineToolToggle.classList.toggle("active", Boolean(open));
        lineToolToggle.setAttribute("aria-expanded", open ? "true" : "false");
        if (open) {
          lineToolMenu?.querySelector('[aria-checked="true"]')?.focus();
        }
      });
      lineToolMenu?.querySelectorAll("[data-line-tool-mode]").forEach(button => {
        button.addEventListener("click", event => {
          event.preventDefault();
          event.stopPropagation();
          setDrawingLineMode(button.dataset.lineToolMode);
          document.getElementById("drawing-line")?.focus();
        });
      });
      lineToolMenu?.addEventListener("keydown", event => {
        const choices = Array.from(lineToolMenu.querySelectorAll("[data-line-tool-mode]"));
        const index = choices.indexOf(document.activeElement);
        if (event.key === "Escape") {
          event.preventDefault();
          event.stopPropagation();
          closeDrawingModeMenu("line");
          lineToolToggle?.focus();
        } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
          event.preventDefault();
          const delta = event.key === "ArrowDown" ? 1 : -1;
          choices[(index + delta + choices.length) % choices.length]?.focus();
        } else if (event.key === "Home" || event.key === "End") {
          event.preventDefault();
          choices[event.key === "Home" ? 0 : choices.length - 1]?.focus();
        }
      });
      document.addEventListener("click", event => {
        if (!event.target?.closest?.("#cursor-mode-control")) closeDrawingModeMenu("cursor");
        if (!event.target?.closest?.("#line-tool-control")) closeDrawingModeMenu("line");
      });
      document.getElementById("drawing-magnet").addEventListener("click", () => {
        state.drawing.magnet = !state.drawing.magnet;
        setServerSettingValue("aef:drawingMagnet", state.drawing.magnet ? "true" : "false");
        applyDrawingUi();
        renderDrawingOverlay();
      });
      document.getElementById("drawing-visibility").addEventListener("click", () => {
        state.drawing.hidden = !state.drawing.hidden;
        state.drawing.hoverPoint = null;
        if (state.drawing.hidden) state.drawing.selectedId = null;
        setServerSettingValue("aef:drawingHidden", state.drawing.hidden ? "true" : "false");
        applyDrawingUi();
        renderDrawingOverlay();
      });
      document.getElementById("drawing-delete").addEventListener("click", deleteSelectedChartItem);
      document.getElementById("drawing-clear").addEventListener("click", openDrawingManager);
      document.getElementById("drawing-dialog-close").addEventListener("click", closeDrawingManager);
      document.getElementById("drawing-dialog").addEventListener("click", event => {
        if (event.target?.id === "drawing-dialog") closeDrawingManager();
      });
      const textEditorForm = document.getElementById("object-text-editor");
      const textEditorInput = document.getElementById("object-text-input");
      textEditorForm?.addEventListener("submit", event => {
        event.preventDefault();
        commitDrawingTextEditor();
      });
      document.getElementById("object-text-cancel")?.addEventListener("click", cancelDrawingTextEditor);
      textEditorInput?.addEventListener("input", () => {
        textEditorInput.setCustomValidity("");
        if (state.drawing.draft?.type !== "text") return;
        state.drawing.draft.text = textEditorInput.value;
        textEditorInput.dataset.drawingTextValue = textEditorInput.value;
        renderInteractionOverlay({ immediate: true });
      });
      textEditorInput?.addEventListener("keydown", event => {
        if (event.key === "Escape") {
          event.preventDefault();
          event.stopPropagation();
          cancelDrawingTextEditor();
          return;
        }
        if (
          event.key !== "Enter"
          || event.shiftKey
          || event.metaKey
          || event.ctrlKey
          || event.altKey
          || event.isComposing
        ) return;
        event.preventDefault();
        if (typeof textEditorForm?.requestSubmit === "function") textEditorForm.requestSubmit();
        else textEditorForm?.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
      });
      document.getElementById("drawing-delete-checked").addEventListener("click", deleteCheckedDrawings);
      document.getElementById("drawing-delete-all").addEventListener("click", deleteAllDrawings);
      document.getElementById("object-extend-right").addEventListener("change", event => {
        const object = selectedDrawing();
        if (object?.type === "line" && object.lineVariant === "ruler") {
          event.target.checked = false;
          return;
        }
        updateSelectedDrawing({ extendRight: event.target.checked });
      });
      document.getElementById("object-style").addEventListener("change", event => {
        updateSelectedDrawing({ style: event.target.value });
      });
      document.getElementById("object-width").addEventListener("change", event => {
        updateSelectedDrawing({ width: Number(event.target.value) });
      });
      document.getElementById("object-color").addEventListener("change", event => {
        updateSelectedDrawing({ color: event.target.value });
      });
      document.getElementById("object-channel-guides").addEventListener("change", event => {
        updateSelectedDrawing({ showGuides: event.target.checked });
      });
      document.getElementById("object-ghost-copies").addEventListener("change", event => {
        updateSelectedDrawing({ ghostCopies: Number(event.target.value) });
      });
      document.addEventListener("keydown", event => {
        const tag = event.target?.tagName;
        const editing = event.target?.isContentEditable || tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA";
        if (event.key === "Escape" && editing && event.target?.closest?.("#drawing-dialog")) {
          event.target.blur?.();
          return;
        }
        if (editing) return;
        if (event.key === "Escape") {
          const cursorMenuOpen = Boolean(cursorModeMenu && !cursorModeMenu.classList.contains("hidden"));
          const lineMenuOpen = Boolean(lineToolMenu && !lineToolMenu.classList.contains("hidden"));
          if (cursorMenuOpen || lineMenuOpen) {
            event.preventDefault();
            closeDrawingModeMenu("cursor");
            closeDrawingModeMenu("line");
            if (lineMenuOpen) lineToolToggle?.focus();
            else cursorModeToggle?.focus();
            return;
          }
        }
        const key = String(event.key || "").toLowerCase();
        const code = String(event.code || "");
        const modified = (event.metaKey || event.ctrlKey) && !event.altKey;
        const zKey = key === "z" || code === "KeyZ";
        const yKey = key === "y" || code === "KeyY";
        const undoShortcut = modified && !event.shiftKey && zKey;
        const redoShortcut = modified && (
          (event.shiftKey && zKey)
          || (event.ctrlKey && !event.metaKey && !event.shiftKey && yKey)
        );
        if (undoShortcut || redoShortcut) {
          if (state.drawing.drag || state.drawing.draft || state.alerts.selectedId || state.optionTargets.selectedId) return;
          event.preventDefault();
          if (redoShortcut) redoDrawingMutation();
          else undoDrawingMutation();
          return;
        }
        if ((event.key === "Delete" || event.key === "Backspace") && (state.drawing.selectedId || state.alerts.selectedId || state.optionTargets.selectedId)) {
          event.preventDefault();
          deleteSelectedChartItem();
        }
        if (event.key === "Enter" && state.drawing.draft?.type === "path") {
          event.preventDefault();
          finishPathDrawing();
        }
        if (event.key === "Escape" && state.contextMenu.open) {
          closeContextMenu();
          return;
        }
        if (event.key === "Escape" && (state.drawing.selectedId || state.alerts.selectedId || state.optionTargets.selectedId || state.drawing.draft)) {
          setChartObjectSelection("", null);
          state.drawing.draft = null;
          state.drawing.hoverPoint = null;
          applyDrawingUi();
          renderDrawingOverlay();
          return;
        }
        if (event.key === "Escape" && state.drawing.managerOpen) closeDrawingManager();
      });
      applyDrawingUi();
    }
