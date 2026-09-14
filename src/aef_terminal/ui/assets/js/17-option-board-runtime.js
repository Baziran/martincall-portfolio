    const OPTION_BOARD_STREAM_STALE_MS = 20_000;
    const OPTION_BOARD_RETRY_MS = 1_500;

    function currentOptionBoardState() {
      state.optionBoard = state.optionBoard || {
        open: false,
        surface: "",
        socket: null,
        routeKey: "",
        expiryMode: "hybrid",
        lifecycleGeneration: 0,
        retryTimer: null,
        staleTimer: null,
        status: "closed",
        message: "",
        snapshot: null,
        revision: 0,
        lastMessageAt: 0,
        addingToken: "",
      };
      return state.optionBoard;
    }

    function optionBoardRouteContext() {
      const board = currentOptionBoardState();
      if (board.surface === "drive" && typeof optionDriveTradeContext === "function") {
        return optionDriveTradeContext();
      }
      const instrument = instrumentForId(state.instrumentId);
      const instrumentId = exactIdentityText(instrument?.instrument_id);
      const routeFingerprint = instrumentRouteFingerprint(instrument);
      const provider = String(instrument?.provider || state.dataSource || "").trim().toLowerCase();
      return instrumentId && routeFingerprint && provider
        ? { instrument, instrumentId, routeFingerprint, provider }
        : null;
    }

    function optionBoardRouteKey() {
      const context = optionBoardRouteContext();
      return context
        ? JSON.stringify([
            context.instrumentId,
            context.routeFingerprint,
            currentOptionBoardState().expiryMode,
          ])
        : "";
    }

    function optionBoardMessageMatchesRoute(message) {
      const board = currentOptionBoardState();
      const context = optionBoardRouteContext();
      return Boolean(
        message
        && typeof message === "object"
        && context
        && exactIdentityText(message.instrument_id) === context.instrumentId
        && exactIdentityText(message.route_fingerprint) === context.routeFingerprint
        && String(message.expiry_mode || "") === board.expiryMode
        && board.routeKey === optionBoardRouteKey()
      );
    }

    function optionBoardNumber(value, digits = null) {
      if (value === null || value === undefined || value === "") return "—";
      const number = Number(value);
      if (!Number.isFinite(number)) return "—";
      const decimals = digits === null
        ? Math.abs(number) < 1 ? 3 : 2
        : digits;
      return number.toFixed(decimals);
    }

    function optionBoardQuoteClass(quote, nowMs = Date.now()) {
      if (quote?.is_delayed === true) return "option-board-delayed";
      const observedAt = Date.parse(
        quote?.received_at
        || quote?.last_provider_ts
        || quote?.provider_ts
        || quote?.ts
        || "",
      );
      if (!Number.isFinite(observedAt) || nowMs - observedAt > 10_000) {
        return "option-board-stale";
      }
      return "";
    }

    function optionBoardPresentationState(nowMs = Date.now()) {
      const board = currentOptionBoardState();
      const quoteStates = [];
      let anySelectable = false;
      let nextTransitionAt = Number.POSITIVE_INFINITY;
      const selectableStatus = ["ready", "partial", "waiting"].includes(
        String(board.status || "").toLowerCase(),
      );
      for (const series of Array.isArray(board.snapshot?.series) ? board.snapshot.series : []) {
        for (const row of Array.isArray(series?.rows) ? series.rows : []) {
          for (const quote of [row?.call, row?.put]) {
            if (!quote || typeof quote !== "object") continue;
            const expiryAtMs = Date.parse(quote.expiry_at || "");
            const selectable = (
              selectableStatus
              && Number.isFinite(expiryAtMs)
              && expiryAtMs > nowMs
            );
            anySelectable = anySelectable || selectable;
            const quoteClass = optionBoardQuoteClass(quote, nowMs);
            quoteStates.push([Number(quote.con_id), quoteClass, selectable]);
            if (selectable) nextTransitionAt = Math.min(nextTransitionAt, expiryAtMs + 1);
            if (quoteClass !== "option-board-delayed" && quoteClass !== "option-board-stale") {
              const observedAt = Date.parse(
                quote.received_at
                || quote.last_provider_ts
                || quote.provider_ts
                || quote.ts
                || "",
              );
              if (Number.isFinite(observedAt)) {
                nextTransitionAt = Math.min(nextTransitionAt, observedAt + 10_001);
              }
            }
          }
        }
      }
      return {
        signature: JSON.stringify([String(board.status || "closed"), anySelectable, quoteStates]),
        nextTransitionAt: Number.isFinite(nextTransitionAt) ? nextTransitionAt : 0,
      };
    }

    function optionBoardPresentationSignature(nowMs = Date.now()) {
      return optionBoardPresentationState(nowMs).signature;
    }

    function refreshOptionBoardFreshness(nowMs = Date.now()) {
      const board = currentOptionBoardState();
      if (!board.open || !board.snapshot) return false;
      const scheduledTransition = Number(board.nextPresentationTransitionAt || 0);
      if (scheduledTransition > nowMs) return false;
      const presentation = optionBoardPresentationState(nowMs);
      board.nextPresentationTransitionAt = presentation.nextTransitionAt;
      if (board.presentationSignature === presentation.signature) return false;
      board.presentationSignature = presentation.signature;
      renderOptionBoard();
      return true;
    }

    function optionBoardQuoteTitle(quote, label, sizeField = "") {
      if (!quote || typeof quote !== "object") return `${label}: unavailable`;
      const rawSize = sizeField ? quote[sizeField] : null;
      const size = (
        rawSize !== null
        && rawSize !== undefined
        && rawSize !== ""
        && Number.isFinite(Number(rawSize))
      )
        ? ` · size ${Number(quote[sizeField])}`
        : "";
      const entitlement = String(
        quote.market_data_entitlement || "unknown",
      ).toUpperCase();
      const localSymbol = String(quote.local_symbol || "");
      return [
        `${label}${size}`,
        localSymbol,
        `conId ${quote.con_id || "—"}`,
        `${quote.expiry || "—"} ${quote.right || ""}`,
        `${quote.trading_class || "—"} @ ${quote.exchange || "—"}`,
        `Data ${entitlement}`,
      ].filter(Boolean).join("\n");
    }

    function optionBoardPriceCell(
      quote,
      field,
      label,
      sizeField = "",
      position = null,
    ) {
      const value = quote?.[field];
      const board = currentOptionBoardState();
      const expiryAtMs = Date.parse(quote?.expiry_at || "");
      const empty = (
        value === null
        || value === undefined
        || value === ""
        || !Number.isFinite(Number(value))
      );
      const selectable = Boolean(
        quote
        && typeof quote === "object"
        && position
        && Number.isSafeInteger(position.seriesIndex)
        && Number.isSafeInteger(position.rowIndex)
        && ["C", "P"].includes(position.side)
        && Number.isSafeInteger(position.revision)
        && ["ready", "partial", "waiting"].includes(
          String(board.status || "").toLowerCase(),
        )
        && Number.isFinite(expiryAtMs)
        && expiryAtMs > Date.now()
      );
      const token = selectable
        ? `${position.seriesIndex}:${position.rowIndex}:${position.side}:${field}`
        : "";
      const classes = [
        selectable ? "option-board-contract-cell" : "",
        token && board.addingToken === token ? "option-board-adding" : "",
        empty ? "option-board-empty" : "",
        optionBoardQuoteClass(quote),
      ].filter(Boolean).join(" ");
      const interactive = selectable
        ? ` role="button" tabindex="0" aria-label="${escapeHtml(`Add ${quote.local_symbol || `${quote.right || ""} ${quote.strike || ""}`} Option Point on the current candle`)}" data-option-board-cell="" data-option-board-series-index="${position.seriesIndex}" data-option-board-row-index="${position.rowIndex}" data-option-board-side="${position.side}" data-option-board-field="${field}" data-option-board-revision="${position.revision}"${board.addingToken ? ' aria-disabled="true"' : ""}${board.addingToken === token ? ' aria-busy="true"' : ""}`
        : "";
      return `<td class="${classes}" title="${escapeHtml(optionBoardQuoteTitle(quote, label, sizeField))}"${interactive}>${optionBoardNumber(value)}</td>`;
    }

    function optionBoardSeriesRows(snapshot) {
      const series = Array.isArray(snapshot?.series) ? snapshot.series : [];
      const output = [];
      const revision = snapshot?.revision;
      for (let seriesIndex = 0; seriesIndex < series.length; seriesIndex += 1) {
        const item = series[seriesIndex];
        const expiry = String(item?.expiry || "");
        const expiryDate = expiry.length === 8
          ? `${expiry.slice(0, 4)}-${expiry.slice(4, 6)}-${expiry.slice(6, 8)}`
          : expiry || "Unknown expiry";
        const seriesLabel = [
          expiryDate,
          item?.trading_class,
          item?.exchange,
          Number.isFinite(Number(item?.multiplier))
            ? `×${Number(item.multiplier)}`
            : "",
          item?.currency,
        ].filter(Boolean).join(" · ");
        output.push(
          `<tr class="option-board-series"><td colspan="9">${escapeHtml(seriesLabel)}</td></tr>`,
        );
        const seriesRows = Array.isArray(item?.rows) ? item.rows : [];
        for (let rowIndex = 0; rowIndex < seriesRows.length; rowIndex += 1) {
          const row = seriesRows[rowIndex];
          const call = row?.call && typeof row.call === "object" ? row.call : null;
          const put = row?.put && typeof row.put === "object" ? row.put : null;
          const callPosition = { seriesIndex, rowIndex, side: "C", revision };
          const putPosition = { seriesIndex, rowIndex, side: "P", revision };
          output.push(`<tr>
            ${optionBoardPriceCell(call, "last", "Call Last", "last_size", callPosition)}
            ${optionBoardPriceCell(call, "bid", "Call Bid", "bid_size", callPosition)}
            ${optionBoardPriceCell(call, "ask", "Call Ask", "ask_size", callPosition)}
            ${optionBoardPriceCell(call, "mid", "Call Mid", "", callPosition)}
            <td class="option-board-strike">${optionBoardNumber(row?.strike)}</td>
            ${optionBoardPriceCell(put, "mid", "Put Mid", "", putPosition)}
            ${optionBoardPriceCell(put, "bid", "Put Bid", "bid_size", putPosition)}
            ${optionBoardPriceCell(put, "ask", "Put Ask", "ask_size", putPosition)}
            ${optionBoardPriceCell(put, "last", "Put Last", "last_size", putPosition)}
          </tr>`);
        }
      }
      return output.join("");
    }

    function optionBoardExactContractFromCell(cell) {
      const board = currentOptionBoardState();
      const snapshot = board.snapshot;
      const cellRevision = Number(cell?.dataset?.optionBoardRevision);
      const seriesIndex = Number(cell?.dataset?.optionBoardSeriesIndex);
      const rowIndex = Number(cell?.dataset?.optionBoardRowIndex);
      const side = String(cell?.dataset?.optionBoardSide || "");
      if (
        !snapshot
        || !optionBoardMessageMatchesRoute(snapshot)
        || !["ready", "partial", "waiting"].includes(
          String(board.status || "").toLowerCase(),
        )
        || !Number.isSafeInteger(cellRevision)
        || cellRevision !== board.revision
        || snapshot.revision !== board.revision
        || !Number.isSafeInteger(seriesIndex)
        || !Number.isSafeInteger(rowIndex)
        || !["C", "P"].includes(side)
      ) return null;
      const series = snapshot.series?.[seriesIndex];
      const row = series?.rows?.[rowIndex];
      const quote = side === "C" ? row?.call : row?.put;
      const providerSymbol = exactIdentityText(snapshot.provider_symbol);
      const expiryAtMs = Date.parse(quote?.expiry_at || "");
      if (
        !quote
        || typeof quote !== "object"
        || !providerSymbol
        || !["0dte", "1dte"].includes(series?.target_dte)
        || !Number.isFinite(expiryAtMs)
        || expiryAtMs <= Date.now()
      ) return null;
      const contract = {
        ...quote,
        provider_symbol: providerSymbol,
        mode: "normal",
        target_dte: series.target_dte,
        estimated_greeks: true,
        target_delta: null,
      };
      if (!sameOptionTargetContract(contract, contract)) return null;
      return {
        contract,
        token: `${seriesIndex}:${rowIndex}:${side}:${String(cell?.dataset?.optionBoardField || "")}`,
      };
    }

    async function addOptionBoardCellToCurrentCandle(cell) {
      const board = currentOptionBoardState();
      const resolved = optionBoardExactContractFromCell(cell);
      if (board.addingToken) return null;
      if (!resolved) {
        throw new Error("The exact option contract snapshot changed; wait for the Board refresh and try again.");
      }
      if (!snapshotMatchesCurrentRoute()) {
        throw new Error("The chart route changed; reopen Option Board and try again.");
      }
      const bars = Array.isArray(state.snapshot?.bars) ? state.snapshot.bars : [];
      const latest = bars[bars.length - 1];
      if (!latest) throw new Error("The current chart has no provider candle yet.");
      const price = currentChartDisplayPrice(state.snapshot, latest);
      const anchor = optionTargetConfirmedAnchorPoint({ ts: latest.ts, price }, latest);
      const requestScope = optionTargetRequestScopeForPoint(anchor);
      board.addingToken = resolved.token;
      renderOptionBoard();
      try {
        const saved = await addOptionTargetFromExactContract(
          resolved.contract,
          requestScope,
        );
        if (!saved) return null;
        closeOptionBoard({ reason: "option point added" });
        showBrowserToast("Option Point added to the current candle. Drag it to the target level.", {
          tone: "success",
        });
        return saved;
      } finally {
        board.addingToken = "";
        if (board.open) renderOptionBoard();
      }
    }

    function handleOptionBoardCellActivation(event) {
      if (
        event.type === "keydown"
        && !["Enter", " "].includes(event.key)
      ) return;
      const rows = document.getElementById("option-board-rows");
      const cell = event.target?.closest?.("[data-option-board-cell]");
      if (!rows || !cell || !rows.contains(cell)) return;
      event.preventDefault();
      addOptionBoardCellToCurrentCandle(cell).catch(error => {
        console.warn(
          "option board point add failed",
          requestErrorMessage(error, "option board point add failed"),
        );
        showBrowserToast(
          `Option Point was not added: ${optionTargetErrorMessage(error)}`,
          { tone: "error" },
        );
      });
    }

    function optionBoardStatusClass(status) {
      return uiPresentationStateDescriptor(status).tone;
    }

    function renderOptionBoard() {
      const board = currentOptionBoardState();
      const root = document.getElementById("option-board-dialog");
      if (!root) return;
      root.classList.toggle("open", board.open && board.surface === "dialog");
      const snapshot = board.snapshot;
      const status = String(board.status || "closed").toLowerCase();
      const statusNode = document.getElementById("option-board-status");
      if (statusNode) {
        statusNode.className = `quality-badge ${optionBoardStatusClass(status)}`.trim();
        applyUiPresentationState(statusNode, status);
      }
      const subtitle = document.getElementById("option-board-subtitle");
      if (subtitle) {
        const providerSymbol = String(snapshot?.provider_symbol || state.symbol || "");
        const contracts = Number(snapshot?.contract_count);
        subtitle.textContent = providerSymbol
          ? `${providerSymbol} · nearest expiry${Number.isFinite(contracts) ? ` · ${contracts} contracts` : ""}`
          : "Exact provider option universe";
      }
      const spot = document.getElementById("option-board-spot");
      if (spot) {
        const reference = snapshot?.spot_reference;
        const source = String(reference?.price_source || "").toLowerCase();
        const sourceLabel = reference?.state === "current"
          ? "LIVE"
          : source === "previous_close"
            ? "CLOSE REF"
            : reference?.state === "reference"
              ? "REFERENCE"
              : "";
        spot.textContent = `Spot ${optionBoardNumber(snapshot?.spot)}${sourceLabel ? ` · ${sourceLabel}` : ""}`;
        spot.title = sourceLabel
          ? `Strike centering uses ${source || "provider reference"}; option entry still requires a fresh live exact-contract BBO.`
          : "Exact provider underlying value used to center the option universe.";
      }
      const entitlement = document.getElementById("option-board-entitlement");
      if (entitlement) {
        const value = String(snapshot?.market_data_entitlement || "—").toUpperCase();
        entitlement.textContent = `Data ${value}`;
      }
      const updated = document.getElementById("option-board-updated");
      if (updated) {
        const capturedAt = Date.parse(snapshot?.captured_at || "");
        updated.textContent = Number.isFinite(capturedAt)
          ? `Updated ${new Date(capturedAt).toLocaleTimeString()}`
          : "Updated —";
      }
      const message = document.getElementById("option-board-message");
      if (message) {
        message.textContent = board.message
          || (status === "loading"
            ? "Qualifying contracts and opening bounded live subscriptions…"
            : status === "waiting"
              ? "Contracts are subscribed; waiting for provider prices."
              : status === "partial"
                ? `Receiving ${Number(snapshot?.priced_contract_count) || 0}/${Number(snapshot?.contract_count) || 0} contract prices.`
                : status === "closed"
                  ? "No option subscriptions are active."
                  : "");
      }
      const rows = document.getElementById("option-board-rows");
      if (rows) {
        const focused = rows.contains(document.activeElement)
          ? {
              series: document.activeElement?.dataset?.optionBoardSeriesIndex,
              row: document.activeElement?.dataset?.optionBoardRowIndex,
              side: document.activeElement?.dataset?.optionBoardSide,
              field: document.activeElement?.dataset?.optionBoardField,
            }
          : null;
        rows.innerHTML = optionBoardSeriesRows(snapshot);
        if (
          focused
          && [focused.series, focused.row, focused.side, focused.field].every(
            value => typeof value === "string" && value,
          )
        ) {
          rows.querySelector(
            `[data-option-board-series-index="${focused.series}"][data-option-board-row-index="${focused.row}"][data-option-board-side="${focused.side}"][data-option-board-field="${focused.field}"]`,
          )?.focus({ preventScroll: true });
        }
      }
      renderOptionBoardButton();
      const presentation = optionBoardPresentationState();
      board.presentationSignature = presentation.signature;
      board.nextPresentationTransitionAt = presentation.nextTransitionAt;
      if (typeof renderOptionDrive === "function") renderOptionDrive();
    }

    function renderOptionBoardButton(visibleOverride = null) {
      const button = document.getElementById("option-board-toggle");
      if (!button) return;
      const board = currentOptionBoardState();
      const capable = typeof providerCapability === "function"
        && providerCapability(state.dataSource, "options");
      const visible = capable && (visibleOverride === null || Boolean(visibleOverride));
      button.classList.toggle("hidden", !visible);
      const dialogOpen = board.open && board.surface === "dialog";
      button.classList.toggle("active", dialogOpen);
      button.classList.toggle(
        "warn",
        ["error", "unavailable", "unsupported"].includes(
          String(board.status || "").toLowerCase(),
        ),
      );
      button.disabled = Boolean(state.serverSleeping || !capable);
      button.setAttribute("aria-pressed", dialogOpen ? "true" : "false");
      button.setAttribute(
        "aria-label",
        dialogOpen ? "Close live option prices" : "Open live option prices",
      );
      button.title = dialogOpen
        ? "Close live option prices"
        : capable
          ? "Open live option prices"
          : "Current provider has no option capability";
    }

    function clearOptionBoardRetry() {
      const board = currentOptionBoardState();
      if (!board.retryTimer) return;
      window.clearTimeout(board.retryTimer);
      board.retryTimer = null;
    }

    function clearOptionBoardStaleTimer() {
      const board = currentOptionBoardState();
      if (!board.staleTimer) return;
      window.clearTimeout(board.staleTimer);
      board.staleTimer = null;
    }

    function armOptionBoardStaleTimer(socket, lifecycleGeneration) {
      const board = currentOptionBoardState();
      clearOptionBoardStaleTimer();
      const timer = window.setTimeout(() => {
        if (
          board.socket !== socket
          || board.lifecycleGeneration !== lifecycleGeneration
        ) return;
        const staleFor = Date.now() - Number(board.lastMessageAt || 0);
        if (staleFor >= OPTION_BOARD_STREAM_STALE_MS) {
          board.status = "error";
          board.message = `Option Board stream stalled for ${Math.round(staleFor / 1000)}s; reconnecting.`;
          renderOptionBoard();
          try { socket.close(4000, "option board stale"); } catch (_) { /* noop */ }
          return;
        }
        armOptionBoardStaleTimer(socket, lifecycleGeneration);
      }, OPTION_BOARD_STREAM_STALE_MS);
      board.staleTimer = timer;
    }

    function scheduleOptionBoardReconnect(lifecycleGeneration) {
      const board = currentOptionBoardState();
      clearOptionBoardRetry();
      if (!board.open || state.serverSleeping) return;
      board.retryTimer = window.setTimeout(() => {
        board.retryTimer = null;
        if (
          board.open
          && board.lifecycleGeneration === lifecycleGeneration
          && board.routeKey === optionBoardRouteKey()
        ) {
          connectOptionBoard();
        }
      }, OPTION_BOARD_RETRY_MS);
    }

    function connectOptionBoard() {
      const board = currentOptionBoardState();
      if (!board.open || state.serverSleeping) return;
      const context = optionBoardRouteContext();
      const instrumentId = context?.instrumentId || "";
      const routeFingerprint = context?.routeFingerprint || "";
      const routeKey = optionBoardRouteKey();
      if (
        !instrumentId
        || !routeFingerprint
        || !routeKey
        || !providerCapability(context?.provider, "options")
      ) {
        board.status = "unsupported";
        board.message = "The selected exact provider route has no option capability.";
        renderOptionBoard();
        return;
      }
      if (
        board.socket
        && board.socket.readyState <= WebSocket.OPEN
        && board.routeKey === routeKey
      ) return;
      clearOptionBoardRetry();
      if (board.socket) {
        try { board.socket.close(4001, "option board replaced"); } catch (_) { /* noop */ }
      }
      board.routeKey = routeKey;
      board.status = "loading";
      board.message = "";
      board.snapshot = null;
      board.revision = 0;
      const lifecycleGeneration = ++board.lifecycleGeneration;
      const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      const params = new URLSearchParams({
        instrument_id: instrumentId,
        expected_route_fingerprint: routeFingerprint,
        expiry_mode: board.expiryMode,
      });
      const socket = new WebSocket(
        `${protocol}//${window.location.host}/ws/options-board?${params.toString()}`,
      );
      board.socket = socket;
      board.lastMessageAt = Date.now();
      armOptionBoardStaleTimer(socket, lifecycleGeneration);
      socket.addEventListener("open", () => {
        if (
          board.socket !== socket
          || board.lifecycleGeneration !== lifecycleGeneration
        ) return;
        board.lastMessageAt = Date.now();
        armOptionBoardStaleTimer(socket, lifecycleGeneration);
        board.status = "loading";
        board.message = "";
        renderOptionBoard();
      });
      socket.addEventListener("message", event => {
        if (
          board.socket !== socket
          || board.lifecycleGeneration !== lifecycleGeneration
        ) return;
        let message;
        try {
          message = JSON.parse(event.data);
        } catch (_) {
          board.status = "error";
          board.message = "Option Board received invalid JSON.";
          renderOptionBoard();
          return;
        }
        if (!optionBoardMessageMatchesRoute(message)) {
          closeOptionBoard({ reason: "Provider route changed." });
          return;
        }
        board.lastMessageAt = Date.now();
        armOptionBoardStaleTimer(socket, lifecycleGeneration);
        if (message.type === "option_board_snapshot") {
          const revision = message.revision;
          if (
            !Number.isSafeInteger(revision)
            || revision <= 0
            || !Array.isArray(message.series)
          ) {
            board.status = "error";
            board.message = "Option Board received an invalid typed snapshot.";
          } else if (revision >= board.revision) {
            board.revision = revision;
            board.snapshot = message;
            board.status = String(message.status || "ready").toLowerCase();
            board.message = "";
          }
        } else if (message.type === "option_board_status") {
          board.status = String(message.status || "error").toLowerCase();
          board.message = String(message.message || "");
          if (["unavailable", "error", "unsupported"].includes(board.status)) {
            board.snapshot = null;
            board.addingToken = "";
          }
        } else if (message.type !== "option_board_heartbeat") {
          board.status = "error";
          board.message = `Unknown Option Board message: ${String(message.type || "missing type")}`;
        }
        renderOptionBoard();
      });
      socket.addEventListener("close", () => {
        if (board.socket !== socket) return;
        clearOptionBoardStaleTimer();
        board.socket = null;
        if (
          board.open
          && board.lifecycleGeneration === lifecycleGeneration
          && board.routeKey === optionBoardRouteKey()
        ) {
          board.status = "loading";
          board.message = "Option Board stream closed; reconnecting.";
          renderOptionBoard();
          scheduleOptionBoardReconnect(lifecycleGeneration);
        }
      });
      socket.addEventListener("error", () => {
        if (board.socket !== socket) return;
        board.status = "error";
        board.message = "Option Board WebSocket transport failed.";
        renderOptionBoard();
      });
      renderOptionBoard();
    }

    function suspendOptionBoard(reason = "Option Board paused while the page is hidden.") {
      const board = currentOptionBoardState();
      if (!board.open) return;
      clearOptionBoardRetry();
      clearOptionBoardStaleTimer();
      board.lifecycleGeneration += 1;
      const socket = board.socket;
      board.socket = null;
      if (socket && socket.readyState <= WebSocket.OPEN) {
        try { socket.close(1000, "option board paused"); } catch (_) { /* noop */ }
      }
      board.status = "sleeping";
      board.message = reason;
      board.lastMessageAt = 0;
      renderOptionBoard();
    }

    function openOptionBoard() {
      const board = currentOptionBoardState();
      if (
        !exactIdentityText(state.instrumentId)
        || !instrumentRouteFingerprint()
        || !providerCapability(state.dataSource, "options")
      ) {
        board.status = "unsupported";
        board.message = "Select an exact provider-qualified instrument with options.";
        board.open = true;
        board.surface = "dialog";
        renderOptionBoard();
        return;
      }
      board.open = true;
      board.surface = "dialog";
      board.routeKey = optionBoardRouteKey();
      board.status = "loading";
      board.message = "";
      renderOptionBoard();
      requestAnimationFrame(() => {
        clampFloatingPanelToViewport("option-board-dialog");
      });
      connectOptionBoard();
    }

    function closeOptionBoard(options = {}) {
      const board = currentOptionBoardState();
      const closedSurface = board.surface;
      board.open = false;
      board.surface = "";
      clearOptionBoardRetry();
      clearOptionBoardStaleTimer();
      board.lifecycleGeneration += 1;
      const socket = board.socket;
      board.socket = null;
      if (socket && socket.readyState <= WebSocket.OPEN) {
        try { socket.close(1000, String(options.reason || "option board closed")); } catch (_) { /* noop */ }
      }
      board.routeKey = "";
      board.status = "closed";
      board.message = "";
      board.snapshot = null;
      board.revision = 0;
      board.lastMessageAt = 0;
      const root = document.getElementById("option-board-dialog");
      if (root?.contains(document.activeElement)) document.activeElement?.blur?.();
      renderOptionBoard();
      if (typeof onOptionBoardSurfaceClosed === "function") {
        onOptionBoardSurfaceClosed(closedSurface);
      }
    }

    function toggleOptionBoard() {
      const board = currentOptionBoardState();
      if (board.open && board.surface === "dialog") closeOptionBoard();
      else openOptionBoard();
    }

    function setupOptionBoard() {
      setupFloatingPanelDrag("option-board-dialog", {
        root: document.getElementById("option-board-dialog"),
        surface: document.querySelector("#option-board-dialog .modal-card"),
        handle: document.querySelector("#option-board-dialog .modal-head"),
      });
      document.getElementById("option-board-toggle")?.addEventListener(
        "click",
        toggleOptionBoard,
      );
      document.getElementById("option-board-close")?.addEventListener(
        "click",
        () => closeOptionBoard(),
      );
      const rows = document.getElementById("option-board-rows");
      rows?.addEventListener("click", handleOptionBoardCellActivation);
      rows?.addEventListener("keydown", handleOptionBoardCellActivation);
      document.addEventListener("keydown", event => {
        if (
          event.key === "Escape"
          && currentOptionBoardState().open
          && !event.defaultPrevented
        ) {
          event.preventDefault();
          closeOptionBoard();
        }
      });
      renderOptionBoard();
    }
