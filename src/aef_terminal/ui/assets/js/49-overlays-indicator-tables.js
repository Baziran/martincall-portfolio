function indicatorTableLabel(source, table = null) {
      const title = String(table?.title || "").trim();
      const tableContract = typeof indicatorRendererTableContract === "function" ? indicatorRendererTableContract(source) : {};
      const contractLabel = String(tableContract?.label || "").trim();
      const spec = typeof indicatorSpec === "function" ? indicatorSpec(source) : (window.INDICATOR_REGISTRY_MANIFEST || {})[source] || {};
      return contractLabel || title || spec.label || "Indicator";
    }

    const indicatorTableModelRenderers = new Map();
    const indicatorTableAnimationRenderers = new Map();
    const indicatorTableHeaderIconRenderers = new Map();

    function registerIndicatorTableModel(modelRef, renderer) {
      const key = String(modelRef || "").trim();
      if (!key || typeof renderer !== "function") throw new Error(`Invalid indicator table model: ${key || "missing"}`);
      indicatorTableModelRenderers.set(key, renderer);
    }

    function indicatorTableModelColumns(table, overlay = null) {
      if (Array.isArray(table?.columns) && table.columns.length) return table.columns;
      const modelRef = String(table?.model_ref || overlay?.model_ref || "").trim();
      if (!modelRef) return [];
      const renderer = indicatorTableModelRenderers.get(modelRef);
      if (!renderer) throw new Error(`Unknown indicator table model: ${modelRef}`);
      const columns = renderer(table, overlay);
      return Array.isArray(columns) ? columns : [];
    }

    function registerIndicatorTableAnimation(animationRef, renderer) {
      const key = String(animationRef || "").trim();
      if (!key || typeof renderer !== "function") throw new Error(`Invalid indicator table animation: ${key || "missing"}`);
      if (indicatorTableAnimationRenderers.has(key)) throw new Error(`Duplicate indicator table animation: ${key}`);
      indicatorTableAnimationRenderers.set(key, renderer);
    }

    function drawIndicatorTableAnimation(animationRef, context) {
      const key = String(animationRef || "").trim();
      if (!key || key === "table_thinking") return;
      const renderer = indicatorTableAnimationRenderers.get(key);
      if (!renderer) throw new Error(`Unknown indicator table animation: ${key}`);
      renderer(context);
    }

    function registerIndicatorTableHeaderIcon(iconRef, renderer, options = {}) {
      const key = String(iconRef || "").trim();
      if (!key || typeof renderer !== "function") throw new Error(`Invalid indicator table header icon: ${key || "missing"}`);
      if (!options || typeof options !== "object" || Array.isArray(options)) {
        throw new Error(`Invalid indicator table header icon options: ${key}`);
      }
      if (indicatorTableHeaderIconRenderers.has(key)) throw new Error(`Duplicate indicator table header icon: ${key}`);
      indicatorTableHeaderIconRenderers.set(key, {
        renderer,
        action: String(options.action || "").trim(),
      });
    }

    let indicatorTableThinkingAnimationFrame = 0;
    let indicatorTableThinkingAnimationLast = 0;
    let lastIndicatorTableThinkingRequest = 0;

    function indicatorTableThinkingActive(table, overlay = null) {
      if (indicatorTableThinkingRefActive(String(table?.thinking_ref || overlay?.thinking_ref || ""))) return true;
      if (table?.thinking === true || table?.ai_thinking === true) return true;
      const status = String(table?.status || table?.ai_status || overlay?.connector?.status || "").toLowerCase();
      if (["pending_external_worker", "pending", "thinking", "requesting"].includes(status)) return true;
      const targetHead = String(table?.thinking_column || "").toUpperCase();
      if (!targetHead) return false;
      const columns = Array.isArray(table?.columns) ? table.columns : [];
      return columns.some(column => {
        const cells = Array.isArray(column?.cells) ? column.cells : Array.isArray(column) ? column : [];
        return String(cells[0] || "").toUpperCase() === targetHead && /PEND|THINK|WAIT/i.test(String(cells[1] || ""));
      });
    }

    function ensureIndicatorTableThinkingAnimationLoop(table, overlay = null) {
      lastIndicatorTableThinkingRequest = Date.now();
      if (indicatorTableThinkingAnimationFrame || !uiMotionEnabled() || !state.snapshot?.bars?.length) return;
      const tick = timestamp => {
        if (!uiMotionEnabled() || !state.snapshot?.bars?.length || Date.now() - lastIndicatorTableThinkingRequest >= 500) {
          indicatorTableThinkingAnimationFrame = 0;
          return;
        }
        indicatorTableThinkingAnimationFrame = requestAnimationFrame(tick);
        if (!indicatorTableThinkingAnimationLast || timestamp - indicatorTableThinkingAnimationLast >= 110) {
          indicatorTableThinkingAnimationLast = timestamp;
          if (typeof renderPriceOverlay === "function") renderPriceOverlay(state.snapshot);
        }
      };
      indicatorTableThinkingAnimationFrame = requestAnimationFrame(tick);
    }

    function drawIndicatorTableThinkingBackdrop(ctx, left, top, tableW, cellH, headerW) {
      const t = Date.now() / 1000;
      const sweep = (Math.sin(t * 2.4) + 1) / 2;
      const glow = 0.18 + 0.08 * Math.sin(t * 5.2);
      ctx.save();
      ctx.globalCompositeOperation = "lighter";
      const halo = ctx.createLinearGradient(left - headerW, top, left + tableW, top + cellH);
      halo.addColorStop(0, `rgba(82,168,255,${Math.max(glow * 0.35, 0.03)})`);
      halo.addColorStop(0.48, `rgba(171,120,255,${Math.max(glow, 0.05)})`);
      halo.addColorStop(1, `rgba(78,227,191,${Math.max(glow * 0.55, 0.04)})`);
      ctx.strokeStyle = halo;
      ctx.lineWidth = 1.4;
      ctx.strokeRect(left - headerW - 1, top - 1, tableW + headerW + 2, cellH + 2);

      const sweepX = left - headerW + sweep * (tableW + headerW);
      const sweepGradient = ctx.createLinearGradient(sweepX - 34, top, sweepX + 34, top);
      sweepGradient.addColorStop(0, "rgba(82,168,255,0)");
      sweepGradient.addColorStop(0.5, "rgba(216,236,255,0.42)");
      sweepGradient.addColorStop(1, "rgba(82,168,255,0)");
      ctx.strokeStyle = sweepGradient;
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.moveTo(sweepX - 34, top + 2);
      ctx.lineTo(sweepX + 34, top + 2);
      ctx.stroke();

      const nodes = [
        [left - headerW / 2, top + cellH * 0.30, 0],
        [left + tableW * 0.18, top + cellH * 0.18, 1.1],
        [left + tableW * 0.52, top + cellH * 0.82, 2.2],
        [left + tableW * 0.86, top + cellH * 0.22, 3.0],
      ];
      ctx.strokeStyle = "rgba(174,220,255,0.18)";
      ctx.lineWidth = 0.8;
      ctx.beginPath();
      nodes.forEach(([x, y], index) => {
        if (index === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      });
      ctx.stroke();
      nodes.forEach(([x, y, phase]) => {
        const pulse = (Math.sin(t * 5.5 + phase) + 1) / 2;
        ctx.fillStyle = `rgba(216,236,255,${0.22 + pulse * 0.34})`;
        ctx.beginPath();
        ctx.arc(x, y, 1.4 + pulse * 1.7, 0, Math.PI * 2);
        ctx.fill();
      });
      ctx.restore();
    }

    function drawIndicatorTableThinkingDots(ctx, x0, top, cellW, cellH, textColor) {
      const t = Date.now() / 1000;
      ctx.save();
      ctx.globalCompositeOperation = "lighter";
      for (let i = 0; i < 3; i += 1) {
        const pulse = (Math.sin(t * 7.0 + i * 1.4) + 1) / 2;
        ctx.fillStyle = rgbaFromCssColor(textColor || "#d8ecff", 0.30 + pulse * 0.52);
        ctx.beginPath();
        ctx.arc(x0 + cellW / 2 + 23 + i * 5, top + cellH / 2, 1.2 + pulse * 1.1, 0, Math.PI * 2);
        ctx.fill();
      }
      ctx.restore();
    }

    function drawIndicatorTableHeader(ctx, label, left, top, width, height, accent, textColor, tooltip, options = {}) {
      const headerW = Number(options.headerWidth) || 14;
      const headerLeft = left - headerW;
      const headerBg = accent ? rgbaFromCssColor(accent, 0.96) : rgbaFromCssColor(css("--panel-2"), 0.96);
      const headerText = readableTextForBg(headerBg, textColor || null);
      const backgroundOpacity = clamp(Number(options.backgroundOpacity) || 1, 0, 1);
      ctx.save();
      ctx.fillStyle = headerBg;
      ctx.globalAlpha = backgroundOpacity;
      ctx.fillRect(headerLeft, top, headerW, height);
      ctx.globalAlpha = 1;
      ctx.strokeStyle = rgbaFromCssColor(headerText, 0.22);
      ctx.strokeRect(headerLeft, top, headerW, height);
      ctx.fillStyle = headerText;
      const iconEntry = indicatorTableHeaderIconRenderers.get(String(options.icon || "").trim());
      if (iconEntry) {
        iconEntry.renderer({
          ctx,
          centerX: headerLeft + headerW / 2,
          centerY: top + height / 2,
          size: Math.min(31, height - 2),
          color: headerText,
          background: headerBg,
        });
        ctx.restore();
        if (tooltip) {
          registerCanvasTooltip(
            "price",
            headerLeft + headerW / 2,
            top + height / 2,
            headerW,
            height,
            tooltip,
            iconEntry.action || null,
          );
        }
        return headerW;
      }
      ctx.font = "900 6px -apple-system, BlinkMacSystemFont, sans-serif";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.save();
      ctx.translate(headerLeft + headerW / 2, top + height / 2);
      ctx.rotate(-Math.PI / 2);

      const text = String(label || "Indicator").toUpperCase();
      const words = text.split(/\s+/);
      if (words.length > 1 && text.length > 9) {
        let bestSplit = 1;
        let minDiff = Infinity;
        for (let i = 1; i < words.length; i++) {
          const s1 = words.slice(0, i).join(" ");
          const s2 = words.slice(i).join(" ");
          const diff = Math.abs(s1.length - s2.length);
          if (diff < minDiff) {
            minDiff = diff;
            bestSplit = i;
          }
        }
        const line1 = words.slice(0, bestSplit).join(" ");
        const line2 = words.slice(bestSplit).join(" ");
        const spacing = 3.2;
        ctx.fillText(line1, 0, -spacing);
        ctx.fillText(line2, 0, spacing);
      } else {
        ctx.fillText(text.slice(0, 32), 0, 0.5);
      }
      ctx.restore();
      ctx.restore();
      if (tooltip) registerCanvasTooltip("price", headerLeft + headerW / 2, top + height / 2, headerW, height, tooltip);
      return headerW;
    }

    function tableTopForPosition(position, stackOffset, pad, priceH, cellH) {
      const margin = 1;
      const offset = Math.max(0, Number(stackOffset) || 0);
      return position === "top"
        ? pad.top + margin - 7 + offset
        : pad.top + priceH - cellH - margin - offset;
    }

    function tableValueFont(head, value, size = 8.5) {
      const text = `${head || ""} ${value || ""}`;
      const mono = /(?:PLAN|ENTRY|STOP|TARGET|PRICE|RANGE|BANDS|RR|RISK|INV|LEVEL|E\s|S\s|T\s|\d+\.\d+)/i.test(text);
      return `${mono ? 750 : 700} ${size}px ${mono ? "JetBrains Mono, Roboto Mono, Menlo, Consolas, monospace" : "-apple-system, BlinkMacSystemFont, sans-serif"}`;
    }

    const INDICATOR_TABLE_POSITIONS = new Set([
      "bottom",
      "top",
      "top-left",
      "top-right",
      "bottom-left",
      "bottom-right",
      "dock",
      "off",
    ]);
    const INDICATOR_TABLE_CORNER_POSITIONS = new Set([
      "top-left",
      "top-right",
      "bottom-left",
      "bottom-right",
    ]);

    function indicatorTableControlValue(source, key, fallback) {
      const spec = typeof indicatorSpec === "function" ? indicatorSpec(source) : (window.INDICATOR_REGISTRY_MANIFEST || {})[source] || {};
      const placements = Array.isArray(spec?.renderer_contract?.placements) ? spec.renderer_contract.placements : [];
      if (!placements.includes("table") && !spec?.table_contract) return fallback;
      const control = (Array.isArray(spec?.controls) ? spec.controls : []).find(item => item?.key === key);
      const stateKey = spec?.ui?.state_key || "";
      const group = stateKey ? state.indicators?.[stateKey] || {} : {};
      return control ? group?.[control.state_key || control.key] ?? control.default : fallback;
    }

    function tablePositionForSource(source) {
      const value = String(indicatorTableControlValue(source, "tablePosition", "bottom") || "bottom");
      return INDICATOR_TABLE_POSITIONS.has(value) ? value : "bottom";
    }

    function tableOpacityForSource(source) {
      const value = Number(indicatorTableControlValue(source, "tableOpacity", 90));
      return clamp(Number.isFinite(value) ? value : 90, 65, 100) / 100;
    }

    function indicatorTableIsCornerPosition(position) {
      return INDICATOR_TABLE_CORNER_POSITIONS.has(String(position || ""));
    }

    function indicatorTableColumnCells(column, table, overlay) {
      let cells = Array.isArray(column?.cells)
        ? column.cells
        : Array.isArray(column) ? column : ["", "", ""];
      if (column?.action && typeof indicatorTableActionCells === "function") {
        cells = indicatorTableActionCells(column.action, column, table, overlay, cells);
      }
      return cells.map(indicatorTableCellText);
    }

    function indicatorTableEstimatedHeight(overlay, position) {
      if (!indicatorTableIsCornerPosition(position)) return 35;
      const table = overlay?.table;
      const columns = indicatorTableModelColumns(table, overlay);
      return 24 + Math.max(columns.length, 1) * 28 + 2;
    }

    function indicatorTableStackLimit(position, priceH) {
      return indicatorTableIsCornerPosition(position)
        ? Math.max(74, Math.floor(Number(priceH || 0) * 0.46))
        : Math.max(35, Math.floor(Number(priceH || 0) * 0.28));
    }

    function indicatorTablePlacementPad(pad, width) {
      const gutter = typeof gexProfileGutterGeometry === "function"
        ? gexProfileGutterGeometry(pad, width)
        : null;
      return {
        ...pad,
        left: gutter?.shellVisible ? Math.max(pad.left, gutter.panelRight + 4) : pad.left,
      };
    }

    function indicatorTableWidthLayout(overlay, position, pad, width, columns = indicatorTableModelColumns(overlay.table, overlay)) {
      const availableWidth = Math.max(0, width - pad.left - pad.right - 8);
      const contract = indicatorRendererTableContract(overlay.source);
      const headerWidth = clamp(Number(contract.header_width || 14), 14, 64);
      const corner = indicatorTableIsCornerPosition(position);
      const cellWidth = columns.length ? clamp((availableWidth - headerWidth) / columns.length, 94, 150) : 0;
      return {
        fits: availableWidth >= (corner ? 160 : headerWidth + columns.length * 94),
        headerWidth,
        cellWidth,
        width: corner
          ? Math.min(clamp(Number(contract.corner_width || 248), 210, 300), availableWidth)
          : headerWidth + cellWidth * columns.length,
      };
    }

    let indicatorLensEntries = [];
    let indicatorLensRenderKey = "";
    let indicatorLensTooltipPointer = null;

    function hideIndicatorLensTooltip() {
      if (!indicatorLensTooltipPointer) return;
      indicatorLensTooltipPointer = null;
      hideCanvasTooltip();
    }

    function updateIndicatorLensTooltip(pointer = indicatorLensTooltipPointer) {
      if (!pointer) return;
      const { clientX, clientY } = pointer;
      const root = document.getElementById("indicator-lens-tables");
      const target = document.elementFromPoint(clientX, clientY)
        ?.closest(".indicator-lens-card-head, .indicator-lens-row");
      if (!workspaceDockSurfaceIsActive("indicator-lens") || !target || !root?.contains(target)) {
        hideIndicatorLensTooltip();
        return;
      }
      const card = target.closest(".indicator-lens-card");
      // Resolve against current entries even when only tooltip facts changed and the DOM was reused.
      const overlay = indicatorLensEntries[Number(card.dataset.indicatorLensEntry)]?.overlay;
      if (!overlay) {
        hideIndicatorLensTooltip();
        return;
      }
      const column = target.dataset.indicatorLensColumn === undefined
        ? null
        : indicatorTableModelColumns(overlay.table, overlay)[Number(target.dataset.indicatorLensColumn)];
      indicatorLensTooltipPointer = { clientX, clientY };
      showCanvasTooltip(clientX, clientY, standardTableTooltip(overlay, column));
    }

    function indicatorLensEntryKey(entry) {
      const overlay = entry?.overlay;
      const table = overlay?.table;
      const columns = indicatorTableModelColumns(table, overlay);
      return JSON.stringify([
        overlay?.source || "",
        entry?.reason || "dock",
        tableOpacityForSource(overlay?.source),
        table?.title || "",
        table?.tone || overlay?.tone || "",
        columns,
      ]);
    }

    function indicatorLensCellNode(text, className = "indicator-lens-cell") {
      const node = document.createElement("span");
      node.className = className;
      node.textContent = displayTableCell(text, 28);
      return node;
    }

    function indicatorLensTableCard(entry, entryIndex) {
      const overlay = entry.overlay;
      const table = overlay?.table || {};
      const columns = indicatorTableModelColumns(table, overlay);
      const opacity = tableOpacityForSource(overlay?.source);
      const tableBg = table.bg || overlay?.bg || css("--panel-2");
      const tableText = table.text || overlay?.text || css("--text");
      const semanticAccent = table.tone || overlay?.tone
        ? indicatorOverlayColor(table.tone ? table : overlay, 1)
        : null;
      const accent = table.accent || overlay?.accent || semanticAccent || tableBg;
      const card = document.createElement("section");
      card.className = "indicator-lens-card";
      card.dataset.indicatorSource = String(overlay?.source || "");
      card.dataset.indicatorLensEntry = String(entryIndex);
      card.style.backgroundColor = rgbaFromCssColor(tableBg, opacity);

      const head = document.createElement("header");
      head.className = "indicator-lens-card-head";
      head.style.backgroundColor = rgbaFromCssColor(accent, opacity);
      head.style.color = readableTextForBg(accent, tableText);
      const title = document.createElement("span");
      title.textContent = indicatorTableLabel(overlay?.source, table).toUpperCase();
      const reason = document.createElement("small");
      reason.textContent = entry.reason === "overflow" ? "CHART OVERFLOW" : "DOCK";
      head.append(title, reason);
      card.appendChild(head);

      columns.forEach((column, index) => {
        const cells = indicatorTableColumnCells(column, table, overlay);
        const row = document.createElement(column?.action ? "button" : "div");
        row.className = "indicator-lens-row";
        row.dataset.indicatorLensColumn = String(index);
        if (column?.action) row.type = "button";
        const semanticBg = column?.tone
          ? indicatorOverlayColor(column, 1)
          : null;
        const background = column?.bg
          || semanticBg
          || (index === 0 && accent ? accent : tableBg);
        const textColor = readableTextForBg(background, column?.text || tableText);
        row.style.backgroundColor = rgbaFromCssColor(background, opacity);
        row.style.color = textColor;
        row.append(
          indicatorLensCellNode(cells[0]),
          indicatorLensCellNode(cells[1]),
          indicatorLensCellNode(cells[2]),
        );
        if (column?.action) {
          row.addEventListener("click", () => {
            runIndicatorTableAction(column.action, {
              snapshot: state.snapshot,
              overlay,
              column,
              surface: "indicator-lens",
            });
          });
        }
        card.appendChild(row);
      });
      return card;
    }

    function renderIndicatorLens() {
      const count = document.getElementById("indicator-lens-count");
      if (count) {
        count.textContent = String(indicatorLensEntries.length);
        count.hidden = indicatorLensEntries.length === 0;
      }
      const root = document.getElementById("indicator-lens-tables");
      if (!root) return;
      const key = indicatorLensEntries.map(indicatorLensEntryKey).join("|");
      if (key === indicatorLensRenderKey && root.childNodes.length) {
        updateIndicatorLensTooltip();
        return;
      }
      indicatorLensRenderKey = key;
      root.replaceChildren();
      if (!indicatorLensEntries.length) {
        hideIndicatorLensTooltip();
        const empty = document.createElement("div");
        empty.className = "indicator-lens-empty";
        empty.textContent = "Choose Indicator Lens in an indicator’s Table setting. Tables that cannot fit safely on the chart also appear here.";
        root.appendChild(empty);
        return;
      }
      const fragment = document.createDocumentFragment();
      indicatorLensEntries.forEach((entry, index) => fragment.appendChild(indicatorLensTableCard(entry, index)));
      root.appendChild(fragment);
      updateIndicatorLensTooltip();
    }

    function updateIndicatorLensTables(entries) {
      indicatorLensEntries = Array.isArray(entries) ? entries : [];
      renderIndicatorLens();
    }

    function setupIndicatorLens() {
      registerWorkspaceDockSurface("indicator-lens", {
        activate: renderIndicatorLens,
        refresh: renderIndicatorLens,
        deactivate: hideIndicatorLensTooltip,
      });
      const tables = document.getElementById("indicator-lens-tables");
      tables?.addEventListener("pointerenter", updateIndicatorLensTooltip);
      tables?.addEventListener("pointermove", updateIndicatorLensTooltip);
      tables?.addEventListener("pointerleave", hideIndicatorLensTooltip);
      tables?.addEventListener("scroll", hideIndicatorLensTooltip, { passive: true });
      document.getElementById("indicator-lens-toggle")?.addEventListener("click", () => {
        toggleWorkspaceDockSurface("indicator-lens", { reason: "Indicator Lens toggled" });
      });
      document.getElementById("indicator-lens-collapse")?.addEventListener("click", () => {
        closeWorkspaceDockSurface("indicator-lens", { reason: "Indicator Lens collapsed" });
      });
      renderIndicatorLens();
      renderWorkspaceDockShell();
    }
