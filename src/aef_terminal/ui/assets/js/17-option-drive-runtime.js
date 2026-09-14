    const OPTION_DRIVE_ARM_TIMEOUT_MS = 60_000;
    const OPTION_DRIVE_LADDER_RADIUS = 8;

    function currentOptionDriveState() {
      state.optionDrive = state.optionDrive || {
        open: false,
        instrumentId: "",
        selectionInitialized: false,
        routeKey: "",
        expiryMode: "0dte",
        selected: null,
        locked: false,
        frozenRows: [],
        ladderPrices: [],
        armed: false,
        armedUntilMs: 0,
        armTimer: null,
        submitting: false,
        message: "",
        messageTone: "",
        paperStats: null,
        paperStatsKey: "",
        paperStatsPromise: null,
      };
      return state.optionDrive;
    }

    function optionDriveAvailableTradeInstruments() {
      return (state.instruments || []).filter(instrument => {
        const instrumentId = exactIdentityText(instrument?.instrument_id);
        const routeFingerprint = instrumentRouteFingerprint(instrument);
        const provider = String(instrument?.provider || "").trim().toLowerCase();
        return Boolean(
          instrumentId
          && routeFingerprint
          && provider
          && providerCapability(provider, "options")
        );
      });
    }

    function optionDriveEnsureTradeInstrument() {
      const drive = currentOptionDriveState();
      if (drive.selectionInitialized) return;
      const available = optionDriveAvailableTradeInstruments();
      if (!available.length) return;
      const chartInstrumentId = exactIdentityText(state.instrumentId);
      const selected = available.find(instrument => (
        exactIdentityText(instrument?.instrument_id) === chartInstrumentId
      )) || available[0];
      drive.instrumentId = exactIdentityText(selected?.instrument_id);
      drive.selectionInitialized = Boolean(drive.instrumentId);
    }

    function optionDriveTradeContext() {
      const drive = currentOptionDriveState();
      optionDriveEnsureTradeInstrument();
      const instrument = instrumentForId(drive.instrumentId);
      const instrumentId = exactIdentityText(instrument?.instrument_id);
      const routeFingerprint = instrumentRouteFingerprint(instrument);
      const provider = String(instrument?.provider || "").trim().toLowerCase();
      return (
        instrumentId
        && routeFingerprint
        && provider
        && providerCapability(provider, "options")
      ) ? { instrument, instrumentId, routeFingerprint, provider } : null;
    }

    function optionDriveInstrumentLabel(instrument) {
      const display = String(
        instrument?.display
        || instrument?.instrument_key
        || instrument?.key
        || instrument?.provider_symbol
        || "Qualified instrument"
      ).trim();
      const provider = String(instrument?.provider || "").trim().toUpperCase();
      return `${display}${provider ? ` · ${provider}` : ""}`;
    }

    function optionDriveRenderInstrumentSelector() {
      optionDriveEnsureTradeInstrument();
      const drive = currentOptionDriveState();
      const select = document.getElementById("option-drive-instrument");
      const chartBadge = document.getElementById("option-drive-chart-instrument");
      const available = optionDriveAvailableTradeInstruments();
      const selectedAvailable = available.some(instrument => (
        exactIdentityText(instrument?.instrument_id) === exactIdentityText(drive.instrumentId)
      ));
      if (select) {
        const unavailable = drive.selectionInitialized && !selectedAvailable
          ? `<option value="${escapeHtml(drive.instrumentId)}">UNAVAILABLE · exact route removed</option>`
          : "";
        select.innerHTML = `${unavailable}${available.map(instrument => {
          const instrumentId = exactIdentityText(instrument?.instrument_id);
          const selected = instrumentId === exactIdentityText(drive.instrumentId) ? " selected" : "";
          return `<option value="${escapeHtml(instrumentId)}"${selected}>${escapeHtml(optionDriveInstrumentLabel(instrument))}</option>`;
        }).join("")}` || '<option value="">No option-capable watchlist routes</option>';
        select.disabled = !available.length || drive.submitting;
      }
      if (chartBadge) {
        const chartInstrument = instrumentForId(state.instrumentId);
        const chartLabel = String(
          chartInstrument?.display
          || chartInstrument?.instrument_key
          || chartInstrument?.key
          || state.symbol
          || "—"
        ).trim();
        chartBadge.textContent = `CHART ${chartLabel}`;
        chartBadge.title = `Price chart: ${optionDriveInstrumentLabel(chartInstrument)}`;
      }
    }

    function optionDriveRound(value) {
      return Math.round((Number(value) + Number.EPSILON) * 100_000_000) / 100_000_000;
    }

    function optionDrivePriceRules(value) {
      if (!Array.isArray(value) || !value.length) return [];
      const rules = [];
      for (const item of value) {
        const lowEdge = Number(item?.low_edge);
        const increment = Number(item?.increment);
        if (
          !Number.isFinite(lowEdge)
          || lowEdge < 0
          || !Number.isFinite(increment)
          || increment <= 0
          || (rules.length && lowEdge <= rules.at(-1).lowEdge)
        ) return [];
        rules.push({ lowEdge, increment });
      }
      return rules[0].lowEdge === 0 ? rules : [];
    }

    function optionDriveRuleIndex(value, rules) {
      const premium = Number(value);
      if (!Number.isFinite(premium) || premium <= 0 || !rules.length) return -1;
      let active = 0;
      for (let index = 1; index < rules.length; index += 1) {
        if (premium < rules[index].lowEdge) break;
        active = index;
      }
      return active;
    }

    function optionDrivePriceIncrement(value, priceIncrements) {
      const rules = optionDrivePriceRules(priceIncrements);
      const index = optionDriveRuleIndex(value, rules);
      return index >= 0 ? rules[index].increment : null;
    }

    function optionDriveAlignedPrice(value, priceIncrements) {
      const premium = Number(value);
      const rules = optionDrivePriceRules(priceIncrements);
      const index = optionDriveRuleIndex(premium, rules);
      if (index < 0) return null;
      const rule = rules[index];
      let aligned = optionDriveRound(
        rule.lowEdge
        + Math.round((premium - rule.lowEdge) / rule.increment) * rule.increment
      );
      const nextLowEdge = rules[index + 1]?.lowEdge;
      if (Number.isFinite(nextLowEdge) && aligned >= nextLowEdge) aligned = nextLowEdge;
      if (aligned < rule.lowEdge) aligned = rule.lowEdge;
      if (aligned <= 0) aligned = rule.increment;
      return optionDriveRound(aligned);
    }

    function optionDriveNextTick(value, direction, priceIncrements) {
      const premium = Number(value);
      const rules = optionDrivePriceRules(priceIncrements);
      const index = optionDriveRuleIndex(premium, rules);
      if (!Number.isFinite(premium) || premium <= 0 || ![-1, 1].includes(direction)) {
        return null;
      }
      if (index < 0) return null;
      const rule = rules[index];
      if (direction > 0) {
        const candidate = optionDriveRound(premium + rule.increment);
        const nextLowEdge = rules[index + 1]?.lowEdge;
        return Number.isFinite(nextLowEdge) && candidate >= nextLowEdge
          ? nextLowEdge
          : candidate;
      }
      if (premium > rule.lowEdge) {
        const candidate = optionDriveRound(
          Math.max(rule.lowEdge, premium - rule.increment)
        );
        if (candidate > 0) return candidate;
      }
      if (index === 0) return null;
      const previous = rules[index - 1];
      const units = Math.ceil(
        (rule.lowEdge - previous.lowEdge) / previous.increment
      ) - 1;
      const candidate = optionDriveRound(
        previous.lowEdge + Math.max(0, units) * previous.increment
      );
      return candidate > 0 ? candidate : null;
    }

    function optionDriveLadderPrices(reference, priceIncrements) {
      const center = optionDriveAlignedPrice(reference, priceIncrements);
      if (!center) return [];
      const lower = [];
      let cursor = center;
      for (let index = 0; index < OPTION_DRIVE_LADDER_RADIUS; index += 1) {
        cursor = optionDriveNextTick(cursor, -1, priceIncrements);
        if (!cursor || lower.includes(cursor)) break;
        lower.push(cursor);
      }
      const upper = [];
      cursor = center;
      for (let index = 0; index < OPTION_DRIVE_LADDER_RADIUS; index += 1) {
        cursor = optionDriveNextTick(cursor, 1, priceIncrements);
        if (!cursor || upper.includes(cursor)) break;
        upper.push(cursor);
      }
      return [...upper.reverse(), center, ...lower];
    }

    function optionDriveExactSeries(snapshot) {
      const drive = currentOptionDriveState();
      return (Array.isArray(snapshot?.series) ? snapshot.series : []).find(series => (
        String(series?.target_dte || "") === drive.expiryMode
        && Array.isArray(series?.rows)
      )) || null;
    }

    function optionDriveVisibleRows(snapshot) {
      const series = optionDriveExactSeries(snapshot);
      const rows = Array.isArray(series?.rows) ? series.rows : [];
      if (rows.length <= 13) return rows;
      const spot = Number(snapshot?.spot);
      let atmIndex = 0;
      let atmDistance = Number.POSITIVE_INFINITY;
      rows.forEach((row, index) => {
        const strike = Number(row?.strike);
        const distance = Number.isFinite(strike) && Number.isFinite(spot)
          ? Math.abs(strike - spot)
          : Number.POSITIVE_INFINITY;
        if (distance < atmDistance) {
          atmDistance = distance;
          atmIndex = index;
        }
      });
      const start = Math.max(0, Math.min(rows.length - 13, atmIndex - 6));
      return rows.slice(start, start + 13);
    }

    function optionDriveQuoteByConId(snapshot, conId) {
      const exactConId = Number(conId);
      if (!Number.isSafeInteger(exactConId) || exactConId <= 0) return null;
      for (const series of Array.isArray(snapshot?.series) ? snapshot.series : []) {
        for (const row of Array.isArray(series?.rows) ? series.rows : []) {
          for (const quote of [row?.call, row?.put]) {
            if (Number(quote?.con_id) === exactConId) return quote;
          }
        }
      }
      return null;
    }

    function optionDriveLockedRows(snapshot) {
      const drive = currentOptionDriveState();
      return drive.frozenRows.map(row => ({
        strike: row?.strike,
        call: row?.call
          ? { ...row.call, ...(optionDriveQuoteByConId(snapshot, row.call.con_id) || {}) }
          : null,
        put: row?.put
          ? { ...row.put, ...(optionDriveQuoteByConId(snapshot, row.put.con_id) || {}) }
          : null,
      }));
    }

    function optionDriveLiveBbo(quote, nowMs = Date.now()) {
      const bid = Number(quote?.bid);
      const ask = Number(quote?.ask);
      const observedAt = Date.parse(quote?.bid_ask_received_at || "");
      const live = (
        Number.isFinite(bid)
        && bid >= 0
        && Number.isFinite(ask)
        && ask > 0
        && ask >= bid
        && quote?.market_data_entitlement === "live"
        && quote?.is_delayed === false
        && Number.isFinite(observedAt)
        && nowMs - observedAt >= -1_000
        && nowMs - observedAt <= 10_000
      );
      return live ? { bid, ask, mid: (bid + ask) / 2, observedAt } : null;
    }

    function optionDriveQuotePresentationState(quote, nowMs = Date.now()) {
      if (optionDriveLiveBbo(quote, nowMs)) return "live";
      if (!quote || typeof quote !== "object") return "unavailable";
      if (quote.is_delayed === true || quote.market_data_entitlement === "delayed") return "delayed";
      const observedAt = Date.parse(quote.bid_ask_received_at || "");
      if (Number.isFinite(observedAt) && nowMs - observedAt > 10_000) return "stale";
      return "unavailable";
    }

    function optionDriveRouteReady(board = currentOptionBoardState()) {
      const context = optionDriveTradeContext();
      return Boolean(context && board.snapshot && optionBoardMessageMatchesRoute(board.snapshot));
    }

    function renderOptionDriveExecutionEligibility(nowMs = Date.now()) {
      const drive = currentOptionDriveState();
      const board = currentOptionBoardState();
      const context = optionDriveTradeContext();
      const routeReady = optionDriveRouteReady(board);
      const quote = optionDriveQuoteByConId(board.snapshot, drive.selected?.conId);
      const bbo = optionDriveLiveBbo(quote, nowMs);
      const quoteState = optionDriveQuotePresentationState(quote, nowMs);
      const contractReady = Boolean(drive.selected && drive.locked);
      const chartOwnsSession = Boolean(
        context
        && context.instrumentId === exactIdentityText(state.instrumentId)
        && context.routeFingerprint === instrumentRouteFingerprint()
        && snapshotMatchesCurrentRoute(state.snapshot)
      );
      const session = chartOwnsSession
        ? snapshotSessionPresentation(state.snapshot)
        : { state: "unknown", title: "Current chart session facts do not own the selected Option Drive route." };
      const actionState = drive.submitting
        ? "submitting"
        : !routeReady || !contractReady || !bbo
          ? "blocked"
          : drive.armed
            ? "armed"
            : "ready";
      const actionTitle = drive.submitting
        ? "The selected option paper order request is in progress."
        : !routeReady
          ? "Order entry is blocked until the option snapshot matches the exact Option Drive route."
          : !contractReady
            ? "Select an exact contract and lock its strike table before arming."
            : !bbo
              ? "Order entry is blocked without a fresh live Bid/Ask for the selected exact contract."
              : drive.armed
                ? "One paper limit-order ladder click is armed."
                : "The exact contract can be armed for one paper limit-order ladder click.";
      const node = document.getElementById("option-drive-execution-eligibility");
      if (node) node.dataset.liveBbo = bbo ? "true" : "false";
      renderExecutionEligibility(
        node,
        actionState,
        [
          { label: "ROUTE", state: routeReady ? "ready" : "blocked", title: actionTitle },
          {
            label: "CONTRACT",
            state: contractReady ? "ready" : drive.selected ? "waiting" : "unavailable",
            title: contractReady ? "The selected exact option contract is locked." : "The exact option contract is not locked.",
          },
          { label: "BBO", state: quoteState, title: "Option entry requires a fresh live Bid/Ask for the selected exact contract." },
          { label: "SESSION", state: session.state, title: session.title },
        ],
        { title: actionTitle },
      );
      return { actionState, bbo };
    }

    function refreshOptionDriveFreshness(nowMs = Date.now()) {
      const drive = currentOptionDriveState();
      if (!drive.open) return false;
      const board = currentOptionBoardState();
      const quote = optionDriveQuoteByConId(board.snapshot, drive.selected?.conId);
      const hasLiveBbo = Boolean(optionDriveLiveBbo(quote, nowMs));
      if (drive.armed && !hasLiveBbo) {
        optionDriveDisarm("Fresh live BBO was lost. Re-arm when the quote returns.");
        renderOptionDrive();
        return true;
      }
      if (drive.armed) {
        renderOptionDrive();
        return true;
      }
      const node = document.getElementById("option-drive-execution-eligibility");
      if (node && node.dataset.liveBbo !== (hasLiveBbo ? "true" : "false")) {
        renderOptionDrive();
        return true;
      }
      renderOptionDriveExecutionEligibility(nowMs);
      return false;
    }

    function optionDriveContractLabel(contract) {
      if (!contract) return "Select and lock an exact contract";
      const right = String(contract.right || "") === "C" ? "CALL" : "PUT";
      return [
        contract.local_symbol || contract.provider_symbol,
        contract.expiry,
        `${optionBoardNumber(contract.strike)} ${right}`,
        `conId ${contract.con_id}`,
      ].filter(Boolean).join(" · ");
    }

    function optionDriveContractButton(quote, side, selectedConId) {
      if (!quote || typeof quote !== "object") return "<button type=\"button\" disabled>—</button>";
      const conId = Number(quote.con_id);
      const selected = conId === Number(selectedConId);
      const bid = optionBoardNumber(quote.bid);
      const ask = optionBoardNumber(quote.ask);
      const title = optionBoardQuoteTitle(quote, `${side} Bid / Ask`);
      return `<button type="button" class="${selected ? "selected" : ""}" data-option-drive-con-id="${conId}" title="${escapeHtml(title)}"><b>${escapeHtml(side)}</b> ${bid}/${ask}</button>`;
    }

    function optionDriveContractSize(value) {
      const size = Number(value);
      return Number.isFinite(size) && size >= 0 ? optionBoardNumber(size, 0) : "—";
    }

    function optionDriveDisarm(message = "") {
      const drive = currentOptionDriveState();
      if (drive.armTimer) window.clearTimeout(drive.armTimer);
      drive.armTimer = null;
      drive.armed = false;
      drive.armedUntilMs = 0;
      if (message) {
        drive.message = message;
        drive.messageTone = "";
      }
    }

    function optionDriveSetMessage(message, tone = "") {
      const drive = currentOptionDriveState();
      drive.message = String(message || "");
      drive.messageTone = tone;
    }

    function optionDriveResetSelection(message = "") {
      const drive = currentOptionDriveState();
      optionDriveDisarm();
      drive.selected = null;
      drive.locked = false;
      drive.frozenRows = [];
      drive.ladderPrices = [];
      if (message) optionDriveSetMessage(message);
    }

    function optionDriveSetStreamUnavailable(message) {
      const board = currentOptionBoardState();
      clearOptionBoardRetry();
      clearOptionBoardStaleTimer();
      board.lifecycleGeneration += 1;
      const socket = board.socket;
      board.socket = null;
      if (socket && socket.readyState <= WebSocket.OPEN) {
        try { socket.close(4001, "Option Drive trade route unavailable"); } catch (_) { /* noop */ }
      }
      board.open = true;
      board.surface = "drive";
      board.routeKey = "";
      board.status = "unsupported";
      board.message = message;
      board.snapshot = null;
      board.revision = 0;
      renderOptionBoard();
    }

    function optionDriveChangeTradeInstrument(instrumentId) {
      const drive = currentOptionDriveState();
      const identity = exactIdentityText(instrumentId);
      const selected = optionDriveAvailableTradeInstruments().find(instrument => (
        exactIdentityText(instrument?.instrument_id) === identity
      ));
      if (!selected) {
        optionDriveSetMessage("The selected trade instrument is not an exact option-capable watchlist route.", "error");
        renderOptionDrive();
        return false;
      }
      if (identity === exactIdentityText(drive.instrumentId)) return true;
      drive.instrumentId = identity;
      drive.selectionInitialized = true;
      drive.routeKey = "";
      drive.paperStats = null;
      drive.paperStatsKey = "";
      optionDriveResetSelection(`Loading ${optionDriveInstrumentLabel(selected)} exact contracts…`);
      if (drive.open) optionDriveStartStream();
      void loadOptionDrivePaperStats({ force: true });
      return true;
    }

    function reconcileOptionDriveInstrumentCatalog() {
      const drive = currentOptionDriveState();
      optionDriveEnsureTradeInstrument();
      const context = optionDriveTradeContext();
      if (!context) {
        optionDriveResetSelection("Select an exact option-capable trade instrument from the watchlist.");
        if (drive.open) {
          optionDriveSetStreamUnavailable(
            "The selected trade instrument is unavailable. The chart selection was not substituted.",
          );
        } else {
          renderOptionDrive();
        }
        return;
      }
      const routeKey = JSON.stringify([context.instrumentId, context.routeFingerprint]);
      if (drive.open && drive.routeKey !== routeKey) {
        optionDriveResetSelection("Trade instrument route changed. Loading exact contracts…");
        optionDriveStartStream();
        void loadOptionDrivePaperStats({ force: true });
        return;
      }
      renderOptionDrive();
    }

    function optionDriveStartStream() {
      const drive = currentOptionDriveState();
      const board = currentOptionBoardState();
      const context = optionDriveTradeContext();
      if (!context) {
        optionDriveSetMessage("Select an exact option-capable trade instrument from the watchlist.", "error");
        optionDriveSetStreamUnavailable(
          "The selected trade instrument has no current exact option route.",
        );
        return;
      }
      clearOptionBoardRetry();
      clearOptionBoardStaleTimer();
      board.lifecycleGeneration += 1;
      const socket = board.socket;
      board.socket = null;
      if (socket && socket.readyState <= WebSocket.OPEN) {
        try { socket.close(4001, "Option Drive mode changed"); } catch (_) { /* noop */ }
      }
      board.open = true;
      board.surface = "drive";
      board.expiryMode = drive.expiryMode;
      board.routeKey = optionBoardRouteKey();
      board.status = "loading";
      board.message = "";
      board.snapshot = null;
      board.revision = 0;
      drive.routeKey = JSON.stringify([
        context.instrumentId,
        context.routeFingerprint,
      ]);
      renderOptionBoard();
      connectOptionBoard();
    }

    function activateOptionDrive() {
      const drive = currentOptionDriveState();
      drive.open = true;
      const context = optionDriveTradeContext();
      if (!context) {
        optionDriveSetMessage("Select an exact option-capable trade instrument from the watchlist.", "error");
        renderOptionDrive();
        return;
      }
      optionDriveSetMessage("Loading exact contracts…");
      optionDriveStartStream();
      void loadOptionDrivePaperStats({ force: true });
    }

    function deactivateOptionDrive(options = {}) {
      const drive = currentOptionDriveState();
      drive.open = false;
      optionDriveDisarm("Option Drive disarmed.");
      if (currentOptionBoardState().surface === "drive") {
        closeOptionBoard({ reason: options.reason || "Option Drive collapsed" });
      }
      renderOptionDrive();
    }

    function openOptionDrive(options = {}) {
      openWorkspaceDockSurface("option-drive", options);
    }

    function collapseOptionDrive(options = {}) {
      closeWorkspaceDockSurface("option-drive", options);
    }

    function onOptionBoardSurfaceClosed(surface) {
      if (surface !== "drive") return;
      const drive = currentOptionDriveState();
      drive.open = false;
      optionDriveDisarm();
      if (workspaceDockSurfaceIsActive("option-drive")) {
        closeWorkspaceDockSurface("option-drive", { reason: "Option Drive stream closed" });
      }
      renderOptionDrive();
    }

    function optionDriveSelectContract(conId) {
      const drive = currentOptionDriveState();
      const board = currentOptionBoardState();
      if (drive.locked || !board.snapshot) return;
      const quote = optionDriveQuoteByConId(board.snapshot, conId);
      if (!quote) {
        optionDriveResetSelection("The selected exact contract is no longer current.");
        renderOptionDrive();
        return;
      }
      optionDriveDisarm();
      drive.selected = {
        conId: Number(quote.con_id),
        contract: { ...quote, provider_symbol: board.snapshot.provider_symbol },
      };
      drive.ladderPrices = [];
      optionDriveSetMessage("Contract selected. Lock the strike table to enable the ladder.");
      renderOptionDrive();
    }

    function optionDriveToggleLock() {
      const drive = currentOptionDriveState();
      const board = currentOptionBoardState();
      if (drive.locked) {
        optionDriveDisarm();
        drive.locked = false;
        drive.frozenRows = [];
        drive.ladderPrices = [];
        optionDriveSetMessage("Strike table unlocked. Select or relock the exact contract.");
        renderOptionDrive();
        return;
      }
      if (!drive.selected || !board.snapshot) return;
      const quote = optionDriveQuoteByConId(board.snapshot, drive.selected.conId);
      const bbo = optionDriveLiveBbo(quote);
      if (!quote || !bbo) {
        optionDriveSetMessage("Fresh live Bid/Ask is required before locking.", "error");
        renderOptionDrive();
        return;
      }
      const ladderPrices = optionDriveLadderPrices(
        bbo.mid,
        quote.price_increments,
      );
      if (!ladderPrices.length) {
        optionDriveSetMessage(
          "The provider price-increment rule is unavailable. Limit entry is blocked.",
          "error",
        );
        renderOptionDrive();
        return;
      }
      drive.locked = true;
      drive.frozenRows = optionDriveVisibleRows(board.snapshot).map(row => ({
        strike: row?.strike,
        call: row?.call ? { ...row.call } : null,
        put: row?.put ? { ...row.put } : null,
      }));
      drive.ladderPrices = ladderPrices;
      optionDriveSetMessage(
        "STRIKE LOCKED · premium ladder follows live BBO. Arm for 60 seconds to enable one LMT click.",
        "ok",
      );
      renderOptionDrive();
    }

    function optionDriveToggleArm() {
      const drive = currentOptionDriveState();
      const board = currentOptionBoardState();
      if (drive.armed) {
        optionDriveDisarm("Option Drive disarmed.");
        renderOptionDrive();
        return;
      }
      const quote = optionDriveQuoteByConId(board.snapshot, drive.selected?.conId);
      if (!drive.locked || !optionDriveLiveBbo(quote) || drive.submitting) return;
      drive.armed = true;
      drive.armedUntilMs = Date.now() + OPTION_DRIVE_ARM_TIMEOUT_MS;
      drive.message = "ARMED for one paper LMT click · live-follow enabled · auto-disarm in 60s.";
      drive.messageTone = "error";
      drive.armTimer = window.setTimeout(() => {
        optionDriveDisarm("ARM timeout. Re-arm to trade.");
        renderOptionDrive();
      }, OPTION_DRIVE_ARM_TIMEOUT_MS);
      renderOptionDrive();
    }

    async function submitOptionDriveOrder(side, entry) {
      const drive = currentOptionDriveState();
      const board = currentOptionBoardState();
      const context = optionDriveTradeContext();
      const quote = optionDriveQuoteByConId(board.snapshot, drive.selected?.conId);
      const bbo = optionDriveLiveBbo(quote);
      const price = Number(entry);
      const qty = Math.trunc(Number(document.getElementById("option-drive-qty")?.value) || 1);
      if (
        !drive.armed
        || !drive.locked
        || drive.submitting
        || !context
        || !optionDriveRouteReady(board)
        || !bbo
        || !["long", "short"].includes(side)
        || !Number.isFinite(price)
        || qty < 1
        || qty > 100
      ) return false;
      const request = {
        id: `po-${createBrowserUuidV4()}`,
        instrument_id: context.instrumentId,
        route_fingerprint: context.routeFingerprint,
        timeframe: String(state.timeframe || ""),
        expiry_mode: drive.expiryMode,
        snapshot_revision: board.revision,
        con_id: drive.selected.conId,
        side,
        order_type: "limit",
        qty,
        entry: price,
      };
      optionDriveDisarm();
      drive.submitting = true;
      optionDriveSetMessage(`Sending PAPER ${side === "long" ? "BUY" : "SELL"} LMT ${optionBoardNumber(price)}…`);
      renderOptionDrive();
      try {
        const response = await fetchJson("/api/paper/options/orders", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(request),
          cache: "no-store",
        });
        if (!response?.ok) throw new Error(apiErrorMessage(response, "option paper order failed"));
        const responseOrder = response?.order;
        const responseContract = response?.paper_contract || responseOrder?.paper_contract;
        if (
          exactIdentityText(response?.instrument_id) !== request.instrument_id
          || exactIdentityText(response?.route_fingerprint) !== request.route_fingerprint
          || exactIdentityText(responseOrder?.id) !== request.id
          || Number(responseContract?.con_id) !== request.con_id
          || String(responseContract?.scope_kind || "") !== "option"
        ) throw new Error("option paper order exact identity mismatch");
        const existing = Array.isArray(drive.paperStats?.orders) ? drive.paperStats.orders : [];
        const orders = String(responseOrder.status || "pending") === "pending"
          ? [responseOrder, ...existing.filter(order => exactIdentityText(order.id) !== request.id)].slice(0, 100)
          : existing.filter(order => exactIdentityText(order.id) !== request.id);
        drive.paperStats = { ...(drive.paperStats || {}), orders };
        rawLocalStorageSetItem("aef:paperJournalUpdatedAt", String(Date.now()));
        const outcome = response.filled
          ? `FILLED ${side === "long" ? "BUY" : "SELL"} @ ${optionBoardNumber(responseOrder.fill_price)}`
          : `WORKING ${side === "long" ? "BUY" : "SELL"} LMT @ ${optionBoardNumber(price)}`;
        optionDriveSetMessage(outcome, response.filled ? "ok" : "");
        showBrowserToast(`Option Drive · ${outcome}`, { tone: response.filled ? "success" : "neutral" });
        await loadOptionDrivePaperStats({ force: true });
        return true;
      } catch (error) {
        optionDriveSetMessage(
          `Order rejected: ${requestErrorMessage(error, "option paper order failed")}`,
          "error",
        );
        return false;
      } finally {
        drive.submitting = false;
        renderOptionDrive();
      }
    }

    function optionDrivePaperContract(entity) {
      const contract = entity?.paper_contract || entity?.payload?.paper_contract;
      return contract?.scope_kind === "option" ? contract : null;
    }

    async function loadOptionDrivePaperStats(options = {}) {
      const drive = currentOptionDriveState();
      if (!drive.open && !options.force) return drive.paperStats;
      const context = optionDriveTradeContext();
      if (!context) return drive.paperStats;
      const requestKey = JSON.stringify([context.instrumentId, context.routeFingerprint]);
      if (drive.paperStatsPromise && drive.paperStatsKey === requestKey) {
        return drive.paperStatsPromise;
      }
      if (drive.paperStatsKey !== requestKey) drive.paperStats = null;
      const request = (async () => {
        const params = new URLSearchParams({
          limit: "500",
          instrument_id: context.instrumentId,
          route_fingerprint: context.routeFingerprint,
          paper_scope_kind: "option",
        });
        try {
          const payload = await fetchJson(`/api/paper/trades?${params.toString()}`, {
            cache: "no-store",
            sharedTtlMs: options.force ? 0 : 1500,
          });
          const current = optionDriveTradeContext();
          if (
            !current
            || current.instrumentId !== context.instrumentId
            || current.routeFingerprint !== context.routeFingerprint
          ) return drive.paperStats;
          if (
            payload?.ok
            && exactIdentityText(payload?.instrument_id) === context.instrumentId
            && exactIdentityText(payload?.route_fingerprint) === context.routeFingerprint
            && payload?.paper_scope_kind === "option"
          ) {
            drive.paperStats = payload;
          } else {
            drive.paperStats = { ok: false, trades: [], orders: [], fills: [] };
          }
        } catch (error) {
          const current = optionDriveTradeContext();
          if (
            current
            && current.instrumentId === context.instrumentId
            && current.routeFingerprint === context.routeFingerprint
          ) {
            drive.paperStats = {
              ok: false,
              message: requestErrorMessage(error, "option paper journal load failed"),
              trades: [],
              orders: [],
              fills: [],
            };
          }
        } finally {
          if (drive.paperStatsPromise === request) drive.paperStatsPromise = null;
          renderOptionDriveJournal();
        }
        return drive.paperStats;
      })();
      drive.paperStatsKey = requestKey;
      drive.paperStatsPromise = request;
      return request;
    }

    function optionDrivePaperStatsMatchesScope(scope) {
      const drive = currentOptionDriveState();
      const instrumentId = exactIdentityText(scope?.instrument_id);
      const routeFingerprint = exactIdentityText(scope?.route_fingerprint);
      const context = optionDriveTradeContext();
      return Boolean(
        instrumentId
        && routeFingerprint
        && drive.paperStats?.paper_scope_kind === "option"
        && (
          (
            context
            && context.instrumentId === instrumentId
            && context.routeFingerprint === routeFingerprint
          )
          || (
            exactIdentityText(drive.paperStats?.instrument_id) === instrumentId
            && exactIdentityText(drive.paperStats?.route_fingerprint) === routeFingerprint
          )
        )
      );
    }

    function optionDriveRemoveJournalOrder(orderId, scope) {
      if (!optionDrivePaperStatsMatchesScope(scope)) return;
      const drive = currentOptionDriveState();
      const orders = Array.isArray(drive.paperStats?.orders) ? drive.paperStats.orders : [];
      drive.paperStats = {
        ...(drive.paperStats || {}),
        orders: orders.filter(order => exactIdentityText(order?.id) !== exactIdentityText(orderId)),
      };
      renderOptionDriveJournal();
    }

    function optionDriveApplyCloseMutation(response, tradeId, scope) {
      if (!optionDrivePaperStatsMatchesScope(scope)) return;
      const drive = currentOptionDriveState();
      const orders = Array.isArray(drive.paperStats?.orders) ? drive.paperStats.orders : [];
      const trades = Array.isArray(drive.paperStats?.trades) ? drive.paperStats.trades : [];
      if (response?.outcome === "pending" && response?.order) {
        const orderId = exactIdentityText(response.order.id);
        drive.paperStats = {
          ...(drive.paperStats || {}),
          orders: [
            response.order,
            ...orders.filter(order => exactIdentityText(order?.id) !== orderId),
          ],
        };
      } else if (["filled", "no_op"].includes(response?.outcome) && response?.trade) {
        const exactTradeId = exactIdentityText(tradeId);
        drive.paperStats = {
          ...(drive.paperStats || {}),
          orders: orders.filter(order => exactIdentityText(order?.position_id) !== exactTradeId),
          trades: trades.map(trade => (
            exactIdentityText(trade?.id) === exactTradeId ? response.trade : trade
          )),
        };
      }
      renderOptionDriveJournal();
    }

    function optionDriveJournalRows(field, timestampField) {
      const drive = currentOptionDriveState();
      const context = optionDriveTradeContext();
      const rows = Array.isArray(drive.paperStats?.[field]) ? drive.paperStats[field] : [];
      const instrumentId = context?.instrumentId
        || exactIdentityText(drive.paperStats?.instrument_id);
      const routeFingerprint = context?.routeFingerprint
        || exactIdentityText(drive.paperStats?.route_fingerprint);
      return rows.filter(entity => (
        instrumentId
        && routeFingerprint
        && exactIdentityText(entity?.instrument_id) === instrumentId
        && exactIdentityText(entity?.route_fingerprint) === routeFingerprint
        && optionDrivePaperContract(entity)
        && (!timestampField || paperJournalIsRecent(entity?.[timestampField]))
      ));
    }

    function optionDriveJournalOrders() {
      return optionDriveJournalRows("orders", "created_at").sort((left, right) => (
        (paperJournalTimestampMs(right?.updated_at || right?.created_at) || 0)
        - (paperJournalTimestampMs(left?.updated_at || left?.created_at) || 0)
      ));
    }

    function optionDriveJournalTrades() {
      return optionDriveJournalRows("trades", "");
    }

    function optionDriveJournalFills() {
      return optionDriveJournalRows("fills", "filled_at").sort((left, right) => (
        (paperJournalTimestampMs(right?.filled_at) || 0)
        - (paperJournalTimestampMs(left?.filled_at) || 0)
      ));
    }

    function optionDriveJournalContractLabel(entity) {
      const contract = optionDrivePaperContract(entity);
      if (!contract) return String(entity?.symbol || "OPTION");
      return `${contract.expiry} ${optionBoardNumber(contract.strike)}${contract.right}`;
    }

    function renderOptionDriveJournal() {
      const workingNode = document.getElementById("option-drive-working");
      const positionsNode = document.getElementById("option-drive-positions");
      const historyNode = document.getElementById("option-drive-history");
      if (!workingNode || !positionsNode || !historyNode) return;
      const working = optionDriveJournalOrders().filter(order => (
        String(order.status || "pending") === "pending"
      )).slice(0, 20);
      const positions = optionDriveJournalTrades().filter(trade => (
        String(trade.status || "").toLowerCase() === "open"
      )).slice(0, 20);
      const history = optionDriveJournalFills().slice(0, 30);
      document.getElementById("option-drive-working-count").textContent = String(working.length);
      document.getElementById("option-drive-positions-count").textContent = String(positions.length);
      document.getElementById("option-drive-history-count").textContent = String(history.length);
      workingNode.innerHTML = working.length ? working.map(order => (
        `<div class="option-drive-journal-row"><div><b>${escapeHtml(optionDriveJournalContractLabel(order))} · ${escapeHtml(String(order.side || "").toUpperCase())} LMT</b><span>${escapeHtml(paperJournalTime(order.created_at))} · Q ${optionBoardNumber(order.qty)} · ${optionBoardNumber(order.entry)}</span></div><button type="button" data-option-drive-cancel="${escapeHtml(order.id)}">CANCEL</button></div>`
      )).join("") : `<div class="option-drive-empty">No working option orders</div>`;
      positionsNode.innerHTML = positions.length ? positions.map(trade => {
        const pnl = trade.unrealized_pnl === null || trade.unrealized_pnl === undefined
          ? "PnL —"
          : `PnL ${paperPnlUnitLabel(trade.pnl_unit)} ${paperSigned(trade.unrealized_pnl)}`;
        return `<div class="option-drive-journal-row"><div><b>${escapeHtml(optionDriveJournalContractLabel(trade))} · ${escapeHtml(String(trade.side || "").toUpperCase())}</b><span>Q ${optionBoardNumber(trade.qty)} · E ${optionBoardNumber(trade.entry)} · M ${optionBoardNumber(trade.mark_price)} · ${escapeHtml(pnl)}</span></div><button class="close-market" type="button" data-option-drive-close="${escapeHtml(trade.id)}" title="Reduce-only paper close at the current paper mark">CLOSE MKT</button></div>`;
      }).join("") : `<div class="option-drive-empty">No open option positions</div>`;
      historyNode.innerHTML = history.length ? history.map(fill => (
        `<div class="option-drive-journal-row"><div><b>${escapeHtml(optionDriveJournalContractLabel(fill))} · ${escapeHtml(String(fill.side || "").toUpperCase())} ${escapeHtml(String(fill.role || "entry").toUpperCase())}</b><span>${escapeHtml(paperJournalTime(fill.filled_at))} · Q ${optionBoardNumber(fill.qty)} @ ${optionBoardNumber(fill.price)}</span></div><span>${escapeHtml(String(fill.source || "paper"))}</span></div>`
      )).join("") : `<div class="option-drive-empty">No option fills</div>`;
    }

    function renderOptionDrive() {
      const drive = currentOptionDriveState();
      const board = currentOptionBoardState();
      const root = document.getElementById("option-drive");
      if (!root) return;
      if (drive.open && board.open && board.surface === "dialog") {
        closeWorkspaceDockSurface("option-drive", { reason: "Option Board dialog opened" });
        return;
      }
      document.getElementById("option-drive-surface")?.setAttribute("aria-hidden", drive.open ? "false" : "true");
      const toggle = document.getElementById("option-drive-toggle");
      toggle?.setAttribute("aria-expanded", drive.open ? "true" : "false");
      optionDriveRenderInstrumentSelector();
      const status = String(board.status || "closed").toLowerCase();
      const statusNode = document.getElementById("option-drive-status");
      if (statusNode) {
        statusNode.className = `quality-badge ${optionBoardStatusClass(status)}`.trim();
        applyUiPresentationState(statusNode, status);
      }
      document.querySelectorAll("[data-option-drive-expiry]").forEach(button => {
        button.setAttribute(
          "aria-selected",
          button.dataset.optionDriveExpiry === drive.expiryMode ? "true" : "false",
        );
      });
      const snapshot = board.snapshot;
      const currentQuote = optionDriveQuoteByConId(snapshot, drive.selected?.conId);
      const bbo = optionDriveLiveBbo(currentQuote);
      if (drive.armed && !bbo) {
        optionDriveDisarm("Fresh live BBO was lost. Re-arm when the quote returns.");
      }
      if (drive.locked && bbo) {
        const liveLadderPrices = optionDriveLadderPrices(
          bbo.mid,
          currentQuote?.price_increments || drive.selected?.contract?.price_increments,
        );
        if (liveLadderPrices.length) drive.ladderPrices = liveLadderPrices;
      }
      const rows = drive.locked ? optionDriveLockedRows(snapshot) : optionDriveVisibleRows(snapshot);
      const spot = Number(snapshot?.spot);
      const nearestStrike = rows.reduce((best, row) => {
        const strike = Number(row?.strike);
        if (!Number.isFinite(strike) || !Number.isFinite(spot)) return best;
        return best === null || Math.abs(strike - spot) < Math.abs(best - spot) ? strike : best;
      }, null);
      const chainRows = document.getElementById("option-drive-chain-rows");
      if (chainRows) {
        chainRows.innerHTML = rows.length ? rows.map(row => {
          const strike = Number(row?.strike);
          const selectedConId = drive.selected?.conId;
          const lockedRow = drive.locked && [row?.call?.con_id, row?.put?.con_id].map(Number).includes(Number(selectedConId));
          const classes = [strike === nearestStrike ? "atm" : "", drive.locked ? "locked" : "", lockedRow ? "selected-row" : ""].filter(Boolean).join(" ");
          return `<div class="option-drive-chain-row ${classes}">${optionDriveContractButton(row?.call, "C", selectedConId)}<b>${optionBoardNumber(strike)}</b>${optionDriveContractButton(row?.put, "P", selectedConId)}</div>`;
        }).join("") : `<div class="option-drive-empty">${status === "loading" ? "Loading chain…" : "No exact contracts"}</div>`;
      }
      const selectedLabel = document.getElementById("option-drive-contract-label");
      if (selectedLabel) selectedLabel.textContent = optionDriveContractLabel(drive.selected?.contract);
      const lock = document.getElementById("option-drive-lock");
      if (lock) {
        lock.disabled = !drive.selected || !snapshot || drive.submitting;
        lock.classList.toggle("locked", drive.locked);
        lock.textContent = drive.locked ? "UNLOCK" : "LOCK STRIKE";
      }
      const arm = document.getElementById("option-drive-arm");
      if (arm) {
        arm.disabled = !drive.locked || !bbo || drive.submitting;
        arm.classList.toggle("armed", drive.armed);
        arm.setAttribute("aria-pressed", drive.armed ? "true" : "false");
        const remainingSeconds = Math.max(
          0,
          Math.ceil((Number(drive.armedUntilMs) - Date.now()) / 1000),
        );
        arm.textContent = drive.armed ? `ARMED ${remainingSeconds}s` : "ARM";
      }
      const message = document.getElementById("option-drive-message");
      if (message) {
        message.className = `option-drive-message ${drive.messageTone}`.trim();
        const safetyMessage = drive.selected && !bbo
          ? "Selected contract has no fresh live Bid/Ask. Order entry is blocked."
          : "";
        const armedMessage = drive.armed
          ? `ARMED · ladder follows live BBO · ${Math.max(0, Math.ceil((drive.armedUntilMs - Date.now()) / 1000))}s`
          : "";
        message.textContent = safetyMessage
          || armedMessage
          || drive.message
          || board.message
          || "Select a Call or Put, then lock the strike table.";
      }
      renderOptionDriveExecutionEligibility();
      document.getElementById("option-drive-bid").textContent = (
        `BID ${optionBoardNumber(bbo?.bid)} × ${optionDriveContractSize(currentQuote?.bid_size)}`
      );
      const mid = document.getElementById("option-drive-mid");
      if (mid) {
        mid.textContent = `MID ${optionBoardNumber(bbo?.mid)}${drive.locked && bbo ? " · LIVE" : ""}`;
        mid.classList.toggle("live-follow", Boolean(drive.locked && bbo));
        mid.title = drive.locked && bbo
          ? "Premium ladder is following the current live midpoint."
          : "Current exact-contract midpoint.";
      }
      document.getElementById("option-drive-ask").textContent = (
        `ASK ${optionBoardNumber(bbo?.ask)} × ${optionDriveContractSize(currentQuote?.ask_size)}`
      );
      const ladder = document.getElementById("option-drive-ladder-rows");
      if (ladder) {
        const enabled = drive.armed && drive.locked && Boolean(bbo) && !drive.submitting;
        const bidTick = bbo ? optionDriveAlignedPrice(bbo.bid, currentQuote?.price_increments) : null;
        const askTick = bbo ? optionDriveAlignedPrice(bbo.ask, currentQuote?.price_increments) : null;
        ladder.innerHTML = drive.ladderPrices.length ? drive.ladderPrices.map((price, index) => {
          const rowClasses = [
            index === OPTION_DRIVE_LADDER_RADIUS ? "reference" : "",
            price === bidTick ? "bid-touch" : "",
            price === askTick ? "ask-touch" : "",
            bbo && price >= bbo.bid && price <= bbo.ask ? "inside-spread" : "",
          ].filter(Boolean).join(" ");
          return `<div class="option-drive-ladder-row ${rowClasses}"><button class="buy" type="button" data-option-drive-order-side="long" data-option-drive-order-price="${price}" title="PAPER BUY LMT ${optionBoardNumber(price)}"${enabled ? "" : " disabled"}>BUY</button><b>${optionBoardNumber(price)}</b><button class="sell" type="button" data-option-drive-order-side="short" data-option-drive-order-price="${price}" title="PAPER SELL LMT ${optionBoardNumber(price)}"${enabled ? "" : " disabled"}>SELL</button></div>`;
        }).join("") : `<div class="option-drive-empty">Lock a contract to start the live-follow premium ladder</div>`;
      }
      renderOptionDriveJournal();
    }

    function setupOptionDrive() {
      registerWorkspaceDockSurface("option-drive", {
        activate: activateOptionDrive,
        deactivate: deactivateOptionDrive,
        refresh: renderOptionDrive,
      });
      document.getElementById("option-drive-toggle")?.addEventListener("click", () => {
        toggleWorkspaceDockSurface("option-drive", { reason: "Option Drive toggled" });
      });
      document.getElementById("option-drive-collapse")?.addEventListener("click", () => collapseOptionDrive());
      document.getElementById("option-drive-instrument")?.addEventListener("change", event => {
        optionDriveChangeTradeInstrument(event.target?.value);
      });
      document.getElementById("option-drive-lock")?.addEventListener("click", optionDriveToggleLock);
      document.getElementById("option-drive-arm")?.addEventListener("click", optionDriveToggleArm);
      document.getElementById("option-drive-expiry-tabs")?.addEventListener("click", event => {
        const button = event.target?.closest?.("[data-option-drive-expiry]");
        const expiryMode = String(button?.dataset?.optionDriveExpiry || "");
        const drive = currentOptionDriveState();
        if (!button || !["0dte", "1dte"].includes(expiryMode) || expiryMode === drive.expiryMode) return;
        drive.expiryMode = expiryMode;
        optionDriveResetSelection(`Loading ${expiryMode.toUpperCase()} exact contracts…`);
        optionDriveStartStream();
      });
      document.getElementById("option-drive-chain-rows")?.addEventListener("click", event => {
        const button = event.target?.closest?.("[data-option-drive-con-id]");
        if (button) optionDriveSelectContract(Number(button.dataset.optionDriveConId));
      });
      document.getElementById("option-drive-ladder-rows")?.addEventListener("click", event => {
        const button = event.target?.closest?.("[data-option-drive-order-side]");
        if (!button) return;
        submitOptionDriveOrder(
          String(button.dataset.optionDriveOrderSide || ""),
          Number(button.dataset.optionDriveOrderPrice),
        );
      });
      document.getElementById("option-drive-journal-toggle")?.addEventListener("click", () => {
        const journal = document.getElementById("option-drive-journal");
        const expanded = !journal?.classList.contains("expanded");
        journal?.classList.toggle("expanded", expanded);
        document.getElementById("option-drive-journal-toggle")?.setAttribute("aria-expanded", expanded ? "true" : "false");
      });
      document.getElementById("option-drive-journal")?.addEventListener("click", event => {
        const cancel = event.target?.closest?.("[data-option-drive-cancel]");
        if (cancel) {
          const order = optionDriveJournalOrders().find(item => (
            exactIdentityText(item?.id) === exactIdentityText(cancel.dataset.optionDriveCancel)
          ));
          cancelPaperOrder(cancel.dataset.optionDriveCancel, order).then(renderOptionDrive);
        }
        const close = event.target?.closest?.("[data-option-drive-close]");
        if (close) {
          const trade = optionDriveJournalTrades().find(item => (
            exactIdentityText(item?.id) === exactIdentityText(close.dataset.optionDriveClose)
          ));
          closeManualPaperTrade(close.dataset.optionDriveClose, trade).then(renderOptionDrive);
        }
      });
      document.addEventListener("keydown", event => {
        if (event.key !== "Escape" || !currentOptionDriveState().open) return;
        if (currentOptionDriveState().armed) {
          event.preventDefault();
          optionDriveDisarm("Option Drive disarmed.");
          renderOptionDrive();
        }
      });
      window.addEventListener("blur", () => {
        if (!currentOptionDriveState().armed) return;
        optionDriveDisarm("Window lost focus. Option Drive disarmed.");
        renderOptionDrive();
      });
      renderOptionDrive();
    }
