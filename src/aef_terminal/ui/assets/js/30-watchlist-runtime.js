    const watchlistQuoteFlashState = new Map();
    const watchlistQuoteFlashPending = new Map();
    const QUOTE_FLASH_MIN_CHANGE_PCT = 0.02;
    const WATCHLIST_QUOTE_STATE_COMPACT_LABELS = Object.freeze({
      live: "LIVE",
      stale: "STALE",
      delayed: "DLY",
      delayed_frozen: "D/F",
      frozen: "FRZ",
      error: "ERR",
      unavailable: "N/A",
    });

    function watchlistQuoteStateCompactLabel(stateKey) {
      return WATCHLIST_QUOTE_STATE_COMPACT_LABELS[stateKey]
        || WATCHLIST_QUOTE_STATE_COMPACT_LABELS.unavailable;
    }

    function quoteFlashChangePct(previous, value) {
      const base = Math.max(Math.abs(previous), Math.abs(value), 1e-9);
      return (Math.abs(value - previous) / base) * 100;
    }

    function quoteFlashClass(key, price) {
      const value = Number(price);
      if (!Number.isFinite(value)) return "";
      const id = String(key || "");
      const previous = watchlistQuoteFlashState.get(id);
      const pending = watchlistQuoteFlashPending.get(id);
      if (!Number.isFinite(previous)) {
        watchlistQuoteFlashState.set(id, value);
        return "";
      }
      if (Math.abs(previous - value) < 0.000001) {
        return pending || "";
      }
      if (quoteFlashChangePct(previous, value) < QUOTE_FLASH_MIN_CHANGE_PCT) {
        return pending || "";
      }
      const flashClass = value > previous ? "flash-up" : "flash-down";
      watchlistQuoteFlashState.set(id, value);
      watchlistQuoteFlashPending.set(id, flashClass);
      window.setTimeout(() => {
        if (watchlistQuoteFlashPending.get(id) === flashClass) watchlistQuoteFlashPending.delete(id);
      }, 460);
      return flashClass;
    }

    function indicatorSidebarExtensions() {
      return Object.entries(indicatorRegistry())
        .map(([indicatorId, spec]) => ({
          indicatorId,
          sidebar: spec?.extensions?.sidebar,
        }))
        .filter(item => item.sidebar?.id && item.sidebar?.mount_id)
        .sort((left, right) => (
          (Number(left.sidebar.order) || 500) - (Number(right.sidebar.order) || 500)
          || left.indicatorId.localeCompare(right.indicatorId)
        ));
    }

    function ensureIndicatorSidebarExtensions() {
      const tabs = document.querySelector(".side-tabs");
      const alertsTab = document.getElementById("side-tab-alerts");
      const alertsPanel = document.getElementById("side-alerts");
      if (!tabs || !alertsTab || !alertsPanel) return;
      indicatorSidebarExtensions().forEach(({ sidebar }) => {
        if (!document.getElementById(`side-tab-${sidebar.id}`)) {
          const button = document.createElement("button");
          button.id = `side-tab-${sidebar.id}`;
          button.className = "side-tab";
          button.type = "button";
          button.setAttribute("role", "tab");
          button.dataset.sideTab = sidebar.id;
          button.title = sidebar.title;
          button.setAttribute("aria-label", sidebar.title);
          button.setAttribute("aria-controls", `side-${sidebar.id}`);
          button.setAttribute("aria-selected", "false");
          button.tabIndex = -1;
          button.hidden = true;
          button.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true">${sidebar.icon_svg || ""}</svg>`;
          tabs.insertBefore(button, alertsTab);
        }
        if (!document.getElementById(`side-${sidebar.id}`)) {
          const panel = document.createElement("div");
          panel.id = `side-${sidebar.id}`;
          panel.className = "side-section hidden";
          panel.dataset.indicatorSidebar = "1";
          panel.setAttribute("role", "tabpanel");
          panel.setAttribute("aria-labelledby", `side-tab-${sidebar.id}`);
          panel.setAttribute("aria-label", sidebar.title);
          panel.hidden = true;
          const mount = document.createElement("div");
          mount.id = sidebar.mount_id;
          panel.appendChild(mount);
          alertsPanel.parentNode.insertBefore(panel, alertsPanel);
        }
      });
    }

    function applySideTabs() {
      ensureIndicatorSidebarExtensions();
      const extensions = indicatorSidebarExtensions();
      const extensionByTab = new Map(extensions.map(item => [item.sidebar.id, item]));
      extensions.forEach(({ indicatorId, sidebar }) => {
        const enabled = indicatorCalcForId(indicatorId);
        const button = document.getElementById(`side-tab-${sidebar.id}`);
        if (button) {
          button.hidden = !enabled;
          button.disabled = !enabled;
        }
      });
      const tabs = document.querySelector(".side-tabs");
      const visibleTabCount = tabs
        ? [...tabs.querySelectorAll("[data-side-tab]")].filter(button => !button.hidden).length
        : 4;
      tabs?.style.setProperty("--side-tab-count", String(Math.max(visibleTabCount, 1)));
      const requestedTab = sidePanelTabs().includes(state.sideTab) ? state.sideTab : "instruments";
      const requestedExtension = extensionByTab.get(requestedTab);
      const tab = requestedExtension && !indicatorCalcForId(requestedExtension.indicatorId)
        ? "indicators"
        : requestedTab;
      state.sideTab = tab;
      if (tab !== requestedTab) {
        workspaceSet("sideTab", tab);
      }
      document.querySelectorAll("[data-side-tab]").forEach(button => {
        const selected = button.dataset.sideTab === tab;
        button.classList.toggle("active", selected);
        button.setAttribute("aria-selected", selected ? "true" : "false");
        button.tabIndex = selected ? 0 : -1;
      });
      document.querySelectorAll(".side-section[id^='side-']").forEach(panel => {
        const selected = panel.id === `side-${tab}`;
        panel.classList.toggle("hidden", !selected);
        panel.hidden = !selected;
      });
      if (tab === "go") {
        loadPaperStats({ force: true });
      }
      const sidebarRenderer = indicatorSidebarRendererRegistry.get(tab);
      if (typeof sidebarRenderer === "function") sidebarRenderer(state.snapshot);
      ensureOptionTargetPulseRender();
    }

    function setupSideTabs() {
      ensureIndicatorSidebarExtensions();
      const activateSideTab = (button, focus = false) => {
        if (!button || button.disabled || button.hidden) return;
        state.sideTab = button.dataset.sideTab;
        workspaceSet("sideTab", state.sideTab);
        syncWorkspacePageState();
        applySideTabs();
        if (focus) button.focus();
      };
      document.querySelectorAll("[data-side-tab]").forEach(button => {
        button.addEventListener("click", () => {
          activateSideTab(button);
        });
      });
      document.querySelector(".side-tabs")?.addEventListener("keydown", event => {
        if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
        const tabs = [...document.querySelectorAll("[data-side-tab]")]
          .filter(button => !button.hidden && !button.disabled);
        if (!tabs.length) return;
        const currentIndex = Math.max(tabs.indexOf(document.activeElement), 0);
        const nextIndex = event.key === "Home"
          ? 0
          : event.key === "End"
            ? tabs.length - 1
            : event.key === "ArrowRight"
              ? (currentIndex + 1) % tabs.length
              : (currentIndex - 1 + tabs.length) % tabs.length;
        event.preventDefault();
        activateSideTab(tabs[nextIndex], true);
      });
      applySideTabs();
    }

    let lastWatchlistStructureKey = "";
    let pendingInstrumentRenderOptions = null;
    let instrumentRenderFrame = 0;
    let instrumentRenderQueuePerf = null;
    let watchlistDragKey = "";
    let watchlistInstrumentIndexRows = null;
    let watchlistInstrumentIndex = new Map();
    let watchlistRowViewIndex = new Map();
    const watchlistTrendGeometryCache = new WeakMap();

    function watchlistPresentation(instrumentId) {
      const identity = exactIdentityText(instrumentId);
      const presentation = state.watchlistPresentationByInstrument.get(identity);
      if (!identity || !presentation) {
        throw new Error("watchlist presentation is missing for a current instrument");
      }
      return presentation;
    }

    function watchlistDisplayMode(instrumentId) {
      return watchlistPresentation(instrumentId).display_mode;
    }

    function reconcileWatchlistPresentations(items) {
      const nextPresentations = new Map();
      const reconciled = items.map(item => {
        const instrumentId = exactIdentityText(item?.instrument_id);
        const incoming = normalizeWatchlistPresentation(item?.presentation);
        if (!instrumentId || !incoming) {
          throw new Error("instrument load returned an invalid watchlist presentation");
        }
        const current = state.watchlistPresentationByInstrument.get(instrumentId);
        if (current && current.revision === incoming.revision && current.display_mode !== incoming.display_mode) {
          throw new Error("instrument load returned a conflicting watchlist presentation revision");
        }
        const effective = current && current.revision > incoming.revision ? current : incoming;
        nextPresentations.set(instrumentId, effective);
        return { ...item, presentation: { ...effective } };
      });
      state.watchlistPresentationByInstrument = nextPresentations;
      return reconciled;
    }

    function pruneWatchlistTrendState() {
      const trendRouteKeys = new Set();
      for (const item of state.instruments || []) {
        const instrumentId = exactIdentityText(item?.instrument_id);
        const routeFingerprint = exactIdentityText(item?.route_fingerprint);
        if (!instrumentId) continue;
        if (routeFingerprint && watchlistDisplayMode(instrumentId) === "trend") {
          trendRouteKeys.add(screenerRowIdentityKey(item));
        }
      }
      let cacheChanged = false;
      for (const identityKey of state.watchlistTrendsByIdentity.keys()) {
        if (trendRouteKeys.has(identityKey)) continue;
        state.watchlistTrendsByIdentity.delete(identityKey);
        cacheChanged = true;
      }
      return cacheChanged;
    }

    function applyWatchlistInstrumentPresentation(instrumentId, presentation, options = {}) {
      const identity = exactIdentityText(instrumentId);
      const incoming = normalizeWatchlistPresentation(presentation);
      if (!identity || !incoming) return false;
      const current = state.watchlistPresentationByInstrument.get(identity);
      if (current && incoming.revision < current.revision) return false;
      if (current && incoming.revision === current.revision) {
        if (incoming.display_mode !== current.display_mode) {
          throw new Error("watchlist presentation revision conflict");
        }
        return false;
      }
      state.watchlistPresentationByInstrument.set(identity, incoming);
      const item = (state.instruments || []).find(candidate => (
        exactIdentityText(candidate?.instrument_id) === identity
      ));
      if (!item) return true;
      item.presentation = { ...incoming };
      pruneWatchlistTrendState();
      const identityKey = screenerRowIdentityKey(item);
      renderInstruments({ dirtyIdentityKeys: new Set([identityKey]), immediate: true });
      if (incoming.display_mode === "trend" && options.refreshTrend !== false) {
        loadWatchlistTrends({ force: true, instrumentIds: new Set([identity]) });
      }
      return true;
    }

    async function setWatchlistInstrumentDisplayMode(instrumentId, mode) {
      const identity = exactIdentityText(instrumentId);
      if (!(state.instruments || []).some(item => exactIdentityText(item?.instrument_id) === identity)) return;
      try {
        if (!["classic", "trend"].includes(mode)) {
          throw new Error("watchlist display mode is invalid");
        }
        const displayMode = mode;
        if (watchlistDisplayMode(identity) === displayMode) return;
        const result = await fetchJson(`/api/instruments/${encodeURIComponent(identity)}/presentation`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ display_mode: displayMode }),
          cache: "no-store",
        });
        const presentation = normalizeWatchlistPresentation(result?.presentation);
        if (
          !result?.ok
          || exactIdentityText(result.instrument_id) !== identity
          || !presentation
          || presentation.display_mode !== displayMode
        ) throw new Error(apiErrorMessage(result, "watchlist presentation save failed"));
        if (applyWatchlistInstrumentPresentation(identity, presentation)) {
          publishWatchlistPresentation(identity, presentation);
        }
      } catch (error) {
        showBrowserToast(`Watchlist view save failed: ${requestErrorMessage(error, "watchlist presentation save failed")}`);
      }
    }

    function watchlistTrendGeometry(entry) {
      if (!entry || typeof entry !== "object") {
        return { available: false, direction: "flat", path: "", title: "3h trend unavailable" };
      }
      const cached = watchlistTrendGeometryCache.get(entry);
      if (cached) return cached;
      const status = String(entry.status || "unavailable");
      const points = Array.isArray(entry.points) ? entry.points : [];
      const prices = points.map(point => Number(point?.price));
      const ready = status === "ready" && prices.length >= 2 && prices.every(Number.isFinite);
      const source = String(entry.source || "");
      const asOfDate = entry.as_of ? new Date(entry.as_of) : null;
      const asOf = asOfDate && Number.isFinite(asOfDate.getTime())
        ? localTimeFormatter.format(asOfDate)
        : "";
      const title = [
        "3h trend",
        status.replaceAll("_", " "),
        source,
        asOf ? `as of ${asOf}` : "",
      ].filter(Boolean).join(" · ");
      if (!ready) {
        const unavailable = { available: false, direction: "flat", path: "", title };
        watchlistTrendGeometryCache.set(entry, unavailable);
        return unavailable;
      }
      const minimum = Math.min(...prices);
      const maximum = Math.max(...prices);
      const span = maximum - minimum;
      const path = prices.map((price, index) => {
        const x = prices.length === 1 ? 50 : index * 100 / (prices.length - 1);
        const y = span > 0 ? 19 - ((price - minimum) / span) * 16 : 11;
        return `${index === 0 ? "M" : "L"}${x.toFixed(2)} ${y.toFixed(2)}`;
      }).join(" ");
      const direction = prices.at(-1) > prices[0] ? "up" : prices.at(-1) < prices[0] ? "down" : "flat";
      const geometry = { available: true, direction, path, title };
      watchlistTrendGeometryCache.set(entry, geometry);
      return geometry;
    }

    function watchlistStructureKey() {
      return JSON.stringify([
        state.watchlistEditMode ? "edit" : "view",
        state.instruments.map(item => [
          exactIdentityText(item.instrument_id),
          exactIdentityText(item.route_fingerprint),
          item.custom ? 1 : 0,
          item.display || "",
          watchlistDisplayMode(item.instrument_id),
        ]),
      ]);
    }

    function watchlistQuoteForItem(item, rows) {
      return rows.get(screenerRowIdentityKey(item)) || {};
    }

    function watchlistCurrentContract(item) {
      const identity = item && typeof item.contract_identity === "object" ? item.contract_identity : {};
      const current = identity && typeof identity.current_contract === "object" ? identity.current_contract : {};
      return current || {};
    }

    function watchlistRowMeta(item, quote, nowMs = Date.now()) {
      const currentContract = watchlistCurrentContract(item);
      const change = quote.change_pct;
      const changeValue = change === null || change === undefined ? Number.NaN : Number(change);
      const changeClass = Number.isFinite(changeValue)
        ? changeValue >= 0 ? "long" : "short"
        : "muted";
      const changeAbs = quote.change === null || quote.change === undefined
        ? null
        : Number(quote.change);
      const rawBase = quote.change_base ?? quote.previous_session_close;
      const base = rawBase === null || rawBase === undefined ? Number.NaN : Number(rawBase);
      const quoteClose = quote.quote_close === null || quote.quote_close === undefined
        ? Number.NaN
        : Number(quote.quote_close);
      const baseLabel = Number.isFinite(quoteClose) && Math.abs(base - quoteClose) < 0.0001 ? "IBKR close" : "prev session close";
      const baseTitle = Number.isFinite(base) ? `${baseLabel} ${fmt(base)}` : "";
      const priceSourceCode = quote.price_source === "last"
        ? "L"
        : quote.price_source === "bid_ask_mid"
          ? "M"
          : "";
      const priceSourceLabel = priceSourceCode === "L"
        ? "Last"
        : priceSourceCode === "M"
          ? "Bid/ask midpoint"
          : "Price";
      const quoteObservedAt = Date.parse(quote.quote_ts || "");
      const quoteAge = Number.isFinite(quoteObservedAt)
        ? Math.max((nowMs - quoteObservedAt) / 1000, 0)
        : Number.NaN;
      const quoteStatus = String(quote.quote_status || "unavailable");
      const quotePayload = liveQuotePayloadFromScreenerRow(quote, {
        allowStale: true,
        maxAgeMs: null,
      });
      const freshQuote = quotePayload ? quoteDisplayFromPayload(quotePayload, {
        expectedInstrumentId: item.instrument_id,
        expectedRouteFingerprint: item.route_fingerprint,
        maxAgeMs: LIVE_QUOTE_DISPLAY_TTL_MS,
        nowMs,
      }) : null;
      const quoteIsStale = !freshQuote;
      const displayStatus = quoteStatus === "live" && quoteIsStale
        ? "stale"
        : quoteStatus;
      const quoteStateClass = ["live", "stale", "delayed", "delayed_frozen", "frozen", "error"]
        .includes(displayStatus) ? displayStatus : "unavailable";
      const quoteState = uiPresentationStateDescriptor(quoteStateClass);
      const priceTitle = Number.isFinite(Number(quote.price))
        ? `${priceSourceLabel} ${fmt(quote.price)} · ${displayStatus}${Number.isFinite(quoteAge) ? ` · ${quoteAge.toFixed(1)}s` : ""}`
        : `${priceSourceLabel} unavailable`;
      const contractLabel = String(
        quote.local_symbol
          || currentContract.local_symbol
          || currentContract.contract_key
          || currentContract.contract_month
          || currentContract.expiry
          || quote.contract
          || item.local_symbol
          || item.contract
          || quote.contract_month
          || item.contract_month
          || ""
      ).trim();
      const rolloverWarning = quote.contract_rollover_warning && typeof quote.contract_rollover_warning === "object"
        ? quote.contract_rollover_warning
        : null;
      const rolloverNew = Boolean(quote.contract_rollover_new);
      const rolloverNewMessage = String(quote.contract_rollover_new_message || "").trim();
      const rolloverDue = Boolean((quote.contract_rollover_due || rolloverWarning) && !rolloverNew);
      const rolloverMessage = String(rolloverWarning?.message || rolloverNewMessage || "").trim();
      const title = [
        item.name || item.display,
        contractLabel ? `contract ${contractLabel}` : "",
        rolloverNewMessage,
        rolloverMessage,
        baseTitle,
        priceTitle,
        quote.source,
        quote.warning,
      ].filter(Boolean).join(" · ");
      const flashClass = quoteFlashClass(`watch:${item.instrument_id}`, quote.price);
      return {
        change,
        changeClass,
        changeAbs,
        title,
        flashClass,
        priceTitle,
        contractLabel,
        contractTitle: rolloverMessage || rolloverNewMessage || contractLabel,
        rolloverNew,
        rolloverNewMessage,
        rolloverDue,
        rolloverKey: `${contractLabel}|${rolloverNew ? 1 : 0}|${rolloverDue ? 1 : 0}`,
        quoteStateClass,
        quoteStateKind: quoteState.kind,
        quoteStateLabel: watchlistQuoteStateCompactLabel(quoteStateClass),
        quoteStateTitle: quoteState.label,
      };
    }

    function refreshWatchlistFreshness(nowMs = Date.now()) {
      const rows = screenerRowMap(state.screener || []);
      const dirtyIdentityKeys = new Set();
      for (const item of state.instruments || []) {
        const identityKey = screenerRowIdentityKey(item);
        const view = watchlistRowViewIndex.get(identityKey);
        if (!identityKey || !view?.button) continue;
        const quote = watchlistQuoteForItem(item, rows);
        const meta = watchlistRowMeta(item, quote, nowMs);
        if (view.button.dataset.quoteStatus !== meta.quoteStateClass) {
          dirtyIdentityKeys.add(identityKey);
        }
      }
      if (dirtyIdentityKeys.size) renderInstruments({ dirtyIdentityKeys });
      setScreenerStateLabel();
      updateDocumentTitleFromScreenerQuote();
      return dirtyIdentityKeys.size;
    }

    function watchlistRowHtml(item, quote) {
      const meta = watchlistRowMeta(item, quote);
      const displayMode = watchlistDisplayMode(item.instrument_id);
      const trend = watchlistTrendGeometry(state.watchlistTrendsByIdentity.get(screenerRowIdentityKey(item)));
      const draggable = state.watchlistEditMode ? "true" : "false";
      const rowTitle = [meta.title, displayMode === "trend" ? trend.title : ""].filter(Boolean).join(" · ");
      const quoteStateHtml = `<span class="watch-quote-state" data-state="${escapeHtml(meta.quoteStateClass)}" data-state-kind="${escapeHtml(meta.quoteStateKind)}" title="${escapeHtml(meta.quoteStateTitle)}" aria-hidden="true">${escapeHtml(meta.quoteStateLabel)}</span>`;
      const valueCells = displayMode === "trend"
        ? `<span class="watch-trend ${trend.direction}${trend.available ? "" : " unavailable"}" title="${escapeHtml(trend.title)}">
              <svg viewBox="0 0 100 22" preserveAspectRatio="none" aria-hidden="true" focusable="false"${trend.available ? "" : " hidden"}>
                <path class="watch-trend-track" d="M1 11 H99"></path>
                <path class="watch-trend-line" d="${escapeHtml(trend.path)}"></path>
              </svg>
              <span class="watch-trend-empty"${trend.available ? " hidden" : ""}>—</span>
            </span>
            <span class="watch-price-cell" title="${escapeHtml(meta.priceTitle)}"><span class="watch-price">${fmt(quote.price)}</span></span>
            <span class="watch-change ${meta.changeClass}">${signed(meta.change, "%")}</span>`
        : `<span class="watch-price-cell" title="${escapeHtml(meta.priceTitle)}"><span class="watch-price">${fmt(quote.price)}</span></span>
            <span class="watch-change ${meta.changeClass}">${signed(meta.change, "%")}</span>
            <span class="watch-change-abs ${meta.changeClass}">${Number.isFinite(meta.changeAbs) ? signed(meta.changeAbs) : "-"}</span>`;
      return `<div class="watchlist-row watchlist-row-${displayMode} quote-${meta.quoteStateClass} ${item.instrument_id === state.instrumentId ? "active" : ""} ${meta.flashClass}" role="${state.watchlistEditMode ? "group" : "button"}" tabindex="0" data-instrument-id="${escapeHtml(item.instrument_id)}" data-route-fingerprint="${escapeHtml(item.route_fingerprint)}" data-symbol="${escapeHtml(item.key)}" data-contract-key="${escapeHtml(meta.rolloverKey)}" data-display-mode="${displayMode}" data-quote-status="${meta.quoteStateClass}" draggable="${draggable}" title="${escapeHtml(rowTitle)}" aria-label="${escapeHtml(rowTitle)}"${state.watchlistEditMode ? ' aria-describedby="watchlist-edit-hint"' : ""}>
            <span class="watch-symbol-cell">
              <span class="watch-symbol">${escapeHtml(item.display || item.key)}</span>
              <span class="watch-contract${meta.rolloverDue ? " rollover-due" : ""}${meta.rolloverNew ? " rollover-new" : ""}" title="${escapeHtml(meta.contractTitle)}"${meta.contractLabel ? "" : " hidden"}>
                <span class="watch-contract-label">${escapeHtml(meta.contractLabel)}</span>
                <span class="watch-contract-new" title="${escapeHtml(meta.rolloverNewMessage || "New front-month contract")}"${meta.rolloverNew ? "" : " hidden"}>NEW</span>
                <span class="watch-contract-warn" aria-label="Contract rollover due"${meta.rolloverDue ? "" : " hidden"}>!</span>
              </span>
            </span>
            ${valueCells}
            ${quoteStateHtml}
            <span class="watch-display-mode" aria-label="Watchlist row display mode">
              <button type="button" class="watch-display-mode-button${displayMode === "classic" ? " active" : ""}" data-watch-display-mode="classic" aria-pressed="${displayMode === "classic" ? "true" : "false"}">Classic</button>
              <button type="button" class="watch-display-mode-button${displayMode === "trend" ? " active" : ""}" data-watch-display-mode="trend" aria-pressed="${displayMode === "trend" ? "true" : "false"}">Trend</button>
            </span>
            <button type="button" class="watch-delete" data-watch-delete="${escapeHtml(item.instrument_id)}" data-watch-delete-label="${escapeHtml(item.display || item.key)}" title="Remove instrument" aria-label="Remove ${escapeHtml(item.display || item.key)}">×</button>
          </div>`;
    }

    function bindWatchlistRowView(button, item) {
      const identityKey = screenerRowIdentityKey(item);
      if (
        !button
        || !identityKey
        || exactIdentityText(button.dataset.instrumentId) !== exactIdentityText(item.instrument_id)
        || exactIdentityText(button.dataset.routeFingerprint) !== exactIdentityText(item.route_fingerprint)
      ) return null;
      const displayMode = watchlistDisplayMode(item.instrument_id);
      const view = {
        identityKey,
        mode: displayMode,
        button,
        symbol: button.querySelector(".watch-symbol"),
        contract: button.querySelector(".watch-contract"),
        contractLabel: button.querySelector(".watch-contract-label"),
        contractNew: button.querySelector(".watch-contract-new"),
        contractWarn: button.querySelector(".watch-contract-warn"),
        priceCell: button.querySelector(".watch-price-cell"),
        price: button.querySelector(".watch-price"),
        quoteState: button.querySelector(".watch-quote-state"),
        change: button.querySelector(".watch-change"),
        changeAbs: button.querySelector(".watch-change-abs"),
        trend: button.querySelector(".watch-trend"),
        trendSvg: button.querySelector(".watch-trend svg"),
        trendPath: button.querySelector(".watch-trend-line"),
        trendEmpty: button.querySelector(".watch-trend-empty"),
        classicModeButton: button.querySelector('[data-watch-display-mode="classic"]'),
        trendModeButton: button.querySelector('[data-watch-display-mode="trend"]'),
        deleteButton: button.querySelector(".watch-delete"),
      };
      const required = [
        view.button,
        view.symbol,
        view.contract,
        view.contractLabel,
        view.contractNew,
        view.contractWarn,
        view.priceCell,
        view.price,
        view.quoteState,
        view.change,
        view.classicModeButton,
        view.trendModeButton,
        view.deleteButton,
      ];
      if (displayMode === "classic") required.push(view.changeAbs);
      else required.push(view.trend, view.trendSvg, view.trendPath, view.trendEmpty);
      return required.every(Boolean) ? view : null;
    }

    function buildWatchlistRows(items, rows) {
      const staging = document.createElement("div");
      staging.innerHTML = items
        .map(item => watchlistRowHtml(item, watchlistQuoteForItem(item, rows)))
        .join("");
      const buttons = Array.from(staging.children);
      if (buttons.length !== items.length) {
        throw new Error(`Watchlist row build mismatch: expected ${items.length}, received ${buttons.length}`);
      }
      const views = new Map();
      items.forEach((item, index) => {
        const view = bindWatchlistRowView(buttons[index], item);
        if (!view || views.has(view.identityKey)) {
          throw new Error(`Watchlist row binding failed for instrument ${exactIdentityText(item.instrument_id) || "unknown"}`);
        }
        views.set(view.identityKey, view);
      });
      return { buttons, views };
    }

    function replaceWatchlistRows(list, rows) {
      const next = buildWatchlistRows(state.instruments, rows);
      const fragment = document.createDocumentFragment();
      next.buttons.forEach(button => fragment.appendChild(button));
      list.replaceChildren(fragment);
      watchlistRowViewIndex = next.views;
    }

    function replaceWatchlistRow(item, rows) {
      const identityKey = screenerRowIdentityKey(item);
      const current = watchlistRowViewIndex.get(identityKey);
      if (!identityKey || !current?.button) return false;
      const next = buildWatchlistRows([item], rows);
      const button = next.buttons[0];
      const view = next.views.get(identityKey);
      if (!button || !view) return false;
      current.button.replaceWith(button);
      watchlistRowViewIndex.set(identityKey, view);
      return true;
    }

    function patchWatchlistRow(view, item, quote) {
      if (!view?.button || view.identityKey !== screenerRowIdentityKey(item)) return false;
      if (view.mode !== watchlistDisplayMode(item.instrument_id)) return false;
      const meta = watchlistRowMeta(item, quote);
      const button = view.button;
      const active = item.instrument_id === state.instrumentId;
      if (button.classList.contains("active") !== active) button.classList.toggle("active", active);
      const draggable = Boolean(state.watchlistEditMode);
      if (button.draggable !== draggable) button.draggable = draggable;
      const tabIndex = 0;
      if (button.tabIndex !== tabIndex) button.tabIndex = tabIndex;
      for (const flashClass of ["flash-up", "flash-down"]) {
        const enabled = meta.flashClass === flashClass;
        if (button.classList.contains(flashClass) !== enabled) button.classList.toggle(flashClass, enabled);
      }
      const trend = view.mode === "trend"
        ? watchlistTrendGeometry(state.watchlistTrendsByIdentity.get(view.identityKey))
        : null;
      const rowTitle = [meta.title, trend?.title || ""].filter(Boolean).join(" · ");
      if (button.title !== rowTitle) button.title = rowTitle;
      if (button.getAttribute("aria-label") !== rowTitle) button.setAttribute("aria-label", rowTitle);
      if (state.watchlistEditMode) button.setAttribute("aria-describedby", "watchlist-edit-hint");
      else button.removeAttribute("aria-describedby");
      if (button.dataset.contractKey !== meta.rolloverKey) button.dataset.contractKey = meta.rolloverKey;

      const symbolText = String(item.display || item.key || "");
      if (view.symbol.textContent !== symbolText) view.symbol.textContent = symbolText;
      const priceText = fmt(quote.price);
      if (view.price.textContent !== priceText) view.price.textContent = priceText;
      if (view.priceCell.title !== meta.priceTitle) view.priceCell.title = meta.priceTitle;
      const quoteStatusClassName = `quote-${meta.quoteStateClass}`;
      [...button.classList].filter(name => name.startsWith("quote-")).forEach(name => {
        if (name !== quoteStatusClassName) button.classList.remove(name);
      });
      if (!button.classList.contains(quoteStatusClassName)) button.classList.add(quoteStatusClassName);
      if (button.dataset.quoteStatus !== meta.quoteStateClass) button.dataset.quoteStatus = meta.quoteStateClass;
      if (view.quoteState.dataset.state !== meta.quoteStateClass) view.quoteState.dataset.state = meta.quoteStateClass;
      if (view.quoteState.dataset.stateKind !== meta.quoteStateKind) view.quoteState.dataset.stateKind = meta.quoteStateKind;
      if (view.quoteState.title !== meta.quoteStateTitle) view.quoteState.title = meta.quoteStateTitle;
      if (view.quoteState.textContent !== meta.quoteStateLabel) view.quoteState.textContent = meta.quoteStateLabel;
      const changeText = signed(meta.change, "%");
      if (view.change.textContent !== changeText) view.change.textContent = changeText;
      const changeClassName = `watch-change ${meta.changeClass}`;
      if (view.change.className !== changeClassName) view.change.className = changeClassName;
      if (view.changeAbs) {
        const changeAbsText = Number.isFinite(meta.changeAbs) ? signed(meta.changeAbs) : "-";
        if (view.changeAbs.textContent !== changeAbsText) view.changeAbs.textContent = changeAbsText;
        const changeAbsClassName = `watch-change-abs ${meta.changeClass}`;
        if (view.changeAbs.className !== changeAbsClassName) view.changeAbs.className = changeAbsClassName;
      }
      if (view.trend && trend) {
        const trendClassName = `watch-trend ${trend.direction}${trend.available ? "" : " unavailable"}`;
        if (view.trend.className !== trendClassName) view.trend.className = trendClassName;
        if (view.trend.title !== trend.title) view.trend.title = trend.title;
        const trendSvgHidden = !trend.available;
        if (view.trendSvg.hasAttribute("hidden") !== trendSvgHidden) {
          view.trendSvg.toggleAttribute("hidden", trendSvgHidden);
        }
        if (view.trendPath.getAttribute("d") !== trend.path) view.trendPath.setAttribute("d", trend.path);
        if (view.trendEmpty.hidden !== trend.available) view.trendEmpty.hidden = trend.available;
      }

      const contractClassName = `watch-contract${meta.rolloverDue ? " rollover-due" : ""}${meta.rolloverNew ? " rollover-new" : ""}`;
      if (view.contract.className !== contractClassName) view.contract.className = contractClassName;
      const contractHidden = !meta.contractLabel;
      if (view.contract.hidden !== contractHidden) view.contract.hidden = contractHidden;
      if (view.contract.title !== meta.contractTitle) view.contract.title = meta.contractTitle;
      if (view.contractLabel.textContent !== meta.contractLabel) view.contractLabel.textContent = meta.contractLabel;
      const contractNewHidden = !meta.rolloverNew;
      if (view.contractNew.hidden !== contractNewHidden) view.contractNew.hidden = contractNewHidden;
      const rolloverNewTitle = meta.rolloverNewMessage || "New front-month contract";
      if (view.contractNew.title !== rolloverNewTitle) view.contractNew.title = rolloverNewTitle;
      const contractWarnHidden = !meta.rolloverDue;
      if (view.contractWarn.hidden !== contractWarnHidden) view.contractWarn.hidden = contractWarnHidden;

      const deleteLabel = String(item.display || item.key || "");
      if (view.deleteButton.dataset.watchDelete !== item.instrument_id) {
        view.deleteButton.dataset.watchDelete = item.instrument_id;
      }
      if (view.deleteButton.dataset.watchDeleteLabel !== deleteLabel) {
        view.deleteButton.dataset.watchDeleteLabel = deleteLabel;
      }
      if (view.deleteButton.title !== "Remove instrument") view.deleteButton.title = "Remove instrument";
      const deleteAriaLabel = `Remove ${deleteLabel}`;
      if (view.deleteButton.getAttribute("aria-label") !== deleteAriaLabel) {
        view.deleteButton.setAttribute("aria-label", deleteAriaLabel);
      }
      return true;
    }

    function clearWatchlistDropState(list) {
      const root = list || document;
      if (typeof root.querySelectorAll !== "function") return;
      root.querySelectorAll(".watchlist-dragging, .watchlist-drop-before, .watchlist-drop-after").forEach(node => {
        node.classList.remove("watchlist-dragging", "watchlist-drop-before", "watchlist-drop-after");
      });
    }

    function moveWatchlistInstrument(sourceId, targetId, after = false) {
      const source = String(sourceId || "").trim();
      const target = String(targetId || "").trim();
      if (!source || !target || source === target) return false;
      const current = [...(state.instruments || [])];
      const sourceIndex = current.findIndex(item => item.instrument_id === source);
      if (sourceIndex < 0) return false;
      const [moved] = current.splice(sourceIndex, 1);
      const targetIndex = current.findIndex(item => item.instrument_id === target);
      if (targetIndex < 0) return false;
      current.splice(targetIndex + (after ? 1 : 0), 0, moved);
      state.instruments = current;
      renderInstruments({ force: true, immediate: true });
      return true;
    }

    function switchWatchlistInstrument(nextInstrumentId) {
      const selected = instrumentForId(nextInstrumentId);
      if (!selected || selected.instrument_id === state.instrumentId) return;
      if (typeof publishLinkedCrosshair === "function") {
        publishLinkedCrosshair(state.crosshair, { visible: false, immediate: true });
      }
      if (state.paperTrading?.armed) {
        clearPaperTradeArm("Instrument changed. Paper order disarmed.");
      }
      if (currentOptionDriveState().armed) {
        optionDriveDisarm("Chart changed. Trade instrument preserved; re-arm to trade.");
      }
      if (typeof mtfLensPrepareForPrimaryScopeChange === "function") {
        mtfLensPrepareForPrimaryScopeChange();
      }
      closeQuoteStream();
      if (currentOptionBoardState().surface === "dialog") {
        closeOptionBoard({ reason: "chart instrument switched" });
      }
      state.instrumentId = selected.instrument_id;
      state.instrumentSelectionStatus = {
        state: "resolved",
        code: "INSTRUMENT_SELECTION_RESOLVED",
        instrument_id: state.instrumentId,
        message: "",
      };
      state.symbol = String(selected.key || selected.display || "").trim();
      state.dataSource = String(selected.provider || "").trim().toLowerCase();
      void stopGexLiveContext({
        pruneRouteCaches: true,
        preserveCurrentRoute: true,
        reason: "instrument switched",
      });
      workspaceSet("instrumentId", state.instrumentId);
      state.range = storedRangeForInstrument(state.instrumentId, state.timeframe);
      state.requests.history.exhausted = false;
      clearUiNotices({ routeScopedOnly: true });
      view.barsVisible = storedBarsVisibleForInstrument(state.instrumentId, state.timeframe);
      view.offset = 0;
      view.historyPullOffsetBars = 0;
      view.rightGapBars = DEFAULT_RIGHT_GAP_BARS;
      view.rightGapManual = false;
      view.followLatest = true;
      loadPaperTradingInstrumentSettings();
      renderPaperTradingPanel();
      loadIndicatorSettingsForCurrent();
      clearChartStateForRouteSwitch();
      loadDrawingsForCurrent();
      setActiveGexContext(state.gexContexts[gexContextKey()] || null);
      state.loadingGex = Boolean(state.loadingGexKeys[gexContextKey()]);
      applySettings();
      renderInstruments({ force: true });
      if (typeof mtfLensRebindCurrentScope === "function") mtfLensRebindCurrentScope();
      load({ force: true, historyLoad: true, historyFollowLatest: true });
      if (
        providerCapability(state.dataSource, "live_quote_stream")
        || providerCapability(state.dataSource, "live_quote_polling")
      ) loadLiveQuote();
      connectQuoteStream(false);
      renderOptionDrive();
      syncWorkspacePageState();
    }

    function setupInstrumentList() {
      const list = document.getElementById("instrument-list");
      if (!list || list.dataset.watchlistBound === "1") return;
      list.dataset.watchlistBound = "1";
      list.addEventListener("click", event => {
        const modeButton = event.target?.closest?.("[data-watch-display-mode]");
        if (modeButton) {
          event.stopPropagation();
          event.preventDefault();
          if (!state.watchlistEditMode) return;
          const row = modeButton.closest(".watchlist-row");
          if (!row) return;
          setWatchlistInstrumentDisplayMode(row.dataset.instrumentId, modeButton.dataset.watchDisplayMode);
          return;
        }
        const deleteBtn = event.target?.closest?.("[data-watch-delete]");
        if (deleteBtn) {
          event.stopPropagation();
          event.preventDefault();
          if (!state.watchlistEditMode) return;
          deleteWatchlistInstrument(deleteBtn.dataset.watchDelete, deleteBtn.dataset.watchDeleteLabel);
          return;
        }
        const button = event.target?.closest?.(".watchlist-row");
        if (!button) return;
        if (state.watchlistEditMode) {
          event.preventDefault();
          return;
        }
        switchWatchlistInstrument(button.dataset.instrumentId);
      });
      list.addEventListener("keydown", async event => {
        const reorderRow = event.target?.closest?.(".watchlist-row");
        if (
          state.watchlistEditMode
          && event.altKey
          && ["ArrowUp", "ArrowDown"].includes(event.key)
          && reorderRow
          && !event.target?.closest?.("[data-watch-display-mode], [data-watch-delete]")
        ) {
          const rows = [...list.querySelectorAll(".watchlist-row")];
          const index = rows.indexOf(reorderRow);
          const targetIndex = event.key === "ArrowUp" ? index - 1 : index + 1;
          if (index >= 0 && targetIndex >= 0 && targetIndex < rows.length) {
            event.preventDefault();
            const movedId = reorderRow.dataset.instrumentId;
            const targetId = rows[targetIndex].dataset.instrumentId;
            const moved = moveWatchlistInstrument(movedId, targetId, event.key === "ArrowDown");
            if (moved) {
              await saveWatchlistOrder();
              const movedRow = [...list.querySelectorAll(".watchlist-row")]
                .find(row => row.dataset.instrumentId === movedId);
              movedRow?.focus();
            }
          }
          return;
        }
        if (!["Enter", " "].includes(event.key) || event.target?.closest?.("[data-watch-display-mode], [data-watch-delete]")) return;
        const button = event.target?.closest?.(".watchlist-row");
        if (!button || state.watchlistEditMode) return;
        event.preventDefault();
        switchWatchlistInstrument(button.dataset.instrumentId);
      });
      list.addEventListener("contextmenu", event => {
        const button = event.target?.closest?.(".watchlist-row");
        if (!button || event.target?.closest?.("[data-watch-delete], [data-watch-display-mode]")) return;
        const nextInstrumentId = button.dataset.instrumentId;
        if (!nextInstrumentId) return;
        event.preventDefault();
        event.stopPropagation();
        window.open(chartWindowUrlForInstrument(nextInstrumentId), "_blank", "noopener");
      });
      list.addEventListener("dragstart", event => {
        const button = event.target?.closest?.(".watchlist-row");
        if (!state.watchlistEditMode || !button || event.target?.closest?.("[data-watch-delete], [data-watch-display-mode]")) {
          event.preventDefault();
          return;
        }
        watchlistDragKey = button.dataset.instrumentId || "";
        button.classList.add("watchlist-dragging");
        if (event.dataTransfer) {
          event.dataTransfer.effectAllowed = "move";
          event.dataTransfer.setData("text/plain", watchlistDragKey);
        }
      });
      list.addEventListener("dragover", event => {
        if (!state.watchlistEditMode || !watchlistDragKey) return;
        const button = event.target?.closest?.(".watchlist-row");
        if (!button || button.dataset.instrumentId === watchlistDragKey) return;
        event.preventDefault();
        const rect = button.getBoundingClientRect();
        const after = event.clientY > rect.top + rect.height / 2;
        list.querySelectorAll(".watchlist-drop-before, .watchlist-drop-after").forEach(node => {
          node.classList.remove("watchlist-drop-before", "watchlist-drop-after");
        });
        button.classList.toggle("watchlist-drop-before", !after);
        button.classList.toggle("watchlist-drop-after", after);
        if (event.dataTransfer) event.dataTransfer.dropEffect = "move";
      });
      list.addEventListener("drop", async event => {
        if (!state.watchlistEditMode || !watchlistDragKey) return;
        const button = event.target?.closest?.(".watchlist-row");
        if (!button) return;
        event.preventDefault();
        const targetKey = button.dataset.instrumentId;
        const after = button.classList.contains("watchlist-drop-after");
        clearWatchlistDropState(list);
        const moved = moveWatchlistInstrument(event.dataTransfer?.getData("text/plain") || watchlistDragKey, targetKey, after);
        watchlistDragKey = "";
        if (moved) await saveWatchlistOrder();
      });
      list.addEventListener("dragend", () => {
        watchlistDragKey = "";
        clearWatchlistDropState(list);
      });
    }

    function renderInstruments(options = {}) {
      const incomingDirtyKeys = options.dirtyIdentityKeys instanceof Set
        ? options.dirtyIdentityKeys
        : null;
      const pendingIsFull = Boolean(
        pendingInstrumentRenderOptions
        && pendingInstrumentRenderOptions.dirtyIdentityKeys === null
      );
      const dirtyIdentityKeys = pendingIsFull || !incomingDirtyKeys
        ? null
        : new Set([
            ...(pendingInstrumentRenderOptions?.dirtyIdentityKeys || []),
            ...incomingDirtyKeys,
          ]);
      pendingInstrumentRenderOptions = {
        ...pendingInstrumentRenderOptions,
        ...options,
        force: Boolean(pendingInstrumentRenderOptions?.force || options.force),
        dirtyIdentityKeys,
      };
      if (options.immediate || options.force) {
        if (instrumentRenderFrame) {
          cancelAnimationFrame(instrumentRenderFrame);
          instrumentRenderFrame = 0;
        }
        const targetOptions = pendingInstrumentRenderOptions;
        pendingInstrumentRenderOptions = null;
        if (window.mcPerfEnd) window.mcPerfEnd(instrumentRenderQueuePerf, "immediate", 12);
        instrumentRenderQueuePerf = null;
        renderInstrumentsNow(targetOptions || {});
        return;
      }
      if (instrumentRenderFrame) return;
      if (!instrumentRenderQueuePerf && window.mcPerfStart) {
        instrumentRenderQueuePerf = window.mcPerfStart("watchlistRafQueue");
      }
      instrumentRenderFrame = requestAnimationFrame(() => {
        instrumentRenderFrame = 0;
        const targetOptions = pendingInstrumentRenderOptions || {};
        pendingInstrumentRenderOptions = null;
        if (window.mcPerfEnd) window.mcPerfEnd(instrumentRenderQueuePerf, "rAF", 20);
        instrumentRenderQueuePerf = null;
        renderInstrumentsNow(targetOptions);
      });
    }

    function renderInstrumentsNow(options = {}) {
      const renderPerf = window.mcPerfStart ? window.mcPerfStart("renderWatchlist") : null;
      let renderedRows = 0;
      try {
        const list = document.getElementById("instrument-list");
        if (!list) return;
        const rows = screenerRowMap(state.screener || []);
        const instrumentsChanged = watchlistInstrumentIndexRows !== state.instruments;
        const canPatchDirty = options.dirtyIdentityKeys instanceof Set
          && !instrumentsChanged
          && Boolean(lastWatchlistStructureKey)
          && Boolean(list.childElementCount)
          && Boolean(watchlistRowViewIndex.size);
        const structureKey = canPatchDirty
          ? lastWatchlistStructureKey
          : watchlistStructureKey();
        if (instrumentsChanged) {
          watchlistInstrumentIndexRows = state.instruments;
          watchlistInstrumentIndex = new Map(
            state.instruments
              .map(item => [screenerRowIdentityKey(item), item])
              .filter(([identityKey]) => Boolean(identityKey)),
          );
        }
        const forceRebuild = Boolean(
          options.force
          || structureKey !== lastWatchlistStructureKey
          || list.childElementCount !== state.instruments.length
          || watchlistRowViewIndex.size !== state.instruments.length
        );
        if (forceRebuild) {
          replaceWatchlistRows(list, rows);
          lastWatchlistStructureKey = structureKey;
          renderedRows = state.instruments.length;
          return;
        }
        if (options.dirtyIdentityKeys instanceof Set) {
          for (const identityKey of options.dirtyIdentityKeys) {
            const item = watchlistInstrumentIndex.get(identityKey);
            if (!item) continue;
            const view = watchlistRowViewIndex.get(identityKey);
            if (!view) {
              replaceWatchlistRows(list, rows);
              lastWatchlistStructureKey = watchlistStructureKey();
              renderedRows = state.instruments.length;
              return;
            }
            if (patchWatchlistRow(view, item, rows.get(identityKey) || {})) {
              renderedRows += 1;
              continue;
            }
            if (!replaceWatchlistRow(item, rows)) {
              replaceWatchlistRows(list, rows);
              renderedRows = state.instruments.length;
              lastWatchlistStructureKey = watchlistStructureKey();
              return;
            }
            renderedRows += 1;
          }
          lastWatchlistStructureKey = watchlistStructureKey();
          return;
        }
        for (const item of state.instruments) {
          const identityKey = screenerRowIdentityKey(item);
          const quote = watchlistQuoteForItem(item, rows);
          const view = watchlistRowViewIndex.get(identityKey);
          if (!view) {
            replaceWatchlistRows(list, rows);
            lastWatchlistStructureKey = watchlistStructureKey();
            renderedRows = state.instruments.length;
            return;
          }
          if (patchWatchlistRow(view, item, quote)) renderedRows += 1;
        }
      } catch (error) {
        if (typeof showRuntimeError === "function") showRuntimeError(error, "watchlistRender");
        else console.error("watchlist render failed", error);
      } finally {
        if (window.mcPerfEnd) window.mcPerfEnd(renderPerf, `${renderedRows} rows`, 8);
        if (state.transport.quote.commitPerf && window.mcPerfEnd) {
          window.mcPerfEnd(state.transport.quote.commitPerf, `${renderedRows} rows`, 16);
          state.transport.quote.commitPerf = null;
        }
        if (state.transport.quote.paintPerf && window.mcPerfEnd) {
          const paintPerf = state.transport.quote.paintPerf;
          const gatewayPaintAtMs = Number(state.transport.quote.gatewayPaintAtMs || 0);
          state.transport.quote.paintPerf = null;
          state.transport.quote.gatewayPaintAtMs = 0;
          requestAnimationFrame(() => {
            window.mcPerfEnd(paintPerf, `${renderedRows} rows`, 34);
            const gatewayLatencyMs = Date.now() - gatewayPaintAtMs;
            if (
              gatewayPaintAtMs > 0
              && Number.isFinite(gatewayLatencyMs)
              && gatewayLatencyMs >= 0
              && window.mcRecordBackendTiming
            ) {
              window.mcRecordBackendTiming(
                "quoteGatewayToPaint",
                gatewayLatencyMs,
                `${renderedRows} rows`,
              );
            }
          });
        }
      }
    }

    function timeframeLabel(interval) {
      return (TIMEFRAMES.find(item => item.interval === interval) || {}).label || interval.toUpperCase();
    }

    function renderTimeframes() {
      const node = document.getElementById("timeframe-buttons");
      const lensTimeframe = typeof mtfLensTimeframe === "function" ? mtfLensTimeframe() : "";
      node.innerHTML = TIMEFRAMES
        .map(item => `<button class="tool-button ${item.interval === state.timeframe ? "active" : ""} ${item.interval === lensTimeframe ? "mtf-secondary-timeframe" : ""}" data-timeframe="${item.interval}">${item.label}</button>`)
        .join("");
      node.querySelectorAll("button").forEach(button => {
        button.addEventListener("contextmenu", event => {
          const nextTimeframe = button.dataset.timeframe;
          if (!nextTimeframe) return;
          event.preventDefault();
          event.stopPropagation();
          if (nextTimeframe === state.timeframe) return;
          if (typeof mtfLensToggle === "function") mtfLensToggle(nextTimeframe);
        });
        button.addEventListener("click", () => {
          const nextTimeframe = button.dataset.timeframe;
          if (!nextTimeframe || nextTimeframe === state.timeframe) return;
          if (typeof publishLinkedCrosshair === "function") {
            publishLinkedCrosshair(state.crosshair, { visible: false, immediate: true });
          }
          if (typeof mtfLensHandlePrimaryTimeframeChange === "function") {
            mtfLensHandlePrimaryTimeframeChange(nextTimeframe);
          }
          closeQuoteStream();
          state.timeframe = button.dataset.timeframe;
          workspaceSet("timeframe", state.timeframe);
          state.range = storedRangeForInstrument(state.instrumentId, state.timeframe);
          state.requests.history.exhausted = false;
          clearUiNotices({ routeScopedOnly: true });
          view.barsVisible = storedBarsVisibleForInstrument(state.instrumentId, state.timeframe);
          view.offset = 0;
          view.historyPullOffsetBars = 0;
          view.rightGapBars = DEFAULT_RIGHT_GAP_BARS;
          view.rightGapManual = false;
          view.followLatest = true;
          state.crosshair.visible = false;
          loadIndicatorSettingsForCurrent();
          clearChartStateForRouteSwitch();
          loadDrawingsForCurrent();
          setActiveGexContext(state.gexContexts[gexContextKey()] || null);
          state.loadingGex = Boolean(state.loadingGexKeys[gexContextKey()]);
          applySettings();
          load({ force: true, historyLoad: true, historyFollowLatest: true });
          loadScreener({ allowWhenStream: true });
          connectQuoteStream(true);
          syncWorkspacePageState();
        });
      });
      if (typeof mtfLensSyncTimeframeButtons === "function") mtfLensSyncTimeframeButtons();
      if (typeof mtfLensRebindCurrentScope === "function") mtfLensRebindCurrentScope();
    }
