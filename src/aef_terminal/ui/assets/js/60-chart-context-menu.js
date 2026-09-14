    function barAtLocalX(localX) {
      const geo = priceChartGeometry();
      if (!geo) return null;
      const visualHit = chartVisualHitForLocalX(geo.bars, geo.pad, geo.xStep, localX);
      if (visualHit.gap || !Number.isSafeInteger(visualHit.index)) return null;
      const index = clamp(
        visualHit.index,
        0,
        geo.bars.length - 1,
      );
      return { bar: geo.bars[index], index, geo };
    }

    function priceAtLocalY(localY, geo) {
      return geo.maxP - ((localY - geo.pad.top) / geo.priceH) * (geo.maxP - geo.minP);
    }

    function localPointInsidePricePlot(localX, localY, geo) {
      if (!geo?.pad || !Number.isFinite(Number(localX)) || !Number.isFinite(Number(localY))) return false;
      const left = Number(geo.pad.left);
      const right = Number(geo.rect?.width) - Number(geo.pad.right);
      const top = Number(geo.pad.top);
      const bottom = top + Number(geo.priceH);
      return localX >= left && localX <= right && localY >= top && localY <= bottom;
    }

    function closeContextMenu() {
      state.contextMenu.open = false;
      state.contextMenu.crosshair = null;
      const node = document.getElementById("chart-context-menu");
      if (node) node.classList.remove("open");
    }

    function contextMenuCrosshairLocked() {
      return Boolean(state.contextMenu.open && state.contextMenu.crosshair);
    }

    function applyFrozenContextCrosshair() {
      if (!contextMenuCrosshairLocked()) return false;
      state.crosshair = { ...state.contextMenu.crosshair, visible: true };
      return true;
    }

    const CONTEXT_ACTION_ICON_ALERT = `<svg viewBox="0 0 12 12" class="context-action-icon" aria-hidden="true"><circle cx="6" cy="6.5" r="3.6"/><path d="M6 6.5V4.4"/><path d="M6 6.5l1.35 1"/><path d="M3.4 2.1L2.6 1.4M8.6 2.1l.8-.7"/><path d="M2.4 1.1h1.1M8.5 1.1h1.1"/></svg>`;
    const CONTEXT_ACTION_ICON_OPTION = `<svg viewBox="0 0 12 12" class="context-action-icon context-action-icon-fill" aria-hidden="true"><text x="2.8" y="5.6" font-size="4.1" font-weight="800" font-family="-apple-system,BlinkMacSystemFont,sans-serif">123</text><circle cx="2.2" cy="9.7" r="1.05"/></svg>`;
    const CONTEXT_ACTION_ICON_LINE = `<svg viewBox="0 0 12 12" class="context-action-icon" aria-hidden="true"><line x1="1" y1="6" x2="11" y2="6"/></svg>`;

    function contextActionButton(action, icon, label) {
      return `<button class="tool-button context-action-button" data-context-action="${action}">${icon}<span class="context-action-label">${label}</span></button>`;
    }

    function tradingDayStartBar(bar) {
      const bars = state.snapshot?.bars || [];
      if (!bar || !bars.length) return null;
      const sessionKey = String(bar.session_key || "");
      if (sessionKey) {
        for (const candidate of bars) {
          if (String(candidate?.session_key || "") === sessionKey) return candidate;
        }
      }
      return bar;
    }

    function addHorizontalLineFromContext() {
      const { bar, price } = state.contextMenu || {};
      const level = Number(price);
      if (!bar || !Number.isFinite(level)) return;
      const startBar = tradingDayStartBar(bar);
      const endBar = bar;
      const confirmedDrawingPoint = candidate => {
        if (
          !barIsConfirmedClosed(candidate)
          || candidate?.authoritative === false
        ) return null;
        try {
          return normalizeDrawingPoint({ ts: candidate.ts, price: level });
        } catch (_error) {
          return null;
        }
      };
      const points = [confirmedDrawingPoint(startBar), confirmedDrawingPoint(endBar)];
      if (points.some(point => !point)) return;
      addDrawingObject({
        type: "line",
        points,
        extendRight: true,
        style: "solid",
        width: 1,
        color: isLightTheme() ? themeColor("purple") : "#db2777",
      });
      closeContextMenu();
    }

    function renderContextMenu() {
      const node = document.getElementById("chart-context-menu");
      const { open, x, y, bar, price } = state.contextMenu;
      if (!node || !open || !bar) {
        if (node) node.classList.remove("open");
        return;
      }
      const hasPrice = Number.isFinite(Number(price));
      node.innerHTML = `
        <div class="context-actions">
          ${hasPrice ? contextActionButton("add-alert", CONTEXT_ACTION_ICON_ALERT, "Add price alert") : ""}
          ${hasPrice ? contextActionButton("option-target", CONTEXT_ACTION_ICON_OPTION, "Option price") : ""}
          ${hasPrice ? contextActionButton("add-line", CONTEXT_ACTION_ICON_LINE, "Add horizontal line") : ""}
        </div>`;
      node.classList.add("open");
      const menuW = node.offsetWidth || 230;
      const menuH = node.offsetHeight || 220;
      node.style.left = `${clamp(x, 8, window.innerWidth - menuW - 8)}px`;
      node.style.top = `${clamp(y, 8, window.innerHeight - menuH - 8)}px`;
      node.querySelector('[data-context-action="add-alert"]')?.addEventListener("click", () => {
        addPriceAlert(price, "cross");
        closeContextMenu();
      });
      node.querySelector('[data-context-action="option-target"]')?.addEventListener("click", addOptionTargetLabelFromContext);
      node.querySelector('[data-context-action="add-line"]')?.addEventListener("click", addHorizontalLineFromContext);
    }

    function openChartContextMenu(event, canvas) {
      event.preventDefault();
      if (canvas.id !== "price-chart") {
        closeContextMenu();
        return;
      }
      if (!state.snapshot?.bars?.length) return;
      const rect = canvas.getBoundingClientRect();
      const localX = event.clientX - rect.left;
      const localY = event.clientY - rect.top;
      const hit = barAtLocalX(localX);
      if (!hit) return;
      if (canvas.id === "price-chart" && !localPointInsidePricePlot(localX, localY, hit.geo)) return;
      const point = canvas.id === "price-chart" ? chartPointFromLocal(localX, localY) : null;
      const price = point ? point.price : canvas.id === "price-chart" ? priceAtLocalY(localY, hit.geo) : null;
      const crosshair = snappedCrosshair(canvas.id, localX, localY, rect);
      if (canvas.id === "price-chart" && Number.isFinite(Number(price)) && hit.geo) {
        crosshair.price = Number(price);
        crosshair.y = hit.geo.y(Number(price));
        crosshair.snapKind = point?.snapKind || null;
      }
      state.crosshair = { ...crosshair, visible: true };
      if (typeof hideCanvasTooltip === "function") hideCanvasTooltip();
      state.contextMenu = {
        open: true,
        x: event.clientX,
        y: event.clientY,
        bar: hit.bar,
        price,
        point,
        crosshair: { ...state.crosshair },
      };
      renderContextMenu();
      if (state.snapshot && typeof refreshCrosshairOverlays === "function") {
        refreshCrosshairOverlays({ immediate: true });
      } else if (state.snapshot && canvas.id === "price-chart" && typeof renderInteractionOverlay === "function") {
        renderInteractionOverlay({ immediate: true });
      } else if (state.snapshot && typeof scheduleVolumeInteractionRender === "function") {
        scheduleVolumeInteractionRender(state.snapshot, { immediate: true });
      }
    }
