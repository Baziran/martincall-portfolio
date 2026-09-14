    const paperCloseCommandIds = new Map();

    function paperTradingRiskPrefsPayload() {
      ensurePaperTradingInstrumentSettings();
      syncPaperTradingRiskControlsFromDom();
      const cfg = state.paperTrading || {};
      return {
        stop_points: Math.max(Number(cfg.stopPoints) || 0, 0.01),
        target_points: Math.max(Number(cfg.targetPoints) || 0, 0.01),
        use_stop_loss: cfg.useStopLoss !== false,
        use_target: cfg.useTarget !== false,
      };
    }

    function paperShowTradesOnChart() {
      if (state.paperTrading?.showTradesOnChart === undefined) {
        state.paperTrading.showTradesOnChart = storedBool("aef:paperShowTradesOnChart", true);
      }
      return state.paperTrading.showTradesOnChart !== false;
    }

    function paperWorkSurfaceActive() {
      if (state.sideTab === "go") return true;
      if (typeof currentOptionDriveState === "function" && currentOptionDriveState().open) return true;
      if (state.paperTrading?.armed) return true;
      if (state.paperTrading?.dragOrder) return true;
      return false;
    }

    function paperPollingActive(options = {}) {
      if (options.force) return true;
      if (document.visibilityState !== "visible" || state.serverSleeping) return false;
      if (options.activeOwner !== true && !backgroundPollingActive()) return false;
      return paperWorkSurfaceActive();
    }


    function paperJournalUrl(path = "/paper/trades") {
      const params = new URLSearchParams({
        limit: "1000",
        instrument_id: state.instrumentId,
        route_fingerprint: instrumentRouteFingerprint(),
      });
      return `${path}?${params.toString()}`;
    }

    function openPaperJournalWindow() {
      window.open(paperJournalUrl("/paper/trades"), "_blank", "noopener");
    }

    function downloadPaperJournalCsv() {
      window.open(paperJournalUrl("/api/paper/trades.csv"), "_blank", "noopener");
    }

    async function loadPaperStats(options = {}) {
      if (!paperPollingActive(options)) {
        if (window.mcTelemetryInc) window.mcTelemetryInc("poll.paper_stats.skipped_inactive");
        return state.paperStats;
      }
      const requestInstrumentId = state.instrumentId;
      const requestRouteFingerprint = instrumentRouteFingerprint();
      const requestKey = JSON.stringify([requestInstrumentId, requestRouteFingerprint]);
      if (state.paperStatsPromise && state.paperStatsKey === requestKey) {
        if (!options.force || state.paperStatsForce) return state.paperStatsPromise;
      }
      state.lastPaperStatsLoadAt = Date.now();
      if (state.paperStatsKey !== requestKey) {
        state.paperStats = null;
        state.paperStatsKey = requestKey;
        state.paperStatsBaseline = null;
        state.paperStatsBaselineKey = requestKey;
      }
      state.loadingPaperStats = true;
      state.paperStatsForce = Boolean(options.force);
      if (typeof renderPaperJournals === "function") renderPaperJournals();
      const request = (async () => {
        const params = new URLSearchParams({
          limit: "500",
          instrument_id: requestInstrumentId,
          route_fingerprint: requestRouteFingerprint,
        });
        try {
          await loadPaperOrders(options);
          const payload = await fetchJson(`/api/paper/trades?${params.toString()}`, {
            cache: "no-store",
            sharedTtlMs: options.force ? 0 : 1500,
          });
          if (state.instrumentId !== requestInstrumentId || instrumentRouteFingerprint() !== requestRouteFingerprint) return state.paperStats;
          if (payload?.ok && exactIdentityText(payload?.route_fingerprint) === requestRouteFingerprint) {
            const previousSuccessfulStats = state.paperStatsBaselineKey === requestKey
              ? state.paperStatsBaseline
              : null;
            publishPaperFillTransitions(previousSuccessfulStats, payload);
            state.paperStats = payload;
            state.paperStatsBaseline = payload;
            state.paperStatsBaselineKey = requestKey;
          } else {
            state.paperStats = {
              stats: null,
              trades: [],
              fills: [],
              account: { available: false },
            };
          }
        } catch (error) {
          if (state.instrumentId !== requestInstrumentId || instrumentRouteFingerprint() !== requestRouteFingerprint) return state.paperStats;
          const message = requestErrorMessage(error, "paper stats load failed");
          state.paperStats = {
            ok: false,
            message,
            stats: null,
            trades: [],
            fills: [],
            account: { available: false },
          };
        } finally {
          if (state.paperStatsPromise === request) {
            state.loadingPaperStats = false;
            state.paperStatsPromise = null;
            state.paperStatsForce = false;
            if (typeof renderPaperJournals === "function") renderPaperJournals();
            if (typeof renderPaperTradingLayerNow === "function") renderPaperTradingLayerNow();
            else if (state.snapshot) renderCharts(state.snapshot, { overlayImmediate: true });
          }
        }
        return state.paperStats;
      })();
      state.paperStatsPromise = request;
      return request;
    }

    function paperTradingInstrumentId() {
      const instrumentId = exactIdentityText(state.instrumentId);
      return instrumentId && instrumentForId(instrumentId) ? instrumentId : "";
    }

    function paperTradingInstrumentSettingKey(settingName) {
      const instrumentId = paperTradingInstrumentId();
      const name = String(settingName || "");
      if (!instrumentId || !["paperAutoTrading", "paperRiskPrefs"].includes(name)) return "";
      return `aef:instrument:${JSON.stringify([instrumentId])}:${name}`;
    }

    function applyPaperTradingRiskPrefs(prefs) {
      const cfg = state.paperTrading || {};
      cfg.orderType = "limit";
      cfg.qty = 1;
      cfg.useStopLoss = true;
      cfg.stopPoints = 8;
      cfg.useTarget = true;
      cfg.targetPoints = 16;
      if (prefs && typeof prefs === "object") {
        if (prefs.orderType) cfg.orderType = String(prefs.orderType);
        if (Number.isFinite(Number(prefs.qty))) cfg.qty = clamp(Number(prefs.qty), 0.1, 100);
        if (typeof prefs.useStopLoss === "boolean") cfg.useStopLoss = prefs.useStopLoss;
        if (Number.isFinite(Number(prefs.stopPoints))) cfg.stopPoints = clamp(Number(prefs.stopPoints), 0.01, 500);
        if (typeof prefs.useTarget === "boolean") cfg.useTarget = prefs.useTarget;
        if (Number.isFinite(Number(prefs.targetPoints))) cfg.targetPoints = clamp(Number(prefs.targetPoints), 0.01, 1000);
      }
      cfg.riskPrefsKey = paperTradingInstrumentSettingKey("paperRiskPrefs");
      state.paperTrading = cfg;
    }

    function loadPaperTradingInstrumentSettings() {
      const autoTradingKey = paperTradingInstrumentSettingKey("paperAutoTrading");
      state.settings.paperAutoTrading = autoTradingKey
        ? storedBool(autoTradingKey, false)
        : false;
      const riskPrefsKey = paperTradingInstrumentSettingKey("paperRiskPrefs");
      if (!riskPrefsKey) {
        applyPaperTradingRiskPrefs({});
        return;
      }
      let prefs = {};
      try {
        const raw = serverSettingValue(riskPrefsKey);
        prefs = JSON.parse(raw || "{}");
      } catch (_) {
        prefs = {};
      }
      applyPaperTradingRiskPrefs(prefs);
    }

    function ensurePaperTradingInstrumentSettings() {
      const riskPrefsKey = paperTradingInstrumentSettingKey("paperRiskPrefs");
      if (state.paperTrading?.riskPrefsKey !== riskPrefsKey) loadPaperTradingInstrumentSettings();
    }

    function savePaperTradingRiskPrefs() {
      const cfg = state.paperTrading || {};
      const riskPrefsKey = paperTradingInstrumentSettingKey("paperRiskPrefs");
      if (!riskPrefsKey) return;
      cfg.riskPrefsKey = riskPrefsKey;
      const payload = JSON.stringify({
        orderType: cfg.orderType || "limit",
        qty: clamp(Number(cfg.qty) || 1, 0.1, 100),
        useStopLoss: cfg.useStopLoss !== false,
        stopPoints: clamp(Number(cfg.stopPoints) || 8, 0.01, 500),
        useTarget: cfg.useTarget !== false,
        targetPoints: clamp(Number(cfg.targetPoints) || 16, 0.01, 1000),
      });
      setServerSettingValue(cfg.riskPrefsKey, payload);
    }

    function syncPaperTradingRiskControlsFromDom() {
      const cfg = state.paperTrading || {};
      const type = document.getElementById("paper-order-type");
      const qty = document.getElementById("paper-order-qty");
      const useStop = document.getElementById("paper-order-use-stop");
      const stop = document.getElementById("paper-order-stop-points");
      const useTarget = document.getElementById("paper-order-use-target");
      const target = document.getElementById("paper-order-target-points");
      if (type) cfg.orderType = type.value || "limit";
      if (qty) cfg.qty = clamp(Number(qty.value) || 1, 0.1, 100);
      if (useStop) cfg.useStopLoss = useStop.checked;
      if (stop) cfg.stopPoints = clamp(Number(stop.value) || 8, 0.01, 500);
      if (useTarget) cfg.useTarget = useTarget.checked;
      if (target) cfg.targetPoints = clamp(Number(target.value) || 16, 0.01, 1000);
      state.paperTrading = cfg;
    }

    function paperTradingSetStatus(text) {
      state.paperTrading.status = text;
      const node = document.getElementById("paper-trade-status");
      if (node) node.textContent = text;
    }

    function publishPaperOrderFeedback(order, options = {}) {
      const status = String(options.status || order?.status || "pending").toLowerCase();
      const side = String(order?.side || options.side || "").toUpperCase();
      const orderType = String(order?.order_type || options.orderType || "").toUpperCase();
      const price = Number(order?.fill_price ?? order?.entry ?? options.price);
      const titleByStatus = {
        pending: "Paper order pending",
        filled: "Paper order filled",
        cancelled: "Paper order cancelled",
        error: "Paper order failed",
      };
      const tone = status === "filled" ? "success" : status === "error" ? "error" : status === "pending" ? "warning" : "neutral";
      const ts = order?.filled_at || order?.cancelled_at || order?.updated_at || order?.created_at || options.ts || new Date().toISOString();
      return publishUiFeedback({
        kind: "paper_order",
        category: "execution",
        source: "paper_trading",
        code: `paper_order_${status}`,
        entity_id: String(order?.id || options.entityId || "paper-order"),
        instrument_id: exactIdentityText(order?.instrument_id),
        route_fingerprint: exactIdentityText(order?.route_fingerprint),
        timeframe: String(order?.timeframe || state.timeframe || ""),
        status,
        title: titleByStatus[status] || `Paper order ${status}`,
        detail: options.detail || [side, orderType, Number.isFinite(price) ? `@ ${fmt(price)}` : ""].filter(Boolean).join(" "),
        ts,
        severity: status === "error" ? "high" : status === "pending" ? "warning" : "info",
        tone,
        target_id: "paper-trade-panel",
        target_tab: "go",
        toast: options.toast === true || status === "error",
        attention: status === "error",
      });
    }

    function publishPaperOrderTransitions(previousOrders, nextOrders) {
      const previousById = new Map((previousOrders || []).map(order => [String(order?.id || ""), order]));
      for (const order of nextOrders || []) {
        const id = String(order?.id || "");
        const previous = id ? previousById.get(id) : null;
        if (!previous) continue;
        const previousStatus = String(previous.status || "pending").toLowerCase();
        const nextStatus = String(order?.status || "pending").toLowerCase();
        if (previousStatus === nextStatus) continue;
        publishPaperOrderFeedback(order, { status: nextStatus, toast: nextStatus === "filled" });
      }
    }

    function publishPaperFillTransitions(previousStats, nextStats) {
      if (!previousStats || !nextStats) return;
      const previousKeys = new Set((previousStats.fills || []).map(fill => JSON.stringify([
        fill?.id || fill?.order_id || "",
        fill?.filled_at || "",
        fill?.role || "",
        fill?.price ?? "",
      ])));
      for (const fill of nextStats.fills || []) {
        const fillKey = JSON.stringify([
          fill?.id || fill?.order_id || "",
          fill?.filled_at || "",
          fill?.role || "",
          fill?.price ?? "",
        ]);
        if (previousKeys.has(fillKey)) continue;
        publishPaperOrderFeedback({
          ...fill,
          id: fill?.order_id || fill?.id,
          status: "filled",
          order_type: fill?.role || "fill",
          fill_price: fill?.price,
        }, { status: "filled", toast: true });
      }
    }

    function renderPaperTradingLayerNow() {
      if (!state.snapshot) return;
      if (typeof renderPriceTrading === "function") renderPriceTrading(state.snapshot);
      else renderCharts(state.snapshot, { overlayImmediate: true });
    }

    function paperTradingRouteReady() {
      return Boolean(
        exactIdentityText(state.instrumentId)
        && instrumentRouteFingerprint()
        && snapshotMatchesCurrentRoute(state.snapshot)
      );
    }

    function renderPaperTradingExecutionEligibility(nowMs = Date.now()) {
      const cfg = state.paperTrading || {};
      const routeReady = paperTradingRouteReady();
      const quoteState = typeof currentQuotePresentationState === "function"
        ? currentQuotePresentationState(nowMs)
        : "unavailable";
      const session = snapshotSessionPresentation(state.snapshot);
      const actionState = cfg.submitting
        ? "submitting"
        : !routeReady
          ? "blocked"
          : cfg.armed
            ? "armed"
            : "ready";
      const actionTitle = cfg.submitting
        ? "The current paper order request is in progress."
        : !routeReady
          ? "Paper order entry is blocked until the current chart snapshot matches the exact instrument route and timeframe."
          : cfg.armed
            ? "The chart cursor is armed for one manual paper order."
            : "Manual paper order selection is ready; the server revalidates the exact route on submit.";
      renderExecutionEligibility(
        document.getElementById("paper-execution-eligibility"),
        actionState,
        [
          {
            label: "ROUTE",
            state: routeReady ? "ready" : "blocked",
            title: routeReady ? "Current chart snapshot matches the exact instrument route and timeframe." : actionTitle,
          },
          {
            label: "QUOTE",
            state: quoteState,
            title: "Reference quote freshness is shown explicitly; manual limit/stop selection does not treat it as execution authority.",
          },
          { label: "SESSION", state: session.state, title: session.title },
        ],
        { title: actionTitle },
      );
      const disabled = !routeReady || cfg.submitting;
      const buy = document.getElementById("paper-trade-buy");
      const sell = document.getElementById("paper-trade-sell");
      if (buy) buy.disabled = disabled;
      if (sell) sell.disabled = disabled;
      return actionState;
    }

    function refreshPaperTradingFreshness(nowMs = Date.now()) {
      return renderPaperTradingExecutionEligibility(nowMs);
    }

    function renderPaperTradingPanel() {
      ensurePaperTradingInstrumentSettings();
      const cfg = state.paperTrading || {};
      const panel = document.getElementById("paper-trade-panel");
      const status = document.getElementById("paper-trade-mode-status");
      if (!panel || !status) return;
      panel.classList.toggle("hidden", cfg.expanded === false);
      status.className = cfg.armed ? String(cfg.side || "") : "";
      applyUiPresentationState(status, cfg.armed ? "armed" : "idle", {
        label: cfg.armed ? `${String(cfg.side || "").toUpperCase()} ARMED` : "PAPER",
      });
      document.getElementById("paper-trade-buy")?.classList.toggle("active", cfg.side === "long" && cfg.armed);
      document.getElementById("paper-trade-sell")?.classList.toggle("active", cfg.side === "short" && cfg.armed);
      const type = document.getElementById("paper-order-type");
      const qty = document.getElementById("paper-order-qty");
      const useStop = document.getElementById("paper-order-use-stop");
      const stop = document.getElementById("paper-order-stop-points");
      const useTarget = document.getElementById("paper-order-use-target");
      const target = document.getElementById("paper-order-target-points");
      const showTrades = document.getElementById("paper-show-trades-on-chart");
      if (type) type.value = cfg.orderType || "limit";
      if (qty) qty.value = String(cfg.qty || 1);
      if (useStop) useStop.checked = cfg.useStopLoss !== false;
      if (stop) stop.value = String(cfg.stopPoints || 8);
      if (stop) stop.disabled = cfg.useStopLoss === false;
      if (useTarget) useTarget.checked = cfg.useTarget !== false;
      if (target) target.value = String(cfg.targetPoints || 16);
      if (target) target.disabled = cfg.useTarget === false;
      if (showTrades) showTrades.checked = paperShowTradesOnChart();
      paperTradingSetStatus(cfg.status || "Select BUY or SELL, then click a price on the chart.");
      renderPaperTradingExecutionEligibility();
    }

    function armPaperTrade(side) {
      const cfg = state.paperTrading || {};
      if (cfg.armed && cfg.side === side) {
        clearPaperTradeArm();
        return false;
      }
      if (cfg.submitting) return false;
      if (!paperTradingRouteReady()) {
        paperTradingSetStatus("Order entry blocked: current chart route is not synchronized.");
        renderPaperTradingExecutionEligibility();
        return false;
      }
      state.paperTrading.side = side;
      state.paperTrading.armed = true;
      state.paperTrading.status = `${side === "long" ? "BUY" : "SELL"} armed. Click the chart price to place a ${state.paperTrading.orderType || "limit"} order.`;
      state.sideTab = "go";
      workspaceSet("sideTab", state.sideTab);
      applySideTabs();
      renderPaperTradingPanel();
      const chart = document.getElementById("price-chart");
      chart?.classList.remove("paper-trade-armed-long", "paper-trade-armed-short");
      chart?.classList.add("paper-trade-armed", side === "short" ? "paper-trade-armed-short" : "paper-trade-armed-long");
      return true;
    }

    function clearPaperTradeArm(message = "Select BUY or SELL, then click a price on the chart.") {
      state.paperTrading.armed = false;
      state.paperTrading.side = null;
      state.paperTrading.status = message;
      document.getElementById("price-chart")?.classList.remove("paper-trade-armed", "paper-trade-armed-long", "paper-trade-armed-short");
      renderPaperTradingPanel();
      renderPaperTradingLayerNow();
    }

    async function submitPaperOrderAtPrice(entry) {
      const cfg = state.paperTrading || {};
      if (
        cfg.submitting
        || !cfg.armed
        || !cfg.side
        || !paperTradingRouteReady()
        || !Number.isFinite(Number(entry))
      ) return false;
      savePaperTradingRiskPrefs();
      const risk = paperTradingRiskPrefsPayload();
      const requestScope = {
        order_id: `po-${createBrowserUuidV4()}`,
        instrument_id: exactIdentityText(state.instrumentId),
        route_fingerprint: instrumentRouteFingerprint(),
        timeframe: String(state.timeframe || ""),
      };
      const payload = {
        id: requestScope.order_id,
        instrument_id: requestScope.instrument_id,
        route_fingerprint: requestScope.route_fingerprint,
        timeframe: requestScope.timeframe,
        side: cfg.side,
        order_type: cfg.orderType || "limit",
        qty: Number(cfg.qty) || 1,
        entry: Number(entry),
        stop_points: risk.stop_points,
        target_points: risk.target_points,
        use_stop_loss: risk.use_stop_loss,
        use_target: risk.use_target,
        note: `manual ${cfg.orderType || "limit"}`,
      };
      paperTradingSetStatus(`Sending ${payload.side.toUpperCase()} ${payload.order_type} @ ${fmt(payload.entry)}...`);
      publishUiFeedback({
        kind: "paper_order",
        category: "execution",
        source: "paper_trading",
        code: "paper_order_submitting",
        entity_id: `paper-submit-${state.instrumentId}`,
        instrument_id: state.instrumentId,
        route_fingerprint: instrumentRouteFingerprint(),
        timeframe: String(state.timeframe || ""),
        status: "submitting",
        title: "Sending paper order",
        detail: `${payload.side.toUpperCase()} ${payload.order_type.toUpperCase()} @ ${fmt(payload.entry)}`,
        ts: new Date().toISOString(),
        severity: "info",
        tone: "neutral",
        target_id: "paper-trade-panel",
        target_tab: "go",
      });
      cfg.submitting = true;
      renderPaperTradingPanel();
      try {
        const response = await fetchJson("/api/paper/orders", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
          cache: "no-store",
        });
        if (!response?.ok) throw new Error(apiErrorMessage(response, "paper order failed"));
        const responseOrder = response?.order;
        if (
          exactIdentityText(response?.instrument_id) !== requestScope.instrument_id
          || exactIdentityText(response?.route_fingerprint) !== requestScope.route_fingerprint
          || !responseOrder
          || exactIdentityText(responseOrder.id) !== requestScope.order_id
          || exactIdentityText(responseOrder.instrument_id) !== requestScope.instrument_id
          || exactIdentityText(responseOrder.route_fingerprint) !== requestScope.route_fingerprint
        ) throw new Error("paper order route identity mismatch");
        rawLocalStorageSetItem("aef:paperJournalUpdatedAt", String(Date.now()));
        if (
          exactIdentityText(state.instrumentId) === requestScope.instrument_id
          && instrumentRouteFingerprint() === requestScope.route_fingerprint
        ) {
          const existing = Array.isArray(state.paperOrders) ? state.paperOrders : [];
          state.paperOrders = String(responseOrder.status || "pending") === "pending"
            ? [responseOrder, ...existing.filter(order => exactIdentityText(order.id) !== responseOrder.id)].slice(0, 100)
            : existing.filter(order => exactIdentityText(order.id) !== responseOrder.id);
        }
        const responseFilled = Boolean(response.filled);
        const responsePrice = responseOrder.fill_price ?? payload.entry;
        const doneMessage = response.flipped
          ? `Flipped to ${payload.side.toUpperCase()} @ ${fmt(responsePrice)}`
          : response.closed
            ? `Closed position @ ${fmt(responsePrice)}`
            : response.cancelled
              ? "Paper order is already cancelled"
            : responseFilled
              ? `Filled ${payload.side.toUpperCase()} @ ${fmt(responsePrice)}`
              : `Placed ${payload.side.toUpperCase()} ${payload.order_type} @ ${fmt(payload.entry)}`;
        publishPaperOrderFeedback(responseOrder, {
          status: responseFilled ? "filled" : String(responseOrder.status || "pending"),
          side: payload.side,
          orderType: payload.order_type,
          price: responsePrice,
          toast: responseFilled,
        });
        clearPaperTradeArm(doneMessage);
        if (
          exactIdentityText(state.instrumentId) === requestScope.instrument_id
          && instrumentRouteFingerprint() === requestScope.route_fingerprint
        ) await loadPaperStats({ force: true });
        return true;
      } catch (error) {
        const message = requestErrorMessage(error, "paper order failed");
        paperTradingSetStatus(`Order error: ${message}`);
        publishPaperOrderFeedback(payload, {
          status: "error",
          side: payload.side,
          orderType: payload.order_type,
          price: payload.entry,
          detail: message,
          entityId: `paper-submit-${state.instrumentId}`,
          toast: true,
        });
        return false;
      } finally {
        cfg.submitting = false;
        renderPaperTradingPanel();
      }
    }

    function paperTradingHandleChartClick(localX, localY) {
      if (!state.paperTrading?.armed) return false;
      if (state.paperTrading.submitting) return true;
      if (!paperTradingRouteReady()) {
        clearPaperTradeArm("Order entry blocked: current chart route is not synchronized.");
        return true;
      }
      const point = chartPointFromLocal(localX, localY);
      if (!point || !Number.isFinite(Number(point.price))) return false;
      submitPaperOrderAtPrice(point.price);
      return true;
    }

    async function loadPaperOrders(options = {}) {
      if (!paperPollingActive(options)) {
        if (window.mcTelemetryInc) window.mcTelemetryInc("poll.paper_orders.skipped_inactive");
        return Array.isArray(state.paperOrders) ? state.paperOrders : [];
      }
      const requestInstrumentId = state.instrumentId;
      const requestRouteFingerprint = instrumentRouteFingerprint();
      const requestKey = JSON.stringify([requestInstrumentId, requestRouteFingerprint]);
      if (state.paperOrdersPromise && state.paperOrdersKey === requestKey) return state.paperOrdersPromise;
      const request = (async () => {
        try {
          const params = new URLSearchParams({
            limit: "200",
            since_hours: "24",
            instrument_id: requestInstrumentId,
            route_fingerprint: requestRouteFingerprint,
          });
          const payload = await fetchJson(`/api/paper/orders?${params.toString()}`, { cache: "no-store", sharedTtlMs: 1500 });
          if (
            state.instrumentId === requestInstrumentId
            && instrumentRouteFingerprint() === requestRouteFingerprint
            && payload?.ok
            && exactIdentityText(payload?.instrument_id) === requestInstrumentId
            && exactIdentityText(payload?.route_fingerprint) === requestRouteFingerprint
            && Array.isArray(payload.orders)
          ) {
            const previousOrders = Array.isArray(state.paperOrders) ? state.paperOrders : [];
            publishPaperOrderTransitions(previousOrders, payload.orders);
            state.paperOrders = payload.orders;
          }
          if (typeof touchChartUserObjectsVersion === "function") touchChartUserObjectsVersion();
        } catch (error) {
          if (state.instrumentId === requestInstrumentId && instrumentRouteFingerprint() === requestRouteFingerprint) {
            state.paperOrders = Array.isArray(state.paperOrders) ? state.paperOrders : [];
          }
        } finally {
          if (state.paperOrdersPromise === request) state.paperOrdersPromise = null;
        }
        return state.paperOrders;
      })();
      state.paperOrdersKey = requestKey;
      state.paperOrdersPromise = request;
      return request;
    }

    async function refreshPaperScopeAfterMutation(scope) {
      const instrumentId = exactIdentityText(scope?.instrument_id);
      const routeFingerprint = exactIdentityText(scope?.route_fingerprint);
      if (!instrumentId || !routeFingerprint) return;
      if (
        exactIdentityText(state.instrumentId) === instrumentId
        && instrumentRouteFingerprint() === routeFingerprint
      ) {
        await loadPaperStats({ force: true });
      }
      const driveContext = typeof optionDriveTradeContext === "function"
        ? optionDriveTradeContext()
        : null;
      if (
        driveContext
        && driveContext.instrumentId === instrumentId
        && driveContext.routeFingerprint === routeFingerprint
      ) await loadOptionDrivePaperStats({ force: true });
    }

    async function cancelPaperOrder(id, scopedOrder = null) {
      const requestOrderId = exactIdentityText(id);
      if (!requestOrderId) return;
      const exactOrder = (
        scopedOrder
        && exactIdentityText(scopedOrder?.id) === requestOrderId
      ) ? scopedOrder : null;
      const requestScope = {
        instrument_id: exactIdentityText(exactOrder?.instrument_id || state.instrumentId),
        route_fingerprint: exactIdentityText(
          exactOrder?.route_fingerprint || instrumentRouteFingerprint(),
        ),
        timeframe: String(exactOrder?.timeframe || state.timeframe || ""),
      };
      try {
        const response = await fetchJson(`/api/paper/orders/${encodeURIComponent(requestOrderId)}/cancel`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            instrument_id: requestScope.instrument_id,
            route_fingerprint: requestScope.route_fingerprint,
          }),
          cache: "no-store",
        });
        if (!response?.ok) throw new Error(apiErrorMessage(response, "cancel failed"));
        const cancelledOrder = response?.order;
        if (
          response?.cancelled !== true
          || exactIdentityText(response?.instrument_id) !== requestScope.instrument_id
          || exactIdentityText(response?.route_fingerprint) !== requestScope.route_fingerprint
          || !cancelledOrder
          || exactIdentityText(cancelledOrder.id) !== requestOrderId
          || exactIdentityText(cancelledOrder.instrument_id) !== requestScope.instrument_id
          || exactIdentityText(cancelledOrder.route_fingerprint) !== requestScope.route_fingerprint
          || cancelledOrder.status !== "cancelled"
        ) throw new Error("paper cancel route identity mismatch");
        publishPaperOrderFeedback(cancelledOrder, {
          status: "cancelled",
          entityId: requestOrderId,
        });
        rawLocalStorageSetItem("aef:paperJournalUpdatedAt", String(Date.now()));
        if (
          exactIdentityText(state.instrumentId) === requestScope.instrument_id
          && instrumentRouteFingerprint() === requestScope.route_fingerprint
        ) {
          state.paperOrders = Array.isArray(state.paperOrders)
            ? state.paperOrders.filter(order => exactIdentityText(order.id) !== requestOrderId)
            : [];
          renderPaperJournals();
          renderPaperTradingLayerNow();
        }
        optionDriveRemoveJournalOrder(requestOrderId, requestScope);
        await refreshPaperScopeAfterMutation(requestScope);
      } catch (error) {
        publishPaperOrderFeedback({ id: requestOrderId, ...requestScope }, {
          status: "error",
          entityId: requestOrderId,
          detail: `Cancel failed: ${requestErrorMessage(error, "cancel failed")}`,
          toast: true,
        });
      }
    }

    async function updatePaperOrder(id, patch) {
      const requestOrderId = exactIdentityText(id);
      if (!requestOrderId || !patch) return null;
      const requestScope = {
        instrument_id: exactIdentityText(state.instrumentId),
        route_fingerprint: instrumentRouteFingerprint(),
      };
      const response = await fetchJson(`/api/paper/orders/${encodeURIComponent(requestOrderId)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ...patch,
          instrument_id: requestScope.instrument_id,
          route_fingerprint: requestScope.route_fingerprint,
        }),
        cache: "no-store",
      });
      if (!response?.ok) throw new Error(apiErrorMessage(response, "paper order update failed"));
      const responseOrder = response?.order;
      if (
        exactIdentityText(response?.instrument_id) !== requestScope.instrument_id
        || exactIdentityText(response?.route_fingerprint) !== requestScope.route_fingerprint
        || !responseOrder
        || exactIdentityText(responseOrder.id) !== requestOrderId
        || exactIdentityText(responseOrder.instrument_id) !== requestScope.instrument_id
        || exactIdentityText(responseOrder.route_fingerprint) !== requestScope.route_fingerprint
        || responseOrder.status !== "pending"
      ) throw new Error("paper update route identity mismatch");
      if (
        exactIdentityText(state.instrumentId) === requestScope.instrument_id
        && instrumentRouteFingerprint() === requestScope.route_fingerprint
      ) {
        state.paperOrders = Array.isArray(state.paperOrders)
          ? state.paperOrders.map(order => exactIdentityText(order.id) === requestOrderId ? responseOrder : order)
          : [responseOrder];
      }
      rawLocalStorageSetItem("aef:paperJournalUpdatedAt", String(Date.now()));
      renderPaperJournals();
      renderPaperTradingLayerNow();
      return responseOrder;
    }

    function paperJournalTime(value) {
      const ts = Date.parse(value || "");
      if (!Number.isFinite(ts)) return "-";
      return axisTimeFormatter.format(new Date(ts));
    }

    function paperInitiatorSourceLabel(item) {
      const payload = item?.payload && typeof item.payload === "object" ? item.payload : {};
      const nested = payload.payload && typeof payload.payload === "object" ? payload.payload : {};
      const displayLabel = String(
        item?.signal_source_label
        || payload.signal_source_label
        || nested.signal_source_label
        || "",
      ).trim();
      if (displayLabel) return displayLabel.replace(/_/g, " ");
      const source = String(
        item?.signal_source
        || payload.signal_source
        || nested.signal_source
        || payload.indicator_source
        || nested.indicator_source
        || "",
      ).trim().toLowerCase();
      if (source && !["trade_setup", "trade setup", "decision", "level_cluster", "paper"].includes(source)) {
        return source.replace(/_/g, " ");
      }
      return "";
    }

    function paperJournalSourceLabel(item) {
      const initiator = paperInitiatorSourceLabel(item);
      if (initiator) return initiator;
      const payload = item?.payload && typeof item.payload === "object" ? item.payload : {};
      const nested = payload.payload && typeof payload.payload === "object" ? payload.payload : {};
      const indicator = String(
        item?.source
        || nested.indicator_source
        || payload.indicator_source
        || nested.source_label
        || payload.source_label
        || "",
      ).trim();
      if (indicator && !indicator.toLowerCase().startsWith("manual")) {
        const normalized = indicator.replace(/^indicator:/i, "").replace(/_/g, " ").trim().toLowerCase();
        if (normalized && normalized !== "paper" && normalized !== "trade setup") return indicator.replace(/^indicator:/i, "").replace(/_/g, " ").trim();
      }
      const rawSource = String(item?.source || payload.source || nested.source || "").trim().toLowerCase();
      if (
        !rawSource
        || rawSource === "manual"
        || rawSource === "manual_position"
        || rawSource === "manual_trade_mode"
        || rawSource.startsWith("manual:")
        || rawSource === "paper"
      ) {
        return "manual";
      }
      if (rawSource.startsWith("indicator:")) {
        return rawSource.slice("indicator:".length).replace(/_/g, " ").trim() || "indicator";
      }
      return rawSource.replace(/_/g, " ");
    }


    function paperSigned(value) {
      const number = Number(value);
      if (!Number.isFinite(number)) return "-";
      return `${number > 0 ? "+" : ""}${fmt(number)}`;
    }

    function paperPnlUnitLabel(unit) {
      const value = String(unit || "").trim().toUpperCase();
      if (value === "POINTS") return "PTS";
      return /^[A-Z]{3,8}$/.test(value) ? value : "PNL";
    }

    function paperPnlSummary(account = {}) {
      if (account.available !== true) return { available: false };
      const realized = Number(account.realized_pnl ?? 0) || 0;
      const unrealized = Number(account.unrealized_pnl ?? 0) || 0;
      const net = Number.isFinite(Number(account.net_pnl))
        ? Number(account.net_pnl)
        : realized + unrealized;
      return {
        available: true,
        unit: paperPnlUnitLabel(account.unit),
        realized,
        unrealized,
        net,
      };
    }
    const PAPER_ENTRY_LABEL_OFFSET_KEY = "aef:paperEntryLabelFrameOffset";
    const PAPER_ENTRY_LABEL_DEFAULT_OFFSET = 520;

    function paperEntryLabelFrameOffset() {
      if (state.paperTrading?.entryLabelFrameOffset === undefined) {
        const raw = serverSettingValue(PAPER_ENTRY_LABEL_OFFSET_KEY);
        const value = Number(raw);
        state.paperTrading.entryLabelFrameOffset = raw !== null && Number.isFinite(value)
          ? clamp(value, -600, 1600)
          : PAPER_ENTRY_LABEL_DEFAULT_OFFSET;
      }
      return state.paperTrading.entryLabelFrameOffset;
    }

    function setPaperEntryLabelFrameOffset(value, options = {}) {
      const next = Math.round(clamp(Number(value) || 0, -600, 1600));
      state.paperTrading.entryLabelFrameOffset = next;
      if (options.persist !== false) setServerSettingValue(PAPER_ENTRY_LABEL_OFFSET_KEY, String(next));
      return next;
    }

    function paperEntryLabelX(width, pad, labelW) {
      const rightFrame = width - pad.right;
      const offset = paperEntryLabelFrameOffset();
      return clamp(rightFrame - labelW - offset, pad.left + 4, rightFrame - labelW - 4);
    }

    function paperTradeConnectorColor(fallbackColor) {
      if (isLightTheme()) return rgbaFromCssColor(fallbackColor || "#2563eb", 0.58);
      return "rgba(255, 193, 7, 0.74)";
    }

    function paperPnlClass(value) {
      const number = Number(value);
      if (!Number.isFinite(number) || number === 0) return "flat";
      return number > 0 ? "positive" : "negative";
    }

    async function closeManualPaperTrade(id, scopedTrade = null) {
      const requestTradeId = exactIdentityText(id);
      if (!requestTradeId) return;
      const exactTrade = (
        scopedTrade
        && exactIdentityText(scopedTrade?.id) === requestTradeId
        && String(scopedTrade?.status || "") === "open"
      ) ? scopedTrade : null;
      const trade = exactTrade || paperJournalTrades().find(item => (
        exactIdentityText(item.id) === requestTradeId
        && String(item.status || "") === "open"
      ));
      if (!trade) return;
      const paperContract = trade.paper_contract || trade.payload?.paper_contract;
      const requestScope = {
        instrument_id: exactIdentityText(trade.instrument_id),
        route_fingerprint: exactIdentityText(trade.route_fingerprint),
        paper_contract: paperContract,
      };
      if (
        !requestScope.instrument_id
        || !requestScope.route_fingerprint
        || !paperContract
        || typeof paperContract !== "object"
        || !exactIdentityText(paperContract.scope_key)
      ) {
        showBrowserToast("Close failed: stored paper position route is incomplete");
        return;
      }
      const commandKey = JSON.stringify([
        requestScope.instrument_id,
        requestScope.route_fingerprint,
        paperContract.scope_key,
        requestTradeId,
      ]);
      let commandId = paperCloseCommandIds.get(commandKey);
      if (!commandId) {
        commandId = `pc-${createBrowserUuidV4()}`;
        paperCloseCommandIds.set(commandKey, commandId);
      }
      requestScope.command_id = commandId;
      const action = String(trade.side || "") === "short" ? "buy_close" : "sell_close";
      try {
        const response = await fetchJson(`/api/paper/trades/${encodeURIComponent(requestTradeId)}/close`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            command_id: requestScope.command_id,
            instrument_id: requestScope.instrument_id,
            route_fingerprint: requestScope.route_fingerprint,
            paper_contract: requestScope.paper_contract,
            exit_reason: action,
          }),
          cache: "no-store",
        });
        if (!response?.ok) throw new Error(apiErrorMessage(response, "close failed"));
        const responseContract = response?.paper_contract;
        const exactResponseScope = !(
          exactIdentityText(response?.command_id) !== requestScope.command_id
          || exactIdentityText(response?.instrument_id) !== requestScope.instrument_id
          || exactIdentityText(response?.route_fingerprint) !== requestScope.route_fingerprint
          || exactIdentityText(responseContract?.scope_key) !== exactIdentityText(paperContract.scope_key)
        );
        if (response?.outcome === "pending") {
          const pendingOrder = response?.order;
          const pendingPosition = response?.position;
          if (
            !exactResponseScope
            || !pendingOrder
            || exactIdentityText(pendingOrder.id) !== requestScope.command_id
            || exactIdentityText(pendingOrder.instrument_id) !== requestScope.instrument_id
            || exactIdentityText(pendingOrder.route_fingerprint) !== requestScope.route_fingerprint
            || String(pendingOrder.status || "") !== "pending"
            || !pendingPosition
            || exactIdentityText(pendingPosition.id) !== requestTradeId
            || String(pendingPosition.status || "") !== "open"
          ) throw new Error("paper pending close route identity mismatch");
          rawLocalStorageSetItem("aef:paperJournalUpdatedAt", String(Date.now()));
          showBrowserToast(
            `Close pending — ${String(response.code || "PAPER_EXIT_PRICE_UNAVAILABLE")}: ${String(response.message || "authoritative price unavailable")}`
          );
          optionDriveApplyCloseMutation(response, requestTradeId, requestScope);
          await refreshPaperScopeAfterMutation(requestScope);
          return;
        }
        const closedTrade = response?.trade;
        const closedPosition = response?.position;
        if (
          !["filled", "no_op"].includes(response?.outcome)
          || !exactResponseScope
          || !closedTrade
          || exactIdentityText(closedTrade.id) !== requestTradeId
          || exactIdentityText(closedTrade.instrument_id) !== requestScope.instrument_id
          || exactIdentityText(closedTrade.route_fingerprint) !== requestScope.route_fingerprint
          || closedTrade.status !== "closed"
          || !closedPosition
          || exactIdentityText(closedPosition.id) !== requestTradeId
          || exactIdentityText(closedPosition.instrument_id) !== requestScope.instrument_id
          || exactIdentityText(closedPosition.route_fingerprint) !== requestScope.route_fingerprint
          || closedPosition.status !== "closed"
        ) throw new Error("paper close route identity mismatch");
        rawLocalStorageSetItem("aef:paperJournalUpdatedAt", String(Date.now()));
        optionDriveApplyCloseMutation(response, requestTradeId, requestScope);
        await refreshPaperScopeAfterMutation(requestScope);
        paperCloseCommandIds.delete(commandKey);
      } catch (error) {
        showBrowserToast(`Close failed: ${requestErrorMessage(error, "close failed")}`);
      }
    }

    const PAPER_JOURNAL_RECENT_MS = 24 * 60 * 60 * 1000;

    function paperJournalTimestampMs(value) {
      const ms = Date.parse(value || "");
      return Number.isFinite(ms) ? ms : null;
    }

    function paperJournalIsRecent(value) {
      const ms = paperJournalTimestampMs(value);
      return ms !== null && (Date.now() - ms) <= PAPER_JOURNAL_RECENT_MS;
    }

    function paperJournalOrderTimestamp(order) {
      return order?.filled_at || order?.updated_at || order?.created_at || "";
    }

    function paperJournalOrders() {
      const fromStats = Array.isArray(state.paperStats?.orders) ? state.paperStats.orders : [];
      const fromState = Array.isArray(state.paperOrders) ? state.paperOrders : [];
      const rows = fromStats.length ? fromStats : fromState;
      return rows
        .filter(order => paperJournalIsRecent(paperJournalOrderTimestamp(order)))
        .sort((left, right) => (
          (paperJournalTimestampMs(paperJournalOrderTimestamp(right)) || 0)
          - (paperJournalTimestampMs(paperJournalOrderTimestamp(left)) || 0)
        ));
    }

    function paperJournalFills() {
      const rows = Array.isArray(state.paperStats?.fills) ? state.paperStats.fills : [];
      return rows
        .filter(fill => paperJournalIsRecent(fill.filled_at))
        .sort((left, right) => (
          (paperJournalTimestampMs(right.filled_at) || 0)
          - (paperJournalTimestampMs(left.filled_at) || 0)
        ));
    }

    function renderPaperJournals() {
      const orderNode = document.getElementById("paper-orders-list");
      const tradeNode = document.getElementById("paper-trades-list");
      const fillNode = document.getElementById("paper-fills-list");
      const accountNode = document.getElementById("paper-account-summary");
      const ordersCount = document.getElementById("paper-orders-count");
      const tradesCount = document.getElementById("paper-trades-count");
      const fillsCount = document.getElementById("paper-fills-count");
      const orders = paperJournalOrders().slice(0, 12);
      const allPositions = paperJournalTrades();
      const openPositions = allPositions.filter(trade => String(trade.status || "").toLowerCase() === "open");
      const closedPositions = allPositions.filter(trade => String(trade.status || "").toLowerCase() !== "open");
      const trades = [...openPositions, ...closedPositions].slice(0, 12);
      const fills = paperJournalFills().slice(0, 12);
      const account = state.paperStats?.account || {};
      const pnlSummary = paperPnlSummary(account);
      if (ordersCount) ordersCount.textContent = String(orders.length);
      if (tradesCount) tradesCount.textContent = String(trades.length);
      if (fillsCount) fillsCount.textContent = String(fills.length);
      if (accountNode) {
        accountNode.innerHTML = pnlSummary.available
          ? `<b>${escapeHtml(pnlSummary.unit)} ${paperSigned(pnlSummary.net)}</b><span><em>Realized ${paperSigned(pnlSummary.realized)}</em><em>Unrealized ${paperSigned(pnlSummary.unrealized)}</em></span>`
          : `<b>Result unavailable</b><span><em>Current PnL facts are incomplete</em></span>`;
      }
      if (orderNode) {
        orderNode.innerHTML = orders.length ? orders.map(order => {
          const status = String(order.status || "pending").toUpperCase();
          const side = String(order.side || "").toUpperCase();
          const role = String(order.role || "entry").toLowerCase();
          const roleLabel = role === "stop" ? "STOP" : role === "take" ? "TAKE" : `${side} ${String(order.order_type || "").toUpperCase()}`;
          const cancel = status === "PENDING" ? `<button class="paper-order-cancel" data-paper-order-cancel="${escapeHtml(order.id)}" title="Cancel order">x</button>` : "";
          const source = ` · ${escapeHtml(paperJournalSourceLabel(order))}`;
          return `<div class="paper-journal-row ${escapeHtml(String(order.status || ""))}">
            <div><b>${escapeHtml(order.symbol || state.symbol)} ${escapeHtml(roleLabel)}</b><span>${escapeHtml(status)} · ${escapeHtml(order.timeframe || state.timeframe)}${source} · ${escapeHtml(paperJournalTime(order.created_at))}</span></div>
            <div class="paper-journal-price">E ${fmt(order.entry)}<br>${order.use_stop_loss === false ? "" : `S ${fmt(order.stop_loss)} `}${order.use_target === false ? "" : `T ${fmt(order.target)}`}</div>${cancel}
          </div>`;
        }).join("") : `<div class="paper-empty">No orders</div>`;
      }
      if (tradeNode) {
        tradeNode.innerHTML = trades.length ? trades.map(trade => {
          const status = String(trade.status || "").toUpperCase();
          const side = String(trade.side || "").toUpperCase();
          const pnlValue = trade.pnl_kind === "realized" ? trade.realized_pnl : trade.unrealized_pnl;
          const pnlLabel = trade.pnl_kind === "realized" ? "Realized" : "Unrealized";
          const pnlUnit = paperPnlUnitLabel(trade.pnl_unit);
          const pnl = trade.pnl_kind ? ` · ${pnlLabel} ${pnlUnit} ${paperSigned(pnlValue)}` : "";
          const pnlBlock = trade.pnl_kind ? `<br><em class="paper-pnl ${paperPnlClass(pnlValue)}">${pnlLabel} ${escapeHtml(pnlUnit)} ${paperSigned(pnlValue)}</em>` : "";
          const close = String(trade.status || "") === "open"
            ? `<button class="paper-order-cancel paper-trade-close" data-paper-trade-close="${escapeHtml(trade.id)}" title="${side === "SHORT" ? "Buy to close" : "Sell to close"}">CLOSE</button>`
            : "";
          const execTime = paperJournalTime(trade.opened_at);
          const closeTime = trade.closed_at ? ` → ${paperJournalTime(trade.closed_at)}` : "";
          const source = ` · ${escapeHtml(paperJournalSourceLabel(trade))}`;
          const qtyLabel = Number.isFinite(Number(trade.qty)) ? `Q ${fmt(trade.qty)} · ` : "";
          return `<div class="paper-journal-row ${escapeHtml(String(trade.status || ""))}">
            <div><b>${escapeHtml(trade.symbol || state.symbol)} ${escapeHtml(side)} ${escapeHtml(status)}</b><span>${escapeHtml(execTime)}${escapeHtml(closeTime)} · ${escapeHtml(trade.timeframe || state.timeframe)}${source}${escapeHtml(pnl)}</span></div>
            <div class="paper-journal-price">${qtyLabel}E ${fmt(trade.entry)}${trade.exit_price ? ` → ${fmt(trade.exit_price)}` : ""}${pnlBlock}</div>${close}
          </div>`;
        }).join("") : `<div class="paper-empty">No positions</div>`;
      }
      if (fillNode) {
        fillNode.innerHTML = fills.length ? fills.map(fill => {
          const role = String(fill.role || "entry").toUpperCase();
          const side = String(fill.side || "").toUpperCase();
          const pnl = fill.pnl_points === null || fill.pnl_points === undefined ? "" : ` · PnL ${fmt(fill.pnl_points)} pts`;
          const source = ` · ${escapeHtml(paperJournalSourceLabel(fill))}`;
          return `<div class="paper-journal-row ${escapeHtml(String(fill.role || ""))}">
            <div><b>${escapeHtml(fill.symbol || state.symbol)} ${escapeHtml(side)} ${escapeHtml(role)}</b><span>${escapeHtml(paperJournalTime(fill.filled_at))} · ${escapeHtml(fill.timeframe || state.timeframe)}${source}${escapeHtml(pnl)}</span></div>
            <div class="paper-journal-price">${fmt(fill.price)}<br>Q ${fmt(fill.qty)}</div>
          </div>`;
        }).join("") : `<div class="paper-empty">No fills</div>`;
      }
      if (typeof renderOptionDriveJournal === "function") renderOptionDriveJournal();
    }

    function drawablePaperTradeOrders() {
      return (Array.isArray(state.paperOrders) ? state.paperOrders : [])
        .filter(order => (
          String(order.status || "pending") === "pending"
          && exactIdentityText(order.instrument_id) === exactIdentityText(state.instrumentId)
          && exactIdentityText(order.route_fingerprint) === instrumentRouteFingerprint()
          && String(order.timeframe || "") === String(state.timeframe || "")
        ));
    }

    function paperJournalTrades() {
      const scoped = Array.isArray(state.paperStats?.trades)
        ? state.paperStats.trades
        : [];
      const detached = Array.isArray(state.paperStats?.detached_open_trades)
        ? state.paperStats.detached_open_trades
        : [];
      const scopedIds = new Set(scoped.map(trade => exactIdentityText(trade?.id)));
      return [
        ...scoped,
        ...detached.filter(trade => !scopedIds.has(exactIdentityText(trade?.id))),
      ];
    }

    function drawablePaperTrades() {
      return paperJournalTrades()
        .filter(trade => (
          String(trade.status || "").toLowerCase() === "open"
          && Number.isFinite(Number(trade.entry))
          && exactIdentityText(trade.instrument_id) === exactIdentityText(state.instrumentId)
          && exactIdentityText(trade.route_fingerprint) === instrumentRouteFingerprint()
          && String(trade.timeframe || "") === String(state.timeframe || "")
        ));
    }

    function paperActiveTradeForChart(snapshot = null) {
      const instrumentId = exactIdentityText(state.instrumentId);
      const timeframe = String(state.timeframe || "");
      const open = drawablePaperTrades()[0] || null;
      const orders = Array.isArray(state.paperOrders)
        ? state.paperOrders.filter(order => (
          String(order.status || "pending") === "pending"
          && exactIdentityText(order.instrument_id) === instrumentId
          && exactIdentityText(order.route_fingerprint) === instrumentRouteFingerprint()
          && String(order.timeframe || "") === timeframe
          && ["stop", "take"].includes(String(order.role || "").toLowerCase())
        ))
        : [];
      return { active: Boolean(open || orders.length), open, orders, symbol: state.symbol, instrumentId, timeframe };
    }

    function paperTradePlanTooltipItem(trade) {
      const payload = trade?.payload && typeof trade.payload === "object" ? trade.payload : {};
      const signal = payload.signal && typeof payload.signal === "object" ? payload.signal : {};
      const side = String(trade?.side || payload.side || signal.side || signal.direction || "").toLowerCase();
      return {
        source: payload.indicator_source || trade.source || "paper",
        signal_source: payload.signal_source || signal.signal_source,
        action_card: payload.action_card || signal.action_card,
        plan: {
          entry: Number(trade?.entry),
          stop: Number(trade?.stop),
          target: Number(trade?.target),
          direction: side,
          reason: signal.reason ?? payload.reason,
        },
        direction: side,
        side,
        action: "GO",
        score: signal.score ?? payload.score,
        setup: signal.setup ?? payload.setup,
        code: signal.code ?? payload.code,
        reason: signal.reason ?? payload.reason,
        confluence_sources: payload.confluence_sources || signal.confluence_sources,
      };
    }

    function paperTradeEntryTooltip(trade) {
      if (!trade || typeof trade !== "object") return "";
      const payload = trade.payload && typeof trade.payload === "object" ? trade.payload : {};
      const item = paperTradePlanTooltipItem(trade);
      const source = payload.signal_source || trade.source || payload.indicator_source || "paper";
      const reason = item.reason || payload.reason || "";
      if (typeof standardSignalTooltip === "function") {
        return standardSignalTooltip(item, { source, force: true });
      }
      const lines = [
        `${String(trade.side || "").toUpperCase()} ENTRY`,
        `Source: ${paperJournalSourceLabel(trade)}`,
        Number.isFinite(Number(trade.entry)) ? `ENTRY: ${fmt(trade.entry)}` : "",
        Number.isFinite(Number(trade.stop)) ? `STOP: ${fmt(trade.stop)}` : "",
        Number.isFinite(Number(trade.target)) ? `TARGET: ${fmt(trade.target)}` : "",
        reason ? `REASON: ${reason}` : "",
      ];
      return lines.filter(Boolean).join("\n");
    }

    function paperOpenTradeForOrder(order) {
      const positionId = String(order?.position_id || "");
      if (!positionId) return null;
      return paperJournalTrades().find(trade => (
        String(trade.status || "").toLowerCase() === "open"
        && (String(trade.id) === positionId || String(trade.payload?.position_id) === positionId)
      )) || null;
    }

    function paperProtectiveOrderTooltip(order) {
      const role = String(order?.role || "").toLowerCase();
      const trade = paperOpenTradeForOrder(order);
      const orderPayload = order?.payload && typeof order.payload === "object" ? order.payload : {};
      const tradePayload = trade?.payload && typeof trade.payload === "object" ? trade.payload : {};
      const signal = (tradePayload.signal && typeof tradePayload.signal === "object" ? tradePayload.signal : null)
        || (orderPayload.signal && typeof orderPayload.signal === "object" ? orderPayload.signal : {});
      const side = String(trade?.side || tradePayload.side || signal.side || signal.direction || "").toLowerCase();
      const entry = Number(trade?.entry);
      const stopPrice = Number(role === "stop" ? order.entry : (trade?.stop ?? signal.stop ?? orderPayload.stop));
      const targetPrice = Number(role === "take" ? order.entry : (trade?.target ?? signal.target ?? orderPayload.target));
      const source = tradePayload.signal_source || orderPayload.signal_source || trade?.signal_source || trade?.source || "";
      const item = {
        source,
        signal_source: source,
        action_card: tradePayload.action_card || signal.action_card || orderPayload.action_card,
        direction: side,
        side,
        role,
        action: role === "stop" ? "STOP" : role === "take" ? "TARGET_HIT" : "GO",
        plan: {
          entry,
          stop: stopPrice,
          target: targetPrice,
          direction: side,
          reason: signal.reason ?? tradePayload.reason ?? orderPayload.reason,
        },
        score: signal.score ?? tradePayload.score ?? orderPayload.score,
        setup: signal.setup ?? tradePayload.setup ?? orderPayload.setup,
        code: signal.code ?? tradePayload.code ?? orderPayload.code,
        reason: signal.reason ?? tradePayload.reason ?? orderPayload.reason,
        confluence_sources: tradePayload.confluence_sources || signal.confluence_sources || orderPayload.confluence_sources,
      };
      const reason = item.reason || "";
      if (typeof standardSignalTooltip === "function") {
        return standardSignalTooltip(item, { source, force: true });
      }
      const sourceLabel = paperInitiatorSourceLabel({ ...orderPayload, signal_source: source, signal_source_label: orderPayload.signal_source_label }) || paperJournalSourceLabel(order);
      const lines = [
        role === "stop" ? "STOP" : role === "take" ? "TAKE" : String(role || "ORDER").toUpperCase(),
        sourceLabel ? `Source: ${sourceLabel}` : "",
        Number.isFinite(Number(order?.entry)) ? `PRICE: ${fmt(order.entry)}` : "",
        Number.isFinite(Number(order?.qty)) ? `QTY: ${fmt(order.qty)}` : "",
      ];
      return lines.filter(Boolean).join("\n");
    }

    function hitTestPaperOrderAction(localX, localY) {
      const hits = Array.isArray(state.paperOrderActionHits) ? state.paperOrderActionHits : [];
      return hits.slice().reverse().find(hit => (
        localX >= hit.x
        && localX <= hit.x + hit.width
        && localY >= hit.y
        && localY <= hit.y + hit.height
      )) || null;
    }

    function paperTradingLabelLeft(pad, width, options = {}) {
      const base = Number(options.leftBound);
      if (Number.isFinite(base) && base > 0) return Math.max(pad.left + 8, base + 8);
      if (typeof gexLeftCompanionRight === "function") {
        return Math.max(pad.left + 8, gexLeftCompanionRight(pad, width, state.snapshot) + 8);
      }
      return pad.left + 8;
    }

    function paperOrderPatchForEntry(order, entry) {
      return { entry: Number(entry) };
    }

    function applyPaperOrderPatchLocal(id, patch) {
      state.paperOrders = Array.isArray(state.paperOrders)
        ? state.paperOrders.map(order => String(order.id) === String(id) ? { ...order, ...patch } : order)
        : [];
    }

    function startPaperOrderDrag(canvas, event, id) {
      if (String(id || "").startsWith("entry-label:")) {
        return startPaperEntryLabelDrag(canvas, event, String(id).slice("entry-label:".length));
      }
      const order = (Array.isArray(state.paperOrders) ? state.paperOrders : []).find(item => String(item.id) === String(id));
      if (!order || String(order.status || "pending") !== "pending") return false;
      const rect = canvas.getBoundingClientRect();
      state.paperTrading.dragOrder = { id: String(id), original: { ...order }, lastEntry: Number(order.entry) };
      canvas.setPointerCapture(event.pointerId);
      canvas.classList.add("dragging");
      const onMove = move => {
        const point = chartPointFromLocal(move.clientX - rect.left, move.clientY - rect.top);
        if (!point || !Number.isFinite(Number(point.price))) return;
        const patch = paperOrderPatchForEntry(state.paperTrading.dragOrder.original, Number(point.price));
        state.paperTrading.dragOrder.lastEntry = patch.entry;
        applyPaperOrderPatchLocal(id, patch);
        if (state.snapshot && typeof renderPriceTrading === "function") renderPriceTrading(state.snapshot);
      };
      const onUp = async () => {
        canvas.classList.remove("dragging");
        canvas.removeEventListener("pointermove", onMove);
        canvas.removeEventListener("pointerup", onUp);
        const drag = state.paperTrading.dragOrder;
        state.paperTrading.dragOrder = null;
        if (!drag) return;
        const current = (Array.isArray(state.paperOrders) ? state.paperOrders : []).find(item => String(item.id) === String(id));
        try {
          await updatePaperOrder(id, {
            entry: current?.entry ?? drag.lastEntry,
            stop_loss: current?.stop_loss,
            target: current?.target,
            use_stop_loss: current?.use_stop_loss !== false,
            use_target: current?.use_target !== false,
          });
        } catch (error) {
          applyPaperOrderPatchLocal(id, drag.original);
          if (state.snapshot && typeof renderPriceTrading === "function") renderPriceTrading(state.snapshot);
          else if (state.snapshot) renderCharts(state.snapshot);
          showBrowserToast(`Move failed: ${requestErrorMessage(error, "move failed")}`);
        }
      };
      canvas.addEventListener("pointermove", onMove);
      canvas.addEventListener("pointerup", onUp);
      return true;
    }

    function startPaperEntryLabelDrag(canvas, event, id, hit = null) {
      if (!id) return false;
      const rect = canvas.getBoundingClientRect();
      const startX = event.clientX - rect.left;
      const currentHit = hit || hitTestPaperOrderAction(startX, event.clientY - rect.top);
      const currentX = Number(currentHit?.labelX);
      if (!Number.isFinite(currentX)) return false;
      event.preventDefault();
      event.stopPropagation();
      state.paperTrading.dragEntryLabel = {
        id,
        startX,
        initialOffset: paperEntryLabelFrameOffset(),
      };
      try {
        canvas.setPointerCapture(event.pointerId);
      } catch (_) {
        // Pointer capture is best-effort; window listeners keep the drag alive.
      }
      canvas.classList.add("dragging");
      const onMove = move => {
        const drag = state.paperTrading.dragEntryLabel;
        if (!drag || String(drag.id) !== String(id)) return;
        move.preventDefault();
        const localX = move.clientX - rect.left;
        setPaperEntryLabelFrameOffset(drag.initialOffset - (localX - drag.startX), { persist: false });
        if (state.snapshot && typeof renderPriceTrading === "function") renderPriceTrading(state.snapshot);
        else if (state.snapshot) renderCharts(state.snapshot);
      };
      const onUp = () => {
        canvas.classList.remove("dragging");
        window.removeEventListener("pointermove", onMove);
        window.removeEventListener("pointerup", onUp);
        window.removeEventListener("pointercancel", onUp);
        setPaperEntryLabelFrameOffset(paperEntryLabelFrameOffset());
        state.paperTrading.dragEntryLabel = null;
      };
      window.addEventListener("pointermove", onMove);
      window.addEventListener("pointerup", onUp);
      window.addEventListener("pointercancel", onUp);
      return true;
    }

    function paperTradeEntryFills(trade) {
      const positionId = String(trade?.id || trade?.payload?.position_id || "");
      if (!positionId) return [];
      return paperJournalFills().filter(fill => (
        String(fill.position_id || "") === positionId
        && String(fill.role || "").toLowerCase() === "entry"
        && !fill.reduce_only
        && Number.isFinite(Number(fill.price))
        && Number(fill.qty) > 0
      ));
    }

    function paperTradeEquilibriumPrice(trade) {
      const fills = paperTradeEntryFills(trade);
      if (fills.length > 1) {
        let totalQty = 0;
        let weighted = 0;
        fills.forEach(fill => {
          const qty = Math.abs(Number(fill.qty) || 0);
          const price = Number(fill.price);
          if (qty > 0 && Number.isFinite(price)) {
            totalQty += qty;
            weighted += price * qty;
          }
        });
        if (totalQty > 0) return weighted / totalQty;
      }
      if (fills.length === 1) {
        const price = Number(fills[0].price);
        if (Number.isFinite(price)) return price;
      }
      const entry = Number(trade?.entry);
      return Number.isFinite(entry) ? entry : null;
    }

    function paperTradeQtyLabel(qty) {
      const number = Number(qty);
      if (!Number.isFinite(number) || number <= 0) return "";
      if (Math.abs(number - Math.round(number)) < 1e-6) return String(Math.round(number));
      return fmt(number);
    }

    function paperTradeEntryLabel(trade) {
      const side = String(trade?.side || "").toUpperCase();
      const prefix = side === "SHORT" ? "S" : "L";
      const qtyText = paperTradeQtyLabel(trade?.qty);
      const averaged = paperTradeEntryFills(trade).length > 1;
      const tag = averaged ? "AVG" : "ENTRY";
      const qtyPart = qtyText ? ` x${qtyText}` : "";
      const pnlValue = trade?.pnl_kind === "realized" ? trade?.realized_pnl : trade?.unrealized_pnl;
      const pnlText = trade?.pnl_kind && Number.isFinite(Number(pnlValue))
        ? ` ${trade.pnl_kind === "realized" ? "REAL" : "UNR"} ${paperPnlUnitLabel(trade?.pnl_unit)} ${paperSigned(pnlValue)}`
        : "";
      return `${prefix} ${tag}${qtyPart}${pnlText}`;
    }

    function paperFillBarIndex(fill, bars) {
      if (!Array.isArray(bars) || !bars.length) return -1;
      const fillTs = Date.parse(fill?.filled_at || fill?.ts || "");
      if (!Number.isFinite(fillTs)) return -1;
      return chartBarIndexForEventTimestamp(bars, fillTs);
    }

    function paperTradeEntryLineStartX(trade, options, fallbackX) {
      const bars = Array.isArray(options?.bars) ? options.bars : [];
      const x = typeof options?.x === "function" ? options.x : null;
      if (!bars.length || !x) return fallbackX;
      const index = paperFillBarIndex({ filled_at: trade?.opened_at || trade?.created_at }, bars);
      return index >= 0 ? x(index) : fallbackX;
    }

    function paperFillMarker(fill) {
      const role = String(fill?.role || "entry").toLowerCase();
      const side = String(fill?.side || "").toLowerCase();
      if (role === "stop") return { glyph: "×", color: themeColor("down", 0.96), dy: -10 };
      if (role === "take") return { glyph: "✓", color: themeColor("up", 0.96), dy: -10 };
      if (role === "close") return { glyph: "◆", color: themeColor("gold", 0.96), dy: -10 };
      return side === "short"
        ? { glyph: "▼", color: themeColor("down", 0.96), dy: -10 }
        : { glyph: "▲", color: themeColor("up", 0.96), dy: -10 };
    }

    function drawPaperFillMarkers(ctx, pad, width, y, options = {}) {
      const bars = Array.isArray(options.bars) ? options.bars : [];
      const x = typeof options.x === "function" ? options.x : null;
      if (!bars.length || !x) return;
      const fills = paperJournalFills().filter(fill => (
        Number.isFinite(Number(fill.price))
        && exactIdentityText(fill.instrument_id) === exactIdentityText(state.instrumentId)
        && exactIdentityText(fill.route_fingerprint) === instrumentRouteFingerprint()
        && String(fill.timeframe || "") === String(state.timeframe || "")
      )).slice(0, 80);
      if (!fills.length) return;
      ctx.save();
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.font = "900 10px -apple-system, BlinkMacSystemFont, sans-serif";
      fills.forEach(fill => {
        const index = paperFillBarIndex(fill, bars);
        if (index < 0) return;
        const xx = x(index);
        const yy = y(Number(fill.price));
        if (!isChartPlotXVisible(xx, pad, width, 8) || yy < pad.top - 16 || yy > (ctx.canvas.clientHeight || 0) + 16) return;
        const marker = paperFillMarker(fill);
        const bg = rgbaFromCssColor(css("--bg"), 0.72);
        ctx.fillStyle = bg;
        ctx.beginPath();
        ctx.arc(xx, yy + marker.dy, 6, 0, Math.PI * 2);
        ctx.fill();
        ctx.fillStyle = marker.color;
        ctx.fillText(marker.glyph, xx, yy + marker.dy + 0.5);
      });
      ctx.restore();
    }

    function drawPaperTradeOrders(ctx, pad, width, y, options = {}) {
      const orders = drawablePaperTradeOrders();
      const showTrades = paperShowTradesOnChart();
      const trades = showTrades ? drawablePaperTrades() : [];
      if (options.actions) state.paperOrderActionHits = [];
      if (!orders.length && !trades.length && !(showTrades && paperJournalFills().length)) return;
      ctx.save();
      ctx.font = "800 10px -apple-system, BlinkMacSystemFont, sans-serif";
      const labelLeft = paperTradingLabelLeft(pad, width, options);
      for (const trade of trades.slice(0, 8)) {
        const anchorPrice = paperTradeEquilibriumPrice(trade);
        if (!Number.isFinite(anchorPrice)) continue;
        const label = paperTradeEntryLabel(trade);
        const lines = [
          { price: anchorPrice, label, color: themeColor("blue", 0.86), dash: [3, 4] },
        ];
        lines.forEach((line, index) => {
          const yy = y(line.price);
          if (yy < pad.top - 10 || yy > (ctx.canvas.clientHeight || 0) + 10) return;
          const labelW = Math.min(ctx.measureText(line.label).width + 12, 172);
          const labelX = index === 0 ? paperEntryLabelX(width, pad, labelW) : width - pad.right - labelW - 8;
          const entryStartX = paperTradeEntryLineStartX(trade, options, labelLeft);
          ctx.strokeStyle = paperTradeConnectorColor(line.color);
          ctx.lineWidth = 0.85;
          ctx.setLineDash(line.dash);
          ctx.beginPath();
          if (index === 0) {
            const labelEdge = labelX <= entryStartX ? labelX + labelW : labelX;
            ctx.moveTo(entryStartX, yy);
            ctx.lineTo(labelEdge, yy);
          } else {
            ctx.moveTo(labelLeft, yy);
            ctx.lineTo(width - pad.right, yy);
          }
          ctx.stroke();
          ctx.setLineDash([]);
          ctx.fillStyle = rgbaFromCssColor(line.color, 0.18);
          ctx.strokeStyle = rgbaFromCssColor(line.color, 0.68);
          roundedRectPath(ctx, labelX, yy - 9, labelW, 18, 4);
          ctx.fill();
          ctx.stroke();
          ctx.fillStyle = readableTextForBg(rgbaFromCssColor(line.color, 0.18), css("--text"));
          ctx.fillText(line.label, labelX + 6, yy + 3, labelW - 10);
          if (options.actions && index === 0) {
            state.paperOrderActionHits.push({ action: "drag-entry-label", id: String(trade.id), labelX, x: labelX - 10, y: yy - 16, width: labelW + 20, height: 32 });
          }
          if (typeof registerCanvasTooltip === "function") {
            registerCanvasTooltip("price", labelX + labelW / 2, yy, labelW, 22, () => paperTradeEntryTooltip(trade));
          }
        });
      }
      for (const order of orders.slice(0, 24)) {
        if (!Number.isFinite(Number(order.entry))) continue;
        const yy = y(Number(order.entry));
        if (yy < pad.top - 10 || yy > pad.top + (ctx.canvas.clientHeight || 0)) continue;
        const pending = String(order.status || "pending") === "pending";
        const longSide = String(order.side || "") === "long";
        const role = String(order.role || "entry").toLowerCase();
        const orderPayload = order.payload && typeof order.payload === "object" ? order.payload : {};
        const sourceTag = String(
          orderPayload.signal_source_label
          || orderPayload.signal_source
          || orderPayload.indicator_source
          || ""
        ).replace(/_/g, " ");
        const trailTag = orderPayload.trail_applied ? " TRL" : "";
        const color = role === "stop"
          ? themeColor("down", pending ? 0.9 : 0.74)
          : role === "take"
          ? themeColor("up", pending ? 0.9 : 0.74)
          : themeColor(longSide ? "up" : "down", pending ? 0.86 : 0.74);
        const labelSide = role === "stop"
          ? `STOP${trailTag}`
          : role === "take"
          ? "TAKE"
          : `${longSide ? "BUY" : "SELL"} ${String(order.order_type || "").toUpperCase()}`;
        const sourceSuffix = sourceTag ? ` · ${sourceTag}` : "";
        const label = `${labelSide} ${fmt(order.entry)}${sourceSuffix}`;
        ctx.strokeStyle = paperTradeConnectorColor(color);
        ctx.lineWidth = 0.85;
        ctx.setLineDash(pending ? [6, 4] : [2, 5]);
        ctx.beginPath();
        ctx.moveTo(labelLeft, yy);
        ctx.lineTo(width - pad.right, yy);
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle = rgbaFromCssColor(color, pending ? 0.95 : 0.18);
        ctx.strokeStyle = rgbaFromCssColor(color, pending ? 0.95 : 0.76);
        const textW = Math.min(ctx.measureText(label).width + 12, 210);
        roundedRectPath(ctx, labelLeft, yy - 10, textW, 20, 4);
        ctx.fill();
        ctx.stroke();
        ctx.fillStyle = readableTextForBg(rgbaFromCssColor(color, pending ? 0.95 : 0.18), css("--text"));
        ctx.fillText(label, labelLeft + 6, yy + 3, textW - 12);
        if (typeof registerCanvasTooltip === "function" && (role === "stop" || role === "take")) {
          registerCanvasTooltip("price", labelLeft + textW / 2, yy, textW, 22, () => paperProtectiveOrderTooltip(order));
        }
        if (options.actions && pending) {
          state.paperOrderActionHits.push({ action: "drag", id: String(order.id), x: labelLeft - 4, y: yy - 13, width: textW + 8, height: 26 });
        }
        if (options.actions && pending) {
          const boxSize = 18;
          const boxX = labelLeft + textW + 4;
          const boxY = yy - boxSize / 2;
          ctx.fillStyle = rgbaFromCssColor(css("--panel"), 0.92);
          ctx.strokeStyle = rgbaFromCssColor(color, 0.82);
          roundedRectPath(ctx, boxX, boxY, boxSize, boxSize, 4);
          ctx.fill();
          ctx.stroke();
          ctx.strokeStyle = css("--red");
          ctx.lineWidth = 1.8;
          ctx.beginPath();
          ctx.moveTo(boxX + 5, boxY + 5);
          ctx.lineTo(boxX + boxSize - 5, boxY + boxSize - 5);
          ctx.moveTo(boxX + boxSize - 5, boxY + 5);
          ctx.lineTo(boxX + 5, boxY + boxSize - 5);
          ctx.stroke();
          state.paperOrderActionHits.push({ action: "cancel", id: String(order.id), x: boxX - 3, y: boxY - 3, width: boxSize + 6, height: boxSize + 6 });
        }
      }
      if (showTrades) drawPaperFillMarkers(ctx, pad, width, y, options);
      ctx.restore();
    }

    function setupPaperTradingControls() {
      const bindRiskControl = (id, eventName, apply) => {
        document.getElementById(id)?.addEventListener(eventName, event => {
          apply(event);
          savePaperTradingRiskPrefs();
          renderPaperTradingPanel();
        });
      };
      document.getElementById("paper-trade-toggle")?.addEventListener("click", () => {
        state.paperTrading.expanded = state.paperTrading.expanded === false;
        renderPaperTradingPanel();
      });
      document.getElementById("paper-trade-buy")?.addEventListener("click", () => armPaperTrade("long"));
      document.getElementById("paper-trade-sell")?.addEventListener("click", () => armPaperTrade("short"));
      document.getElementById("paper-trade-cancel-arm")?.addEventListener("click", () => clearPaperTradeArm());
      bindRiskControl("paper-order-type", "change", event => { state.paperTrading.orderType = event.target.value; });
      bindRiskControl("paper-order-qty", "input", event => { state.paperTrading.qty = clamp(Number(event.target.value) || 1, 0.1, 100); });
      bindRiskControl("paper-order-qty", "change", event => { state.paperTrading.qty = clamp(Number(event.target.value) || 1, 0.1, 100); });
      bindRiskControl("paper-order-use-stop", "change", event => { state.paperTrading.useStopLoss = event.target.checked; });
      bindRiskControl("paper-order-stop-points", "input", event => { state.paperTrading.stopPoints = clamp(Number(event.target.value) || 8, 0.01, 500); });
      bindRiskControl("paper-order-stop-points", "change", event => { state.paperTrading.stopPoints = clamp(Number(event.target.value) || 8, 0.01, 500); });
      bindRiskControl("paper-order-use-target", "change", event => { state.paperTrading.useTarget = event.target.checked; });
      bindRiskControl("paper-order-target-points", "input", event => { state.paperTrading.targetPoints = clamp(Number(event.target.value) || 16, 0.01, 1000); });
      bindRiskControl("paper-order-target-points", "change", event => { state.paperTrading.targetPoints = clamp(Number(event.target.value) || 16, 0.01, 1000); });
      document.getElementById("paper-show-trades-on-chart")?.addEventListener("change", event => {
        state.paperTrading.showTradesOnChart = event.target.checked;
        setServerSettingValue("aef:paperShowTradesOnChart", event.target.checked ? "true" : "false");
        renderPaperTradingLayerNow();
      });
      document.getElementById("paper-orders-list")?.addEventListener("click", event => {
        const id = event.target?.dataset?.paperOrderCancel;
        if (id) cancelPaperOrder(id);
      });
      document.getElementById("paper-trades-list")?.addEventListener("click", event => {
        const id = event.target?.dataset?.paperTradeClose;
        if (id) closeManualPaperTrade(id);
      });
      renderPaperTradingPanel();
    }
