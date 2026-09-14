    function indicatorRegistry() {
      return window.INDICATOR_REGISTRY_MANIFEST || {};
    }

    function indicatorSpec(id) {
      return indicatorRegistry()[id] || {};
    }

    function indicatorUiSpec(id) {
      return indicatorSpec(id).ui || {};
    }

    function indicatorControlSpecs(id) {
      const controls = indicatorSpec(id).controls;
      return Array.isArray(controls) ? controls : [];
    }

    const indicatorProcessEffectRegistry = new Map();
    const indicatorControlEffectRegistry = new Map();
    const indicatorLifecycleSyncRegistry = new Map();
    const indicatorSettingsSyncRegistry = new Map();
    const indicatorSidebarRendererRegistry = new Map();
    const indicatorPanelRendererRegistry = new Map();
    const indicatorOverlayContributionRegistry = new Map();
    const indicatorDrawingDecoratorRegistry = new Map();
    const indicatorCanvasHookRegistry = new Map();

    function registerIndicatorProcessEffect(ref, handler) {
      const key = String(ref || "").trim();
      if (!key || typeof handler !== "function") throw new Error(`Invalid indicator process effect: ${key || "missing"}`);
      indicatorProcessEffectRegistry.set(key, handler);
    }

    function registerIndicatorControlEffect(ref, handler) {
      const key = String(ref || "").trim();
      if (!key || typeof handler !== "function") throw new Error(`Invalid indicator control effect: ${key || "missing"}`);
      indicatorControlEffectRegistry.set(key, handler);
    }

    function registerIndicatorLifecycleSync(indicatorId, handler, options = {}) {
      const key = String(indicatorId || "").trim();
      if (!key || typeof handler !== "function") throw new Error(`Invalid indicator lifecycle sync: ${key || "missing"}`);
      indicatorLifecycleSyncRegistry.set(key, {
        handler,
        poll: options.poll === true,
      });
    }

    async function syncIndicatorModuleLifecycles(options = {}) {
      const entries = Array.from(indicatorLifecycleSyncRegistry.entries())
        .filter(([, entry]) => !options.poll || entry.poll);
      const results = await Promise.allSettled(
        entries.map(([, entry]) => Promise.resolve().then(() => entry.handler(options)))
      );
      results.forEach((result, index) => {
        if (result.status !== "rejected") return;
        console.warn(
          `indicator lifecycle failed: ${entries[index][0]}`,
          result.reason,
        );
      });
    }

    function registerIndicatorSettingsSync(indicatorId, handler) {
      const key = String(indicatorId || "").trim();
      if (!key || typeof handler !== "function") throw new Error(`Invalid indicator settings sync: ${key || "missing"}`);
      indicatorSettingsSyncRegistry.set(key, handler);
    }

    function syncIndicatorModuleSettings(options = {}) {
      indicatorSettingsSyncRegistry.forEach(handler => handler(options));
    }

    function registerIndicatorSidebarRenderer(sidebarId, renderer) {
      const key = String(sidebarId || "").trim();
      if (!key || typeof renderer !== "function") throw new Error(`Invalid indicator sidebar renderer: ${key || "missing"}`);
      indicatorSidebarRendererRegistry.set(key, renderer);
    }

    function registerIndicatorPanelRenderer(indicatorId, renderer) {
      const key = String(indicatorId || "").trim();
      if (!indicatorSpec(key)?.id || typeof renderer !== "function") {
        throw new Error(`Invalid indicator panel renderer: ${key || "missing"}`);
      }
      indicatorPanelRendererRegistry.set(key, renderer);
    }

    function renderIndicatorExtensionPanels(snapshot = state.snapshot) {
      indicatorPanelRendererRegistry.forEach((renderer, indicatorId) => {
        const settingsRow = document.querySelector(
          `[data-generated-indicator-id="${indicatorId}"]`
        );
        const controls = settingsRow?.querySelector(".indicator-controls") || null;
        renderer(snapshot, {
          calcEnabled: indicatorCalcForId(indicatorId),
          visible: indicatorVisibleForId(indicatorId),
          settingsRow,
          controls,
          mount(key, className = "indicator-note") {
            if (!controls) return null;
            const panelKey = String(key || "default").trim() || "default";
            let node = Array.from(
              controls.querySelectorAll("[data-indicator-panel-key]")
            ).find(item => item.dataset.indicatorPanelKey === panelKey);
            if (!node) {
              node = document.createElement("div");
              node.dataset.indicatorPanelKey = panelKey;
              controls.appendChild(node);
            }
            node.className = className;
            return node;
          },
        });
      });
    }

    function registerIndicatorOverlayContribution(source, contribution) {
      const key = String(source || "").trim();
      if (!key || !contribution || typeof contribution.collect !== "function") {
        throw new Error(`Invalid indicator overlay contribution: ${key || "missing"}`);
      }
      indicatorOverlayContributionRegistry.set(key, { ...contribution, source: key });
    }

    function indicatorOverlayContributionStateKey() {
      return Array.from(indicatorOverlayContributionRegistry.entries())
        .map(([source, contribution]) => (
          `${source}:${typeof contribution.stateKey === "function" ? contribution.stateKey() : ""}`
        ))
        .join("|");
    }

    function registerIndicatorCanvasHook(indicatorId, hook, handler, options = {}) {
      const owner = String(indicatorId || "").trim();
      const hookName = String(hook || "").trim();
      if (
        !indicatorSpec(owner)?.id
        || !hookName
        || typeof handler !== "function"
      ) {
        throw new Error(`Invalid indicator canvas hook: ${owner || "missing"}:${hookName || "missing"}`);
      }
      const entries = indicatorCanvasHookRegistry.get(hookName) || new Map();
      if (entries.has(owner)) {
        throw new Error(`Duplicate indicator canvas hook: ${owner}:${hookName}`);
      }
      entries.set(owner, {
        handler,
        requireCalc: options.requireCalc !== false,
        requireVisible: options.requireVisible !== false,
        stateKey: typeof options.stateKey === "function" ? options.stateKey : null,
      });
      indicatorCanvasHookRegistry.set(hookName, entries);
    }

    function orderedIndicatorCanvasHookEntries(hook) {
      const entries = indicatorCanvasHookRegistry.get(String(hook || "").trim());
      if (!entries) return [];
      return Array.from(entries.entries()).sort((left, right) => {
        const leftOrder = numericValueOrFallback(indicatorUiSpec(left[0]).runtime_order, 500);
        const rightOrder = numericValueOrFallback(indicatorUiSpec(right[0]).runtime_order, 500);
        return leftOrder - rightOrder || left[0].localeCompare(right[0]);
      });
    }

    function runIndicatorCanvasHooks(hook, payload = {}) {
      for (const [indicatorId, entry] of orderedIndicatorCanvasHookEntries(hook)) {
        if (entry.requireCalc && !indicatorCalcForId(indicatorId)) continue;
        if (entry.requireVisible && indicatorVisibleForId(indicatorId) === false) continue;
        try {
          entry.handler(payload);
        } catch (error) {
          console.error(`Indicator canvas hook failed: ${indicatorId}:${hook}`, error);
        }
      }
    }

    function indicatorCanvasHookStateKey() {
      return Array.from(indicatorCanvasHookRegistry.entries())
        .flatMap(([hook, entries]) => Array.from(entries.entries()).map(([indicatorId, entry]) => {
          const calcEnabled = indicatorCalcForId(indicatorId);
          const visible = indicatorVisibleForId(indicatorId) !== false;
          const prefix = [
            hook,
            indicatorId,
            calcEnabled ? "calc1" : "calc0",
            visible ? "vis1" : "vis0",
          ];
          if (
            (entry.requireCalc && !calcEnabled)
            || (entry.requireVisible && !visible)
          ) {
            return [...prefix, "inactive"].join(":");
          }
          try {
            return [
              ...prefix,
              entry.stateKey ? String(entry.stateKey() || "") : "",
            ].join(":");
          } catch (error) {
            console.error(
              `Indicator canvas state key failed: ${indicatorId}:${hook}`,
              error,
            );
            return [...prefix, "state_error"].join(":");
          }
        }))
        .join("|");
    }

    function reportIndicatorDrawingDecoratorError(indicatorId, phase, error) {
      const message = error?.message || String(error || "unknown error");
      console.error(`Indicator drawing decorator failed: ${indicatorId}:${phase}`, error);
      if (typeof debugStep === "function") {
        debugStep("indicator drawing decorator failed", `${indicatorId}:${phase}: ${message}`);
      }
    }

    function registerIndicatorDrawingDecorator(indicatorId, decorator) {
      const key = String(indicatorId || "").trim();
      if (
        !indicatorSpec(key)?.id
        || !decorator
        || typeof decorator !== "object"
        || typeof decorator.decorate !== "function"
      ) {
        throw new Error(`Invalid indicator drawing decorator: ${key || "missing"}`);
      }
      if (indicatorDrawingDecoratorRegistry.has(key)) {
        throw new Error(`Duplicate indicator drawing decorator: ${key}`);
      }
      indicatorDrawingDecoratorRegistry.set(key, decorator);
    }

    function orderedIndicatorDrawingDecoratorEntries() {
      return Array.from(indicatorDrawingDecoratorRegistry.entries())
        .sort((left, right) => {
          const leftOrder = numericValueOrFallback(indicatorUiSpec(left[0]).runtime_order, 500);
          const rightOrder = numericValueOrFallback(indicatorUiSpec(right[0]).runtime_order, 500);
          return leftOrder - rightOrder || left[0].localeCompare(right[0]);
        });
    }

    function beginIndicatorDrawingDecoratorFrame(env = {}) {
      const entries = [];
      for (const [indicatorId, decorator] of orderedIndicatorDrawingDecoratorEntries()) {
        if (!indicatorCalcForId(indicatorId) || indicatorVisibleForId(indicatorId) === false) continue;
        try {
          entries.push({
            indicatorId,
            decorator,
            state: typeof decorator.begin === "function" ? decorator.begin(env) : null,
            failed: false,
          });
        } catch (error) {
          reportIndicatorDrawingDecoratorError(indicatorId, "begin", error);
        }
      }
      return { env, entries, closed: false };
    }

    function runIndicatorDrawingDecorators(frame, event, payload = {}) {
      if (!frame || frame.closed || !Array.isArray(frame.entries)) return;
      const eventName = String(event || "").trim();
      if (!eventName) return;
      for (const entry of frame.entries) {
        if (entry.failed) continue;
        try {
          entry.decorator.decorate(eventName, payload, entry.state, frame.env);
        } catch (error) {
          entry.failed = true;
          reportIndicatorDrawingDecoratorError(entry.indicatorId, eventName, error);
        }
      }
    }

    function endIndicatorDrawingDecoratorFrame(frame) {
      if (!frame || frame.closed || !Array.isArray(frame.entries)) return;
      frame.closed = true;
      for (const entry of frame.entries) {
        if (entry.failed || typeof entry.decorator.end !== "function") continue;
        try {
          entry.decorator.end(frame.env, entry.state);
        } catch (error) {
          reportIndicatorDrawingDecoratorError(entry.indicatorId, "end", error);
        }
      }
    }

    function notifyIndicatorDrawingCommit(payload = {}) {
      for (const [indicatorId, decorator] of orderedIndicatorDrawingDecoratorEntries()) {
        if (!indicatorCalcForId(indicatorId) || typeof decorator.commit !== "function") continue;
        try {
          const result = decorator.commit(payload);
          if (result && typeof result.catch === "function") {
            void result.catch(error => {
              reportIndicatorDrawingDecoratorError(indicatorId, "commit", error);
            });
          }
        } catch (error) {
          reportIndicatorDrawingDecoratorError(indicatorId, "commit", error);
        }
      }
    }

    const indicatorTableActionRegistry = new Map();

    function registerIndicatorTableAction(action, handlers) {
      const key = String(action || "").trim();
      if (!key || !handlers || typeof handlers !== "object") throw new Error(`Invalid indicator table action: ${key || "missing"}`);
      indicatorTableActionRegistry.set(key, handlers);
    }

    function indicatorTableAction(action) {
      const key = String(action || "").trim();
      if (!key) return null;
      const entry = indicatorTableActionRegistry.get(key);
      if (!entry) throw new Error(`Unknown indicator table action: ${key}`);
      return entry;
    }

    function indicatorTableActionCells(action, col, table, overlay, fallbackCells) {
      const entry = indicatorTableAction(action);
      return typeof entry?.cells === "function" ? entry.cells(col, table, overlay) : fallbackCells;
    }

    function runIndicatorTableAction(action, payload = {}) {
      const entry = indicatorTableAction(action);
      if (typeof entry?.click !== "function") return false;
      entry.click(payload);
      return true;
    }

    const indicatorTableThinkingRefRegistry = new Map();

    function registerIndicatorTableThinkingRef(ref, isActive) {
      const key = String(ref || "").trim();
      if (!key || typeof isActive !== "function") throw new Error(`Invalid indicator table thinking ref: ${key || "missing"}`);
      indicatorTableThinkingRefRegistry.set(key, isActive);
    }

    function indicatorTableThinkingRefActive(ref) {
      const key = String(ref || "").trim();
      if (!key) return false;
      const isActive = indicatorTableThinkingRefRegistry.get(key);
      if (!isActive) throw new Error(`Unknown indicator table thinking ref: ${key}`);
      return Boolean(isActive());
    }

    function indicatorRendererRegistry() {
      return Object.fromEntries(
        Object.entries(indicatorRegistry()).map(([id, spec]) => [
          id,
          {
            kind: spec.renderer_kind || "generic",
            ref: spec.renderer_ref || "generic_overlay_renderer",
            overlayContract: spec.overlay_contract || "overlay-contract-v1",
            overlayLayer: spec.overlay_layer || "signals",
            overlayFilterRef: spec.overlay_filter_ref || "generic_overlay_filter",
            overlayCollect: spec.overlay_collect !== false,
            tableContract: spec.table_contract || "",
            statusContract: spec.status_contract || "indicator-status-v1",
            rendererContract: spec.renderer_contract || {},
            fallback: spec.renderer_kind === "generic" || !spec.renderer_ref,
          },
        ])
      );
    }

    const INDICATOR_RENDERER_REGISTRY = indicatorRendererRegistry();

    function indicatorControlDomHealth(id) {
      const controls = indicatorControlSpecs(id).filter(control => control?.element_id);
      const missing = controls
        .filter(control => !document.getElementById(control.element_id))
        .map(control => control.element_id);
      return { total: controls.length, missing };
    }

    function annotateIndicatorControlsFromManifest(root = document) {
      Object.entries(indicatorRegistry()).forEach(([indicatorId]) => {
        indicatorControlSpecs(indicatorId).forEach(control => {
          if (!control?.element_id) return;
          const node = root.getElementById ? root.getElementById(control.element_id) : document.getElementById(control.element_id);
          if (!node) return;
          node.dataset.indicatorId = indicatorId;
          node.dataset.indicatorControlKey = control.key || "";
          node.dataset.indicatorStorageKey = control.storage_key || "";
          node.dataset.indicatorControlAction = control.action || "apply";
          node.dataset.indicatorControlType = control.control_type || "";
          node.dataset.indicatorControlScope = control.scope;
          node.dataset.indicatorControlCompact = control.compact === true ? "1" : "0";
          const label = node.closest?.("label");
          if (label) {
            label.dataset.indicatorControlCompact = node.dataset.indicatorControlCompact;
            if (control.label && !label.dataset.label) label.dataset.label = control.label;
          }
        });
      });
    }

    function indicatorStateForId(id) {
      const stateKey = indicatorUiSpec(id).state_key;
      return stateKey ? state.indicators?.[stateKey] : null;
    }

    function indicatorVisibleForId(id) {
      const group = indicatorStateForId(id);
      const ui = indicatorUiSpec(id);
      if (group && ui.visible_key && ui.visible_key === ui.calc_key) return group.enabled !== false;
      return group ? group.visible !== false : ui.default_visible !== false;
    }

    function indicatorCalcForId(id) {
      const group = indicatorStateForId(id);
      return group ? group.enabled !== false : indicatorUiSpec(id).default_calc !== false;
    }

    const INDICATOR_DEPENDENCY_LINKS = Object.fromEntries(
      Object.entries(indicatorRegistry())
        .map(([id, spec]) => {
          const dependencyIds = [...(spec.depends_on || []), ...(spec.optional_context || [])]
            .map(item => String(item || "").trim())
            .filter(Boolean);
          const links = dependencyIds
            .map(linkId => {
              const dependency = indicatorSpec(linkId);
              const dependencyStateKey = dependency?.ui?.state_key;
              return {
                dependencyId: linkId,
                dependencyLabel: dependency?.label || linkId,
                stateKey: dependencyStateKey || "",
                registered: Boolean(dependencyStateKey),
              };
            })
          return links.length ? [id, links] : null;
        })
        .filter(Boolean)
    );

    const INDICATOR_PROCESS_IDS = {
      ...Object.fromEntries(
        Object.entries(indicatorRegistry())
          .filter(([, spec]) => spec?.ui?.process_control_id)
          .map(([id, spec]) => [id, spec.ui.process_control_id])
      ),
    };

    const RUNTIME_INDICATOR_ROWS = [
      ...Object.entries(indicatorRegistry())
        .filter(([, spec]) => spec?.ui?.show_in_runtime !== false && spec?.ui?.chart_control_id)
        .sort((a, b) => (
          numericValueOrFallback(a[1]?.ui?.runtime_order, 500)
          - numericValueOrFallback(b[1]?.ui?.runtime_order, 500)
        ))
        .map(([id, spec]) => [
          id,
          spec.ui.chart_control_id,
          () => indicatorVisibleForId(id),
          () => indicatorCalcForId(id),
        ]),
    ];

    function indicatorLinkBadgeHtml(indicatorId, className = "indicator-settings-link-badge") {
      const links = INDICATOR_DEPENDENCY_LINKS[indicatorId];
      if (!Array.isArray(links) || !links.length) return "";
      const states = links.map(link => {
        const depState = link.stateKey ? state.indicators?.[link.stateKey] : null;
        return {
          ...link,
          linked: link.registered && depState?.enabled !== false,
        };
      });
      const linked = states.every(item => item.linked);
      const linkedLabels = states.filter(item => item.linked).map(item => item.dependencyLabel);
      const soloLabels = states.filter(item => !item.linked).map(item => item.dependencyLabel);
      const titleParts = [
        linkedLabels.length ? `Linked: ${linkedLabels.join(", ")}` : "",
        soloLabels.length
          ? `Solo: ${soloLabels.join(", ")} ${states.some(item => !item.registered) ? "uninstalled / " : ""}Calc OFF`
          : "",
      ].filter(Boolean);
      if (linked) {
        return `<span class="${className} ok" title="${escapeHtml(titleParts.join(" · "))}">LINK</span>`;
      }
      return `<span class="${className} bad" title="${escapeHtml(titleParts.join(" · "))}">SOLO</span>`;
    }

    function renderIndicatorSettingsLinkBadges() {
      document.querySelectorAll(".indicator-settings-link-badge-wrap[data-indicator-id]").forEach(node => {
        const html = indicatorLinkBadgeHtml(node.dataset.indicatorId);
        node.innerHTML = html;
        node.hidden = !html;
      });
    }

    function indicatorIdForProcessControl(processId) {
      return Object.entries(INDICATOR_PROCESS_IDS).find(([, id]) => id === processId)?.[0] || "";
    }

    function runtimeIndicatorLabel(id, status) {
      const preferred = indicatorSpec(id).label;
      return preferred || status?.label || id;
    }

    function indicatorSignalSummary(indicator) {
      if (!indicator || typeof indicator !== "object") return null;
      const latest = indicator.latest && typeof indicator.latest === "object" ? indicator.latest : {};
      const rawLifecycle = latest.lifecycle && typeof latest.lifecycle === "object" ? latest.lifecycle : null;
      const lifecycle = rawLifecycle?.active ? rawLifecycle : null;
      const latestSignal = latest.signal && typeof latest.signal === "object" ? latest.signal : null;
      const signal = latestSignal && !latestSignal.obsolete ? latestSignal : null;
      if (!signal && !lifecycle) return null;
      const action = String(lifecycle?.state || signal?.action || "").toUpperCase();
      const direction = String(lifecycle?.direction || signal?.direction || "").toLowerCase();
      const entry = Number(lifecycle?.entry ?? signal?.trigger);
      const stop = Number(lifecycle?.stop ?? signal?.stop);
      const target = Number(lifecycle?.target ?? signal?.target);
      const hasDirectionalSignal = direction === "long" || direction === "short";
      const hasPlan = Number.isFinite(entry) || Number.isFinite(stop) || Number.isFinite(target);
      if (signal && !lifecycle && !hasDirectionalSignal && !hasPlan) return null;
      const rr = Number(signal?.rr);
      const score = Number(signal?.score);
      return {
        action,
        direction,
        entry: Number.isFinite(entry) ? entry : null,
        stop: Number.isFinite(stop) ? stop : null,
        target: Number.isFinite(target) ? target : null,
        rr: Number.isFinite(rr) ? rr : null,
        score: Number.isFinite(score) ? score : null,
        lifecycle,
        lifecycleActive: Boolean(lifecycle?.active),
        lifecycleFilled: Boolean(lifecycle?.filled),
        code: String(signal?.code || ""),
        reason: String(lifecycle?.reason || signal?.reason_code || signal?.reason || ""),
      };
    }

    function normalizeRuntimeAction(action) {
      const text = String(action || "").trim().toUpperCase();
      if (!text || text === "-") return "";
      return ["WAIT", "WATCH", "CANDIDATE", "ARM", "GO", "IN", "TRAIL", "BLOCK"].includes(text)
        ? text
        : "";
    }

    const indicatorRuntimeStateRefRegistry = new Map();

    function registerIndicatorRuntimeStateRef(ref, resolver) {
      const key = String(ref || "").trim();
      if (!key || typeof resolver !== "function") throw new Error(`Invalid indicator runtime state ref: ${key || "missing"}`);
      indicatorRuntimeStateRefRegistry.set(key, resolver);
    }

    function indicatorRuntimeStateForRef(ref, snapshot) {
      const key = String(ref || "").trim();
      if (!key) return null;
      const resolver = indicatorRuntimeStateRefRegistry.get(key);
      if (!resolver) throw new Error(`Unknown indicator runtime state ref: ${key}`);
      return resolver(snapshot);
    }

    function refreshIndicatorSettingsStatusBadges(snapshot = state.snapshot) {
      document.querySelectorAll(".indicator-settings-status[data-settings-status-ref]").forEach(node => {
        const ref = String(node.dataset.settingsStatusRef || "").trim();
        let runtimeState = null;
        try {
          runtimeState = indicatorRuntimeStateForRef(ref, snapshot);
        } catch (error) {
          runtimeState = {
            text: "ERROR",
            actionClass: "bad",
            details: error?.message || `Unknown settings status ref: ${ref}`,
          };
        }
        const text = String(runtimeState?.text || "WAIT").trim().toUpperCase();
        const stateClass = String(runtimeState?.actionClass || "warn").trim();
        node.textContent = text;
        node.className = `indicator-settings-status ${stateClass}`;
        node.title = String(runtimeState?.details || text);
        node.setAttribute("aria-label", `Connection status: ${text}`);
      });
    }
    window.refreshIndicatorSettingsStatusBadges = refreshIndicatorSettingsStatusBadges;

    function runtimeActionClass(actionText, direction = "") {
      const action = String(actionText || "").toUpperCase();
      const side = String(direction || "").toLowerCase();
      if (action === "GO" || action === "IN" || action === "SIGNAL") return side === "short" ? "bad" : "signal";
      if (action === "TRAIL") return side === "short" ? "bad" : "warn";
      if (action === "CANDIDATE" || action === "ARM" || action === "TRAIL" || action === "WATCH" || action === "BLOCK") return "warn";
      if (action === "OFF" || action === "ERROR" || action === "STALE") return "bad";
      return "ok";
    }

    function runtimeDirectionMark(direction) {
      if (direction === "long") return "↑";
      if (direction === "short") return "↓";
      return "·";
    }

    function runtimeCompactPlanLine(summary) {
      if (!summary) return "";
      const bits = [];
      if (summary.direction === "long" || summary.direction === "short") {
        bits.push(`<span class="dir-${summary.direction}">${runtimeDirectionMark(summary.direction)}</span>`);
      }
      if (summary.entry !== null) bits.push(`E ${fmt(summary.entry)}`);
      if (summary.target !== null) bits.push(`T ${fmt(summary.target)}`);
      return bits.join(" · ");
    }

    function runtimePlanLine(summary) {
      if (!summary) return "";
      const bits = [];
      if (summary.direction === "long" || summary.direction === "short") {
        bits.push(`<span class="dir-${summary.direction}">${runtimeDirectionMark(summary.direction)}</span>`);
      }
      if (summary.entry !== null) bits.push(`E ${fmt(summary.entry)}`);
      if (summary.stop !== null) bits.push(`S ${fmt(summary.stop)}`);
      if (summary.target !== null) bits.push(`T ${fmt(summary.target)}`);
      if (summary.rr !== null) bits.push(`RR ${Number(summary.rr).toFixed(1)}`);
      else if (summary.score !== null) bits.push(`Q ${Math.round(summary.score)}`);
      return bits.join(" · ");
    }

    function indicatorAnalysisLifecycleRuntimeState(snapshot) {
      const meta = snapshot?.meta || {};
      const rawStatus = String(meta.analysis_status || "").trim().toLowerCase();
      if (!rawStatus) return null;
      const lifecycle = typeof marketAnalysisLifecyclePresentation === "function"
        ? marketAnalysisLifecyclePresentation(snapshot)
        : null;
      const label = String(lifecycle?.label || rawStatus).trim().toUpperCase();
      const reasonCode = String(lifecycle?.reasonCode || "").trim();
      const reasonMessage = String(lifecycle?.reasonMessage || "").trim();
      const runtimeStatus = String(lifecycle?.runtimeStatus || rawStatus).trim().toLowerCase();
      const details = [
        `Analysis lifecycle: ${label}`,
        runtimeStatus && runtimeStatus !== rawStatus
          ? `Server analysis status: ${runtimeStatus.toUpperCase()}`
          : "",
        reasonCode ? `Code: ${reasonCode}` : "",
        reasonMessage,
      ].filter(Boolean);
      if (rawStatus === "ready") {
        details.push("Analysis completed, but this enabled indicator returned no typed status.");
        return {
          text: "MISSING",
          muted: false,
          actionClass: "bad",
          details: details.join(String.fromCharCode(10)),
        };
      }
      const transition = {
        queued: "QUEUED",
        running: "RUNNING",
        repairing: "REPAIRING",
      }[rawStatus];
      if (transition) {
        return {
          text: transition,
          muted: false,
          actionClass: "warn",
          details: details.join(String.fromCharCode(10)),
        };
      }
      const terminal = {
        missing: "MISSING",
        error: "ERROR",
        degraded: "DEGRADED",
        superseded: "DEGRADED",
      }[rawStatus] || "DEGRADED";
      return {
        text: terminal,
        muted: false,
        actionClass: "bad",
        details: details.join(String.fromCharCode(10)),
      };
    }

    function indicatorRuntimeState(calcEnabled, visible, status, summary, snapshot = state.snapshot) {
      if (!calcEnabled || status?.mode === "disabled") return { text: "OFF", muted: true, actionClass: "bad" };
      if (!visible) return { text: "HIDE", muted: true, actionClass: "bad" };
      const analysisState = indicatorAnalysisLifecycleRuntimeState(snapshot);
      if (
        analysisState
        && String(snapshot?.meta?.analysis_status || "").trim().toLowerCase() !== "ready"
      ) return analysisState;
      const stateCode = String(status?.state_code || "").toLowerCase();
      const health = String(status?.health || "").toLowerCase();
      const mode = String(status?.mode || "").toLowerCase();
      const reasonCode = String(status?.reason_code || "").toLowerCase();
      if (!Object.keys(status || {}).length) {
        if (analysisState) return analysisState;
        return {
          text: "LOAD",
          muted: false,
          actionClass: "warn",
          details: "Waiting for the first calculated indicator snapshot.",
        };
      }
      if (health === "error" || stateCode === "error") return { text: "ERROR", muted: false, actionClass: "bad" };
      if (health === "stale" || stateCode === "stale") return { text: "STALE", muted: false, actionClass: "bad" };
      if (stateCode === "blocked_context") {
        const warmup = reasonCode.endsWith("_warmup");
        const unsupported = reasonCode === "unsupported_price_domain";
        return {
          text: warmup ? "WARM" : unsupported ? "UNSUP" : "BLOCK",
          muted: false,
          actionClass: "warn",
          details: reasonCode ? reasonCode.replaceAll("_", " ").toUpperCase() : "REQUIRED CONTEXT UNAVAILABLE",
        };
      }
      if (stateCode === "degraded_context") {
        return {
          text: "DEGRADED",
          muted: false,
          actionClass: "warn",
          details: reasonCode ? reasonCode.replaceAll("_", " ").toUpperCase() : "OPTIONAL CONTEXT UNAVAILABLE",
        };
      }
      if (["waiting", "loading", "pending"].includes(stateCode) || ["loading", "pending"].includes(mode)) {
        return {
          text: "LOAD",
          muted: false,
          actionClass: "warn",
          details: "Indicator calculation is still loading.",
        };
      }
      if (summary?.lifecycleActive) {
        const lifecycleState = String(summary.lifecycle?.state || "").toUpperCase();
        if (summary.lifecycleFilled && ["FOLLOW", "RIDE", "TRAIL"].includes(lifecycleState)) {
          const actionText = lifecycleState === "TRAIL" ? "TRAIL" : "IN";
          return { text: actionText, muted: false, actionClass: runtimeActionClass(actionText, summary.direction) };
        }
        if (lifecycleState === "WAIT_ENTRY") {
          return { text: "ARM", muted: false, actionClass: "warn" };
        }
      }
      const action = normalizeRuntimeAction(summary?.action);
      if (action) {
        return { text: action, muted: ["WATCH", "WAIT"].includes(action), actionClass: runtimeActionClass(action, summary?.direction) };
      }
      if (status?.blocked_signal === true || stateCode === "blocked_signal") return { text: "BLOCK", muted: false, actionClass: "warn" };
      if (status?.preview_active === true || stateCode === "live_preview_signal") return { text: "LIVE", muted: false, actionClass: "signal" };
      if (status?.has_signal === true || stateCode === "signal") return { text: "SIGNAL", muted: false, actionClass: "signal" };
      if (stateCode === "no_signal") return { text: "NO SIG", muted: true, actionClass: "ok" };
      return {
        text: String(status?.state || "WAIT").toUpperCase().slice(0, 8),
        muted: true,
        actionClass: "ok",
        details: "Calculated; waiting for a new actionable signal.",
      };
    }

    function resolveIndicatorRuntimeState(
      calcEnabled,
      visible,
      status,
      summary,
      snapshot = state.snapshot,
      runtimeStateRef = "",
    ) {
      const canonicalState = indicatorRuntimeState(calcEnabled, visible, status, summary, snapshot);
      const analysisStatus = String(snapshot?.meta?.analysis_status || "").trim().toLowerCase();
      if (analysisStatus && analysisStatus !== "ready") return canonicalState;
      return indicatorRuntimeStateForRef(runtimeStateRef, snapshot) || canonicalState;
    }

    function setupIndicatorRuntimeToggles() {
      const listNode = document.getElementById("indicator-runtime-list");
      if (!listNode || listNode.dataset.runtimeToggleReady === "1") return;
      listNode.dataset.runtimeToggleReady = "1";
      listNode.addEventListener("click", event => {
        const button = event.target?.closest?.("[data-runtime-toggle]");
        if (button) {
          const input = document.getElementById(button.dataset.runtimeToggle);
          if (input) {
            input.checked = !input.checked;
            input.dispatchEvent(new Event("change", { bubbles: true }));
            if (state.snapshot) renderIndicatorRuntimeStatus(state.snapshot);
          }
          return;
        }

        const liveBtn = event.target?.closest?.("[data-runtime-live-toggle]");
        if (liveBtn) {
          const input = document.getElementById(liveBtn.dataset.runtimeLiveToggle);
          if (input) {
            const wasOff = !input.checked;
            input.checked = !input.checked;
            input.dispatchEvent(new Event("change", { bubbles: true }));
            if (wasOff) {
              const mainBtn = liveBtn.closest(".indicator-runtime-row")?.querySelector("[data-runtime-toggle]");
              const mainInput = mainBtn ? document.getElementById(mainBtn.dataset.runtimeToggle) : null;
              if (mainInput && !mainInput.checked) {
                mainInput.checked = true;
                mainInput.dispatchEvent(new Event("change", { bubbles: true }));
              }
            }
            if (state.snapshot) renderIndicatorRuntimeStatus(state.snapshot);
          }
          return;
        }

        const tableBtn = event.target?.closest?.("[data-runtime-table-toggle]");
        if (tableBtn) {
          const select = document.getElementById(tableBtn.dataset.runtimeTableToggle);
          if (select) {
            const wasOff = select.value === "off";
            if (wasOff) {
              select.value = select.dataset.lastPosition || "bottom";
            } else {
              select.dataset.lastPosition = select.value;
              select.value = "off";
            }
            select.dispatchEvent(new Event("change", { bubbles: true }));

            if (wasOff) {
              const mainBtn = tableBtn.closest(".indicator-runtime-row")?.querySelector("[data-runtime-toggle]");
              const input = mainBtn ? document.getElementById(mainBtn.dataset.runtimeToggle) : null;
              if (input && !input.checked) {
                input.checked = true;
                input.dispatchEvent(new Event("change", { bubbles: true }));
              }
            }
            if (state.snapshot) renderIndicatorRuntimeStatus(state.snapshot);
          }
        }
      });
    }

    function setupIndicatorQuickToggles() {
      function indicatorIdForSettingsRow(row, titleText = "") {
        const explicit = String(row?.dataset?.indicatorId || row?.dataset?.generatedIndicatorId || "").trim();
        return explicit;
      }
      function processInputForSettingsRow(row, controls, titleText) {
        const indicatorId = indicatorIdForSettingsRow(row, titleText);
        const processId = indicatorId ? indicatorUiSpec(indicatorId).process_control_id : "";
        if (processId) {
          const input = [...controls.querySelectorAll("label.toggle input")].find(node => node.id === processId);
          if (input) return input;
        }
        return controls.querySelector('label.toggle input[id$="-process"]');
      }
      function updateIndicatorCalcBadge(button) {
        if (!button) return;
        const control = document.getElementById(button.dataset.calcStatusFor || button.id || "");
        const enabled = control ? control.checked !== false : false;
        const indicatorId = indicatorIdForProcessControl(control?.id || button.dataset.calcStatusFor || button.id || "");
        const indicator = indicatorSpec(indicatorId);
        const uiOnly = indicator?.module_type === "ui-only" || indicator?.pipeline_stage === "ui";
        button.innerHTML = `<span class="indicator-calc-prefix">Calc:</span><span class="indicator-calc-switch"><span class="indicator-calc-value">${enabled ? "ON" : "OFF"}</span><span class="indicator-calc-knob"></span></span>`;
        button.classList.toggle("on", enabled);
        button.classList.toggle("off", !enabled);
        button.setAttribute("aria-pressed", enabled ? "true" : "false");
        button.title = enabled
          ? `Calculation is ON. Click to remove this indicator from ${uiOnly ? "frontend" : "backend"} processing.`
          : `Calculation is OFF. Click to enable ${uiOnly ? "frontend" : "backend"} processing for this indicator.`;
      }
      function updateIndicatorCalcBadges(root = document) {
        root.querySelectorAll?.(".indicator-calc-status").forEach(updateIndicatorCalcBadge);
      }
      window.updateIndicatorCalcBadges = updateIndicatorCalcBadges;
      const INDICATOR_MANAGER_MANIFEST = [
        ...Object.entries(indicatorRegistry())
          .filter(([, spec]) => spec?.ui?.show_in_manager !== false && spec?.ui?.process_control_id)
          .map(([id, spec]) => ({
            id,
            processControlId: spec.ui.process_control_id,
            label: spec.label || id,
            moduleType: spec.module_type || "signal",
            managerOrder: numericValueOrFallback(spec.ui.manager_order, 500),
          })),
      ].sort((a, b) => (
        numericValueOrFallback(a.managerOrder, 500)
        - numericValueOrFallback(b.managerOrder, 500)
      ));
      function indicatorModuleCatalog() {
        return window.INDICATOR_MODULE_CATALOG || { installed: [], available: [], invalid: [], skipped: [] };
      }
      function indicatorModuleName(entry) {
        const moduleName = String(entry?.module || "");
        return moduleName.split(".").filter(Boolean).pop() || moduleName || "module";
      }
      function indicatorManagerCatalogDiagnostics() {
        const catalog = indicatorModuleCatalog();
        const invalid = Array.isArray(catalog.invalid) ? catalog.invalid : [];
        const available = Array.isArray(catalog.available) ? catalog.available : [];
        return { invalid, available };
      }
      function indicatorManagerCatalogRows() {
        const { invalid, available } = indicatorManagerCatalogDiagnostics();
        const invalidRows = invalid.map((entry, index) => {
          const detail = [entry.reason, entry.detail].filter(Boolean).join(" · ");
          return `
            <div class="indicator-manager-row indicator-manager-diagnostic is-invalid" data-manager-diagnostic="invalid-${index}">
              <span class="indicator-manager-label">
                <b>${escapeHtml(indicatorModuleName(entry))}</b>
                <small>${escapeHtml(detail || "invalid module")}</small>
              </span>
              <span class="indicator-manager-type">invalid</span>
            </div>
          `;
        });
        const availableRows = available.map((entry, index) => `
          <div class="indicator-manager-row indicator-manager-diagnostic is-available" data-manager-diagnostic="available-${index}">
            <span class="indicator-manager-label">
              <b>${escapeHtml(indicatorModuleName(entry))}</b>
              <small>${escapeHtml(entry.reason || "available candidate")}</small>
            </span>
            <span class="indicator-manager-type">available</span>
          </div>
        `);
        return [...invalidRows, ...availableRows];
      }
      function indicatorSettingsOrderKey() {
        return instrumentIndicatorSettingKey("indicatorSettingsOrder", state.instrumentId);
      }
      function storedIndicatorSettingsOrder() {
        const key = indicatorSettingsOrderKey();
        const raw = serverSettingValue(key) || "";
        return String(raw || "").split(",").map(item => item.trim()).filter(Boolean);
      }
      function saveIndicatorSettingsOrder(ids) {
        const key = indicatorSettingsOrderKey();
        const value = ids.join(",");
        setServerSettingValue(key, value);
      }
      function orderedIndicatorManagerManifest() {
        const saved = storedIndicatorSettingsOrder();
        const rank = new Map(saved.map((id, index) => [id, index]));
        return [...INDICATOR_MANAGER_MANIFEST].sort((a, b) => {
          const aOrder = numericValueOrFallback(a.managerOrder, 500);
          const bOrder = numericValueOrFallback(b.managerOrder, 500);
          const ar = rank.has(a.id) ? rank.get(a.id) : 1000 + aOrder;
          const br = rank.has(b.id) ? rank.get(b.id) : 1000 + bOrder;
          return ar - br || aOrder - bOrder;
        });
      }
      function indicatorManagerDiagnostics(item) {
        if (item.uiOnly) return "ui-only";
        const status = state.snapshot?.indicators?.[item.id]?.status || {};
        const bits = [item.moduleType || "signal"];
        const controls = indicatorControlDomHealth(item.id);
        if (controls.total) bits.push(`${controls.total} controls`);
        if (controls.missing.length) bits.push(`${controls.missing.length} missing`);
        if (status.state_code) bits.push(String(status.state_code).toUpperCase().slice(0, 14));
        if (status.mode) bits.push(String(status.mode));
        if (Number.isFinite(Number(status.elapsed_ms))) bits.push(`${Number(status.elapsed_ms).toFixed(1)}ms`);
        if (status.preview_active === true) bits.push("preview");
        return bits.join(" · ");
      }
      function managedIndicatorRows() {
        return orderedIndicatorManagerManifest()
          .map(item => {
            const calc = document.getElementById(item.processControlId);
            const row = calc?.closest?.(".indicator-row");
            if (!calc || !row) return null;
            const label = String(
              row.querySelector(".indicator-title-text")?.textContent
              || row.querySelector(".indicator-title")?.textContent
              || "Indicator"
            ).trim();
            return { ...item, row, calc, label };
          })
          .filter(Boolean)
          .sort((a, b) =>
            Number(b.row.dataset.indicatorOrderLock === "top")
            - Number(a.row.dataset.indicatorOrderLock === "top")
          );
      }
      function applyIndicatorSettingsOrder() {
        managedIndicatorRows().forEach((item, index) => {
          if (item.row.dataset.indicatorOrderLock === "top") {
            item.row.style.order = String(indicatorRowOrder(item.row));
            delete item.row.dataset.indicatorOrderId;
          } else {
            item.row.style.order = String(index + 1);
            item.row.dataset.indicatorOrderId = item.id;
          }
        });
      }
      function reorderIndicatorSettings(sourceId, targetId, placement = "before") {
        if (!sourceId || !targetId || sourceId === targetId) return;
        const ids = managedIndicatorRows()
          .filter(item => item.row.dataset.indicatorOrderLock !== "top")
          .map(item => item.id);
        const sourceIndex = ids.indexOf(sourceId);
        const targetIndex = ids.indexOf(targetId);
        if (sourceIndex < 0 || targetIndex < 0) return;
        const [moved] = ids.splice(sourceIndex, 1);
        const nextTargetIndex = ids.indexOf(targetId);
        ids.splice(placement === "after" ? nextTargetIndex + 1 : nextTargetIndex, 0, moved);
        saveIndicatorSettingsOrder(ids);
        applyIndicatorSettingsOrder();
        refreshIndicatorManager();
      }
      function setIndicatorCalcState(calc, enabled) {
        if (!calc) return;
        calc.checked = Boolean(enabled);
        updateIndicatorCalcBadge(calc);
        calc.dispatchEvent(new Event("change", { bubbles: true }));
      }
      function refreshIndicatorManager() {
        const list = document.getElementById("indicator-manager-list");
        const count = document.getElementById("indicator-manager-count");
        const diagnostics = document.getElementById("indicator-manager-diagnostics");
        if (!list) return;
        const items = managedIndicatorRows();
        const activeCount = items.filter(item => item.calc.checked !== false).length;
        const catalogDiagnostics = indicatorManagerCatalogDiagnostics();
        const invalidCount = catalogDiagnostics.invalid.length;
        const availableCount = catalogDiagnostics.available.length;
        if (count) count.textContent = `${activeCount} / ${items.length}${invalidCount ? ` · ${invalidCount} invalid` : ""}`;
        if (diagnostics) {
          diagnostics.textContent = [
            `${items.length} installed`,
            availableCount ? `${availableCount} available` : "",
            invalidCount ? `${invalidCount} invalid` : "",
          ].filter(Boolean).join(" · ");
          diagnostics.classList.toggle("has-invalid", invalidCount > 0);
        }
        for (const item of items) {
          item.row.classList.toggle("indicator-row-disabled", item.calc.checked === false);
        }
        const installedRows = items.map((item, index) => {
          const enabled = item.calc.checked !== false;
          const linkBadge = indicatorLinkBadgeHtml(item.id);
          return `
            <div class="indicator-manager-row ${enabled ? "is-on" : "is-off"}" data-manager-id="${escapeHtml(item.id)}" data-manager-index="${index}">
              <span class="indicator-manager-label">
                <b>${escapeHtml(item.label)}</b>
                <small>${escapeHtml(indicatorManagerDiagnostics(item))}</small>
              </span>
              <div class="indicator-manager-actions">
                <span class="indicator-manager-type">${escapeHtml(item.moduleType || "signal")}</span>
                ${linkBadge ? `<span class="indicator-settings-link-badge-wrap" data-indicator-id="${escapeHtml(item.id)}">${linkBadge}</span>` : ""}
                <button type="button" class="indicator-calc-status ${enabled ? "on" : "off"}" data-manager-calc-index="${index}" aria-pressed="${enabled ? "true" : "false"}">
                  <span class="indicator-calc-prefix">Calc:</span><span class="indicator-calc-switch"><span class="indicator-calc-value">${enabled ? "ON" : "OFF"}</span><span class="indicator-calc-knob"></span></span>
                </button>
              </div>
            </div>
          `;
        });
        list.innerHTML = [...installedRows, ...indicatorManagerCatalogRows()].join("");
        renderIndicatorSettingsLinkBadges();
      }
      window.refreshIndicatorManager = refreshIndicatorManager;
      function setupIndicatorManager() {
        const toggle = document.getElementById("indicator-manager-toggle");
        const panel = document.getElementById("indicator-manager-panel");
        const list = document.getElementById("indicator-manager-list");
        if (!toggle || !panel || !list || toggle.dataset.managerReady === "1") return;
        toggle.dataset.managerReady = "1";
        toggle.addEventListener("click", () => {
          const open = panel.classList.toggle("hidden") === false;
          toggle.setAttribute("aria-expanded", open ? "true" : "false");
          refreshIndicatorManager();
        });
        list.addEventListener("click", event => {
          const button = event.target?.closest?.("[data-manager-calc-index]");
          if (!button) return;
          const item = managedIndicatorRows()[Number(button.dataset.managerCalcIndex)];
          if (!item) return;
          setIndicatorCalcState(item.calc, item.calc.checked === false);
          refreshIndicatorManager();
        });
      }
      function indicatorRowOrder(row) {
        if (row?.dataset?.indicatorOrderLock === "top") return -100;
        const explicit = Number(row?.dataset?.indicatorOrder);
        if (Number.isFinite(explicit)) return explicit;
        const indicatorId = indicatorIdForSettingsRow(row, row?.querySelector?.(".indicator-title")?.textContent || "");
        const spec = indicatorId ? indicatorSpec(indicatorId) : {};
        return Number(spec?.ui?.settings_order ?? spec?.ui?.manager_order ?? 0) || 0;
      }
      function indicatorFieldLabel(control, row = null) {
        const manifestLabel = String(indicatorControlSpecForElement(control, row)?.label || "").trim();
        if (manifestLabel) return manifestLabel;
        const explicit = String(control?.dataset?.label || "").trim();
        if (explicit) return explicit;
        const title = String(control?.title || "").trim();
        if (title) return title.replace(/\s*%$/, " %");
        return "Setting";
      }
      function wrapIndicatorField(control, row = null) {
        if (!control || control.closest?.(".indicator-field")) return control?.closest?.(".indicator-field") || control;
        const wrapper = document.createElement("label");
        wrapper.className = "indicator-field indicator-advanced-control";
        wrapper.title = control.title || "";
        const label = document.createElement("span");
        label.textContent = indicatorFieldLabel(control, row);
        control.parentElement.insertBefore(wrapper, control);
        wrapper.appendChild(label);
        wrapper.appendChild(control);
        return wrapper;
      }
      function indicatorControlSpecForElement(element, row = null) {
        const control = element.querySelector?.("input, select") || element;
        const indicatorId = String(
          control?.dataset?.indicatorId
          || element?.dataset?.indicatorId
          || indicatorIdForSettingsRow(row, row?.querySelector?.(".indicator-title")?.textContent || "")
          || ""
        ).trim();
        if (!indicatorId) return null;
        const controlId = String(control?.id || element?.id || "").trim();
        const controlKey = String(control?.dataset?.indicatorControlKey || element?.dataset?.indicatorControlKey || "").trim();
        return indicatorControlSpecs(indicatorId).find(item => {
          if (!item) return false;
          if (controlId && item.element_id === controlId) return true;
          if (element?.id && item.element_id === element.id) return true;
          return Boolean(controlKey && item.key === controlKey);
        }) || null;
      }
      function explicitCompactControlFlag(element, control) {
        const raw = String(element?.dataset?.compactControl || control?.dataset?.compactControl || "").trim().toLowerCase();
        if (!raw) return null;
        if (["1", "true", "yes", "compact"].includes(raw)) return true;
        if (["0", "false", "no", "advanced"].includes(raw)) return false;
        return null;
      }
      function isCompactIndicatorControl(element, row = null) {
        const control = element.querySelector?.("input, select") || element;
        const id = String(control?.id || "");
        if (id.endsWith("-process")) return false;
        const explicit = explicitCompactControlFlag(element, control);
        if (explicit !== null) return explicit;
        const manifestFlag = String(element?.dataset?.indicatorControlCompact || control?.dataset?.indicatorControlCompact || "").trim();
        if (manifestFlag) return manifestFlag === "1" || manifestFlag === "true";
        const spec = indicatorControlSpecForElement(element, row);
        return spec?.compact === true;
      }
      let draggedIndicatorOrderId = "";
      function clearIndicatorDropPreview() {
        document.querySelectorAll("#side-indicators .indicator-row-drop-before, #side-indicators .indicator-row-drop-after").forEach(row => {
          row.classList.remove("indicator-row-drop-before", "indicator-row-drop-after");
        });
      }
      function indicatorRowFromDragTarget(target) {
        return target?.closest?.("#side-indicators .indicator-row[data-indicator-order-id]") || null;
      }
      function indicatorDropPlacement(row, event) {
        const rect = row.getBoundingClientRect();
        const midpoint = rect.top + rect.height / 2;
        return event.clientY > midpoint ? "after" : "before";
      }
      function showIndicatorDropPreview(row, placement) {
        clearIndicatorDropPreview();
        if (!row) return;
        row.classList.toggle("indicator-row-drop-before", placement !== "after");
        row.classList.toggle("indicator-row-drop-after", placement === "after");
      }
      function keyboardReorderIndicatorSettings(handle, event) {
        if (!event.altKey || !["ArrowUp", "ArrowDown", "Home", "End"].includes(event.key)) {
          return false;
        }
        const sourceId = String(handle?.dataset?.indicatorOrderHandle || "");
        const ids = managedIndicatorRows()
          .filter(item => item.row.dataset.indicatorOrderLock !== "top")
          .map(item => item.id);
        const sourceIndex = ids.indexOf(sourceId);
        if (sourceIndex < 0 || ids.length < 2) return false;
        let targetIndex = sourceIndex;
        let placement = "before";
        if (event.key === "ArrowUp") targetIndex = Math.max(sourceIndex - 1, 0);
        if (event.key === "ArrowDown") {
          targetIndex = Math.min(sourceIndex + 1, ids.length - 1);
          placement = "after";
        }
        if (event.key === "Home") targetIndex = 0;
        if (event.key === "End") {
          targetIndex = ids.length - 1;
          placement = "after";
        }
        if (targetIndex === sourceIndex) return false;
        event.preventDefault();
        event.stopPropagation();
        reorderIndicatorSettings(sourceId, ids[targetIndex], placement);
        window.requestAnimationFrame(() => {
          document.querySelector(`[data-indicator-order-handle="${CSS.escape(sourceId)}"]`)?.focus();
        });
        showBrowserToast(`Indicator order updated: ${sourceId}`, { tone: "success" });
        return true;
      }
      function setupIndicatorOrderHandle(handle) {
        if (!handle || handle.dataset.dragReady === "1") return;
        handle.dataset.dragReady = "1";
        handle.addEventListener("click", event => {
          event.preventDefault();
          event.stopPropagation();
        });
        handle.addEventListener("keydown", event => {
          keyboardReorderIndicatorSettings(handle, event);
        });
        handle.addEventListener("dragstart", event => {
          draggedIndicatorOrderId = handle.dataset.indicatorOrderHandle || "";
          const row = indicatorRowFromDragTarget(handle);
          row?.classList.add("indicator-row-dragging");
          if (event.dataTransfer) {
            event.dataTransfer.effectAllowed = "move";
            event.dataTransfer.setData("text/plain", draggedIndicatorOrderId);
          }
        });
        handle.addEventListener("dragend", () => {
          draggedIndicatorOrderId = "";
          document.querySelectorAll("#side-indicators .indicator-row-dragging, #side-indicators .indicator-row-drag-over").forEach(row => {
            row.classList.remove("indicator-row-dragging", "indicator-row-drag-over");
          });
          clearIndicatorDropPreview();
        });
      }
      function setupIndicatorOrderDrops() {
        document.querySelectorAll("#side-indicators .indicator-row[data-indicator-order-id]").forEach(row => {
          if (row.dataset.orderDropReady === "1") return;
          row.dataset.orderDropReady = "1";
          row.addEventListener("dragover", event => {
            if (!draggedIndicatorOrderId || row.dataset.indicatorOrderId === draggedIndicatorOrderId) return;
            event.preventDefault();
            row.classList.add("indicator-row-drag-over");
            showIndicatorDropPreview(row, indicatorDropPlacement(row, event));
            if (event.dataTransfer) event.dataTransfer.dropEffect = "move";
          });
          row.addEventListener("dragleave", () => {
            row.classList.remove("indicator-row-drag-over");
            clearIndicatorDropPreview();
          });
          row.addEventListener("drop", event => {
            const sourceId = event.dataTransfer?.getData("text/plain") || draggedIndicatorOrderId;
            const targetId = row.dataset.indicatorOrderId || "";
            const placement = indicatorDropPlacement(row, event);
            row.classList.remove("indicator-row-drag-over");
            clearIndicatorDropPreview();
            if (!sourceId || !targetId || sourceId === targetId) return;
            event.preventDefault();
            reorderIndicatorSettings(sourceId, targetId, placement);
          });
        });
      }
      function createManifestControlNode(control) {
        if (!control?.element_id) return null;
        const label = document.createElement("label");
        const text = document.createElement("span");
        text.textContent = control.label || control.key || "Control";
        label.title = control.label || control.key || "";
        if (control.control_type === "toggle") {
          label.className = "toggle";
          const input = document.createElement("input");
          input.id = control.element_id;
          input.type = "checkbox";
          label.appendChild(input);
          label.appendChild(text);
          return label;
        }
        label.className = "control-inline";
        label.appendChild(text);
        const input = document.createElement(control.control_type === "select" ? "select" : "input");
        input.id = control.element_id;
        input.className = control.control_type === "color" ? "settings-control color-control" : "settings-control";
        if (control.control_type === "number") {
          input.type = "number";
          if (control.minimum !== null && control.minimum !== undefined) input.min = String(control.minimum);
          if (control.maximum !== null && control.maximum !== undefined) input.max = String(control.maximum);
          if (control.step !== null && control.step !== undefined) input.step = String(control.step);
        } else if (control.control_type === "color") {
          input.type = "color";
        } else {
          (control.options || []).forEach((optionValue, optionIndex) => {
            const option = document.createElement("option");
            option.value = String(optionValue);
            option.textContent = String(control.option_labels?.[optionIndex] || optionValue);
            input.appendChild(option);
          });
        }
        label.appendChild(input);
        return label;
      }
      function ensureManifestIndicatorSettingsRows() {
        if (typeof ensureManagedIndicatorStateGroups === "function") ensureManagedIndicatorStateGroups();
        const list = document.querySelector("#side-indicators .indicator-list");
        if (!list) return;
        Object.entries(indicatorRegistry()).forEach(([indicatorId, spec]) => {
          const ui = spec?.ui || {};
          if (ui.show_in_manager === false || !ui.process_control_id || document.getElementById(ui.process_control_id)) return;
          const row = document.createElement("div");
          row.className = "indicator-row indicator-row-manifest";
          row.dataset.generatedIndicatorId = indicatorId;
          const header = document.createElement("div");
          const title = document.createElement("div");
          title.className = "indicator-title";
          title.textContent = spec.label || indicatorId;
          const note = document.createElement("div");
          note.className = "indicator-note";
          note.textContent = spec.calculates || "";
          header.appendChild(title);
          header.appendChild(note);
          const controls = document.createElement("div");
          controls.className = "indicator-controls";
          const processLabel = document.createElement("label");
          processLabel.className = "toggle";
          processLabel.title = `Run ${spec.label || indicatorId} calculations`;
          const processInput = document.createElement("input");
          processInput.id = ui.process_control_id;
          processInput.type = "checkbox";
          const processText = document.createElement("span");
          processText.textContent = "Calc";
          processLabel.appendChild(processInput);
          processLabel.appendChild(processText);
          controls.appendChild(processLabel);
          if (ui.chart_control_id && ui.chart_control_id !== ui.process_control_id) {
            const visibleLabel = document.createElement("label");
            visibleLabel.className = "toggle";
            visibleLabel.title = `Show ${spec.label || indicatorId}`;
            const visibleInput = document.createElement("input");
            visibleInput.id = ui.chart_control_id;
            visibleInput.type = "checkbox";
            const visibleText = document.createElement("span");
            visibleText.textContent = "Show";
            visibleLabel.appendChild(visibleInput);
            visibleLabel.appendChild(visibleText);
            controls.insertBefore(visibleLabel, processLabel);
          }
          (spec.controls || []).forEach(control => {
            const node = createManifestControlNode(control);
            if (node) controls.appendChild(node);
          });
          row.appendChild(header);
          row.appendChild(controls);
          list.appendChild(row);
        });
      }
      ensureManifestIndicatorSettingsRows();
      annotateIndicatorControlsFromManifest();
      document.querySelectorAll("#side-indicators .indicator-row").forEach(row => {
        if (row.dataset.quickToggleReady === "1") return;
        const controls = row.querySelector(".indicator-controls");
        const titleBlock = row.firstElementChild;
        const title = row.querySelector(".indicator-title");
        const note = row.querySelector(".indicator-note");
        if (!controls || !titleBlock) return;
        const titleText = String(title?.textContent || "Indicator").trim();
        const noteText = String(note?.textContent || "").trim();
        const processInput = processInputForSettingsRow(row, controls, titleText);
        const processInputId = processInput?.id || "";
        const showInput = [...controls.querySelectorAll('label.toggle input[id$="-toggle"]')]
          .find(input => input.id !== processInputId);
        const showLabel = showInput?.closest("label.toggle");
        row.style.order = String(indicatorRowOrder(row));
        if (title && noteText) {
          title.title = noteText;
          title.setAttribute("aria-label", `${titleText}. ${noteText}`);
          title.setAttribute("tabindex", "0");
        }
        if (title && processInput && !title.querySelector(".indicator-calc-status")) {
          const processLabel = processInput.closest("label.toggle");
          const processId = processInput.id;
          const titleLabel = String(title.textContent || titleText).trim();
          const calcButton = document.createElement("button");
          calcButton.id = processId;
          calcButton.type = "button";
          calcButton.className = "indicator-calc-status";
          calcButton.dataset.calcStatusFor = processId;
          calcButton.checked = processInput.checked !== false;
          calcButton.addEventListener("click", event => {
            event.preventDefault();
            event.stopPropagation();
            calcButton.checked = !calcButton.checked;
            updateIndicatorCalcBadge(calcButton);
            calcButton.dispatchEvent(new Event("change", { bubbles: true }));
          });
          title.classList.add("indicator-title-with-calc");
          title.textContent = "";
          const titleSpan = document.createElement("span");
          titleSpan.className = "indicator-title-text";
          titleSpan.textContent = titleLabel;
          const actions = document.createElement("span");
          actions.className = "indicator-title-actions";
          const indicatorId = indicatorIdForProcessControl(processId);
          if (indicatorId && INDICATOR_DEPENDENCY_LINKS[indicatorId]) {
            const badgeWrap = document.createElement("span");
            badgeWrap.className = "indicator-settings-link-badge-wrap";
            badgeWrap.dataset.indicatorId = indicatorId;
            actions.appendChild(badgeWrap);
          }
          const settingsStatusRef = String(
            indicatorId ? indicatorSpec(indicatorId)?.ui?.settings_status_ref || "" : ""
          ).trim();
          if (settingsStatusRef) {
            const statusBadge = document.createElement("span");
            statusBadge.className = "indicator-settings-status warn";
            statusBadge.dataset.settingsStatusRef = settingsStatusRef;
            statusBadge.textContent = "WAIT";
            statusBadge.setAttribute("role", "status");
            actions.appendChild(statusBadge);
          }
          title.appendChild(titleSpan);
          processLabel?.remove();
          actions.appendChild(calcButton);
          if (indicatorId && row.dataset.indicatorOrderLock !== "top") {
            const dragHandle = document.createElement("button");
            dragHandle.type = "button";
            dragHandle.className = "indicator-order-handle";
            dragHandle.draggable = true;
            dragHandle.dataset.indicatorOrderHandle = indicatorId;
            dragHandle.title = "Drag to reorder · Alt+Arrow/Home/End for keyboard";
            dragHandle.setAttribute("aria-label", `Reorder ${titleLabel}. Use Alt plus Arrow, Home, or End.`);
            actions.appendChild(dragHandle);
            setupIndicatorOrderHandle(dragHandle);
          }
          title.appendChild(actions);
          calcButton.addEventListener("change", () => {
            updateIndicatorCalcBadge(calcButton);
            refreshIndicatorManager();
          });
          updateIndicatorCalcBadge(calcButton);
        }
        titleBlock.title = noteText ? `${titleText}: ${noteText}` : `Open ${titleText} settings`;
        titleBlock.setAttribute("role", "button");
        titleBlock.setAttribute("tabindex", "0");
        if (title) title.style.fontSize = "12px";
        if (note) note.style.fontSize = "10px";
        row.setAttribute("aria-expanded", "false");
        const toggleExpanded = () => {
          const expanded = !row.classList.contains("indicator-expanded");
          row.classList.toggle("indicator-expanded", expanded);
          row.setAttribute("aria-expanded", expanded ? "true" : "false");
        };
        titleBlock.addEventListener("click", toggleExpanded);
        titleBlock.addEventListener("keydown", event => {
          if (event.key !== "Enter" && event.key !== " ") return;
          event.preventDefault();
          toggleExpanded();
        });
        controls.querySelectorAll("label.toggle").forEach(label => {
          const labelText = String(label.querySelector("span")?.textContent || "").trim();
          if (!label.title && labelText) label.title = labelText === "Show" ? `Show ${titleText}` : `${labelText} · ${titleText}`;
        });
        controls.querySelectorAll(":scope > input.settings-control, :scope > select.settings-control, :scope > input.color-control").forEach(control => {
          wrapIndicatorField(control, row);
        });
        titleBlock.classList.add("indicator-main");
        if (showLabel && showLabel.parentElement === controls) {
          showLabel.classList.add("indicator-show");
          const titleRow = document.createElement("div");
          titleRow.className = "indicator-head-row";
          titleRow.appendChild(showLabel);
          titleRow.appendChild(titleBlock);
          row.insertBefore(titleRow, row.firstChild);
        }

        row.style.display = "flex";
        row.style.flexDirection = "column";
        row.style.alignItems = "stretch";
          row.style.paddingBottom = "6px";
          row.style.fontSize = "12px";

        controls.style.width = "100%";
        controls.style.display = "flex";
        controls.style.flexWrap = "wrap";
        controls.style.gap = "4px";
          controls.style.paddingLeft = "4px";

        controls.querySelectorAll(":scope > label, :scope > .indicator-field, :scope > .inline-controls, :scope > .option-target-active-list").forEach(element => {
          element.classList.toggle("indicator-advanced-control", !isCompactIndicatorControl(element, row));
        });
        updateIndicatorCalcBadges(row);
        row.dataset.quickToggleReady = "1";
      });
      setupIndicatorManager();
      applyIndicatorSettingsOrder();
      setupIndicatorOrderDrops();
      refreshIndicatorManager();
      renderIndicatorSettingsLinkBadges();
      refreshIndicatorSettingsStatusBadges();
    }
