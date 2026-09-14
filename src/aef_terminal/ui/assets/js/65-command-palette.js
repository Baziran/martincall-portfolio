    const COMMAND_PALETTE_RESULT_LIMIT = 50;
    const commandPaletteState = {
      open: false,
      actions: [],
      filtered: [],
      activeIndex: 0,
      previousFocus: null,
    };

    function commandPaletteSearchText(value) {
      return String(value || "").trim().toLowerCase();
    }

    function commandPaletteClickDataButton(attribute, value) {
      const button = Array.from(document.querySelectorAll(`[data-${attribute}]`))
        .find(node => String(node.getAttribute(`data-${attribute}`) || "") === value);
      button?.click();
    }

    function commandPaletteActions() {
      const actions = [
        {
          id: "chart:go-live",
          group: "Chart",
          label: "Go Live",
          keywords: ["latest", "realtime", "follow", "market tail"],
          run: () => setFollowLatest(true, { refreshTail: true }),
        },
        {
          id: "chart:reset-view",
          group: "Chart",
          label: "Reset chart view",
          keywords: ["fit", "zoom", "viewport", "default"],
          run: () => resetView(),
        },
        {
          id: "chart:view-classic",
          group: "Chart view",
          label: "Classic chart",
          keywords: ["classic", "indicators", "full labels"],
          run: () => setChartViewMode("classic"),
        },
        {
          id: "chart:view-advisor",
          group: "Chart view",
          label: "Advisor chart",
          keywords: ["advisor", "trade center", "focused"],
          run: () => setChartViewMode("advisor"),
        },
        {
          id: "panel:settings",
          group: "Navigation",
          label: "Open settings",
          keywords: ["preferences", "configuration", "theme"],
          run: () => {
            if (!state.settings?.open) document.getElementById("settings-toggle")?.click();
          },
        },
        ...[
          ["instruments", "Instruments", ["watchlist", "symbols"]],
          ["indicators", "Indicators", ["signals", "studies"]],
          ["ai", "AI Advisor", ["codex", "discussion", "third opinion"]],
          ["alerts", "Alerts", ["notifications", "attention"]],
          ["go", "GO", ["paper", "trade center"]],
        ].map(([tab, label, keywords]) => ({
          id: `panel:${tab}`,
          group: "Navigation",
          label: `Open ${label}`,
          keywords,
          run: () => commandPaletteClickDataButton("side-tab", tab),
        })),
      ];

      for (const timeframe of TIMEFRAMES) {
        const interval = String(timeframe?.interval || "");
        if (!interval) continue;
        actions.push({
          id: `timeframe:${interval}`,
          group: "Timeframe",
          label: `Timeframe ${String(timeframe?.label || interval).toUpperCase()}`,
          keywords: [interval, "interval", "chart period"],
          run: () => commandPaletteClickDataButton("timeframe", interval),
        });
      }

      document.querySelectorAll("#view-preset-select option").forEach(option => {
        const preset = String(option.value || "").trim();
        if (!preset) return;
        actions.push({
          id: `preset:${preset}`,
          group: "View preset",
          label: `Preset ${String(option.textContent || preset).trim()}`,
          keywords: [preset, "layout", "workspace"],
          run: () => applyViewPresetSelection(preset),
        });
      });

      document.querySelectorAll("[data-drawing-tool]").forEach(button => {
        const tool = String(button.dataset.drawingTool || "").trim();
        if (!tool) return;
        const title = tool === "line"
          ? "Trend line"
          : String(button.title || tool).trim();
        actions.push({
          id: `drawing:${tool}`,
          group: "Drawing",
          label: title,
          keywords: [tool, "drawing", "annotation"],
          run: () => {
            if (tool === "line") setDrawingLineMode("line");
            else setDrawingTool(tool);
          },
        });
      });
      actions.push({
        id: "drawing:ruler",
        group: "Drawing",
        label: "Ruler",
        keywords: ["ruler", "measure", "bars", "percent", "drawing", "annotation"],
        run: () => setDrawingLineMode("ruler"),
      });

      (state.instruments || []).forEach(instrument => {
        const instrumentId = exactIdentityText(instrument?.instrument_id);
        if (!instrumentId) return;
        const display = String(
          instrument?.display || instrument?.key || instrument?.provider_symbol || "Instrument",
        ).trim() || "Instrument";
        const provider = String(instrument?.provider || "").trim();
        actions.push({
          id: `instrument:${instrumentId}`,
          group: "Instrument",
          label: provider ? `${display} · ${provider.toUpperCase()}` : display,
          keywords: [
            instrument?.display,
            instrument?.key,
            instrument?.provider_symbol,
            instrument?.provider,
            instrument?.asset_class,
          ],
          run: () => switchWatchlistInstrument(instrumentId),
        });
      });

      return actions;
    }

    function commandPaletteFilteredActions(query) {
      const terms = commandPaletteSearchText(query).split(/\s+/).filter(Boolean);
      if (!terms.length) return commandPaletteState.actions.slice(0, COMMAND_PALETTE_RESULT_LIMIT);
      return commandPaletteState.actions.filter(action => {
        const haystack = commandPaletteSearchText([
          action.group,
          action.label,
          ...(Array.isArray(action.keywords) ? action.keywords : []),
        ].filter(Boolean).join(" "));
        return terms.every(term => haystack.includes(term));
      }).slice(0, COMMAND_PALETTE_RESULT_LIMIT);
    }

    function setCommandPaletteActive(index, options = {}) {
      const count = commandPaletteState.filtered.length;
      const input = document.getElementById("command-palette-input");
      if (!count) {
        commandPaletteState.activeIndex = 0;
        input?.removeAttribute("aria-activedescendant");
        return;
      }
      commandPaletteState.activeIndex = ((Number(index) || 0) + count) % count;
      document.querySelectorAll(".command-palette-option").forEach((node, optionIndex) => {
        const active = optionIndex === commandPaletteState.activeIndex;
        node.classList.toggle("active", active);
        node.setAttribute("aria-selected", active ? "true" : "false");
        if (active) {
          input?.setAttribute("aria-activedescendant", node.id);
          if (options.scroll !== false) node.scrollIntoView({ block: "nearest" });
        }
      });
    }

    function renderCommandPalette(query = "") {
      const results = document.getElementById("command-palette-results");
      const status = document.getElementById("command-palette-status");
      if (!results) return;
      commandPaletteState.filtered = commandPaletteFilteredActions(query);
      commandPaletteState.activeIndex = 0;
      results.replaceChildren();

      if (!commandPaletteState.filtered.length) {
        const empty = document.createElement("div");
        empty.className = "command-palette-empty";
        empty.textContent = "No matching commands";
        results.appendChild(empty);
        document.getElementById("command-palette-input")?.removeAttribute("aria-activedescendant");
        if (status) status.textContent = "No commands";
        return;
      }

      commandPaletteState.filtered.forEach((action, index) => {
        const option = document.createElement("button");
        option.type = "button";
        option.id = `command-palette-option-${index}`;
        option.className = "command-palette-option";
        option.setAttribute("role", "option");
        option.setAttribute("aria-selected", "false");
        option.tabIndex = -1;

        const label = document.createElement("span");
        label.className = "command-palette-option-label";
        label.textContent = action.label;
        const group = document.createElement("span");
        group.className = "command-palette-option-group";
        group.textContent = action.group;
        option.append(label, group);
        option.addEventListener("pointerenter", () => setCommandPaletteActive(index, { scroll: false }));
        option.addEventListener("click", () => runCommandPaletteAction(index));
        results.appendChild(option);
      });

      if (status) {
        const count = commandPaletteState.filtered.length;
        status.textContent = `${count} command${count === 1 ? "" : "s"}`;
      }
      setCommandPaletteActive(0, { scroll: false });
    }

    function closeCommandPalette(options = {}) {
      if (!commandPaletteState.open) return;
      const root = document.getElementById("command-palette");
      const input = document.getElementById("command-palette-input");
      commandPaletteState.open = false;
      root?.classList.remove("open");
      if (root) root.hidden = true;
      input?.setAttribute("aria-expanded", "false");
      input?.removeAttribute("aria-activedescendant");
      const previousFocus = commandPaletteState.previousFocus;
      commandPaletteState.previousFocus = null;
      commandPaletteState.actions = [];
      commandPaletteState.filtered = [];
      if (options.restoreFocus !== false && previousFocus?.isConnected) {
        previousFocus.focus({ preventScroll: true });
      }
    }

    function openCommandPalette() {
      const root = document.getElementById("command-palette");
      const input = document.getElementById("command-palette-input");
      if (!root || !input) return;
      if (commandPaletteState.open) {
        input.focus({ preventScroll: true });
        input.select();
        return;
      }
      commandPaletteState.previousFocus = document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
      commandPaletteState.open = true;
      commandPaletteState.actions = commandPaletteActions();
      input.value = "";
      input.setAttribute("aria-expanded", "true");
      root.hidden = false;
      root.classList.add("open");
      renderCommandPalette();
      window.requestAnimationFrame(() => {
        if (!commandPaletteState.open) return;
        input.focus({ preventScroll: true });
      });
    }

    function reportCommandPaletteFailure(action, error) {
      console.error(`Command palette action failed: ${action?.id || "unknown"}`, error);
      if (typeof showBrowserToast === "function") {
        showBrowserToast(`${action?.label || "Command"} failed`);
      }
    }

    function runCommandPaletteAction(index = commandPaletteState.activeIndex) {
      const action = commandPaletteState.filtered[index];
      if (!action || typeof action.run !== "function") return;
      closeCommandPalette();
      try {
        const result = action.run();
        if (result && typeof result.then === "function") {
          result.catch(error => reportCommandPaletteFailure(action, error));
        }
      } catch (error) {
        reportCommandPaletteFailure(action, error);
      }
    }

    function handleCommandPaletteKeydown(event) {
      const key = String(event.key || "").toLowerCase();
      const code = String(event.code || "");
      const paletteShortcut = (event.metaKey || event.ctrlKey)
        && !event.altKey
        && !event.shiftKey
        && (key === "k" || code === "KeyK");
      if (paletteShortcut) {
        event.preventDefault();
        event.stopPropagation();
        if (commandPaletteState.open) closeCommandPalette();
        else openCommandPalette();
        return;
      }
      if (!commandPaletteState.open) return;
      if (event.isComposing) return;
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        closeCommandPalette();
        return;
      }
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        event.stopPropagation();
        setCommandPaletteActive(
          commandPaletteState.activeIndex + (event.key === "ArrowDown" ? 1 : -1),
        );
        return;
      }
      if (event.key === "Enter") {
        event.preventDefault();
        event.stopPropagation();
        runCommandPaletteAction();
        return;
      }
      if (event.key === "Tab") {
        event.preventDefault();
        event.stopPropagation();
        document.getElementById("command-palette-input")?.focus({ preventScroll: true });
      }
    }

    function setupCommandPalette() {
      const root = document.getElementById("command-palette");
      const input = document.getElementById("command-palette-input");
      if (!root || !input || root.dataset.bound === "1") return;
      root.dataset.bound = "1";
      input.addEventListener("input", event => renderCommandPalette(event.target.value));
      root.addEventListener("pointerdown", event => {
        if (event.target === root) closeCommandPalette();
      });
      document.addEventListener("keydown", handleCommandPaletteKeydown, true);
    }

    setupCommandPalette();
