    function tooltipLineClass(line) {
      if (!line) return "";
      const role = tooltipLineRole(line);
      if (role === "separator") return "tooltip-separator";
      if (role === "title") return "tooltip-line tooltip-title tooltip-wrap";
      if (role === "plan") return "tooltip-line tooltip-plan";
      if (role === "entry") return "tooltip-line tooltip-entry tooltip-wrap";
      if (role === "stop") return "tooltip-line tooltip-stop";
      if (role === "target") return "tooltip-line tooltip-target";
      if (role === "meta") return "tooltip-line tooltip-meta tooltip-wrap";
      if (role === "positive") return "tooltip-line tooltip-positive tooltip-wrap";
      if (role === "negative") return "tooltip-line tooltip-negative tooltip-wrap";
      if (role === "role") return "tooltip-line tooltip-role tooltip-wrap";
      if (role === "flow") return "tooltip-line tooltip-flow tooltip-wrap";
      if (role === "pro") return "tooltip-line tooltip-pro tooltip-wrap";
      if (role === "con") return "tooltip-line tooltip-con tooltip-wrap";
      if (role === "more") return "tooltip-line tooltip-more";
      return "tooltip-line tooltip-wrap";
    }

    function tooltipImportantSpanClass(token, line) {
      const value = String(token || "").trim();
      if (!value.trim()) return "";
      const tokenRole = tooltipLineTokenRole(line, value);
      if (tokenRole === "go-long") return `tooltip-key tooltip-positive ${directionalGoMarkClass("long")}`;
      if (tokenRole === "go-short") return `tooltip-key tooltip-negative ${directionalGoMarkClass("short")}`;
      if (tokenRole === "call") return "tooltip-key tooltip-type tooltip-call";
      if (tokenRole === "put") return "tooltip-key tooltip-type tooltip-put";
      if (tokenRole === "type") return "tooltip-key tooltip-type";
      if (tokenRole === "price") return "tooltip-key tooltip-price";
      if (tokenRole === "strength") return "tooltip-key tooltip-strength";
      if (tokenRole === "percentile-low") return "tooltip-key tooltip-percentile-low";
      if (tokenRole === "percentile-mid") return "tooltip-key tooltip-percentile-mid";
      if (tokenRole === "percentile-high") return "tooltip-key tooltip-percentile-high";
      if (tokenRole === "positive") return "tooltip-key tooltip-positive";
      if (tokenRole === "negative") return "tooltip-key tooltip-negative";
      if (tokenRole === "warning") return "tooltip-key tooltip-warning";
      if (tokenRole === "muted") return "tooltip-key tooltip-muted";
      const role = tooltipLineRole(line);
      if (["entry", "stop", "target"].includes(role)) return "tooltip-key tooltip-price";
      if (role === "positive") return "tooltip-key tooltip-positive";
      if (role === "negative") return "tooltip-key tooltip-negative";
      return "";
    }

    function tooltipLineTokenPattern(line) {
      const roleTokens = line && typeof line === "object" && line.token_roles && typeof line.token_roles === "object"
        ? Object.keys(line.token_roles)
          .map(token => String(token || "").trim())
          .filter(Boolean)
          .sort((a, b) => b.length - a.length)
          .map(token => {
            const escaped = tooltipRegexEscape(token);
            const startBoundary = /^\w/.test(token) ? "\\b" : "";
            const endBoundary = /\w$/.test(token) ? "\\b" : "";
            return `${startBoundary}${escaped}${endBoundary}`;
          })
        : [];
      if (["entry", "stop", "target", "positive", "negative"].includes(tooltipLineRole(line))) {
        roleTokens.push("\\$?[-+]?\\d[\\d,.]*(?:\\.\\d+)?(?:[KMB])?(?:\\/pt)?", "\\b\\d+%");
      }
      return roleTokens.length ? new RegExp(`(${roleTokens.join("|")})`, "gi") : null;
    }

    function appendHighlightedTooltipLine(row, line) {
      const text = tooltipLineText(line);
      const tokenPattern = tooltipLineTokenPattern(line);
      if (!tokenPattern) {
        row.textContent = text;
        return;
      }
      let cursor = 0;
      let match = null;
      let appended = false;
      while ((match = tokenPattern.exec(text)) !== null) {
        if (match.index > cursor) row.appendChild(document.createTextNode(text.slice(cursor, match.index)));
        const token = match[0];
        const spanClass = tooltipImportantSpanClass(token, line);
        if (spanClass) {
          const span = document.createElement("span");
          span.className = spanClass;
          span.textContent = token;
          row.appendChild(span);
          appended = true;
        } else {
          row.appendChild(document.createTextNode(token));
        }
        cursor = match.index + token.length;
      }
      if (cursor < text.length) row.appendChild(document.createTextNode(text.slice(cursor)));
      if (!appended && !row.childNodes.length) row.textContent = text;
    }

    function tooltipCellText(cell) {
      if (cell && typeof cell === "object") return String(cell.text ?? cell.value ?? "");
      return String(cell ?? "");
    }

    function tooltipCellTone(cell) {
      if (cell && typeof cell === "object") return String(cell.tone || cell.className || "").trim().toLowerCase();
      return "";
    }

    function renderCanvasTooltipTable(tooltip, table) {
      if (!table || typeof table !== "object") return;
      const columns = Array.isArray(table.columns) ? table.columns.map(tooltipCellText).filter(Boolean) : [];
      const rows = Array.isArray(table.rows) ? table.rows : [];
      if (!columns.length || !rows.length) return;
      if (table.title) {
        const title = document.createElement("div");
        title.className = "tooltip-table-title";
        title.textContent = String(table.title);
        tooltip.appendChild(title);
      }
      const grid = document.createElement("table");
      grid.className = "tooltip-table";
      const thead = document.createElement("thead");
      const headRow = document.createElement("tr");
      columns.forEach(column => {
        const th = document.createElement("th");
        th.textContent = column;
        headRow.appendChild(th);
      });
      thead.appendChild(headRow);
      grid.appendChild(thead);
      const tbody = document.createElement("tbody");
      const maxRows = Math.max(1, Number(table.max_rows || 12));
      rows.slice(0, maxRows).forEach(row => {
        const rawCells = Array.isArray(row) ? row : Array.isArray(row?.cells) ? row.cells : [];
        const tr = document.createElement("tr");
        columns.forEach((_, index) => {
          const cell = rawCells[index] ?? "";
          const td = document.createElement("td");
          const tone = tooltipCellTone(cell);
          if (tone) td.classList.add(`tooltip-cell-${tone}`);
          td.textContent = tooltipCellText(cell);
          tr.appendChild(td);
        });
        tbody.appendChild(tr);
      });
      grid.appendChild(tbody);
      tooltip.appendChild(grid);
      if (rows.length > maxRows) {
        const more = document.createElement("div");
        more.className = "tooltip-line tooltip-more";
        more.textContent = `+${rows.length - maxRows} more rows`;
        tooltip.appendChild(more);
      }
    }

    function renderCanvasTooltipLines(tooltip, lines, maxLines) {
      lines.slice(0, maxLines).forEach(line => {
        const lineText = tooltipLineText(line).trim();
        if (/^-{3,}$/.test(lineText) || tooltipLineRole(line) === "separator") {
          const divider = document.createElement("div");
          divider.className = "tooltip-separator";
          tooltip.appendChild(divider);
          return;
        }
        const row = document.createElement("div");
        row.className = tooltipLineClass(line);
        appendHighlightedTooltipLine(row, line);
        tooltip.appendChild(row);
      });
      if (lines.length > maxLines) {
        const more = document.createElement("div");
        more.className = "tooltip-line tooltip-more";
        more.textContent = `+${lines.length - maxLines} more`;
        tooltip.appendChild(more);
      }
    }

    function renderCanvasTooltipContent(tooltip, text) {
      tooltip.replaceChildren();
      if (text && typeof text === "object" && !Array.isArray(text)) {
        const rawLines = Array.isArray(text.lines) ? text.lines : tooltipTextValue(text.text || text.title || "").split(/\n+/);
        const lines = rawLines
          .map(line => line && typeof line === "object" ? { ...line, text: tooltipLineText(line).trim() } : String(line || "").trim())
          .filter(line => tooltipLineText(line).trim());
        renderCanvasTooltipLines(tooltip, lines, Number(text.max_lines || CANVAS_TOOLTIP_DEFAULT_MAX_LINES));
        const tables = Array.isArray(text.tables) ? text.tables : [];
        tables.forEach(table => renderCanvasTooltipTable(tooltip, table));
        return;
      }
      const lines = tooltipTextValue(text).split(/\n+/).map(line => line.trim()).filter(Boolean);
      renderCanvasTooltipLines(tooltip, lines, CANVAS_TOOLTIP_DEFAULT_MAX_LINES);
    }

    function resetCanvasTooltips(chart) {
      if (!state.tooltip) state.tooltip = { price: [], volume: [] };
      state.tooltip[chart] = [];
      if (state.tooltipFront?.[chart]) state.tooltipFront[chart] = [];
    }

    function registerCanvasTooltip(
      chart,
      x,
      y,
      width,
      height,
      text,
      action = null,
      zIndex = canvasLayerZIndex("tooltips"),
      source = "",
    ) {
      if (!text) return;
      const bucket = state.tooltip?.[chart];
      if (!bucket) return;
      bucket.push({
        x1: x - width / 2,
        y1: y - height / 2,
        x2: x + width / 2,
        y2: y + height / 2,
        text,
        action,
        zIndex: Number.isFinite(Number(zIndex)) ? Number(zIndex) : canvasLayerZIndex("tooltips"),
        source: typeof source === "string" ? source : "",
      });
    }

    function clearCanvasTooltipsBySource(chart, sources) {
      const sourceSet = new Set(
        (Array.isArray(sources) ? sources : [sources])
          .filter(source => typeof source === "string" && source),
      );
      if (!sourceSet.size) return;
      if (Array.isArray(state.tooltip?.[chart])) {
        state.tooltip[chart] = state.tooltip[chart].filter(
          item => !sourceSet.has(item?.source),
        );
      }
      if (Array.isArray(state.tooltipFront?.[chart])) {
        state.tooltipFront[chart] = state.tooltipFront[chart].filter(
          item => !sourceSet.has(item?.source),
        );
      }
    }

    function queueCanvasTooltipFront(
      chart,
      x,
      y,
      width,
      height,
      text,
      action = null,
      zIndex = canvasLayerZIndex("tooltips"),
      source = "",
    ) {
      if (!text) return;
      state.tooltipFront = state.tooltipFront || { price: [], volume: [] };
      const bucket = state.tooltipFront[chart];
      if (!bucket) return;
      bucket.push({ chart, x, y, width, height, text, action, zIndex, source });
    }

    function flushCanvasTooltipFront(chart) {
      const bucket = state.tooltipFront?.[chart];
      if (!Array.isArray(bucket) || !bucket.length) return;
      const rows = bucket.splice(0, bucket.length);
      rows.forEach(item => registerCanvasTooltip(
        item.chart,
        item.x,
        item.y,
        item.width,
        item.height,
        item.text,
        item.action,
        item.zIndex,
        item.source,
      ));
    }

    function canvasTooltipHit(chart, x, y) {
      const bucket = state.tooltip?.[chart] || [];
      let best = null;
      for (let index = bucket.length - 1; index >= 0; index -= 1) {
        const box = bucket[index];
        if (x < box.x1 || x > box.x2 || y < box.y1 || y > box.y2) continue;
        const z = Number(box.zIndex || 0);
        if (!best || z > Number(best.zIndex || 0)) best = box;
      }
      return best;
    }

    function canvasActionHit(chart, x, y) {
      const hit = canvasTooltipHit(chart, x, y);
      return hit?.action ? hit : null;
    }

    function resolveCanvasTooltipText(hit) {
      if (!hit) return "";
      if (typeof hit.text !== "function") return hit.text || "";
      if (hit.resolvedText !== undefined) return hit.resolvedText;
      try {
        hit.resolvedText = hit.text() || "";
      } catch (error) {
        hit.resolvedText = "";
        console.warn("canvas tooltip resolver failed", error);
      }
      return hit.resolvedText;
    }

    function canvasTooltipContentKey(text) {
      if (text && typeof text === "object") {
        try {
          return JSON.stringify(text).slice(0, 24000);
        } catch (_) {
          return String(Date.now());
        }
      }
      return String(text || "");
    }

    function hideCanvasTooltip() {
      const tooltip = document.getElementById("canvas-tooltip");
      if (!tooltip) return;
      tooltip.classList.remove("open");
    }

    function showCanvasTooltip(clientX, clientY, text, boundsEl = null) {
      const tooltip = document.getElementById("canvas-tooltip");
      if (!tooltip || !text) {
        hideCanvasTooltip();
        return;
      }
      let rectWidth = Number(tooltip.dataset.tooltipWidth);
      let rectHeight = Number(tooltip.dataset.tooltipHeight);
      const tooltipKey = canvasTooltipContentKey(text);
      if (tooltip.dataset.tooltipText !== tooltipKey || !Number.isFinite(rectWidth) || !Number.isFinite(rectHeight)) {
        renderCanvasTooltipContent(tooltip, text);
        tooltip.dataset.tooltipText = tooltipKey;
        tooltip.style.left = "0px";
        tooltip.style.top = "0px";
        tooltip.style.visibility = "hidden";
        tooltip.classList.add("open");
        const rect = tooltip.getBoundingClientRect();
        rectWidth = rect.width;
        rectHeight = rect.height;
        tooltip.dataset.tooltipWidth = String(rectWidth);
        tooltip.dataset.tooltipHeight = String(rectHeight);
      } else {
        tooltip.classList.add("open");
      }
      const margin = 10;
      const gap = 14;
      const viewportW = Math.max(window.innerWidth || 0, document.documentElement?.clientWidth || 0);
      const viewportH = Math.max(window.innerHeight || 0, document.documentElement?.clientHeight || 0);
      let minLeft = margin;
      let minTop = margin;
      let maxRight = viewportW - margin;
      let maxBottom = viewportH - margin;
      if (boundsEl) {
        const boundsRect = boundsEl.getBoundingClientRect();
        minLeft = Math.max(margin, boundsRect.left + 4);
        minTop = Math.max(margin, boundsRect.top + 4);
        maxRight = Math.min(viewportW - margin, boundsRect.right - 4);
        maxBottom = Math.min(viewportH - margin, boundsRect.bottom - 4);
      }
      const availableHeight = Math.max(48, maxBottom - minTop);
      if (boundsEl && rectHeight > availableHeight) {
        tooltip.style.maxHeight = `${availableHeight}px`;
        tooltip.style.overflowY = "auto";
        tooltip.style.visibility = "hidden";
        const constrainedRect = tooltip.getBoundingClientRect();
        rectHeight = constrainedRect.height;
        tooltip.dataset.tooltipHeight = String(rectHeight);
      } else {
        tooltip.style.maxHeight = "";
        tooltip.style.overflowY = "";
      }
      const maxLeft = Math.max(minLeft, maxRight - rectWidth);
      const maxTop = Math.max(minTop, maxBottom - rectHeight);
      const spaceBelow = maxBottom - clientY;
      const spaceAbove = clientY - minTop;
      let preferAbove;
      if (boundsEl) {
        preferAbove = spaceBelow < rectHeight + gap && spaceAbove >= spaceBelow;
      } else {
        preferAbove = clientY > viewportH * 0.62 || clientY + gap + rectHeight > viewportH - margin;
      }
      const preferLeft = clientX + gap + rectWidth > maxRight || (boundsEl && clientX > (minLeft + maxRight) / 2);
      let left = preferLeft ? clientX - rectWidth - gap : clientX + gap;
      let top = preferAbove ? clientY - rectHeight - gap : clientY + gap;
      left = Math.max(minLeft, Math.min(left, maxLeft));
      top = Math.max(minTop, Math.min(top, maxTop));
      tooltip.style.left = `${left}px`;
      tooltip.style.top = `${top}px`;
      tooltip.style.visibility = "";
    }

    function canvasTooltipBoundsFor(chart) {
      if (chart === "gex-dock") return document.getElementById("gex-dock-surface");
      if (chart === "volume") return document.getElementById("volume-frame");
      return document.getElementById("price-frame");
    }

    function candleCursorTooltip(bar, cursorPrice = null) {
      if (!bar || typeof bar !== "object") return "";
      const live = barIsLivePreview(bar);
      const confirmed = barIsConfirmedClosed(bar);
      const hasPrices = hasPriceBar(bar);
      const lifecycle = String(bar.state || "").trim().replace(/_/g, " ").toUpperCase();
      let status = "PROVISIONAL";
      let statusTone = "warning";
      if (confirmed) {
        status = "CONFIRMED";
        statusTone = "positive";
      } else if (live && bar.state === "forming") {
        status = "LIVE / FORMING";
        statusTone = "positive";
      } else if (live && bar.state === "awaiting_provider_confirmation") {
        status = "AWAITING PROVIDER CONFIRMATION";
      } else if (lifecycle) {
        status = lifecycle;
      }
      const time = axisTimeFormatter.format(new Date(bar.ts));
      const lines = [tooltipLine(`CANDLE ${time}`, "title")];
      if (hasPrices) {
        const range = Number(bar.high) - Number(bar.low);
        const body = Math.abs(Number(bar.close) - Number(bar.open));
        lines.push(
          tooltipPriceLine("OPEN", bar.open, "meta"),
          tooltipPriceLine("HIGH", bar.high, "meta"),
          tooltipPriceLine("LOW", bar.low, "meta"),
          tooltipPriceLine("CLOSE", bar.close, "meta"),
          tooltipLabeledLine(
            "VOLUME",
            Number.isFinite(Number(bar.volume))
              ? Math.round(Number(bar.volume)).toLocaleString("en-US")
              : "-",
            "meta",
            "muted",
          ),
          tooltipLabeledLine("STATUS", status, "meta", statusTone),
          tooltipPriceLine("RANGE", range, "meta"),
          tooltipPriceLine("BODY", body, "meta"),
        );
        if (Number.isFinite(Number(cursorPrice))) {
          lines.push(tooltipPriceLine("CURSOR", cursorPrice, "meta"));
        }
      } else {
        lines.push(tooltipLabeledLine("STATUS", status, "meta", statusTone));
      }
      return { lines };
    }

    function updateCanvasTooltip(canvasId, localX, localY, clientX, clientY) {
      if (typeof chartInteractionQualityActive === "function" && chartInteractionQualityActive()) {
        hideCanvasTooltip();
        return;
      }
      const chart = canvasId === "gex-dock-canvas" ? "gex-dock"
        : canvasId === "volume-chart" || canvasId === "volume-interaction" ? "volume" : "price";
      const hit = canvasTooltipHit(chart, localX, localY);
      if (hit) {
        showCanvasTooltip(clientX, clientY, resolveCanvasTooltipText(hit), canvasTooltipBoundsFor(chart));
        return;
      }
      const informative = (
        state.drawing.tool === "cursor"
        && normalizeCursorMode(state.drawing.cursorMode) === "informative"
      );
      const geometry = informative && chart === "price" ? priceChartGeometry() : null;
      const volumeCanvas = informative && chart === "volume"
        ? document.getElementById(canvasId)
        : null;
      const volumeRect = volumeCanvas?.getBoundingClientRect();
      const insideCandlePlot = Boolean(
        informative
        && (
          (
            chart === "price"
            && geometry
            && localPointInsidePricePlot(localX, localY, geometry)
          )
          || (
            chart === "volume"
            && volumeRect
            && localX >= CHART_LEFT_PAD
            && localX <= volumeRect.width - PRICE_AXIS_WIDTH
            && localY >= 4
            && localY <= volumeRect.height - 4
          )
        )
      );
      const visible = insideCandlePlot ? visibleBars(state.snapshot) : null;
      const index = Number(state.crosshair.index);
      const bar = Number.isInteger(index) && index >= 0 && index < (visible?.bars?.length || 0)
        ? visible.bars[index]
        : null;
      if (bar) {
        showCanvasTooltip(
          clientX,
          clientY,
          candleCursorTooltip(bar, state.crosshair.price),
          canvasTooltipBoundsFor(chart),
        );
        return;
      }
      hideCanvasTooltip();
    }
