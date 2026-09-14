    const OPTION_TARGET_FUTURE_DRAG_TICK_MS = 80;
    const OPTION_TARGET_FUTURE_DRAG_ARM_PX = 4;
    const CHART_TOUCH_CONTEXT_HOLD_MS = 520;
    const CHART_TOUCH_CONTEXT_MOVE_PX = 10;

    function startOptionTargetDrag(canvas, event, id) {
      if (!id || typeof beginOptionTargetDrag !== "function" || !beginOptionTargetDrag(id)) return;
      canvas.setPointerCapture(event.pointerId);
      canvas.classList.add("dragging");
      const startX = event.clientX;
      const startY = event.clientY;
      const startRightGapBars = rightGapBars();
      const startRightGapManual = Boolean(view.rightGapManual);
      let lastClientX = startX;
      let lastClientY = startY;
      let lastPoint = null;
      let moved = false;
      let futureAdvanceArmed = false;
      let futureSpaceAdvanced = false;
      const applyPoint = point => {
        if (!point) return;
        lastPoint = point;
        updateOptionTargetPointLocal(id, point);
      };
      const updateFromEvent = move => {
        lastClientX = move.clientX;
        lastClientY = move.clientY;
        if (!moved && Math.hypot(move.clientX - startX, move.clientY - startY) < 3) return;
        moved = true;
        if (move.clientX > startX + OPTION_TARGET_FUTURE_DRAG_ARM_PX) {
          futureAdvanceArmed = true;
        }
        const rect = canvas.getBoundingClientRect();
        applyPoint(optionTargetPointFromLocal(move.clientX - rect.left, move.clientY - rect.top));
      };
      const futureAdvanceTimer = setInterval(() => {
        if (!moved || !futureAdvanceArmed) return;
        const rect = canvas.getBoundingClientRect();
        const point = advanceOptionTargetFutureDrag(
          lastClientX - rect.left,
          lastClientY - rect.top,
        );
        if (!point) return;
        futureSpaceAdvanced = true;
        applyPoint(point);
      }, OPTION_TARGET_FUTURE_DRAG_TICK_MS);
      const cleanup = () => {
        clearInterval(futureAdvanceTimer);
        canvas.classList.remove("dragging");
        canvas.removeEventListener("pointermove", onMove);
        canvas.removeEventListener("pointerup", onUp);
        canvas.removeEventListener("pointercancel", onCancel);
        canvas.removeEventListener("lostpointercapture", onCancel);
      };
      const onMove = move => updateFromEvent(move);
      const onUp = move => {
        updateFromEvent(move);
        cleanup();
        if (!moved) {
          state.optionTargets.draggingId = null;
          return;
        }
        if (futureSpaceAdvanced) scheduleViewStatePersist();
        finishOptionTargetDrag(id, lastPoint).catch(error => {
          console.warn("option target drag save failed", error);
          fetchOptionTargets().catch(refreshError => console.warn("option target refresh failed", refreshError));
        });
      };
      const onCancel = () => {
        cleanup();
        if (futureSpaceAdvanced) {
          view.rightGapBars = startRightGapBars;
          view.rightGapManual = startRightGapManual;
          scheduleViewStatePersist();
          refreshView({ panel: false, preserveSmoothOffset: true });
        }
        state.optionTargets.draggingId = null;
        fetchOptionTargets().catch(error => console.warn("option target refresh failed", error));
      };
      canvas.addEventListener("pointermove", onMove);
      canvas.addEventListener("pointerup", onUp);
      canvas.addEventListener("pointercancel", onCancel);
      canvas.addEventListener("lostpointercapture", onCancel);
    }

    // Display-only regional liquidity windows. Provider trading intervals remain
    // the sole authority for bar slots, history verification and gap repair.
    const REGIONAL_DISPLAY_SESSIONS = Object.freeze([
      { key: "asia", label: "ASIA", startMinuteUtc: 0, endMinuteUtc: 7 * 60, colorVar: "--session-asia" },
      { key: "london", label: "LONDON", startMinuteUtc: 7 * 60, endMinuteUtc: 13 * 60 + 30, colorVar: "--session-london" },
      { key: "new-york", label: "NEW YORK", startMinuteUtc: 13 * 60 + 30, endMinuteUtc: 20 * 60, colorVar: "--session-new-york" },
    ]);

    function regionalDisplaySessionForBar(bar) {
      const timestamp = Date.parse(bar?.ts || "");
      if (!Number.isFinite(timestamp)) return null;
      const date = new Date(timestamp);
      const minuteUtc = date.getUTCHours() * 60 + date.getUTCMinutes();
      const session = REGIONAL_DISPLAY_SESSIONS.find(item => (
        minuteUtc >= item.startMinuteUtc && minuteUtc < item.endMinuteUtc
      ));
      if (!session) return null;
      const dayStart = Date.UTC(date.getUTCFullYear(), date.getUTCMonth(), date.getUTCDate());
      const open = dayStart + session.startMinuteUtc * 60000;
      const close = dayStart + session.endMinuteUtc * 60000;
      return {
        ...session,
        key: `regional:${session.key}:${dayStart}`,
        open,
        close,
      };
    }

    function localSessionTime(timestamp) {
      return localTimeFormatter.format(new Date(timestamp));
    }

    function drawSessionBackground(ctx, bars, pad, height, xStep, x, options = {}) {
      if (!bars.length) return;
      ctx.save();
      const sessionAlpha = clamp(Number(options.opacity) || 0, 0, 10) / 100;
      const withLabels = Boolean(options.labels);
      let startIndex = 0;
      let activeSession = regionalDisplaySessionForBar(bars[0]);
      const flush = endIndex => {
        if (!activeSession || endIndex <= startIndex) return;
        const left = x(startIndex) - xStep / 2;
        const right = x(endIndex - 1) + xStep / 2;
        if (sessionAlpha > 0) {
          ctx.fillStyle = rgbaFromCssColor(css(activeSession.colorVar), sessionAlpha);
          ctx.fillRect(left, pad.top, Math.max(right - left, 1), height);
        }
        if (withLabels && sessionAlpha > 0 && right > pad.left && left < ctx.canvas.width) {
          ctx.fillStyle = rgbaFromCssColor(css("--session-label"), clamp(0.10 + sessionAlpha * 3.2, 0.10, 0.38));
          ctx.font = "10px -apple-system, BlinkMacSystemFont, sans-serif";
          ctx.fillText(
            `${activeSession.label} ${localSessionTime(activeSession.open)}-${localSessionTime(activeSession.close)}`,
            left + 6,
            pad.top + 14,
          );
        }
      };
      for (let index = 1; index <= bars.length; index += 1) {
        const nextSession = index < bars.length ? regionalDisplaySessionForBar(bars[index]) : null;
        if (nextSession?.key === activeSession?.key) continue;
        flush(index);
        startIndex = index;
        activeSession = nextSession;
      }
      if (state.settings.showCandleGrid) {
        ctx.strokeStyle = rgbaFromCssColor(css("--grid-line"), 0.28);
        ctx.lineWidth = 0.6;
        bars.forEach((_, index) => {
          const bx = x(index) - xStep / 2;
          ctx.beginPath();
          ctx.moveTo(bx, pad.top);
          ctx.lineTo(bx, pad.top + height);
          ctx.stroke();
        });
      }
      ctx.restore();
    }

    function xForTime(times, timestamp, xStep, x) {
      if (!times.length || timestamp < times[0]) return null;
      for (let index = 1; index < times.length; index += 1) {
        if (timestamp <= times[index]) {
          const span = Math.max(times[index] - times[index - 1], 1);
          const ratio = (timestamp - times[index - 1]) / span;
          return x(index - 1) + ratio * (x(index) - x(index - 1));
        }
      }
      if (timestamp > times[times.length - 1]) {
        const lastSpan = times.length > 1 ? Math.max(times[times.length - 1] - times[times.length - 2], 1) : 60000;
        const ratio = (timestamp - times[times.length - 1]) / lastSpan;
        return x(times.length - 1) + ratio * xStep;
      }
      return x(times.length - 1);
    }

    function drawSessionBoundaries(ctx, bars, pad, height, xStep, x, withLabels) {
      if (bars.length < 2) return;
      ctx.save();
      ctx.setLineDash([1, 4]);
      ctx.lineWidth = 0.8;
      ctx.strokeStyle = css("--crosshair-muted");
      ctx.fillStyle = css("--axis");
      ctx.font = "10px -apple-system, BlinkMacSystemFont, sans-serif";
      let previousSession = regionalDisplaySessionForBar(bars[0]);
      for (let index = 1; index < bars.length; index += 1) {
        const currentSession = regionalDisplaySessionForBar(bars[index]);
        if (currentSession?.key === previousSession?.key) continue;
        const bx = x(index) - xStep / 2;
        if (bx >= pad.left && bx <= ctx.canvas.width - (pad.right || 0)) {
          ctx.beginPath();
          ctx.moveTo(bx, pad.top);
          ctx.lineTo(bx, pad.top + height);
          ctx.stroke();
          if (withLabels && previousSession) {
            ctx.fillText(`${previousSession.label} END`, bx + 4, pad.top + height - 8);
          }
        }
        previousSession = currentSession;
      }
      ctx.restore();
    }

    function drawTimeAxis(ctx, bars, pad, chartH, xStep) {
      const labelCount = clamp(Math.floor(bars.length / 18), 4, 9);
      const x = typeof chartX === "function" ? chartX(pad, xStep, bars) : index => pad.left + index * xStep + xStep * 0.5;
      ctx.save();
      ctx.fillStyle = css("--axis");
      ctx.strokeStyle = css("--grid-line");
      ctx.font = `10px ${CANVAS_MONO_FONT}`;
      for (let tick = 0; tick <= labelCount; tick += 1) {
        const index = Math.min(bars.length - 1, Math.round(tick * (bars.length - 1) / labelCount));
        const xValue = x(index);
        const label = axisTimeFormatter.format(new Date(bars[index].ts));
        if (state.settings.showVerticalGrid) {
          ctx.beginPath();
          ctx.moveTo(xValue, pad.top);
          ctx.lineTo(xValue, pad.top + chartH);
          ctx.stroke();
        }
        ctx.fillText(label, xValue - 22, pad.top + chartH + 16);
      }
      ctx.restore();
    }

    function setupSplitters() {
      const vertical = document.getElementById("vertical-splitter");
      const horizontal = document.getElementById("horizontal-splitter");
      const gexSidebarResizer = document.getElementById("gex-sidebar-resizer");
      const gexSidebarWidthInput = document.getElementById("gex-sidebar-width");
      let gexSidebarResizeFrame = 0;
      const renderGexSidebarResizeFrame = () => {
        gexSidebarResizeFrame = 0;
        applyLayout();
        if (!state.snapshot) return;
        if (typeof renderPriceGexLayers === "function") {
          renderPriceGexLayers(state.snapshot);
        }
        renderPriceOverlay(state.snapshot);
        requestObjectLayerRedraw("gex sidebar resize", {
          force: true,
          immediate: true,
        });
      };
      const scheduleGexSidebarResizeFrame = () => {
        if (gexSidebarResizeFrame) return;
        gexSidebarResizeFrame = requestAnimationFrame(renderGexSidebarResizeFrame);
      };
      const updateGexSidebarRequestedWidth = value => {
        const nextWidth = typeof gexSidebarRequestedWidth === "function"
          ? gexSidebarRequestedWidth(value)
          : Math.round(clamp(Number(value) || 248, 154, 420));
        const changed = nextWidth !== Number(layout.gexSidebarWidth);
        layout.gexSidebarWidth = nextWidth;
        if (!changed) return false;
        scheduleGexSidebarResizeFrame();
        return true;
      };
      const commitGexSidebarRequestedWidth = () => {
        if (gexSidebarResizeFrame) {
          cancelAnimationFrame(gexSidebarResizeFrame);
          renderGexSidebarResizeFrame();
        } else {
          applyLayout();
        }
        setServerSettingValue(
          "aef:gexSidebarWidth",
          String(layout.gexSidebarWidth),
        );
        if (state.snapshot) {
          renderCharts(state.snapshot, {
            force: true,
            overlayImmediate: true,
            reason: "gex sidebar resize committed",
          });
        }
      };
      vertical.addEventListener("pointerdown", event => {
        event.preventDefault();
        vertical.setPointerCapture(event.pointerId);
        vertical.classList.add("dragging");
        const onMove = move => {
          const next = window.innerWidth - move.clientX;
          layout.sideWidth = Math.round(
            clamp(next, 260, Math.floor(window.innerWidth * 0.55)),
          );
          applyLayout();
        };
        const onEnd = () => {
          vertical.classList.remove("dragging");
          vertical.removeEventListener("pointermove", onMove);
          vertical.removeEventListener("pointerup", onEnd);
          vertical.removeEventListener("pointercancel", onEnd);
          vertical.removeEventListener("lostpointercapture", onEnd);
          setServerSettingValue("aef:sideWidth", String(layout.sideWidth));
          if (state.snapshot) renderCharts(state.snapshot);
        };
        vertical.addEventListener("pointermove", onMove);
        vertical.addEventListener("pointerup", onEnd);
        vertical.addEventListener("pointercancel", onEnd);
        vertical.addEventListener("lostpointercapture", onEnd);
      });

      horizontal.addEventListener("pointerdown", event => {
        event.preventDefault();
        horizontal.setPointerCapture(event.pointerId);
        horizontal.classList.add("dragging");
        const chartPanel = document.querySelector(".chart-panel");
        const panelBottom = () => chartPanel.getBoundingClientRect().bottom;
        const onMove = move => {
          const next = panelBottom() - move.clientY;
          layout.volumeHeight = Math.round(
            clamp(next, 80, Math.floor(chartPanel.clientHeight * 0.55)),
          );
          applyLayout();
        };
        const onEnd = () => {
          horizontal.classList.remove("dragging");
          horizontal.removeEventListener("pointermove", onMove);
          horizontal.removeEventListener("pointerup", onEnd);
          horizontal.removeEventListener("pointercancel", onEnd);
          horizontal.removeEventListener("lostpointercapture", onEnd);
          setServerSettingValue("aef:volumeHeight", String(layout.volumeHeight));
          if (state.snapshot) renderCharts(state.snapshot);
        };
        horizontal.addEventListener("pointermove", onMove);
        horizontal.addEventListener("pointerup", onEnd);
        horizontal.addEventListener("pointercancel", onEnd);
        horizontal.addEventListener("lostpointercapture", onEnd);
      });

      if (gexSidebarResizer) {
        gexSidebarResizer.addEventListener("pointerdown", event => {
          event.preventDefault();
          gexSidebarResizer.setPointerCapture(event.pointerId);
          gexSidebarResizer.classList.add("dragging");
          const priceFrame = document.getElementById("price-frame");
          const frameRect = priceFrame?.getBoundingClientRect();
          const initialGeometry = typeof gexProfileGutterGeometry === "function"
            ? gexProfileGutterGeometry(
                { left: CHART_LEFT_PAD, right: PRICE_AXIS_WIDTH },
                Number(frameRect?.width) || 0,
              )
            : null;
          const initialVisibleWidth = Math.max(
            0,
            Number(initialGeometry?.effectiveWidth)
              || Number(layout.gexSidebarWidth)
              || 248,
          );
          const initialClientX = Number(event.clientX) || 0;
          let changed = false;
          const onMove = move => {
            changed = updateGexSidebarRequestedWidth(
              initialVisibleWidth + (Number(move.clientX) - initialClientX),
            )
              || changed;
          };
          const onEnd = () => {
            gexSidebarResizer.classList.remove("dragging");
            gexSidebarResizer.removeEventListener("pointermove", onMove);
            gexSidebarResizer.removeEventListener("pointerup", onEnd);
            gexSidebarResizer.removeEventListener("pointercancel", onEnd);
            gexSidebarResizer.removeEventListener("lostpointercapture", onEnd);
            if (changed) commitGexSidebarRequestedWidth();
          };
          gexSidebarResizer.addEventListener("pointermove", onMove);
          gexSidebarResizer.addEventListener("pointerup", onEnd);
          gexSidebarResizer.addEventListener("pointercancel", onEnd);
          gexSidebarResizer.addEventListener("lostpointercapture", onEnd);
        });
        gexSidebarResizer.addEventListener("keydown", event => {
          let nextWidth = null;
          if (event.key === "ArrowLeft") {
            nextWidth = Number(layout.gexSidebarWidth) - 8;
          } else if (event.key === "ArrowRight") {
            nextWidth = Number(layout.gexSidebarWidth) + 8;
          } else if (event.key === "Home") {
            nextWidth = typeof GEX_SIDEBAR_WIDTH_MIN === "number"
              ? GEX_SIDEBAR_WIDTH_MIN
              : 154;
          } else if (event.key === "End") {
            nextWidth = typeof GEX_SIDEBAR_WIDTH_MAX === "number"
              ? GEX_SIDEBAR_WIDTH_MAX
              : 420;
          }
          if (nextWidth === null) return;
          event.preventDefault();
          updateGexSidebarRequestedWidth(nextWidth);
          commitGexSidebarRequestedWidth();
        });
        gexSidebarResizer.addEventListener("dblclick", event => {
          event.preventDefault();
          updateGexSidebarRequestedWidth(
            typeof GEX_SIDEBAR_WIDTH_DEFAULT === "number"
              ? GEX_SIDEBAR_WIDTH_DEFAULT
              : 248,
          );
          commitGexSidebarRequestedWidth();
        });
      }

      if (gexSidebarWidthInput) {
        gexSidebarWidthInput.addEventListener("input", event => {
          updateGexSidebarRequestedWidth(event.currentTarget.value);
        });
        gexSidebarWidthInput.addEventListener("change", event => {
          updateGexSidebarRequestedWidth(event.currentTarget.value);
          commitGexSidebarRequestedWidth();
        });
      }
    }

    function refreshCrosshairOverlays(options = {}) {
      if (!state.snapshot) return;
      if (typeof renderInteractionOverlay === "function") renderInteractionOverlay(options);
      if (typeof scheduleVolumeInteractionRender === "function") {
        scheduleVolumeInteractionRender(state.snapshot, options);
      }
    }

    function updateAccessibleChartLoadStatus(snapshot = state.snapshot) {
      if (state.crosshair?.visible) return false;
      const status = document.getElementById("chart-accessible-status");
      if (!status) return false;
      if (status.dataset.chartState === "ready") return false;
      const bars = Array.isArray(snapshot?.bars) ? snapshot.bars : [];
      const scope = `${String(state.symbol || "Instrument")} ${String(state.timeframe || "")}`.trim();
      status.dataset.chartState = "ready";
      status.textContent = bars.length
        ? `${scope} chart loaded. ${bars.length} candles available.`
        : `${scope} chart has no loaded candles.`;
      return true;
    }

    function accessibleChartBarSummary(bar) {
      if (!bar || typeof bar !== "object") return "No loaded candle is available.";
      const priceText = value => (
        Number.isFinite(Number(value)) ? fmt(Number(value)) : "unavailable"
      );
      const volume = Number(bar.volume);
      const volumeText = Number.isFinite(volume)
        ? Math.round(volume).toLocaleString("en-US")
        : "unavailable";
      const lifecycle = barIsConfirmedClosed(bar)
        ? "confirmed"
        : barIsLivePreview(bar)
          ? String(bar.state || "provisional").replace(/_/g, " ")
          : "provisional";
      const timestamp = Number.isFinite(Date.parse(bar.ts || ""))
        ? axisTimeFormatter.format(new Date(bar.ts))
        : "time unavailable";
      return [
        `${String(state.symbol || "Instrument")} ${String(state.timeframe || "")} candle ${timestamp}.`,
        `Open ${priceText(bar.open)}, high ${priceText(bar.high)}, low ${priceText(bar.low)}, close ${priceText(bar.close)}.`,
        `Volume ${volumeText}. Status ${lifecycle}.`,
      ].join(" ");
    }

    function moveAccessibleChartCursor(canvas, requestedIndex) {
      const status = document.getElementById("chart-accessible-status");
      const visible = state.snapshot ? visibleBars(state.snapshot) : null;
      const bars = visible?.bars || [];
      if (!bars.length) {
        if (status) status.textContent = "No loaded market candles are available.";
        return false;
      }
      const index = clamp(Math.trunc(Number(requestedIndex) || 0), 0, bars.length - 1);
      const bar = bars[index];
      const rect = canvas.getBoundingClientRect();
      const pad = { left: CHART_LEFT_PAD, right: PRICE_AXIS_WIDTH };
      const visualSpan = chartVisualSpan(bars);
      const xStep = (rect.width - pad.left - pad.right) / (visualSpan + rightGapBars());
      const source = canvas.id === "volume-chart" ? "volume" : "price";
      const price = source === "price" && Number.isFinite(Number(bar.close))
        ? Number(bar.close)
        : null;
      const geometry = source === "price" ? priceChartGeometry() : null;
      state.crosshair = {
        visible: true,
        source,
        x: chartX(pad, xStep, bars)(index),
        y: geometry && price !== null ? geometry.y(price) : rect.height / 2,
        index,
        ts: bar.ts || null,
        price,
        snapKind: price !== null ? "C" : null,
      };
      if (status) status.textContent = accessibleChartBarSummary(bar);
      refreshCrosshairOverlays({ immediate: true });
      if (typeof publishLinkedCrosshair === "function") publishLinkedCrosshair(state.crosshair);
      return true;
    }

    function setupAccessibleChartKeyboard() {
      for (const canvas of [
        document.getElementById("price-chart"),
        document.getElementById("volume-chart"),
      ].filter(Boolean)) {
        const source = canvas.id === "volume-chart" ? "volume" : "price";
        canvas.addEventListener("focus", () => {
          const bars = state.snapshot ? visibleBars(state.snapshot).bars : [];
          const currentIndex = state.crosshair.visible && state.crosshair.source === source
            ? Number(state.crosshair.index)
            : bars.length - 1;
          moveAccessibleChartCursor(canvas, Number.isInteger(currentIndex) ? currentIndex : bars.length - 1);
        });
        canvas.addEventListener("keydown", event => {
          if (event.altKey || event.ctrlKey || event.metaKey) return;
          const bars = state.snapshot ? visibleBars(state.snapshot).bars : [];
          const currentIndex = state.crosshair.visible && state.crosshair.source === source
            ? clamp(Number(state.crosshair.index) || 0, 0, Math.max(bars.length - 1, 0))
            : Math.max(bars.length - 1, 0);
          const nextIndex = {
            ArrowLeft: currentIndex - 1,
            ArrowRight: currentIndex + 1,
            Home: 0,
            End: bars.length - 1,
          }[event.key];
          if (event.key === "Escape") {
            event.preventDefault();
            event.stopPropagation();
            state.crosshair.visible = false;
            const status = document.getElementById("chart-accessible-status");
            if (status) status.textContent = "Chart cursor cleared.";
            refreshCrosshairOverlays({ immediate: true });
            if (typeof publishLinkedCrosshair === "function") {
              publishLinkedCrosshair(state.crosshair, { visible: false, immediate: true });
            }
            return;
          }
          if (!Number.isFinite(nextIndex) || !bars.length) return;
          event.preventDefault();
          event.stopPropagation();
          moveAccessibleChartCursor(canvas, nextIndex);
        });
      }
    }

    function setupChartInteractions() {
      setupChartNavigationKeyboard();
      setupAccessibleChartKeyboard();
      const volumeInteractionCanvas = document.getElementById("volume-interaction") || document.getElementById("volume-chart");
      for (const canvas of [document.getElementById("price-chart"), volumeInteractionCanvas].filter(Boolean)) {
        canvas.addEventListener("mousemove", event => {
          const pointerCommitPerf = window.mcPerfStart ? window.mcPerfStart("crosshairPointerToCommit") : null;
          const rect = canvas.getBoundingClientRect();
          const localX = event.clientX - rect.left;
          const localY = event.clientY - rect.top;
          if (canvas.id === "price-chart") {
            const overPriceAxis = localX >= rect.width - PRICE_AXIS_WIDTH;
            canvas.classList.toggle("price-scale-hover", overPriceAxis);
            if (!state.priceScale) state.priceScale = { percentHover: false };
            const hoverChanged = state.priceScale.percentHover !== overPriceAxis;
            state.priceScale.percentHover = overPriceAxis;
            if (hoverChanged && typeof requestObjectLayerRedraw === "function") {
              requestObjectLayerRedraw("price percent scale hover", { immediate: true, force: true });
            }
          }
          if (typeof applyFrozenContextCrosshair === "function" && applyFrozenContextCrosshair()) {
            refreshCrosshairOverlays();
            return;
          }
          if (canvas.id === "price-chart") {
            const overPriceAxis = localX >= rect.width - PRICE_AXIS_WIDTH;
            state.crosshair = snappedCrosshair(canvas.id, localX, localY, rect);
            if (state.drawing.tool !== "cursor" && !overPriceAxis) {
              state.drawing.hoverPoint = drawingPointFromLocal(localX, localY);
            }
          } else {
            state.crosshair = snappedCrosshair(canvas.id, localX, localY, rect);
          }
          if (state.snapshot) {
            refreshCrosshairOverlays({ immediate: true });
            if (typeof publishLinkedCrosshair === "function") publishLinkedCrosshair(state.crosshair);
            if (window.mcPerfEnd) {
              window.mcPerfEnd(pointerCommitPerf, state.crosshair.source || "none", 8);
            }
            updateCanvasTooltip(canvas.id, localX, localY, event.clientX, event.clientY);
          }
        });
        canvas.addEventListener("mouseleave", () => {
          const priceScaleWasVisible = canvas.id === "price-chart" && state.priceScale?.percentHover;
          if (canvas.id === "price-chart") {
            canvas.classList.remove("price-scale-hover");
            if (state.priceScale) state.priceScale.percentHover = false;
            if (priceScaleWasVisible && typeof requestObjectLayerRedraw === "function") {
              requestObjectLayerRedraw("price percent scale leave", { immediate: true, force: true });
            }
          }
          if (typeof contextMenuCrosshairLocked === "function" && contextMenuCrosshairLocked()) {
            if (priceScaleWasVisible) refreshCrosshairOverlays({ immediate: true });
            return;
          }
          state.crosshair.visible = false;
          if (typeof publishLinkedCrosshair === "function") {
            publishLinkedCrosshair(state.crosshair, { visible: false, immediate: true });
          }
          if (canvas.id === "price-chart") state.drawing.hoverPoint = null;
          hideCanvasTooltip();
          refreshCrosshairOverlays({ immediate: true });
        });
        canvas.addEventListener("wheel", event => {
          event.preventDefault();
          closeContextMenu();
          if (!state.snapshot) return;
          const delta = Math.abs(Number(event.deltaX)) > Math.abs(Number(event.deltaY))
            ? Number(event.deltaX)
            : Number(event.deltaY);
          if (!delta) return;
          if (event.altKey) {
            zoomPrice(wheelFactor(-delta, PRICE_ZOOM_SENSITIVITY));
          } else {
            const rect = canvas.getBoundingClientRect();
            const plotWidth = Math.max(rect.width - CHART_LEFT_PAD - PRICE_AXIS_WIDTH, 1);
            const anchorFraction = clamp((event.clientX - rect.left - CHART_LEFT_PAD) / plotWidth, 0, 1);
            zoomTime(wheelFactor(delta, TIME_ZOOM_SENSITIVITY), { anchorFraction });
          }
        }, { passive: false });

        const handleChartContextRequest = event => {
          cancelTouchContextHold();
          if (canvas.id === "price-chart" && state.paperTrading?.armed) {
            event.preventDefault();
            closeContextMenu();
            clearPaperTradeArm();
            return;
          }
          if (canvas.id === "price-chart" && state.drawing.draft?.type === "path") {
            event.preventDefault();
            finishPathDrawing();
            return;
          }
          openChartContextMenu(event, canvas);
        };

        const touchNavigation = {
          points: new Map(),
          mode: "",
          primaryId: null,
          followLatestAtStart: true,
          startX: 0,
          startY: 0,
          startPriceZoom: 1,
          startPriceShift: 0,
          dragStep: 1,
          pinch: null,
          lastViewportKey: "",
          contextHold: null,
          contextHoldTimer: 0,
        };

        const cancelTouchContextHold = () => {
          if (touchNavigation.contextHoldTimer) clearTimeout(touchNavigation.contextHoldTimer);
          touchNavigation.contextHoldTimer = 0;
          touchNavigation.contextHold = null;
        };

        const armTouchContextHold = event => {
          cancelTouchContextHold();
          if (canvas.id !== "price-chart") return;
          const hold = {
            pointerId: event.pointerId,
            clientX: event.clientX,
            clientY: event.clientY,
          };
          touchNavigation.contextHold = hold;
          touchNavigation.contextHoldTimer = setTimeout(() => {
            const point = touchNavigation.points.get(hold.pointerId);
            if (
              touchNavigation.contextHold !== hold
              || !point
              || touchNavigation.points.size !== 1
              || touchNavigation.mode !== "pan"
            ) {
              cancelTouchContextHold();
              return;
            }
            touchNavigation.contextHoldTimer = 0;
            touchNavigation.contextHold = null;
            touchNavigation.points.clear();
            try {
              if (canvas.hasPointerCapture?.(hold.pointerId)) {
                canvas.releasePointerCapture(hold.pointerId);
              }
            } catch (_error) {
              // Pointer capture may already have been released by the browser.
            }
            finishTouchNavigation();
            handleChartContextRequest({
              clientX: hold.clientX,
              clientY: hold.clientY,
              pointerType: "touch",
              preventDefault() {},
            });
          }, CHART_TOUCH_CONTEXT_HOLD_MS);
        };

        const beginSingleTouchNavigation = (point, rect, preserveSequence = false) => {
          touchNavigation.mode = point.priceScaleMode ? "price" : "pan";
          touchNavigation.primaryId = point.pointerId;
          if (!preserveSequence) touchNavigation.followLatestAtStart = view.followLatest;
          touchNavigation.startX = point.clientX;
          touchNavigation.startY = point.clientY;
          touchNavigation.startPriceZoom = view.priceZoom;
          touchNavigation.startPriceShift = view.priceShift;
          view.dragStartX = point.clientX;
          view.dragStartY = point.clientY;
          view.dragStartOffset = view.offset;
          view.dragStartPriceZoom = view.priceZoom;
          view.dragStartPriceShift = view.priceShift;
          view.dragStartRightGapBars = rightGapBars();
          view.dragStartHistoryPullOffsetBars = Number(view.historyPullOffsetBars) || 0;
          view.dragCurrentDeltaBars = 0;
          const visible = visibleBars(state.snapshot);
          touchNavigation.dragStep = Math.max(
            (rect.width - 118) / Math.max(chartVisualSpan(visible.bars) + rightGapBars(), 1),
            1,
          );
          touchNavigation.pinch = null;
          touchNavigation.lastViewportKey = "";
          view.dragActive = !point.priceScaleMode;
          canvas.classList.toggle("dragging", !point.priceScaleMode);
          canvas.classList.toggle("price-scale-dragging", point.priceScaleMode);
        };

        const beginTouchPinch = rect => {
          const points = Array.from(touchNavigation.points.values()).slice(0, 2);
          if (points.length < 2 || points.some(point => point.priceScaleMode)) return false;
          const distance = Math.max(Math.abs(points[1].clientX - points[0].clientX), 24);
          const midpointX = (points[0].clientX + points[1].clientX) / 2 - rect.left;
          const plotWidth = Math.max(rect.width - CHART_LEFT_PAD - PRICE_AXIS_WIDTH, 1);
          touchNavigation.mode = "pinch";
          touchNavigation.primaryId = null;
          touchNavigation.pinch = {
            startBarsVisible: view.barsVisible,
            startVirtualOffset: timeViewportVirtualOffset(),
            startRightGapBars: rightGapBars(),
            startDistance: distance,
            startMidpointFraction: clamp((midpointX - CHART_LEFT_PAD) / plotWidth, 0, 1),
          };
          touchNavigation.lastViewportKey = "";
          view.dragActive = true;
          view.smoothOffsetBars = 0;
          canvas.classList.add("dragging");
          canvas.classList.remove("price-scale-dragging");
          return true;
        };

        const beginTouchNavigation = (event, rect, priceScaleMode) => {
          if (touchNavigation.points.size >= 2) return;
          if (touchNavigation.points.size > 0) cancelTouchContextHold();
          const point = {
            pointerId: event.pointerId,
            clientX: event.clientX,
            clientY: event.clientY,
            priceScaleMode,
          };
          touchNavigation.points.set(event.pointerId, point);
          canvas.setPointerCapture(event.pointerId);
          if (touchNavigation.points.size === 1) {
            beginSingleTouchNavigation(point, rect);
            if (!priceScaleMode) armTouchContextHold(event);
          } else {
            beginTouchPinch(rect);
          }
          if (typeof beginChartInteractionQuality === "function") beginChartInteractionQuality(600);
        };

        const finishTouchNavigation = () => {
          cancelTouchContextHold();
          canvas.classList.remove("dragging", "price-scale-dragging");
          view.dragActive = false;
          view.dragCurrentDeltaBars = 0;
          view.dragStartHistoryPullOffsetBars = 0;
          view.historyPullOffsetBars = 0;
          view.smoothOffsetBars = 0;
          persistViewState();
          if (viewPanelRenderTimer) {
            clearTimeout(viewPanelRenderTimer);
            viewPanelRenderTimer = 0;
          }
          if (state.snapshot) renderPanel(state.snapshot);
          if (state.snapshot) renderCharts(state.snapshot, { overlayImmediate: true });
          if (typeof endChartInteractionQuality === "function") endChartInteractionQuality();
          if (!touchNavigation.followLatestAtStart && view.followLatest) {
            setFollowLatest(true, { refreshTail: true });
          }
          touchNavigation.mode = "";
          touchNavigation.primaryId = null;
          touchNavigation.pinch = null;
          touchNavigation.lastViewportKey = "";
        };

        const moveTouchNavigation = event => {
          const point = touchNavigation.points.get(event.pointerId);
          if (!point || !state.snapshot) return;
          event.preventDefault();
          const hold = touchNavigation.contextHold;
          if (
            hold?.pointerId === event.pointerId
            && Math.hypot(event.clientX - hold.clientX, event.clientY - hold.clientY)
              > CHART_TOUCH_CONTEXT_MOVE_PX
          ) {
            cancelTouchContextHold();
          }
          point.clientX = event.clientX;
          point.clientY = event.clientY;
          if (typeof beginChartInteractionQuality === "function") beginChartInteractionQuality(220);
          if (touchNavigation.mode === "pinch" && touchNavigation.pinch) {
            const rect = canvas.getBoundingClientRect();
            const points = Array.from(touchNavigation.points.values()).slice(0, 2);
            if (points.length < 2) return;
            const currentDistance = Math.max(Math.abs(points[1].clientX - points[0].clientX), 24);
            const midpointX = (points[0].clientX + points[1].clientX) / 2 - rect.left;
            const plotWidth = Math.max(rect.width - CHART_LEFT_PAD - PRICE_AXIS_WIDTH, 1);
            const result = pinchTimeViewport({
              ...touchNavigation.pinch,
              currentDistance,
              midpointFraction: clamp((midpointX - CHART_LEFT_PAD) / plotWidth, 0, 1),
            });
            const previousBarsVisible = view.barsVisible;
            view.barsVisible = result.barsVisible;
            applyTimeViewportVirtualOffset(result.virtualOffset);
            view.followLatest = result.virtualOffset <= 0;
            updateChartNavigationControls();
            const key = `${view.offset}|${view.rightGapBars}|${view.barsVisible}`;
            if (key === touchNavigation.lastViewportKey) return;
            touchNavigation.lastViewportKey = key;
            scheduleViewPanelRender(140);
            renderCharts(state.snapshot, { overlayImmediate: true });
            if (view.barsVisible > previousBarsVisible) maybeLoadMoreHistory();
            return;
          }
          if (event.pointerId !== touchNavigation.primaryId) return;
          if (touchNavigation.mode === "price") {
            view.smoothOffsetBars = 0;
            const deltaY = touchNavigation.startY - event.clientY;
            view.priceZoom = clamp(
              touchNavigation.startPriceZoom * Math.exp(deltaY * PRICE_ZOOM_SENSITIVITY),
              MIN_PRICE_ZOOM,
              MAX_PRICE_ZOOM,
            );
          } else if (touchNavigation.mode === "pan") {
            const rawDeltaBars = (event.clientX - touchNavigation.startX) / touchNavigation.dragStep;
            view.dragCurrentDeltaBars = rawDeltaBars;
            const startVirtualOffset = view.dragStartOffset
              + view.dragStartHistoryPullOffsetBars
              - Math.max(view.dragStartRightGapBars - DEFAULT_RIGHT_GAP_BARS, 0);
            const applied = applyTimeViewportVirtualOffset(startVirtualOffset + rawDeltaBars);
            if (canvas.id === "price-chart") {
              view.priceShift = clamp(
                touchNavigation.startPriceShift
                + (event.clientY - touchNavigation.startY) / Math.max(canvas.clientHeight - 40, 1),
                -5,
                5,
              );
            }
            if (Math.abs(rawDeltaBars) >= 0.01) view.followLatest = applied.virtualOffset <= 0;
            updateChartNavigationControls();
            maybeLoadMoreHistory();
          }
          const key = `${view.offset}|${view.rightGapBars}|${view.smoothOffsetBars.toFixed(4)}|${view.priceZoom.toFixed(4)}|${view.priceShift.toFixed(4)}|${view.barsVisible}`;
          if (key === touchNavigation.lastViewportKey) return;
          touchNavigation.lastViewportKey = key;
          scheduleViewPanelRender(140);
          renderCharts(state.snapshot, { overlayImmediate: true });
        };

        const endTouchNavigation = event => {
          if (!touchNavigation.points.has(event.pointerId)) return;
          if (touchNavigation.contextHold?.pointerId === event.pointerId) {
            cancelTouchContextHold();
          }
          touchNavigation.points.delete(event.pointerId);
          if (touchNavigation.mode === "pinch" && touchNavigation.points.size === 1) {
            const remaining = Array.from(touchNavigation.points.values())[0];
            beginSingleTouchNavigation(remaining, canvas.getBoundingClientRect(), true);
            return;
          }
          if (touchNavigation.points.size === 0) finishTouchNavigation();
        };

        canvas.addEventListener("pointermove", moveTouchNavigation);
        canvas.addEventListener("pointerup", endTouchNavigation);
        canvas.addEventListener("pointercancel", endTouchNavigation);
        canvas.addEventListener("lostpointercapture", endTouchNavigation);

        canvas.addEventListener("pointerdown", event => {
          if (event.button === 2) return;
          if (!state.snapshot) return;
          const unmodifiedPrimaryClick = event.button === 0
            && !event.altKey
            && !event.ctrlKey
            && !event.metaKey
            && !event.shiftKey;
          event.preventDefault();
          closeContextMenu();
          const visible = visibleBars(state.snapshot);
          const rect = canvas.getBoundingClientRect();
          const localX = event.clientX - rect.left;
          const localY = event.clientY - rect.top;
          const priceScaleMode = canvas.id === "price-chart" && localX >= rect.width - PRICE_AXIS_WIDTH;
          if (event.pointerType === "touch" && touchNavigation.points.size > 0) {
            beginTouchNavigation(event, rect, priceScaleMode);
            return;
          }
          if (canvas.id === "price-chart") {
            const paperOrderHit = hitTestManagedObjectLayers("paper-order", localX, localY)?.hit;
            if (paperOrderHit?.action === "cancel" && typeof cancelPaperOrder === "function") {
              cancelPaperOrder(paperOrderHit.id);
              return;
            }
            if (paperOrderHit?.action === "drag-entry-label" && typeof startPaperEntryLabelDrag === "function" && startPaperEntryLabelDrag(canvas, event, paperOrderHit.id, paperOrderHit)) {
              return;
            }
            if (paperOrderHit?.action === "drag" && typeof startPaperOrderDrag === "function" && startPaperOrderDrag(canvas, event, paperOrderHit.id)) {
              return;
            }
          }
          if (
            canvas.id === "price-chart"
            && !priceScaleMode
            && unmodifiedPrimaryClick
            && typeof paperTradingHandleChartClick === "function"
            && paperTradingHandleChartClick(localX, localY)
          ) {
            return;
          }
          if (canvas.id === "price-chart" && !priceScaleMode) {
            const actionHit = canvasActionHit("price", localX, localY);
            if (actionHit?.action && typeof runIndicatorTableAction === "function" && runIndicatorTableAction(actionHit.action, { snapshot: state.snapshot, hit: actionHit })) {
              return;
            }
          }
          if (canvas.id === "price-chart" && state.drawing.tool === "cursor" && !priceScaleMode) {
            const optionTargetId = hitTestManagedObjectLayers("option-target", localX, localY)?.id;
            if (optionTargetId) {
              startOptionTargetDrag(canvas, event, optionTargetId);
              return;
            }
            const alertHit = hitTestManagedObjectLayers("alert", localX, localY)?.hit;
            if (alertHit) {
              if (alertHit.action === "delete") {
                deletePriceAlert(alertHit.alert.id);
              } else {
                startPriceAlertDrag(canvas, event, alertHit.alert);
              }
              return;
            }
            const drawingHit = hitTestManagedObjectLayers("drawing", localX, localY);
            const handle = drawingHit?.handle || null;
            if (handle) {
              startDrawingHandleDrag(canvas, event, handle);
              return;
            }
            const hitId = drawingHit?.id || null;
            if (hitId) {
              setChartObjectSelection("drawing", hitId);
              applyDrawingUi();
              renderDrawingOverlay();
              return;
            }
            if (state.drawing.selectedId || state.alerts.selectedId || state.optionTargets.selectedId) {
              setChartObjectSelection("", null);
              applyDrawingUi();
              renderDrawingOverlay();
            }
          }
          if (canvas.id === "price-chart" && state.drawing.tool !== "cursor" && !priceScaleMode) {
            handleDrawingPointerDown(event);
            return;
          }
          if (event.pointerType === "touch") {
            beginTouchNavigation(event, rect, priceScaleMode);
            return;
          }
          canvas.setPointerCapture(event.pointerId);
          view.dragStartX = event.clientX;
          view.dragStartY = event.clientY;
          view.dragStartOffset = view.offset;
          view.dragStartPriceZoom = view.priceZoom;
          view.dragStartPriceShift = view.priceShift;
          view.dragStartRightGapBars = rightGapBars();
          view.dragStartHistoryPullOffsetBars = Number(view.historyPullOffsetBars) || 0;
          view.dragCurrentDeltaBars = 0;
          view.dragActive = !priceScaleMode;
          const followLatestAtDragStart = view.followLatest;
          view.dragStep = Math.max((rect.width - 118) / Math.max(chartVisualSpan(visible.bars) + rightGapBars(), 1), 1);
          canvas.classList.add(priceScaleMode ? "price-scale-dragging" : "dragging");
          if (typeof beginChartInteractionQuality === "function") beginChartInteractionQuality(600);
          let lastViewportKey = "";
          const onMove = move => {
            if (typeof beginChartInteractionQuality === "function") beginChartInteractionQuality(220);
            if (priceScaleMode) {
              view.smoothOffsetBars = 0;
              const deltaY = view.dragStartY - move.clientY;
              view.priceZoom = clamp(
                view.dragStartPriceZoom * Math.exp(deltaY * PRICE_ZOOM_SENSITIVITY),
                MIN_PRICE_ZOOM,
                MAX_PRICE_ZOOM,
              );
              const key = `${view.priceZoom.toFixed(5)}|${view.priceShift.toFixed(5)}`;
              if (key === lastViewportKey) return;
              lastViewportKey = key;
              renderCharts(state.snapshot, { overlayImmediate: true });
              return;
            }
            const rawDeltaBars = (move.clientX - view.dragStartX) / view.dragStep;
            view.dragCurrentDeltaBars = rawDeltaBars;
            const deltaY = move.clientY - view.dragStartY;
            const startVirtualOffset = view.dragStartOffset
              + view.dragStartHistoryPullOffsetBars
              - Math.max(view.dragStartRightGapBars - DEFAULT_RIGHT_GAP_BARS, 0);
            const applied = applyTimeViewportVirtualOffset(startVirtualOffset + rawDeltaBars);
            if (canvas.id === "price-chart") {
              view.priceShift = clamp(view.dragStartPriceShift + deltaY / Math.max(rect.height - 40, 1), -5, 5);
            }
            if (Math.abs(rawDeltaBars) >= 0.01) view.followLatest = applied.virtualOffset <= 0;
            updateChartNavigationControls();
            const key = `${view.offset}|${view.rightGapBars}|${view.smoothOffsetBars.toFixed(4)}|${view.priceShift.toFixed(4)}|${view.barsVisible}`;
            if (key === lastViewportKey) return;
            lastViewportKey = key;
            scheduleViewPanelRender(140);
            renderCharts(state.snapshot, { overlayImmediate: true });
            maybeLoadMoreHistory();
          };
          const onUp = () => {
            canvas.classList.remove("dragging", "price-scale-dragging");
            view.dragActive = false;
            view.dragCurrentDeltaBars = 0;
            view.dragStartHistoryPullOffsetBars = 0;
            view.historyPullOffsetBars = 0;
            view.smoothOffsetBars = 0;
            persistViewState();
            if (viewPanelRenderTimer) {
              clearTimeout(viewPanelRenderTimer);
              viewPanelRenderTimer = 0;
            }
            if (state.snapshot) renderPanel(state.snapshot);
            if (state.snapshot) renderCharts(state.snapshot, { overlayImmediate: true });
            canvas.removeEventListener("pointermove", onMove);
            canvas.removeEventListener("pointerup", onUp);
            canvas.removeEventListener("pointercancel", onUp);
            if (typeof endChartInteractionQuality === "function") endChartInteractionQuality();
            if (!followLatestAtDragStart && view.followLatest) {
              setFollowLatest(true, { refreshTail: true });
            }
          };
          canvas.addEventListener("pointermove", onMove);
          canvas.addEventListener("pointerup", onUp);
          canvas.addEventListener("pointercancel", onUp);
        });

        canvas.addEventListener("contextmenu", handleChartContextRequest);
        canvas.addEventListener("dblclick", event => {
          if (canvas.id === "price-chart" && state.drawing.draft?.type === "path") {
            event.preventDefault();
            finishPathDrawing();
            return;
          }
          if (canvas.id === "price-chart") {
            const rect = canvas.getBoundingClientRect();
            const localX = event.clientX - rect.left;
            const localY = event.clientY - rect.top;
            const optionTargetId = hitTestManagedObjectLayers("option-target", localX, localY)?.id || null;
            const drawingHit = hitTestManagedObjectLayers("drawing", localX, localY);
            const hitId = optionTargetId || drawingHit?.handle?.id || drawingHit?.id || null;
            if (hitId) {
              event.preventDefault();
              setChartObjectSelection(optionTargetId ? "option-target" : "drawing", optionTargetId || hitId);
              if (!optionTargetId) openDrawingManager({ focusSelectedType: true });
              renderDrawingOverlay();
              return;
            }
          }
          resetView();
        });
      }
      document.addEventListener("click", event => {
        const menu = document.getElementById("chart-context-menu");
        if (menu && !menu.contains(event.target)) closeContextMenu();
      });
    }

    function setupToolbar() {
      document.getElementById("time-zoom-out")?.addEventListener("click", () => zoomTime(1.08));
      document.getElementById("time-zoom-in")?.addEventListener("click", () => zoomTime(0.93));
      document.getElementById("price-zoom-out")?.addEventListener("click", () => zoomPrice(0.94));
      document.getElementById("price-zoom-in")?.addEventListener("click", () => zoomPrice(1.065));
      document.getElementById("reset-view")?.addEventListener("click", resetView);
      document.getElementById("live-toggle")?.addEventListener("click", () => setFollowLatest(!view.followLatest));
      document.getElementById("price-fit-height")?.addEventListener("click", fitPriceHeight);
      const rangeButton = document.getElementById("chart-range-toggle");
      const rangeMenu = document.getElementById("chart-range-menu");
      const rangePresets = {
        "1d": { label: "1D", timeframe: "5m", loadRange: "3d", visibleDurationMs: CHART_VIEW_DAY_MS },
        "5d": { label: "5D", timeframe: "5m", loadRange: "5d" },
        "1m": { label: "1M", timeframe: "15m", loadRange: "31d" },
        "3m": { label: "3M", timeframe: "60m", loadRange: "3mo" },
        "6m": { label: "6M", timeframe: "60m", loadRange: "6mo" },
      };
      const setRangeMenuOpen = open => {
        rangeMenu?.classList.toggle("hidden", !open);
        rangeButton?.classList.toggle("active", Boolean(open));
        rangeButton?.setAttribute("aria-expanded", open ? "true" : "false");
      };
      const fitLoadedRangeToScreen = (durationMs = null) => {
        if (!resetTimeViewportToLatest(state.snapshot, durationMs)) return;
        saveBarsVisibleForCurrent();
        refreshView({ panelDelayMs: 140, overlayImmediate: true });
      };
      async function applyChartRangePreset(key) {
        const preset = rangePresets[key];
        if (!preset) return;
        setRangeMenuOpen(false);
        const timeframeChanged = preset.timeframe !== state.timeframe;
        if (timeframeChanged && typeof publishLinkedCrosshair === "function") {
          publishLinkedCrosshair(state.crosshair, { visible: false, immediate: true });
        }
        if (timeframeChanged && typeof mtfLensHandlePrimaryTimeframeChange === "function") {
          mtfLensHandlePrimaryTimeframeChange(preset.timeframe);
        }
        if (timeframeChanged) closeQuoteStream();
        state.timeframe = preset.timeframe;
        workspaceSet("timeframe", state.timeframe);
        state.range = preset.loadRange;
        setServerSettingValue(rangeStorageKey(state.instrumentId, state.timeframe), state.range);
        state.requests.history.exhausted = false;
        view.offset = 0;
        view.historyPullOffsetBars = 0;
        view.rightGapBars = DEFAULT_RIGHT_GAP_BARS;
        view.rightGapManual = false;
        view.followLatest = true;
        state.crosshair.visible = false;
        clearChartStateForRouteSwitch();
        if (timeframeChanged) {
          loadIndicatorSettingsForCurrent();
          loadDrawingsForCurrent();
          setActiveGexContext(state.gexContexts[gexContextKey()] || null);
          state.loadingGex = Boolean(state.loadingGexKeys[gexContextKey()]);
        }
        setUiNotice(
          "history",
          "HISTORY_PRESET_LOADING",
          `Loading ${preset.label} chart...`,
          { state: "loading" },
        );
        applySettings();
        if (state.snapshot) renderPanel(state.snapshot);
        const loaded = await load({
          force: true,
          range: preset.loadRange,
          historyPreset: true,
        });
        if (loaded && state.range === preset.loadRange && state.timeframe === preset.timeframe) {
          fitLoadedRangeToScreen(preset.visibleDurationMs);
        }
        loadScreener({ allowWhenStream: true });
        connectQuoteStream(true);
        syncWorkspacePageState();
      }
      rangeButton?.addEventListener("click", event => {
        event.stopPropagation();
        setRangeMenuOpen(rangeMenu?.classList.contains("hidden"));
      });
      rangeMenu?.addEventListener("click", event => {
        const button = event.target?.closest?.("[data-chart-range-preset]");
        if (!button) return;
        event.stopPropagation();
        applyChartRangePreset(button.dataset.chartRangePreset);
      });
      document.addEventListener("pointerdown", event => {
        if (rangeMenu?.classList.contains("hidden")) return;
        const target = event.target;
        if (rangeMenu?.contains(target) || rangeButton?.contains(target)) return;
        setRangeMenuOpen(false);
      });
      document.addEventListener("keydown", event => {
        if (event.key === "Escape") setRangeMenuOpen(false);
      });
    }
