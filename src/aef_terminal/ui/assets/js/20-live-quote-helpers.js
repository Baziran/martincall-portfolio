    const SCREENER_QUOTE_FRAME_CONTRACT = window.QUOTE_STREAM_ROW_CONTRACT_MANIFEST;
    if (
      !SCREENER_QUOTE_FRAME_CONTRACT
      || SCREENER_QUOTE_FRAME_CONTRACT.version !== 1
      || !Array.isArray(SCREENER_QUOTE_FRAME_CONTRACT.required_fields)
    ) throw new TypeError("QUOTE_STREAM_ROW_CONTRACT_MANIFEST_INVALID");

    function screenerQuoteFrameMissingFields(row) {
      if (!row || typeof row !== "object" || Array.isArray(row)) {
        return SCREENER_QUOTE_FRAME_CONTRACT.required_fields;
      }
      return SCREENER_QUOTE_FRAME_CONTRACT.required_fields.filter(
        field => !Object.hasOwn(row, field),
      );
    }

    function screenerQuoteFrameObjectsComplete(row) {
      return Object.entries(SCREENER_QUOTE_FRAME_CONTRACT.nullable_object_contracts).every(
        ([field, contract]) => {
          const value = row[field];
          if (value === null) return true;
          if (!value || typeof value !== "object" || Array.isArray(value)) return false;
          const keys = Object.keys(value);
          if (
            keys.length !== contract.required_fields.length
            || contract.required_fields.some(key => !Object.hasOwn(value, key))
          ) return false;
          if (contract.integer_fields.some(key => !Number.isInteger(value[key]))) return false;
          if (contract.non_empty_text_fields.some(
            key => typeof value[key] !== "string" || !value[key],
          )) return false;
          return Object.entries(contract.enum_fields).every(
            ([key, allowed]) => allowed.includes(value[key]),
          );
        },
      );
    }

    function screenerQuoteFrameComplete(row) {
      if (screenerQuoteFrameMissingFields(row).length) return false;
      if (SCREENER_QUOTE_FRAME_CONTRACT.number_fields.some(
        field => row[field] !== null && !Number.isFinite(row[field]),
      )) return false;
      if (SCREENER_QUOTE_FRAME_CONTRACT.boolean_fields.some(
        field => typeof row[field] !== "boolean",
      )) return false;
      if (SCREENER_QUOTE_FRAME_CONTRACT.time_fields.some(field => (
        row[field] !== null && (typeof row[field] !== "string" || !row[field])
      ))) return false;
      return (
        !SCREENER_QUOTE_FRAME_CONTRACT.text_fields.some(
          field => typeof row[field] !== "string",
        )
        && !SCREENER_QUOTE_FRAME_CONTRACT.non_empty_text_fields.some(field => !row[field])
        && !SCREENER_QUOTE_FRAME_CONTRACT.nullable_text_fields.some(
          field => row[field] !== null && typeof row[field] !== "string",
        )
        && !SCREENER_QUOTE_FRAME_CONTRACT.status_fields.some(
          field => !SCREENER_QUOTE_FRAME_CONTRACT.statuses.includes(row[field]),
        )
        && SCREENER_QUOTE_FRAME_CONTRACT.entitlements.includes(
          row[SCREENER_QUOTE_FRAME_CONTRACT.entitlement_field],
        )
        && screenerQuoteFrameObjectsComplete(row)
      );
    }

    function rowHasLiveQuote(row) {
      return Boolean(liveQuotePayloadFromScreenerRow(row, {
        maxAgeMs: LIVE_QUOTE_DISPLAY_TTL_MS,
      }));
    }

    function liveQuotePayloadFromScreenerRow(row, options = {}) {
      if (!screenerQuoteFrameComplete(row)) return null;
      const typedStale = options.allowStale === true
        && row.quote_status === "stale"
        && row.quote_is_stale === true;
      const typedDelayed = options.allowDelayed === true
        && ["delayed", "delayed_frozen"].includes(row.quote_status)
        && row.quote_is_delayed === true
        && typeof row.quote_is_stale === "boolean";
      if (row.live_quote !== true && !typedStale && !typedDelayed) return null;
      const payload = {
        ok: true,
        symbol: state.symbol,
        provider_symbol: row.provider_symbol,
        instrument_id: row.instrument_id,
        route_fingerprint: row.route_fingerprint,
        source: row.source,
        price: row.price,
        price_source: row.price_source,
        bid: row.bid,
        ask: row.ask,
        last: row.last,
        ts: row.quote_ts,
        provider_ts: row.quote_provider_ts,
        received_at: row.quote_received_at,
        time_basis: row.quote_time_basis,
        age_seconds: Number.isFinite(row.quote_age_seconds) ? row.quote_age_seconds : null,
        status: row.quote_status,
        is_delayed: row.quote_is_delayed,
        is_stale: row.quote_is_stale,
        entitlement: row.quote_entitlement,
        last_provider_ts: row.last_provider_ts,
        last_age_seconds: Number.isFinite(row.last_age_seconds) ? row.last_age_seconds : null,
        last_status: row.last_status,
        bid_ask_received_at: row.bid_ask_received_at,
        bid_ask_age_seconds: Number.isFinite(row.bid_ask_age_seconds)
          ? row.bid_ask_age_seconds
          : null,
        bid_ask_status: row.bid_ask_status,
      };
      const maxAgeMs = Object.prototype.hasOwnProperty.call(options, "maxAgeMs")
        ? options.maxAgeMs
        : (typedStale || typedDelayed) ? null : LIVE_QUOTE_DISPLAY_TTL_MS;
      const display = quoteDisplayFromPayload(payload, {
        allowStale: options.allowStale === true,
        allowDelayed: options.allowDelayed === true,
        expectedInstrumentId: row.instrument_id,
        expectedRouteFingerprint: row.route_fingerprint,
        maxAgeMs,
      });
      return display ? payload : null;
    }

    function invalidateLiveQuoteFromScreenerRow(row) {
      if (
        !row
        || !rowMatchesCurrentRoute(row)
        || liveQuotePayloadFromScreenerRow(row, {
          allowStale: true,
          allowDelayed: true,
        })
      ) return false;
      state.liveQuote = null;
      state.lastQuoteAt = 0;
      state.lastPrice = null;
      if (snapshotMatchesCurrentRoute(state.snapshot)) {
        const confirmedBar = typeof latestConfirmedIndicatorBar === "function"
          ? latestConfirmedIndicatorBar(state.snapshot)
          : null;
        const rawConfirmedClose = confirmedBar?.close;
        const confirmedClose = rawConfirmedClose === null
          || rawConfirmedClose === undefined
          || rawConfirmedClose === ""
          ? Number.NaN
          : Number(rawConfirmedClose);
        const rawChangeBase = state.snapshot.meta.previous_session_close
          ?? state.snapshot.meta.change_base;
        const changeBase = rawChangeBase === null
          || rawChangeBase === undefined
          || rawChangeBase === ""
          ? Number.NaN
          : Number(rawChangeBase);
        const hasConfirmedClose = Number.isFinite(confirmedClose);
        state.snapshot.meta = {
          ...state.snapshot.meta,
          ...unavailableLiveQuoteMetaProjection(
            hasConfirmedClose ? confirmedClose : null,
            hasConfirmedClose ? "confirmed_close" : "unavailable",
          ),
        };
        if (hasConfirmedClose && Number.isFinite(changeBase)) {
          state.snapshot.meta.change = confirmedClose - changeBase;
          state.snapshot.meta.change_pct = changeBase !== 0
            ? ((confirmedClose - changeBase) / Math.abs(changeBase)) * 100
            : 0;
        } else {
          state.snapshot.meta.change = null;
          state.snapshot.meta.change_pct = null;
        }
        renderLiveMarketVisuals(state.snapshot);
      }
      return true;
    }

    function barIsLivePreview(bar) {
      return Boolean(bar && (bar.closed === false || ["forming", "awaiting_provider_confirmation"].includes(String(bar.state || ""))));
    }

    function barIsGapPlaceholder(bar) {
      return Boolean(bar && (bar.missing || bar.data_gap || bar.preview_kind === "gap_placeholder"));
    }

    function barIsConfirmedClosed(bar) {
      return Boolean(bar && !barIsGapPlaceholder(bar) && bar.closed === true && (!bar.state || bar.state === "confirmed"));
    }

    function screenerRowIdentityKey(row = {}) {
      const instrumentId = exactIdentityText(row?.instrument_id);
      const routeFingerprint = exactIdentityText(row?.route_fingerprint);
      return instrumentId && routeFingerprint
        ? JSON.stringify([instrumentId, routeFingerprint])
        : "";
    }

    function screenerRowMap(rows = []) {
      if (
        rows === state.screener
        && state.screenerByIdentity instanceof Map
      ) return state.screenerByIdentity;
      const map = new Map();
      (rows || []).forEach(row => {
        const key = screenerRowIdentityKey(row);
        if (key && !map.has(key)) map.set(key, row);
      });
      return map;
    }

    function admitScreenerCacheRevision(options = {}) {
      const cacheEpoch = exactIdentityText(options.cacheEpoch);
      const cacheGeneration = options.cacheGeneration;
      if (!cacheEpoch || !Number.isSafeInteger(cacheGeneration) || cacheGeneration < 0) {
        throw new TypeError("SCREENER_CACHE_REVISION_INVALID");
      }
      const currentEpoch = exactIdentityText(state.transport?.quote?.cacheEpoch);
      const currentGeneration = Number(state.transport?.quote?.cacheGeneration || 0);
      if (!currentEpoch) {
        if (options.allowEpochChange !== true) {
          if (window.mcTelemetryInc) window.mcTelemetryInc("quote_cache.epoch_rejected");
          return false;
        }
      } else if (cacheEpoch !== currentEpoch) {
        if (options.allowEpochChange !== true) {
          if (window.mcTelemetryInc) window.mcTelemetryInc("quote_cache.epoch_rejected");
          return false;
        }
        state.screener = [];
        state.screenerByIdentity = new Map();
      } else if (currentEpoch && cacheGeneration < currentGeneration) {
        if (window.mcTelemetryInc) window.mcTelemetryInc("quote_cache.generation_rejected");
        return false;
      }
      state.transport.quote.cacheEpoch = cacheEpoch;
      state.transport.quote.cacheGeneration = cacheGeneration;
      return true;
    }

    function mergeScreenerRows(nextRows = [], options = {}) {
      const incomingRows = Array.isArray(nextRows) ? nextRows : [];
      const invalidRow = incomingRows.find(row => !screenerQuoteFrameComplete(row));
      if (invalidRow) {
        const missingFields = screenerQuoteFrameMissingFields(invalidRow);
        const code = missingFields.length
          ? `SCREENER_QUOTE_FRAME_INCOMPLETE: ${missingFields.join(", ")}`
          : "SCREENER_QUOTE_FRAME_INVALID";
        throw new TypeError(code);
      }
      if (!admitScreenerCacheRevision(options)) return null;
      const now = Date.now();
      const ttlMs = Number(options.liveTtlMs || LIVE_QUOTE_DISPLAY_TTL_MS);
      const mode = String(options.mode || "http");
      const scopeKeys = options.scopeKeys instanceof Set ? options.scopeKeys : new Set();
      const previousByKey = screenerRowMap(state.screener || []);
      if (mode === "delta") {
        for (const next of incomingRows) {
          const identityKey = screenerRowIdentityKey(next);
          if (!identityKey) continue;
          const replacement = { ...next, _updated_at: now };
          const prior = previousByKey.get(identityKey);
          if (prior) {
            Object.keys(prior).forEach(key => delete prior[key]);
            Object.assign(prior, replacement);
          } else {
            state.screener.push(replacement);
          }
          previousByKey.set(identityKey, prior || replacement);
        }
        state.screenerByIdentity = previousByKey;
        return state.screener;
      }
      const merged = [];
      const mergedKeys = new Set();
      for (const next of incomingRows) {
        const identityKey = screenerRowIdentityKey(next);
        if (!identityKey) continue;
        const prior = previousByKey.get(identityKey);
        const row = mode === "http" && prior
          ? { ...prior, ...next, key: prior.key ?? next?.key, display: next?.display ?? prior.display }
          : next;
        const priorUpdatedAt = Number(prior?._updated_at || 0);
        const priorFresh = priorUpdatedAt > 0 && now - priorUpdatedAt <= ttlMs;
        if (mode === "http" && !options.force && rowHasLiveQuote(prior) && priorFresh && !rowHasLiveQuote(row)) {
          merged.push(prior);
          mergedKeys.add(identityKey);
          continue;
        }
        merged.push({ ...row, _updated_at: now });
        mergedKeys.add(identityKey);
      }
      for (const [identityKey, prior] of previousByKey.entries()) {
        const priorUpdatedAt = Number(prior?._updated_at || 0);
        const preserveUntouched = mode === "delta"
          || (mode === "snapshot" && !scopeKeys.has(identityKey))
          || (mode === "http" && priorUpdatedAt > 0 && now - priorUpdatedAt <= ttlMs);
        if (!mergedKeys.has(identityKey) && preserveUntouched) {
          merged.push(prior);
          mergedKeys.add(identityKey);
        }
      }
      const mergedByIdentity = screenerRowMap(merged);
      state.screener = merged;
      state.screenerByIdentity = mergedByIdentity;
      return merged;
    }

    function instrumentRouteFingerprint(instrument = undefined) {
      if (instrument === null) return "";
      const resolved = exactIdentityText(instrument?.route_fingerprint)
        ? instrument
        : exactIdentityText(instrument?.instrument_id)
          ? instrumentForId(instrument.instrument_id)
          : instrument === undefined ? instrumentForId() : null;
      return exactIdentityText(resolved?.route_fingerprint);
    }

    function rowMatchesCurrentRoute(row = {}) {
      const currentFingerprint = instrumentRouteFingerprint();
      const currentInstrumentId = exactIdentityText(state.instrumentId);
      return Boolean(
        currentInstrumentId
        && currentFingerprint
        && screenerRowIdentityKey(row) === screenerRowIdentityKey({
          instrument_id: currentInstrumentId,
          route_fingerprint: currentFingerprint,
        })
      );
    }

    function chartStreamOpen() {
      const sharedFresh = !state.transport.chart.socket
        && Boolean(state.transport.chart.key)
        && Date.now() - Number(state.transport.chart.lastBarsAt || state.transport.chart.openedAt || 0) < CHART_STREAM_STALE_MS;
      return Boolean((state.transport.chart.socket && state.transport.chart.socket.readyState === WebSocket.OPEN) || sharedFresh);
    }

    function quoteRowsIncludeCurrentRoute(rows = []) {
      return (rows || []).some(row => rowMatchesCurrentRoute(row));
    }

    function currentQuoteRow() {
      const identityKey = screenerRowIdentityKey({
        instrument_id: exactIdentityText(state.instrumentId),
        route_fingerprint: instrumentRouteFingerprint(),
      });
      return identityKey
        ? screenerRowMap(state.screener || []).get(identityKey) || null
        : null;
    }

    function syncLiveQuoteFromScreenerRow(row) {
      const payload = liveQuotePayloadFromScreenerRow(row, {
        allowStale: true,
        allowDelayed: true,
      });
      if (!payload) {
        invalidateLiveQuoteFromScreenerRow(row);
        return false;
      }
      const price = Number(payload.price);
      if (!Number.isFinite(price)) return false;
      state.liveQuote = payload;
      state.lastQuoteAt = Date.now();
      state.lastPrice = price;
      return true;
    }

    function workspaceQuoteInstrumentIds() {
      return WORKSPACE_SLOTS
        .map(slot => exactIdentityText(workspaceStoredValue("instrumentId", slot)))
        .filter(Boolean);
    }

    function handleQuoteSnapshotRows(rows, label, options = {}) {
      state.transport.quote.lastMessageAt = Date.now();
      const priorRowsByIdentity = options.mode === "delta" && window.mcPerfEnabled?.()
        ? screenerRowMap(state.screener || [])
        : null;
      const priorReceivedAtByIdentity = priorRowsByIdentity
        ? new Map(
            rows.map(row => {
              const identityKey = screenerRowIdentityKey(row);
              const prior = identityKey
                ? priorRowsByIdentity.get(identityKey)
                : null;
              return [identityKey, prior?.quote_received_at || null];
            }),
          )
        : null;
      const mergePerf = window.mcPerfStart ? window.mcPerfStart("quoteMerge") : null;
      const mergedRows = mergeScreenerRows(rows, options);
      if (window.mcPerfEnd) window.mcPerfEnd(mergePerf, `${options.mode || "http"}:${rows.length}`, 8);
      if (mergedRows === null) {
        return {
          admitted: false,
          currentRouteObserved: false,
        };
      }
      if (priorReceivedAtByIdentity) {
        let earliestReceivedMs = 0;
        const receivedNowMs = Date.now();
        for (const row of rows) {
          const identityKey = screenerRowIdentityKey(row);
          const nextReceivedAt = row?.quote_received_at || null;
          if (
            !identityKey
            || !nextReceivedAt
            || nextReceivedAt === priorReceivedAtByIdentity.get(identityKey)
          ) continue;
          const receivedMs = Date.parse(nextReceivedAt);
          if (!Number.isFinite(receivedMs) || receivedMs > receivedNowMs) continue;
          earliestReceivedMs = earliestReceivedMs > 0
            ? Math.min(earliestReceivedMs, receivedMs)
            : receivedMs;
        }
        if (earliestReceivedMs > 0) {
          const pendingGatewayMs = Number(state.transport.quote.gatewayPaintAtMs || 0);
          state.transport.quote.gatewayPaintAtMs = pendingGatewayMs > 0
            ? Math.min(pendingGatewayMs, earliestReceivedMs)
            : earliestReceivedMs;
        }
      }
      const hasCurrentRoute = quoteRowsIncludeCurrentRoute(rows);
      const shouldRenderChart = options.renderChart === true;
      const chartPatched = shouldRenderChart ? applyCurrentScreenerQuote() : false;
      const quoteSynced = shouldRenderChart ? false : syncLiveQuoteFromScreenerRow(currentQuoteRow());
      updateDocumentTitleFromScreenerQuote();
      if (!chartPatched) {
        if (!state.transport.quote.commitPerf && options.commitPerf) state.transport.quote.commitPerf = options.commitPerf;
        if (!state.transport.quote.paintPerf && options.paintPerf) state.transport.quote.paintPerf = options.paintPerf;
        renderInstruments({
          dirtyIdentityKeys: new Set(rows.map(row => screenerRowIdentityKey(row)).filter(Boolean)),
        });
      }
      setScreenerStateLabel(label);
      return {
        admitted: true,
        currentRouteObserved: Boolean(chartPatched || quoteSynced || hasCurrentRoute),
      };
    }

    function snapshotMatchesCurrentRoute(snapshot = state.snapshot) {
      if (!snapshot?.meta) return false;
      const snapshotInstrumentId = exactIdentityText(snapshot.meta.instrument_id);
      const snapshotFingerprint = exactIdentityText(snapshot.meta.route_fingerprint);
      const currentFingerprint = instrumentRouteFingerprint();
      const snapshotTimeframe = String(snapshot.meta.timeframe ?? "");
      const currentTimeframe = String(state.timeframe ?? "");
      return Boolean(
        snapshotInstrumentId
        && snapshotInstrumentId === exactIdentityText(state.instrumentId)
        && snapshotFingerprint
        && currentFingerprint
        && snapshotFingerprint === currentFingerprint
        && snapshotTimeframe === currentTimeframe
      );
    }

    function clearChartStateForRouteSwitch() {
      resetHistoryDemand();
      closeChartStream();
      abortMarketAnalysis();
      releaseMarketAnalysisLease();
      state.snapshot = null;
      state.chartBarIndex = new Map();
      state.chartBarIndexBars = null;
      state.chartBarIndexLength = 0;
      state.liveQuote = null;
      state.chartBarQueue = {
        key: "",
        barsByTs: new Map(),
        anonymous: [],
        flushScheduled: false,
        suppressAnalysis: true,
        chartDataQuality: null,
        historyCoverage: null,
        gapRepair: null,
        futureAxis: null,
        futureAxisCanonicalRevision: 0,
        chartQualityRevision: 0,
        canonicalRevision: 0,
        expectedLiveSlot: undefined,
        expectedLiveClose: undefined,
      };
      clearUiNotices({ routeScopedOnly: true });
      state.lastPrice = null;
      state.lastHeaderRenderAt = 0;
      state.lastHeaderText = "";
      state.lastHeaderTitle = "";
      state.lastHeaderStableKey = "";
      state.lastDocumentTitleKey = "";
      lastPanelStableKey = "";
      if (typeof syncIndicatorModuleLifecycles === "function") {
        void syncIndicatorModuleLifecycles({
          reset: true,
          reason: "route-switch",
        });
      }
      state.alerts.drag = null;
      state.crosshair.visible = false;
      if (state.priceScale) state.priceScale.percentHover = false;
      const source = String(instrumentForId()?.provider || "").trim().toLowerCase();
      const symbol = state.symbol;
      document.title = `${symbol} loading`;
      const title = document.getElementById("chart-title");
      if (title) title.textContent = `${symbol} ${timeframeLabel(state.timeframe)} · loading ${source.toUpperCase()}`;
      renderAlertManager();
      renderLoadingCanvases(`${symbol} via ${source.toUpperCase()}`);
    }
