    const ADVISOR_PLAN_ROLES = new Set(["entry", "stop", "target", "trail", "track", "entry_fill", "target_hit", "stop_hit", "trail_stop_hit"]);

    function isAdvisorChartMode() {
      return normalizeChartViewMode(state?.settings?.chartViewMode) === "advisor";
    }

    function advisorLatestBarTs(bars) {
      if (!Array.isArray(bars) || !bars.length) return "";
      return timestampKey(bars[bars.length - 1].ts);
    }

    function overlayTimestampKeys(item) {
      if (!item || typeof item !== "object") return [];
      return [item.ts, item.start_ts, item.end_ts, item.fill_ts]
        .map(value => timestampKey(value))
        .filter(Boolean);
    }

    function isAdvisorPlanLine(item) {
      const role = String(item?.role || "").toLowerCase();
      return ADVISOR_PLAN_ROLES.has(role);
    }

    function isAdvisorPresentationActive() {
      return isAdvisorChartMode();
    }

    function shouldHidePerIndicatorPlanDetail() {
      return isAdvisorPresentationActive();
    }

    function advisorEffectiveSignalDensity() {
      if (isAdvisorChartMode()) return "focus";
      return normalizeSignalDensity(state.settings.signalDensity);
    }

    const LIFECYCLE_EXECUTION_MARKER_ROLES = new Set([
      "entry_fill",
      "target_hit",
      "stop_hit",
      "trail_stop_hit",
      "plan_cancel",
    ]);
    const LIFECYCLE_EXECUTION_ACTIONS = new Set([
      "ENTRY_FILLED",
      "TARGET_HIT",
      "STOP_HIT",
      "TRAIL_STOP_HIT",
      "EXPIRED",
      "REV",
    ]);

    function isLifecycleExecutionMarker(item) {
      if (!item || typeof item !== "object") return false;
      const role = String(item?.role || item?.kind || "").toLowerCase();
      if (LIFECYCLE_EXECUTION_MARKER_ROLES.has(role)) return true;
      const action = String(item?.action || "").toUpperCase();
      if (LIFECYCLE_EXECUTION_ACTIONS.has(action)) return true;
      if (!item.glyph_only) return false;
      return ["entry", "stop", "target", "trail"].includes(String(item.glyph_kind || "").toLowerCase());
    }

    function activeIndicatorLifecycle(snapshot) {
      const lifecycle = snapshot?.lifecycle;
      if (!lifecycle || typeof lifecycle !== "object" || !lifecycle.active) return null;
      return lifecycle;
    }

    function allowLifecycleExecutionMarkerByDensity(item, snapshot) {
      if (!isLifecycleExecutionMarker(item)) return true;
      return advisorEffectiveSignalDensity() === "research";
    }

    function indicatorPlanHandoffActive(snapshot) {
      const lifecycle = activeIndicatorLifecycle(snapshot);
      if (lifecycle) return true;
      const paperCtx = typeof paperActiveTradeForChart === "function" ? paperActiveTradeForChart(snapshot) : null;
      if (paperCtx?.active) return true;
      if (!tradeSetupExecutionAuthority(snapshot).ready) return false;
      const setup = snapshot?.trade_setup;
      const phase = String(setup?.phase || setup?.action || "").trim().toUpperCase();
      if (["GO", "FOLLOW", "RIDE", "WAIT_ENTRY", "POSITION"].includes(phase)) return true;
      return false;
    }

    function filterManagedIndicatorOverlays(overlays, snapshot, bars = null) {
      const barSeries = bars || (typeof visibleBars === "function" ? visibleBars(snapshot)?.bars : null) || snapshot?.bars || [];
      const density = advisorEffectiveSignalDensity();
      const advisor = isAdvisorPresentationActive();
      const planHandoff = indicatorPlanHandoffActive(snapshot);
      const latestSignalOverlays = density === "focus"
        && typeof latestIndicatorSignalOverlays === "function"
        ? latestIndicatorSignalOverlays(overlays)
        : null;

      return (overlays || []).filter(item => {
        const type = String(item?.type || "").toLowerCase();
        const role = String(item?.role || "").toLowerCase();
        const presentationPolicy = String(item?.presentation_policy || "").toLowerCase();

        if (presentationPolicy === "persistent") return true;

        if (planHandoff) {
          if (type === "line" && isAdvisorPlanLine(item)) {
            return false;
          }
          if (isLifecycleExecutionMarker(item)) {
            return false;
          }
          if ((type === "marker" || type === "label") && ["entry", "stop", "target", "trail", "track"].includes(role)) {
            return false;
          }
        }

        if (advisor && type === "table") {
          return item?.advisor_visible === true || item?.advisorVisible === true || item?.table?.advisor_visible === true || item?.table?.advisorVisible === true;
        }

        if (advisor && type === "line" && isAdvisorPlanLine(item)) {
          return false;
        }

        if (type === "marker" || type === "label") {
          if (isLifecycleExecutionMarker(item)) {
            return allowLifecycleExecutionMarkerByDensity(item, snapshot);
          }
          if (density === "research") return true;
          if (typeof allowOverlayLabelByDensity === "function") {
            return allowOverlayLabelByDensity(
              item,
              barSeries,
              snapshot,
              latestSignalOverlays,
            );
          }
        }

        return true;
      });
    }

    function advisorDecisionContext(snapshot) {
      const decision = snapshot?.decision;
      const plan = typeof coherentDecisionPlan === "function" ? coherentDecisionPlan(decision) : null;
      const direction = String(decision?.direction || "flat").trim().toLowerCase();
      if (!plan || direction === "flat") return null;
      return { decision, plan, direction };
    }

    function advisorTradeCenterPhase(snapshot) {
      const authority = tradeSetupExecutionAuthority(snapshot);
      if (!authority.ready) return null;
      const setup = snapshot?.trade_setup;
      if (!setup || typeof setup !== "object") return null;
      const card = setup.action_card && typeof setup.action_card === "object" ? setup.action_card : {};
      const raw = String(card.phase || setup.action || "WAIT").trim().toUpperCase();
      const direction = String(setup.side || card.direction || "flat").trim().toLowerCase();
      if (direction !== "long" && direction !== "short") return null;
      if (raw === "GO" && setup.ok === true && card.blocked !== true) {
        return { raw, display: "GO", kind: "command", executable: true, direction };
      }
      if (["IN", "FOLLOW", "RIDE", "TRAIL"].includes(raw)) {
        return { raw, display: raw === "TRAIL" ? "TRAIL" : "IN", kind: "position", executable: false, direction };
      }
      if (["WATCH", "ARM", "CANDIDATE"].includes(raw)) {
        return { raw, display: "CANDIDATE", kind: "candidate", executable: false, direction };
      }
      return null;
    }

    function advisorPlanPresentation(snapshot) {
      const center = advisorTradeCenterPhase(snapshot);
      if (center) return center;
      const ctx = advisorDecisionContext(snapshot);
      if (!ctx) return null;
      const raw = String(snapshot?.decision?.action || "WAIT").trim().toUpperCase();
      if (raw === "GO") {
        return { raw, display: "CANDIDATE", kind: "decision", executable: false, direction: ctx.direction };
      }
      if (["WATCH", "ARM", "CANDIDATE"].includes(raw)) {
        return { raw, display: "CANDIDATE", kind: "decision", executable: false, direction: ctx.direction };
      }
      return null;
    }

    function advisorTradePlanEnabled() {
      if (!isAdvisorChartMode()) return true;
      return Boolean(advisorDecisionContext(state.snapshot));
    }

    function advisorReadinessAction(snapshot) {
      const phase = advisorPlanPresentation(snapshot);
      if (!phase || phase.kind === "position") return null;
      return phase;
    }

    function advisorToggleMetrics() {
      return { width: 58, height: 24, gap: 6, sentimentW: 70, right: 0, top: 6 };
    }

    function resetAdvisorToggleButtonPosition() {
      const button = document.getElementById("chart-advisor-toggle");
      if (!button) return;
      button.style.left = "";
      button.style.top = "";
      button.style.minWidth = "";
      button.style.height = "";
      button.style.lineHeight = "";
    }

    function positionAdvisorToggleButton(x, y, width = 58, height = 19) {
      const button = document.getElementById("chart-advisor-toggle");
      if (!button) return;
      button.style.left = `${Math.round(Number(x) || 0)}px`;
      button.style.top = `${Math.round(Number(y) || 0)}px`;
      button.style.minWidth = `${Math.round(Number(width) || 58)}px`;
      button.style.height = `${Math.round(Number(height) || 19)}px`;
      button.style.lineHeight = `${Math.max(1, Math.round(Number(height) || 19) - 2)}px`;
    }

    function syncAdvisorTogglePosition(options = {}) {
      const button = document.getElementById("chart-advisor-toggle");
      if (!button) return;
      let pad = options.pad;
      let width = options.width;
      if (!pad || !width) {
        const geo = typeof priceChartGeometry === "function" ? priceChartGeometry() : null;
        if (!geo) {
          resetAdvisorToggleButtonPosition();
          return;
        }
        pad = geo.pad;
        width = geo.rect.width;
      }
      const m = advisorToggleMetrics();
      const buttonW = Number(m.width) || 58;
      const sentimentW = Number(m.sentimentW) || 70;
      const gap = Number(m.gap) || 6;
      const right = Number(m.right) || 0;
      const x = Math.max(0, (Number(width) || 0) - right - sentimentW - gap - buttonW);
      positionAdvisorToggleButton(x, m.top, buttonW, m.height);
    }

    function syncAdvisorChartModeClass() {
      document.body.classList.toggle("chart-advisor-mode", isAdvisorChartMode());
      document.body.classList.toggle("chart-classic-mode", !isAdvisorChartMode());
    }

    function refreshAdvisorToggleButton() {
      const button = document.getElementById("chart-advisor-toggle");
      if (!button) return;
      const active = isAdvisorChartMode();
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", active ? "true" : "false");
      button.title = active
        ? "Advisor: Trade Center plan — CANDIDATE until GO, then entry command"
        : "Classic: full indicator labels and per-indicator plans";
      button.textContent = active ? "Advisor" : "Classic";
      syncAdvisorTogglePosition();
    }

    function setChartViewMode(mode, options = {}) {
      const next = normalizeChartViewMode(mode);
      state.settings.chartViewMode = next;
      setServerSettingValue("aef:chartViewMode", next);
      syncAdvisorChartModeClass();
      refreshAdvisorToggleButton();
      if (options.rerender !== false && typeof renderAll === "function") renderAll();
    }

    function drawAdvisorReadinessMark(ctx, snapshot, bars, x, y, pad, width, priceH) {
      if (!isAdvisorChartMode() || !Array.isArray(bars) || !bars.length) return;
      const readiness = advisorReadinessAction(snapshot);
      if (!readiness || typeof drawIndicatorTextLabel !== "function") return;
      const bar = bars[bars.length - 1];
      const index = bars.length - 1;
      const bullish = readiness.direction === "long";
      const px = x(index);
      const anchorY = y(bullish ? bar.low : bar.high);
      const executable = readiness.executable === true;
      const bg = executable
        ? (bullish ? themeColor("up", 0.92) : themeColor("down", 0.92))
        : themeColor("gold", 0.82);
      const lines = executable
        ? [readiness.display, bullish ? "LONG" : "SHORT"]
        : ["CAND", bullish ? "LONG" : "SHORT"];
      const hit = drawIndicatorTextLabel(ctx, px, anchorY, lines, {
        side: bullish ? "below" : "above",
        color: bg,
        offsetPx: 12,
        lineH: 10,
        font: "800 9px -apple-system, BlinkMacSystemFont, sans-serif",
        minX: pad.left + 2,
        maxX: width - pad.right - 2,
        minY: pad.top + 2,
        maxY: pad.top + priceH - 2,
        direction: readiness.direction,
      });
      if (hit) {
        const command = snapshot?.command;
        const setup = snapshot?.trade_setup || {};
        const decisionOnly = readiness.kind === "decision";
        const tooltip = [
          decisionOnly
            ? "DECISION — advisory only (not entry)"
            : executable ? "TRADE CENTER — entry command (GO)" : "TRADE CENTER — candidate only (not entry)",
          `Phase: ${readiness.raw}`,
          decisionOnly
            ? domainCodeLabel(snapshot?.decision?.kind || "decision")
            : command?.setup || domainCodeLabel(setup?.kind || setup?.state || "setup") || "",
          decisionOnly ? "" : actionCardDoNow(command) || "",
        ].filter(Boolean).join("\n");
        registerCanvasTooltip("price", hit.x, hit.y, hit.width, hit.height, tooltip);
      }
    }
