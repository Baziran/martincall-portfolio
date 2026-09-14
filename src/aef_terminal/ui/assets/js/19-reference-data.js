    function firstInstrumentId(excludedId = "") {
      const excluded = exactIdentityText(excludedId);
      const item = (state.instruments || []).find(candidate => {
        const instrumentId = exactIdentityText(candidate?.instrument_id);
        return instrumentId && instrumentId !== excluded;
      });
      return exactIdentityText(item?.instrument_id);
    }

    function activateLoadedWatchlistRuntime() {
      if (state.instrumentSelectionStatus?.state !== "resolved" || !instrumentForId(state.instrumentId)) {
        return false;
      }
      loadScreener({ allowWhenStream: true });
      loadWatchlistTrends({ force: true });
      connectQuoteStream(true);
      return true;
    }

    async function loadInstruments(options = {}) {
      const previousInstrumentId = state.instrumentId;
      const previousInstrumentAvailable = Boolean(instrumentForId(previousInstrumentId));
      const previousRouteFingerprint = instrumentRouteFingerprint();
      try {
        debugStep("instruments request");
        const payload = await fetchJson("/api/instruments");
        if (payload && payload.ok === false) throw new Error(apiErrorMessage(payload, "instrument load failed"));
        if (!payload || typeof payload !== "object" || Array.isArray(payload) || !Array.isArray(payload.items)) {
          throw new Error("instrument load returned an invalid watchlist snapshot");
        }
        const nextWatchlistVersion = Number(payload.watchlist_version);
        if (!Number.isSafeInteger(nextWatchlistVersion) || nextWatchlistVersion < 0) {
          throw new Error("instrument load returned an invalid watchlist version");
        }
        const nextInstruments = reconcileWatchlistPresentations(payload.items);
        const plannedInstrumentId = state.instrumentId
          || exactIdentityText(nextInstruments[0]?.instrument_id);
        const plannedSelected = nextInstruments.find(item => (
          exactIdentityText(item?.instrument_id) === plannedInstrumentId
        )) || null;
        const plannedRouteFingerprint = exactIdentityText(plannedSelected?.route_fingerprint);
        const selectedRouteScopeChanged = (
          plannedInstrumentId !== previousInstrumentId
          || plannedRouteFingerprint !== previousRouteFingerprint
        );
        if (selectedRouteScopeChanged && typeof mtfLensPrepareForPrimaryScopeChange === "function") {
          mtfLensPrepareForPrimaryScopeChange();
        }
        closeQuoteStream();
        state.instruments = nextInstruments;
        state.watchlistVersion = nextWatchlistVersion;
        pruneWatchlistTrendState();
        let selected = instrumentForId();
        if (!selected && !state.instrumentId) {
          if (state.paperTrading?.armed) {
            clearPaperTradeArm("Instrument changed. Paper order disarmed.");
          }
          state.instrumentId = firstInstrumentId();
          selected = instrumentForId();
        }
        if (!selected && state.paperTrading?.armed) {
          clearPaperTradeArm("Instrument is unavailable. Paper order disarmed.");
        }
        if (selected) {
          state.symbol = String(selected.key || selected.display || "").trim();
          state.instrumentSelectionStatus = {
            state: "resolved",
            code: "INSTRUMENT_SELECTION_RESOLVED",
            instrument_id: state.instrumentId,
            message: "",
          };
          clearUiNotice("watchlist");
          workspaceSet("instrumentId", state.instrumentId);
        } else if (state.instrumentId) {
          const message = "Saved instrument is not in the current watchlist. Select a current watchlist row.";
          state.symbol = "";
          state.dataSource = "";
          state.instrumentSelectionStatus = {
            state: "unresolved",
            code: "INSTRUMENT_SELECTION_NOT_IN_WATCHLIST",
            instrument_id: state.instrumentId,
            message,
          };
          setUiNotice("watchlist", "INSTRUMENT_SELECTION_NOT_IN_WATCHLIST", message, {
            state: "blocked",
            routeScoped: false,
          });
        } else {
          const message = "Watchlist is empty. Add a qualified instrument.";
          state.symbol = "";
          state.dataSource = "";
          state.instrumentSelectionStatus = {
            state: "empty",
            code: "WATCHLIST_EMPTY",
            instrument_id: "",
            message,
          };
          setUiNotice("watchlist", "WATCHLIST_EMPTY", message, {
            state: "empty",
            routeScoped: false,
          });
        }
        if (selected) state.dataSource = String(selected.provider || "").trim().toLowerCase();
        if (selectedRouteScopeChanged && typeof mtfLensRebindCurrentScope === "function") {
          mtfLensRebindCurrentScope();
        }
        const instrumentMembershipChanged = (
          previousInstrumentAvailable !== Boolean(selected)
        );
        let paperSettingsReconciled = true;
        if (
          instrumentMembershipChanged
          && serverStorageReady
          && typeof reconcileServerSettingsFromBackend === "function"
        ) {
          try {
            await reconcileServerSettingsFromBackend();
          } catch (error) {
            paperSettingsReconciled = false;
            console.warn(
              "paper instrument settings reconciliation failed",
              requestErrorMessage(error, "paper instrument settings reconciliation failed"),
            );
          }
        }
        if (state.instrumentId !== previousInstrumentId || instrumentRouteFingerprint() !== previousRouteFingerprint) {
          if (currentOptionDriveState().armed) {
            optionDriveDisarm("Chart route changed. Trade instrument preserved; re-arm to trade.");
          }
          if (currentOptionBoardState().surface === "dialog") {
            closeOptionBoard({ reason: "chart instrument route changed" });
          }
          await stopGexLiveContext({
            pruneRouteCaches: true,
            preserveCurrentRoute: true,
            reason: "instrument route changed",
          });
          state.range = storedRangeForInstrument(state.instrumentId, state.timeframe);
          view.barsVisible = storedBarsVisibleForInstrument(state.instrumentId, state.timeframe);
          syncWorkspacePageState();
          clearChartStateForRouteSwitch();
        }
        if (state.instrumentId !== previousInstrumentId || instrumentMembershipChanged) {
          loadPaperTradingInstrumentSettings();
          if (!paperSettingsReconciled) state.settings.paperAutoTrading = false;
          renderPaperTradingPanel();
          if (typeof applySettings === "function") applySettings();
        }
        reconcileOptionDriveInstrumentCatalog();
        debugStep("instruments response", `${state.instruments.length} rows`);
      } catch (error) {
        console.warn("instrument catalog load failed", error);
        debugStep("instruments error", requestErrorMessage(error, "instruments load failed"));
        throw error;
      }
      renderInstruments();
      renderGexSchedulerInstrumentOptions();
      if (state.instrumentSelectionStatus.state !== "resolved") {
        const unavailable = state.instrumentSelectionStatus.state === "unresolved";
        const title = unavailable ? "Instrument unavailable" : "Watchlist empty";
        document.title = title;
        const chartTitle = document.getElementById("chart-title");
        if (chartTitle) chartTitle.textContent = title;
        renderEmptyCanvas(
          "price-chart",
          unavailable ? "INSTRUMENT NOT IN WATCHLIST" : "WATCHLIST EMPTY",
          state.instrumentSelectionStatus.message,
        );
        renderEmptyCanvas("volume-chart", "NO ACTIVE INSTRUMENT", state.instrumentSelectionStatus.message);
      }
      if (options.activate !== false) activateLoadedWatchlistRuntime();
    }

    async function addWatchlistInstrumentFromForm() {
      if (!state.watchlistEditMode) {
        showBrowserToast("Enable watchlist edit mode first");
        return;
      }
      const symbolInput = document.getElementById("watchlist-symbol-input");
      const providerSelect = document.getElementById("watchlist-provider-select");
      const provider = String(providerSelect?.value || state.dataSource || state.providers?.[0]?.key || "ibkr").trim().toLowerCase();
      const query = String(symbolInput?.value || "").trim();
      if (!query) {
        showBrowserToast("Enter instrument name");
        return;
      }
      try {
        const result = await fetchJson("/api/instruments/search", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ provider, query }),
          cache: "no-store",
        });
        if (!result.ok) {
          showBrowserToast(`Instrument search failed: ${apiErrorMessage(result, "error")}`);
          return;
        }
        const matches = Array.isArray(result.matches) ? result.matches : [];
        if (matches.length === 0) {
          renderWatchlistInstrumentMatches([], provider, query);
          showBrowserToast(`No ${provider.toUpperCase()} instrument found`);
          return;
        }
        if (matches.length === 1) {
          await addWatchlistInstrumentCandidate(matches[0]);
          return;
        }
        renderWatchlistInstrumentMatches(matches, provider, query);
      } catch (error) {
        showBrowserToast(`Instrument search failed: ${requestErrorMessage(error, "instrument search failed")}`);
      }
    }

    async function addWatchlistInstrumentCandidate(candidate) {
      const instrumentId = exactIdentityText(candidate?.instrument_id);
      const provider = String(candidate?.provider || document.getElementById("watchlist-provider-select")?.value || "").trim().toLowerCase();
      const instrumentKey = String(candidate?.instrument_key || "");
      const providerContractId = exactIdentityText(candidate?.provider_contract_id);
      const assetClass = String(candidate?.asset_class || "").trim().toLowerCase();
      const identityScope = String(candidate?.identity_scope || "").trim().toLowerCase();
      const root = String(candidate?.root || (assetClass === "future" && identityScope === "root" ? instrumentKey : ""));
      if (!instrumentId || !provider || !instrumentKey) return;
      try {
        const request = { instrument_id: instrumentId, provider, instrument_key: instrumentKey };
        if (providerContractId) request.provider_contract_id = providerContractId;
        if (root) {
          request.asset_class = "future";
          request.identity_scope = "root";
          request.root = root;
          request.contract_identity = {
            provider,
            asset_class: "future",
            identity_scope: "root",
            root,
            exchange: String(candidate?.exchange || ""),
            currency: String(candidate?.currency || ""),
            trading_class: String(candidate?.trading_class || ""),
          };
        }
        const result = await fetchJson("/api/instruments", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(request),
          cache: "no-store",
        });
        if (!result.ok) {
          showBrowserToast(`Watchlist add failed: ${apiErrorMessage(result, "error")}`);
          return;
        }
        const symbolInput = document.getElementById("watchlist-symbol-input");
        if (symbolInput) symbolInput.value = "";
        renderWatchlistInstrumentMatches([]);
        if (serverStorageReady && typeof reconcileServerSettingsFromBackend === "function") {
          try {
            await reconcileServerSettingsFromBackend();
          } catch (error) {
            console.warn(
              "added instrument settings reconciliation failed",
              requestErrorMessage(error, "added instrument settings reconciliation failed"),
            );
          }
        }
        await loadInstruments();
        showBrowserToast(`${instrumentKey} added to watchlist`);
      } catch (error) {
        showBrowserToast(`Watchlist add failed: ${requestErrorMessage(error, "watchlist add failed")}`);
      }
    }

    function renderWatchlistInstrumentMatches(matches = [], provider = "", query = "") {
      const root = document.getElementById("watchlist-search-results");
      if (!root) return;
      const rows = Array.isArray(matches) ? matches : [];
      root.classList.toggle("has-results", rows.length > 0);
      if (!rows.length) {
        root.innerHTML = "";
        return;
      }
      root.innerHTML = rows.map(item => {
        const instrumentId = exactIdentityText(item?.instrument_id);
        const key = String(item?.instrument_key || "");
        const name = String(item?.name || item?.display || key);
        const providerSymbol = exactIdentityText(item?.provider_symbol);
        if (!instrumentId || !providerSymbol) return "";
        const itemProvider = String(item?.provider || provider || "");
        const contractId = exactIdentityText(item?.provider_contract_id);
        const assetClass = String(item?.asset_class || "");
        const identityScope = String(item?.identity_scope || "");
        const rootKey = String(item?.root || "");
        const exchange = String(item?.primary_exchange || item?.exchange || "");
        const currency = String(item?.currency || "");
        const currentContract = item?.current_contract && typeof item.current_contract === "object" ? item.current_contract : {};
        const currentContractLabel = String(
          currentContract.local_symbol || currentContract.contract_key || currentContract.contract_month || currentContract.expiry || ""
        );
        const marketLabel = [exchange, currency, currentContractLabel].filter(Boolean).join(" · ");
        const title = `${itemProvider.toUpperCase()} ${providerSymbol}${query ? ` · ${query}` : ""}`;
        return `<button class="watchlist-search-result" type="button" data-watchlist-search-instrument-id="${escapeHtml(instrumentId)}" data-watchlist-search-key="${escapeHtml(key)}" data-watchlist-search-provider="${escapeHtml(itemProvider)}" data-watchlist-search-contract="${escapeHtml(contractId)}" data-watchlist-search-asset-class="${escapeHtml(assetClass)}" data-watchlist-search-identity-scope="${escapeHtml(identityScope)}" data-watchlist-search-root="${escapeHtml(rootKey)}" data-watchlist-search-exchange="${escapeHtml(String(item?.exchange || ""))}" data-watchlist-search-currency="${escapeHtml(String(item?.currency || ""))}" data-watchlist-search-trading-class="${escapeHtml(String(item?.trading_class || ""))}" title="${escapeHtml(title)}">
          <span>${escapeHtml(key)}</span>
          <b>${escapeHtml(name)}</b>
          <small>${escapeHtml(itemProvider.toUpperCase())} · ${escapeHtml(providerSymbol)}${marketLabel ? ` · ${escapeHtml(marketLabel)}` : ""}</small>
        </button>`;
      }).join("");
    }

    function renderWatchlistProviderOptions() {
      const select = document.getElementById("watchlist-provider-select");
      if (!select) return;
      const providers = Array.isArray(state.providers) ? state.providers : [];
      const selected = String(select.value || state.dataSource || "ibkr").trim().toLowerCase();
      select.innerHTML = providers.map(provider => {
        const key = String(provider?.key || "").trim().toLowerCase();
        if (!key) return "";
        const label = String(provider?.name || key);
        return `<option value="${escapeHtml(key)}">${escapeHtml(label)}</option>`;
      }).join("");
      if ([...select.options].some(option => option.value === selected)) select.value = selected;
    }

    function renderGexSchedulerInstrumentOptions() {
      const select = document.getElementById("gex-scheduler-instrument-ids");
      if (!select) return;
      const summary = document.getElementById("gex-scheduler-instrument-summary");
      const selectedIds = new Set(
        Array.isArray(state.settings.gexSchedulerInstrumentIds)
          ? state.settings.gexSchedulerInstrumentIds
          : []
      );
      const selectedLabels = [];
      select.innerHTML = (state.instruments || []).map(item => {
        const instrumentId = item?.instrument_id;
        if (typeof instrumentId !== "string" || !instrumentId) return "";
        const display = String(item?.display || item?.instrument_key || "Qualified instrument");
        const provider = String(item?.provider || "").toUpperCase();
        const selected = selectedIds.has(instrumentId) ? " selected" : "";
        const label = `${display}${provider ? ` · ${provider}` : ""}`;
        if (selected) selectedLabels.push(label);
        return `<option value="${escapeHtml(instrumentId)}"${selected}>${escapeHtml(label)}</option>`;
      }).join("");
      if (summary) {
        summary.textContent = selectedLabels.length > 2
          ? `${selectedLabels.slice(0, 2).join(", ")} +${selectedLabels.length - 2}`
          : selectedLabels.join(", ") || "None selected";
        summary.title = selectedLabels.join(", ") || "No automatic GEX refresh contracts selected";
      }
    }

    async function deleteWatchlistInstrument(instrumentId, label = "") {
      if (!state.watchlistEditMode) {
        showBrowserToast("Enable watchlist edit mode first");
        return;
      }
      const identity = exactIdentityText(instrumentId);
      const display = String(label || "instrument").trim();
      if (!identity) return;
      if (!window.confirm(`Remove ${display} from watchlist and stop broker subscription?`)) return;
      try {
        const result = await fetchJson(`/api/instruments/${encodeURIComponent(identity)}`, {
          method: "DELETE",
          cache: "no-store",
        });
        if (!result.ok) {
          showBrowserToast(`Watchlist delete failed: ${apiErrorMessage(result, "error")}`);
          return;
        }
        if (serverStorageReady && typeof reconcileServerSettingsFromBackend === "function") {
          try {
            await reconcileServerSettingsFromBackend();
          } catch (error) {
            console.warn(
              "deleted instrument settings reconciliation failed",
              requestErrorMessage(error, "deleted instrument settings reconciliation failed"),
            );
          }
        }
        if (state.instrumentId === identity) {
          if (state.paperTrading?.armed) {
            clearPaperTradeArm("Instrument removed. Paper order disarmed.");
          }
          if (typeof mtfLensPrepareForPrimaryScopeChange === "function") {
            mtfLensPrepareForPrimaryScopeChange();
          }
          closeQuoteStream();
          state.instrumentId = firstInstrumentId(identity);
          state.instrumentSelectionStatus = {
            state: "pending",
            code: "INSTRUMENT_SELECTION_PENDING",
            instrument_id: state.instrumentId,
            message: "Resolving the next watchlist instrument.",
          };
          workspaceSet("instrumentId", state.instrumentId);
          loadPaperTradingInstrumentSettings();
          renderPaperTradingPanel();
          if (typeof applySettings === "function") applySettings();
          syncWorkspacePageState();
          clearChartStateForRouteSwitch();
        }
        await loadInstruments();
        if (typeof mtfLensRebindCurrentScope === "function") mtfLensRebindCurrentScope();
        showBrowserToast(`${display} removed from watchlist`);
      } catch (error) {
        showBrowserToast(`Watchlist delete failed: ${requestErrorMessage(error, "watchlist delete failed")}`);
      }
    }

    async function saveWatchlistOrder() {
      const order = (state.instruments || []).map(item => item.instrument_id).filter(Boolean);
      if (state.watchlistVersion === null || state.watchlistVersion === undefined) {
        showBrowserToast("Watchlist order save failed: reload watchlist first");
        await loadInstruments();
        return false;
      }
      try {
        const result = await fetchJson("/api/instruments/order", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ order, expected_version: state.watchlistVersion }),
          cache: "no-store",
        });
        if (!result.ok) {
          showBrowserToast(`Watchlist order save failed: ${apiErrorMessage(result, "error")}`);
          await loadInstruments();
          return false;
        }
        if (result.watchlist_version !== undefined && result.watchlist_version !== null) {
          state.watchlistVersion = result.watchlist_version;
        }
        return true;
      } catch (error) {
        showBrowserToast(`Watchlist order save failed: ${requestErrorMessage(error, "watchlist order save failed")}`);
        await loadInstruments();
        return false;
      }
    }

    function setWatchlistEditMode(enabled, options = {}) {
      state.watchlistEditMode = Boolean(enabled);
      const root = document.getElementById("side-instruments");
      const list = document.getElementById("instrument-list");
      const toggle = document.getElementById("watchlist-edit-toggle");
      const hint = document.getElementById("watchlist-edit-hint");
      const addControls = document.querySelectorAll(".watchlist-add input, .watchlist-add select, .watchlist-add button");
      root?.classList.toggle("watchlist-editing", state.watchlistEditMode);
      list?.classList.toggle("watchlist-editing", state.watchlistEditMode);
      toggle?.classList.toggle("active", state.watchlistEditMode);
      toggle?.setAttribute("aria-pressed", state.watchlistEditMode ? "true" : "false");
      if (hint) hint.hidden = !state.watchlistEditMode;
      addControls.forEach(node => {
        node.disabled = !state.watchlistEditMode;
      });
      if (!state.watchlistEditMode) renderWatchlistInstrumentMatches([]);
      if (options.render !== false) renderInstruments({ force: true });
    }

    function setupWatchlistControls() {
      document.getElementById("watchlist-edit-toggle")?.addEventListener("click", () => {
        setWatchlistEditMode(!state.watchlistEditMode);
      });
      document.getElementById("watchlist-add-button")?.addEventListener("click", addWatchlistInstrumentFromForm);
      document.getElementById("watchlist-symbol-input")?.addEventListener("keydown", event => {
        if (event.key === "Enter") addWatchlistInstrumentFromForm();
      });
      document.getElementById("watchlist-provider-select")?.addEventListener("change", () => {
        renderWatchlistInstrumentMatches([]);
      });
      document.getElementById("watchlist-search-results")?.addEventListener("click", event => {
        const button = event.target?.closest?.("[data-watchlist-search-key]");
        if (!button) return;
        addWatchlistInstrumentCandidate({
          instrument_id: button.dataset.watchlistSearchInstrumentId,
          instrument_key: button.dataset.watchlistSearchKey,
          provider: button.dataset.watchlistSearchProvider,
          provider_contract_id: button.dataset.watchlistSearchContract,
          asset_class: button.dataset.watchlistSearchAssetClass,
          identity_scope: button.dataset.watchlistSearchIdentityScope,
          root: button.dataset.watchlistSearchRoot,
          exchange: button.dataset.watchlistSearchExchange,
          currency: button.dataset.watchlistSearchCurrency,
          trading_class: button.dataset.watchlistSearchTradingClass,
        });
      });
      setWatchlistEditMode(state.watchlistEditMode, { render: false });
    }

    async function loadProviders() {
      try {
        debugStep("providers request");
        state.providers = await fetchJson("/api/data-providers");
        debugStep("providers response", `${state.providers.length} rows`);
      } catch (error) {
        console.warn("provider catalog load failed", error);
        debugStep("providers error", requestErrorMessage(error, "providers load failed"));
      }
      renderWatchlistProviderOptions();
      applySettings();
    }

    const SYSTEM_HEALTH_MEMORY_TTL_MS = 5000;
    let systemHealthMemoryAt = 0;
    let systemHealthMemoryPromise = null;
    let systemHealthMemoryDetails = false;
    let systemHealthLoadSequence = 0;
    let systemStorageDiagnostics = null;
    let systemStorageDiagnosticsCheckedAt = "";

    async function fetchSystemHealth(options = {}) {
      const now = Date.now();
      const details = options.details === true;
      if (!options.force && systemHealthMemoryPromise && (!details || systemHealthMemoryDetails)) return systemHealthMemoryPromise;
      if (!options.force && state.systemHealth && (!details || systemHealthMemoryDetails) && now - Number(systemHealthMemoryAt || 0) <= SYSTEM_HEALTH_MEMORY_TTL_MS) {
        return state.systemHealth;
      }
      systemHealthMemoryDetails = details;
      const requestPromise = fetchJson(`/api/system?details=${details ? "true" : "false"}`, { cache: "no-store", timeoutMs: 3600, warnMs: 3200 })
        .then(payload => {
          if (systemHealthMemoryPromise === requestPromise) {
            systemHealthMemoryAt = Date.now();
            systemHealthMemoryPromise = null;
          }
          return payload;
        })
        .catch(error => {
          if (systemHealthMemoryPromise === requestPromise) systemHealthMemoryPromise = null;
          throw error;
        });
      systemHealthMemoryPromise = requestPromise;
      return requestPromise;
    }

    async function loadSystemHealth(options = {}) {
      const loadSequence = ++systemHealthLoadSequence;
      let payload;
      let receivedPayload = false;
      try {
        payload = await fetchSystemHealth(options);
        receivedPayload = true;
      } catch (error) {
        payload = {
          status: "degraded",
          checked_at: new Date().toISOString(),
          app: {},
          storage: { ok: false, message: requestErrorMessage(error, "system health unavailable") },
        };
      }
      if (loadSequence !== systemHealthLoadSequence) return false;
      if (
        options.details === true
        && receivedPayload
        && payload?.storage
        && typeof payload.storage === "object"
      ) {
        systemStorageDiagnostics = { ...payload.storage };
        systemStorageDiagnosticsCheckedAt = String(payload.checked_at || new Date().toISOString());
      }
      state.systemHealth = payload;
      state.serverSleeping = Boolean(state.systemHealth?.sleep?.sleeping);
      if (typeof mtfLensHandleServerSleepState === "function") mtfLensHandleServerSleepState();
      const scheduler = state.systemHealth?.gex?.scheduler || {};
      if (typeof scheduler.enabled !== "undefined") state.settings.gexSchedulerEnabled = Boolean(scheduler.enabled);
      if (
        Array.isArray(scheduler.instrument_ids)
        && scheduler.instrument_ids.every(instrumentId => typeof instrumentId === "string" && instrumentId)
      ) {
        state.settings.gexSchedulerInstrumentIds = [...scheduler.instrument_ids];
        renderGexSchedulerInstrumentOptions();
      }
      const optionCaps = state.systemHealth?.options?.premium_caps;
      if (optionCaps && typeof optionCaps === "object") {
        state.settings.optionCaps = { ...optionCaps };
      }
      if (state.serverSleeping) {
        closeQuoteStream();
        closeChartStream();
      }
      renderSystemHealth();
      applySettings();
      return true;
    }
