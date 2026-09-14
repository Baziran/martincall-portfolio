    const streamTextEncoder = typeof TextEncoder === "function" ? new TextEncoder() : null;
    const WATCHLIST_TREND_WINDOW_MINUTES = 180;
    const WATCHLIST_TREND_BUCKET_MINUTES = 5;
    const WATCHLIST_TREND_CACHE_TTL_MS = 5 * 60 * 1000;
    const WATCHLIST_TREND_SHARED_FETCH_TTL_MS = 1500;
    const WATCHLIST_TREND_SOURCE = "quote_snapshots";
    const WATCHLIST_TREND_STATUSES = new Set([
      "ready",
      "insufficient_data",
      "storage_unavailable",
      "storage_error",
    ]);

    function watchlistTrendRoutes(instrumentIds = null) {
      const routes = new Map();
      for (const item of state.instruments || []) {
        const instrumentId = exactIdentityText(item?.instrument_id);
        const routeFingerprint = exactIdentityText(item?.route_fingerprint);
        if (
          !instrumentId
          || !routeFingerprint
          || (instrumentIds instanceof Set && !instrumentIds.has(instrumentId))
          || watchlistDisplayMode(instrumentId) !== "trend"
        ) continue;
        const route = { instrument_id: instrumentId, route_fingerprint: routeFingerprint };
        routes.set(screenerRowIdentityKey(route), route);
      }
      return Array.from(routes.entries())
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([, route]) => route);
    }

    function applyWatchlistTrendSnapshot(payload, routes, receivedAt = Date.now()) {
      if (
        !payload
        || typeof payload !== "object"
        || Array.isArray(payload)
        || payload.window_minutes !== WATCHLIST_TREND_WINDOW_MINUTES
        || payload.bucket_minutes !== WATCHLIST_TREND_BUCKET_MINUTES
        || !Array.isArray(payload.rows)
        || payload.rows.length !== routes.length
      ) throw new Error("watchlist trends returned an invalid snapshot");
      const expectedRoutes = new Set(routes.map(route => screenerRowIdentityKey(route)));
      const receivedRoutes = new Set();
      const nextEntries = [];
      for (const row of payload.rows) {
        const identityKey = screenerRowIdentityKey(row);
        if (!identityKey || !expectedRoutes.has(identityKey) || receivedRoutes.has(identityKey)) {
          throw new Error("watchlist trends returned an unexpected exact route");
        }
        if (
          !WATCHLIST_TREND_STATUSES.has(row.status)
          || row.source !== WATCHLIST_TREND_SOURCE
          || !Array.isArray(row.points)
          || row.points.length > WATCHLIST_TREND_WINDOW_MINUTES / WATCHLIST_TREND_BUCKET_MINUTES
          || !(row.as_of === null || (typeof row.as_of === "string" && row.as_of))
          || row.points.some(point => (
            !point
            || typeof point !== "object"
            || Array.isArray(point)
            || typeof point.ts !== "string"
            || !point.ts
            || typeof point.price !== "number"
            || !Number.isFinite(point.price)
          ))
        ) throw new Error("watchlist trends returned an invalid row");
        receivedRoutes.add(identityKey);
        nextEntries.push([
          identityKey,
          {
            instrument_id: exactIdentityText(row.instrument_id),
            route_fingerprint: exactIdentityText(row.route_fingerprint),
            status: row.status,
            source: row.source,
            as_of: row.as_of,
            points: row.points,
            window_minutes: payload.window_minutes,
            bucket_minutes: payload.bucket_minutes,
            _updated_at: receivedAt,
            _bucket: Math.floor(receivedAt / WATCHLIST_TREND_CACHE_TTL_MS),
          },
        ]);
      }
      if (receivedRoutes.size !== expectedRoutes.size) {
        throw new Error("watchlist trends returned an incomplete exact-route snapshot");
      }
      nextEntries.forEach(([identityKey, entry]) => {
        const current = state.watchlistTrendsByIdentity.get(identityKey);
        const currentAsOf = Date.parse(current?.as_of || "");
        const nextAsOf = Date.parse(entry.as_of || "");
        if (Number.isFinite(currentAsOf) && Number.isFinite(nextAsOf) && nextAsOf < currentAsOf) return;
        state.watchlistTrendsByIdentity.set(identityKey, entry);
      });
      renderInstruments({ dirtyIdentityKeys: new Set(receivedRoutes) });
      return receivedRoutes;
    }

    function queueSharedWatchlistTrendMessage(message) {
      const routes = Array.isArray(message.scope?.routes) ? message.scope.routes : [];
      const canonicalRoutes = routes.map(route => ({
        instrument_id: exactIdentityText(route?.instrument_id),
        route_fingerprint: exactIdentityText(route?.route_fingerprint),
      }));
      if (
        !canonicalRoutes.length
        || canonicalRoutes.some((route, index) => (
          !route.instrument_id
          || !route.route_fingerprint
          || Object.keys(routes[index] || {}).length !== 2
        ))
        || JSON.stringify(canonicalRoutes) !== JSON.stringify(routes)
        || new Set(canonicalRoutes.map(screenerRowIdentityKey)).size !== canonicalRoutes.length
      ) return;
      const currentRoutes = new Set(watchlistTrendRoutes().map(screenerRowIdentityKey));
      if (canonicalRoutes.some(route => !currentRoutes.has(screenerRowIdentityKey(route)))) return;
      if (message.type === "read_model_requested") {
        const requestOwnerKey = JSON.stringify(["watchlist-trends", JSON.stringify(canonicalRoutes)]);
        const currentBucket = Math.floor(Date.now() / WATCHLIST_TREND_CACHE_TTL_MS);
        const cachedRows = canonicalRoutes.map(route => state.watchlistTrendsByIdentity.get(
          screenerRowIdentityKey(route),
        ));
        if (cachedRows.every(entry => entry && entry._bucket === currentBucket)) {
          shareReadModel("watchlist_trends", message.scope, {
            window_minutes: WATCHLIST_TREND_WINDOW_MINUTES,
            bucket_minutes: WATCHLIST_TREND_BUCKET_MINUTES,
            rows: cachedRows.map(entry => ({
              instrument_id: entry.instrument_id,
              route_fingerprint: entry.route_fingerprint,
              status: entry.status,
              source: entry.source,
              as_of: entry.as_of,
              points: entry.points,
            })),
          });
          return;
        }
        const ownsReadModel = acquireSharedStreamOwner("read-model", requestOwnerKey);
        if (ownsReadModel) {
          loadWatchlistTrends({
            force: true,
            instrumentIds: new Set(canonicalRoutes.map(route => route.instrument_id)),
            sharedRequest: true,
          });
        }
        return;
      }
      if (message.type !== "read_model_snapshot") return;
      try {
        applyWatchlistTrendSnapshot(message.payload, canonicalRoutes);
        if (window.mcTelemetryInc) window.mcTelemetryInc("watchlist_trends.applied");
      } catch (error) {
        if (window.mcTelemetryInc) window.mcTelemetryInc("watchlist_trends.apply_error");
        debugStep("watchlist trends", requestErrorMessage(error, "shared snapshot rejected"));
      }
    }

    async function loadWatchlistTrends(options = {}) {
      const requestedInstrumentIds = options.instrumentIds instanceof Set
        ? new Set(options.instrumentIds)
        : null;
      const candidateRoutes = watchlistTrendRoutes(requestedInstrumentIds);
      if (!candidateRoutes.length) return;
      const now = Date.now();
      const currentBucket = Math.floor(now / WATCHLIST_TREND_CACHE_TTL_MS);
      const peerRoutes = candidateRoutes.filter(route => {
        const cached = state.watchlistTrendsByIdentity.get(screenerRowIdentityKey(route));
        return !cached || cached._bucket !== currentBucket;
      });
      const peerRequested = Boolean(
        options.sharedRequest !== true
        && windowLinkChannel
        && peerRoutes.length
      );
      if (peerRequested) {
        shareReadModel("watchlist_trends", { routes: peerRoutes });
        await new Promise(resolve => window.setTimeout(resolve, WINDOW_LINK_READ_MODEL_RESPONSE_MS));
      }
      const routes = options.force && !peerRequested
        ? candidateRoutes
        : candidateRoutes.filter(route => {
            const cached = state.watchlistTrendsByIdentity.get(screenerRowIdentityKey(route));
            return !cached || cached._bucket !== Math.floor(Date.now() / WATCHLIST_TREND_CACHE_TTL_MS);
          });
      if (!routes.length) return;

      const requestRouteKey = JSON.stringify(routes);
      const requestOwnerKey = JSON.stringify(["watchlist-trends", requestRouteKey]);
      if (!acquireSharedStreamOwner("read-model", requestOwnerKey)) {
        shareReadModel("watchlist_trends", { routes });
        return;
      }
      if (state.requests.watchlistTrends.loading && state.requests.watchlistTrends.key === requestOwnerKey) return;
      state.requests.watchlistTrends.loading = true;
      state.requests.watchlistTrends.key = requestOwnerKey;
      try {
        const params = new URLSearchParams({ routes: JSON.stringify(routes) });
        debugStep("watchlist trends request", params.toString());
        const payload = await fetchJson(`/api/screener/trends?${params.toString()}`, {
          sharedTtlMs: WATCHLIST_TREND_SHARED_FETCH_TTL_MS,
        });
        if (state.requests.watchlistTrends.key !== requestOwnerKey) return;
        if (JSON.stringify(watchlistTrendRoutes(requestedInstrumentIds).filter(route => (
          routes.some(expected => screenerRowIdentityKey(expected) === screenerRowIdentityKey(route))
        ))) !== requestRouteKey) return;

        const receivedRoutes = applyWatchlistTrendSnapshot(payload, routes);
        shareReadModel("watchlist_trends", { routes }, payload);
        clearUiNotice("watchlist_trends");
        debugStep("watchlist trends response", `${receivedRoutes.size} rows`);
      } catch (error) {
        if (state.requests.watchlistTrends.key !== requestOwnerKey) return;
        if (
          requestErrorCode(error) === "ROUTE_SELECTION_MISMATCH"
          && options.routeResyncAttempted !== true
        ) {
          setUiNotice(
            "watchlist_trends",
            "ROUTE_SELECTION_MISMATCH",
            "Watchlist route changed. Refreshing qualified routes...",
            { state: "stale", routeScoped: false },
          );
          try {
            await loadInstruments({ activate: false });
          } catch (routeError) {
            console.warn("watchlist trend route resync failed", routeError);
          }
          window.setTimeout(() => loadWatchlistTrends({
            ...options,
            force: true,
            routeResyncAttempted: true,
          }), 0);
          return;
        }
        const message = requestErrorMessage(error, "watchlist trend refresh failed");
        console.warn("watchlist trend refresh failed", message);
        debugStep("watchlist trends error", message);
      } finally {
        if (state.requests.watchlistTrends.key === requestOwnerKey) {
          state.requests.watchlistTrends.loading = false;
        }
      }
    }

    async function loadScreener(options = {}) {
      loadWatchlistTrends();
      const stateNode = document.getElementById("screener-state");
      const currentInstrument = instrumentForId();
      if (!currentInstrument) return;
      state.dataSource = String(currentInstrument.provider || "").trim().toLowerCase();
      if (state.serverSleeping) {
        closeQuoteStream();
        if (stateNode) stateNode.textContent = "sleep";
        debugStep("screener skipped", "server sleeping");
        return;
      }
      const streamIsStale = quoteStreamStale();
      if (!options.allowWhenStream && quoteStreamOpen() && !streamIsStale) {
        debugStep("screener skipped", "quote stream is active");
        setScreenerStateLabel();
        return;
      }
      if (streamIsStale) {
        debugStep("quote stream stale", `${Math.round((Date.now() - Number(state.transport.quote.lastStateAt || state.transport.quote.openedAt || 0)) / 1000)}s without quote state`);
        if (stateNode) stateNode.textContent = "stale";
        closeQuoteStream();
      }
      const currentFingerprint = instrumentRouteFingerprint();
      const routeEntries = (state.instruments || [])
        .map(item => [exactIdentityText(item?.instrument_id), exactIdentityText(item?.route_fingerprint)])
        .filter(([instrumentId, routeFingerprint]) => instrumentId && routeFingerprint);
      const routes = Array.from(new Map(
        routeEntries.map(route => [JSON.stringify(route), route]),
      ).values());
      const requestInstrumentId = state.instrumentId;
      const requestTimeframe = state.timeframe;
      const requestRouteKey = JSON.stringify(routes);
      const requestOwnerKey = JSON.stringify([requestInstrumentId, currentFingerprint, requestTimeframe, routes]);
      if (state.requests.screener.loading && state.requests.screener.key === requestOwnerKey) {
        debugStep("screener skipped", "previous request is still running");
        return;
      }
      state.requests.screener.loading = true;
      state.requests.screener.key = requestOwnerKey;
      setScreenerStateLabel();
      try {
        const expectedRoutes = new Set(routes.map(route => JSON.stringify(route)));
        const routePayload = routes.map(([instrumentId, routeFingerprint]) => ({
          instrument_id: instrumentId,
          route_fingerprint: routeFingerprint,
        }));
        const params = new URLSearchParams({
          routes: JSON.stringify(routePayload),
          interval: requestTimeframe,
        });
        debugStep("screener request", params.toString());
        const payload = await fetchJson(`/api/screener?${params.toString()}`);
        const rows = payload?.rows;
        if (
          !payload
          || typeof payload !== "object"
          || Array.isArray(payload)
          || !Array.isArray(rows)
          || !exactIdentityText(payload.cache_epoch)
          || !Number.isSafeInteger(payload.cache_generation)
          || payload.cache_generation < 0
        ) throw new Error(apiErrorMessage(payload, "screener returned an invalid cache revision"));
        const currentRouteEntries = (state.instruments || [])
          .map(item => [exactIdentityText(item?.instrument_id), exactIdentityText(item?.route_fingerprint)])
          .filter(([instrumentId, routeFingerprint]) => instrumentId && routeFingerprint);
        const currentRoutes = Array.from(new Map(
          currentRouteEntries.map(route => [JSON.stringify(route), route]),
        ).values());
        if (
          state.instrumentId !== requestInstrumentId
          || state.timeframe !== requestTimeframe
          || instrumentRouteFingerprint() !== currentFingerprint
          || JSON.stringify(currentRoutes) !== requestRouteKey
        ) return;
        if (rows.some(row => !expectedRoutes.has(
          JSON.stringify([exactIdentityText(row?.instrument_id), exactIdentityText(row?.route_fingerprint)]),
        ))) {
          throw new Error("screener returned an unexpected route_fingerprint");
        }
        const mergedRows = mergeScreenerRows(rows, {
          mode: "http",
          cacheEpoch: payload.cache_epoch,
          cacheGeneration: payload.cache_generation,
          allowEpochChange: !quoteStreamOpen(),
        });
        if (mergedRows === null) {
          debugStep("screener response", "superseded by a newer quote cache revision");
          setScreenerStateLabel();
          return;
        }
        state.screenerStatus = { ok: true, message: "", rowCount: rows.length, updatedAt: Date.now() };
        clearUiNotice("screener");
        debugStep("screener response", `${state.screener.length} rows`);
        state.screenerChartPatched = applyCurrentScreenerQuote();
        updateDocumentTitleFromScreenerQuote();
      } catch (error) {
        if (state.requests.screener.key !== requestOwnerKey) return;
        if (
          requestErrorCode(error) === "ROUTE_SELECTION_MISMATCH"
          && options.routeResyncAttempted !== true
        ) {
          setUiNotice(
            "screener",
            "ROUTE_SELECTION_MISMATCH",
            "Watchlist route changed. Refreshing qualified routes...",
            { state: "stale", routeScoped: false },
          );
          try {
            await loadInstruments({ activate: false });
          } catch (routeError) {
            console.warn("screener route resync failed", routeError);
          }
          window.setTimeout(() => loadScreener({
            ...options,
            allowWhenStream: true,
            routeResyncAttempted: true,
          }), 0);
          return;
        }
        const message = requestErrorMessage(error, "screener refresh failed");
        state.screenerStatus = { ok: false, message, updatedAt: Date.now() };
        setUiNotice("screener", "SCREENER_REFRESH_FAILED", `Screener error: ${message}`, {
          state: "error",
          routeScoped: false,
        });
        if (stateNode) {
          stateNode.textContent = "error";
          stateNode.title = message;
        }
        console.warn("screener refresh failed", message);
        debugStep("screener error", message);
      } finally {
        if (state.requests.screener.key === requestOwnerKey) state.requests.screener.loading = false;
      }
      setScreenerStateLabel();
      if (!state.screenerChartPatched) renderInstruments();
      state.screenerChartPatched = false;
      if (streamIsStale && document.visibilityState === "visible" && !state.serverSleeping) {
        connectQuoteStream(true);
      }
    }

    function quoteStreamRoutes() {
      const routes = new Map();
      const addLiveQuoteInstrument = instrumentId => {
        const identity = exactIdentityText(instrumentId);
        if (!identity) return;
        const instrument = instrumentForId(identity);
        if (!instrument) return;
        const source = String(instrument.provider || "").trim().toLowerCase();
        if (!providerCapability(source, "live_quote_stream") && !providerCapability(source, "live_quote_polling")) return;
        const routeFingerprint = exactIdentityText(instrument.route_fingerprint);
        if (routeFingerprint) {
          const route = [identity, routeFingerprint];
          routes.set(JSON.stringify(route), route);
        }
      };
      addLiveQuoteInstrument(state.instrumentId);
      state.instruments.forEach(item => {
        addLiveQuoteInstrument(item?.instrument_id);
      });
      workspaceQuoteInstrumentIds().forEach(instrumentId => {
        addLiveQuoteInstrument(instrumentId);
      });
      return Array.from(routes.values()).sort(([leftId, leftRoute], [rightId, rightRoute]) => (
        leftId.localeCompare(rightId) || leftRoute.localeCompare(rightRoute)
      ));
    }

    let quoteRouteRefreshTimer = null;

    function closeWebSocketQuietly(socket, reason = "client close") {
      if (!socket) return;
      try {
        socket.onmessage = null;
        socket.onerror = null;
        socket.onclose = null;
        if (socket.sharedQuoteTransport) {
          socket.onopen = null;
          socket.close(1000, reason);
          return;
        }
        if (socket.readyState === WebSocket.CONNECTING) {
          socket.onopen = () => {
            try {
              socket.close(1000, reason);
            } catch (_) {
              // noop
            }
          };
          return;
        }
        socket.onopen = null;
        if (socket.readyState === WebSocket.OPEN) socket.close(1000, reason);
      } catch (_) {
        // noop
      }
    }

    function closeQuoteStream(options = {}) {
      window.clearTimeout(quoteRouteRefreshTimer);
      quoteRouteRefreshTimer = null;
      window.clearTimeout(state.transport.quote.retryTimer);
      state.transport.quote.retryTimer = null;
      window.clearTimeout(state.liveQuoteAnalysisRefreshTimer);
      state.liveQuoteAnalysisRefreshTimer = null;
      state.liveQuoteAnalysisRefreshKey = "";
      closeWebSocketQuietly(state.transport.quote.socket, "quote stream close");
      state.transport.quote.socket = null;
      state.transport.quote.key = "";
      state.transport.quote.openedAt = 0;
      state.transport.quote.lastMessageAt = 0;
      state.transport.quote.lastStateAt = 0;
      state.transport.quote.candleSequence = 0;
      state.transport.quote.stateSequence = 0;
      state.transport.quote.stateReady = false;
      state.transport.quote.alertRuntimeReady = false;
      state.transport.quote.alertRuntimeWarning = "";
      state.alerts.runtimeByScope.clear();
      state.transport.quote.degradedWarning = "";
      state.transport.quote.errorCode = "";
      state.transport.quote.errorMessage = "";
      state.transport.quote.commitPerf = null;
      state.transport.quote.epoch = Number(state.transport.quote.epoch || 0) + 1;
    }

    function connectQuoteStream(force = false) {
      if (state.serverSleeping) {
        closeQuoteStream();
        return;
      }
      const stateNode = document.getElementById("screener-state");
      const requestedRoutes = quoteStreamRoutes();
      if (!requestedRoutes.length) return;
      const requestedRouteSet = new Set(
        requestedRoutes.map(route => JSON.stringify(route)),
      );
      const requestTimeframe = String(state.timeframe || "");
      const routePayload = requestedRoutes.map(([instrumentId, routeFingerprint]) => ({
        instrument_id: instrumentId,
        route_fingerprint: routeFingerprint,
      }));
      const key = JSON.stringify([requestedRoutes, requestTimeframe]);
      if (state.transport.quote.key === key && state.transport.quote.socket) {
        if (state.transport.quote.socket.readyState === WebSocket.CONNECTING) return;
        if (state.transport.quote.socket.readyState === WebSocket.OPEN && !quoteStreamStale()) return;
      }
      const requestInstrumentId = exactIdentityText(state.instrumentId);
      const requestRouteFingerprint = instrumentRouteFingerprint();
      const requestRouteIncluded = requestedRouteSet.has(JSON.stringify([
        requestInstrumentId,
        requestRouteFingerprint,
      ]));
      closeQuoteStream();
      if (typeof clearFastIndicatorSnapshotsForRoutes === "function") {
        clearFastIndicatorSnapshotsForRoutes(requestedRouteSet, requestTimeframe);
      }
      const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      const params = new URLSearchParams({
        routes: JSON.stringify(routePayload),
        interval: requestTimeframe,
      });
      let socket;
      try {
        socket = createQuoteStreamTransport(
          key,
          `${protocol}//${window.location.host}/ws/quotes?${params.toString()}`,
          { interval: requestTimeframe, routes: routePayload },
        );
      } catch (error) {
        const errorCode = String(error?.code || "QUOTE_SHARED_WORKER_UNAVAILABLE");
        const errorMessage = requestErrorMessage(error, "shared quote transport unavailable");
        state.transport.quote.errorCode = errorCode;
        state.transport.quote.errorMessage = errorMessage;
        if (window.mcTelemetrySet) {
          window.mcTelemetrySet("quote_ws.last_error_code", errorCode);
          window.mcTelemetrySet("quote_ws.last_error_reason", errorMessage);
          window.mcTelemetrySet("quote_ws.last_error_fatal", true);
        }
        debugStep("quote stream error", `${errorCode} fatal · ${errorMessage}`);
        setUiNotice("screener", errorCode, errorMessage, {
          state: "error",
          routeScoped: false,
        });
        setScreenerStateLabel("error");
        return;
      }
      state.transport.quote.socket = socket;
      state.transport.quote.key = key;
      state.transport.quote.openedAt = Date.now();
      state.transport.quote.lastMessageAt = 0;
      state.transport.quote.lastStateAt = 0;
      state.transport.quote.candleSequence = 0;
      state.transport.quote.stateSequence = 0;
      state.transport.quote.stateReady = false;
      state.transport.quote.alertRuntimeReady = false;
      state.transport.quote.alertRuntimeWarning = "";
      state.transport.quote.degradedWarning = "";
      setScreenerStateLabel("connecting");
      const pageScopeMatchesRequest = () => (
        exactIdentityText(state.instrumentId) === requestInstrumentId
        && instrumentRouteFingerprint() === requestRouteFingerprint
        && String(state.timeframe || "") === requestTimeframe
      );
      const resyncQuoteState = reason => {
        if (state.transport.quote.socket !== socket) return;
        if (!pageScopeMatchesRequest()) {
          if (window.mcTelemetryInc) window.mcTelemetryInc("quote_ws.local_scope_detach");
          debugStep("quote stream detach", "page scope changed before resync");
          socket.close(1000, "quote page scope changed");
          return;
        }
        state.transport.quote.stateReady = false;
        state.transport.quote.alertRuntimeReady = false;
        state.transport.quote.alertRuntimeWarning = "";
        state.alerts.runtimeByScope.clear();
        state.transport.quote.stateSequence = 0;
        if (window.mcTelemetryInc) window.mcTelemetryInc("quote_ws.resync");
        debugStep("quote stream resync", reason);
        try {
          socket.close(4002, "quote state resync");
        } catch (_) {
          // onclose owns the reconnect.
        }
      };
      socket.onopen = () => {
        if (state.transport.quote.socket !== socket || state.transport.quote.key !== key) return;
        if (!pageScopeMatchesRequest()) {
          if (window.mcTelemetryInc) window.mcTelemetryInc("quote_ws.local_scope_detach");
          debugStep("quote stream detach", "page scope changed before open");
          socket.close(1000, "quote page scope changed");
          return;
        }
        debugStep("quote stream", `open ${requestedRoutes.length} routes`);
        state.transport.quote.errorCode = "";
        state.transport.quote.errorMessage = "";
        state.transport.quote.openedAt = Date.now();
        setScreenerStateLabel();
      };
      const applyStreamOptionTargets = (optionTargets, reason) => {
        const exactIdentityKeys = new Set();
        if (
          !Array.isArray(optionTargets)
          || optionTargets.some(item => {
            const identityKey = optionTargetIdentityKey(item);
            if (
              !item
              || typeof item !== "object"
              || Array.isArray(item)
              || !identityKey
              || exactIdentityKeys.has(identityKey)
              || !requestedRouteSet.has(JSON.stringify([
                exactIdentityText(item?.instrument_id),
                exactIdentityText(item?.route_fingerprint),
              ]))
              || String(item?.timeframe || "") !== requestTimeframe
            ) return true;
            exactIdentityKeys.add(identityKey);
            return false;
          })
        ) {
          resyncQuoteState("option target exact-route contract mismatch");
          return false;
        }
        applyOptionTargetsFromServer(optionTargets, {
          reason,
          scopeKeys: [...requestedRouteSet],
        });
        return true;
      };
      const applyStreamFastIndicators = (scopes, reason) => {
        try {
          applyFastIndicatorSnapshots(scopes, {
            reason,
            requestedRouteSet,
            timeframe: requestTimeframe,
          });
          return true;
        } catch (error) {
          resyncQuoteState(requestErrorMessage(error, "fast indicator contract mismatch"));
          return false;
        }
      };
      const applyStreamPriceAlertRuntime = (runtimeRows, reason) => {
        if (
          !Array.isArray(runtimeRows)
          || runtimeRows.some(item => (
            !item
            || typeof item !== "object"
            || Array.isArray(item)
            || typeof item.id !== "string"
            || !item.id
            || !requestedRouteSet.has(JSON.stringify([
              exactIdentityText(item.instrument_id),
              exactIdentityText(item.route_fingerprint),
            ]))
            || String(item.timeframe || "") !== requestTimeframe
          ))
        ) {
          resyncQuoteState("price alert runtime exact-route contract mismatch");
          return false;
        }
        const rowIds = runtimeRows.map(item => item.id);
        if (new Set(rowIds).size !== rowIds.length) {
          resyncQuoteState("price alert runtime ids must be unique");
          return false;
        }
        if (requestRouteIncluded) {
          state.transport.quote.alertRuntimeReady = true;
          if (state.transport.quote.alertRuntimeWarning) return true;
          const activeRows = runtimeRows.filter(item => (
            exactIdentityText(item.instrument_id) === requestInstrumentId
            && exactIdentityText(item.route_fingerprint) === requestRouteFingerprint
          ));
          const changed = applyServerPriceAlerts(activeRows, {
            force: true,
            instrumentId: requestInstrumentId,
            routeFingerprint: requestRouteFingerprint,
            scopeTimeframe: requestTimeframe,
            runtimeOnly: true,
          });
          if (changed) requestObjectLayerRedraw(reason);
        }
        return true;
      };
      socket.onmessage = event => {
        const quoteCommitPerf = window.mcPerfStart ? window.mcPerfStart("quoteToCommit") : null;
        const quotePaintPerf = window.mcPerfStart ? window.mcPerfStart("quoteToPaint") : null;
        const parsePerf = window.mcPerfStart ? window.mcPerfStart("quoteWsParse") : null;
        let parsed = false;
        try {
          if (state.transport.quote.socket !== socket || state.transport.quote.key !== key) return;
          if (!pageScopeMatchesRequest()) {
            if (window.mcTelemetryInc) window.mcTelemetryInc("quote_ws.local_scope_detach");
            debugStep("quote stream detach", "page scope changed while fixed transport was active");
            socket.close(1000, "quote page scope changed");
            return;
          }
          const message = JSON.parse(event.data);
          parsed = true;
          if (window.mcPerfEnd) window.mcPerfEnd(parsePerf, String(message.type || "unknown"), 4);
          if (window.mcPerfEnabled?.() && streamTextEncoder && window.mcTelemetryMax) {
            const payloadBytes = streamTextEncoder.encode(String(event.data || "")).byteLength;
            window.mcTelemetryMax("quote_ws.payload_bytes_max", payloadBytes);
            if (window.mcTelemetryInc) window.mcTelemetryInc("quote_ws.payload_samples");
          }
          if (window.mcTelemetryInc) window.mcTelemetryInc(`quote_ws.message:${String(message.type || "unknown")}`);
          state.transport.quote.lastMessageAt = Date.now();
          if (message.type === "quote_status") {
            setScreenerStateLabel("retry");
            debugStep("quote stream status", message.message || "retrying");
            if (message.requires_resubscribe) {
              window.clearTimeout(quoteRouteRefreshTimer);
              const routeRefreshTimer = window.setTimeout(async () => {
                if (quoteRouteRefreshTimer !== routeRefreshTimer) return;
                quoteRouteRefreshTimer = null;
                if (!pageScopeMatchesRequest()) return;
                try {
                  if (typeof loadInstruments === "function") await loadInstruments();
                } catch (error) {
                  debugStep("quote route refresh", requestErrorMessage(error, "watchlist refresh failed"));
                  if (typeof showRuntimeError === "function") showRuntimeError(error, "quoteRouteRefresh");
                }
              }, 0);
              quoteRouteRefreshTimer = routeRefreshTimer;
              try {
                socket.close(4001, "quote route changed");
              } catch (_) {
                // noop
              }
            }
            return;
          }
          if (message.type === "live_candle_delta") {
            const sequence = message.sequence;
            const lossAfterSequence = message.loss_after_sequence;
            if (
              message.interval !== requestTimeframe
              || !Number.isSafeInteger(sequence)
              || sequence <= 0
              || sequence <= state.transport.quote.candleSequence
              || (
                lossAfterSequence !== null
                && lossAfterSequence !== undefined
                && (
                  !Number.isSafeInteger(lossAfterSequence)
                  || lossAfterSequence <= 0
                  || lossAfterSequence > sequence
                )
              )
            ) return;
            const liveBars = Array.isArray(message.bars)
              ? message.bars.filter(bar => (
                  exactIdentityText(bar?.instrument_id) === requestInstrumentId
                  && exactIdentityText(bar?.route_fingerprint) === requestRouteFingerprint
                  && String(bar?.timeframe ?? "") === requestTimeframe
                ))
              : [];
            state.transport.quote.candleSequence = sequence;
            if (lossAfterSequence !== null && lossAfterSequence !== undefined && window.mcTelemetryInc) {
              window.mcTelemetryInc("live_candle.quote_event_loss");
            }
            if (liveBars.length) {
              const candleKey = JSON.stringify([
                requestInstrumentId,
                requestRouteFingerprint,
                requestTimeframe,
                "quote",
                state.transport.quote.epoch,
              ]);
              queueChartBars(liveBars, candleKey, {
                suppressAnalysis: true,
                streamEpoch: state.transport.quote.epoch,
                streamSeq: sequence,
                promoteExpectedBounds: true,
              });
            }
            return;
          }
          const sequencedType = [
            "quote_snapshot",
            "quote_delta",
            "quote_heartbeat",
            "option_targets_snapshot",
            "fast_indicators_snapshot",
            "quote_aux_status",
          ].includes(message.type);
          if (!sequencedType) return;
          const sequence = message.sequence;
          const sharedBootstrap = Boolean(
            !state.transport.quote.stateReady
            && socket.sharedQuoteTransport
            && message.type === "quote_snapshot"
            && message.shared_transport_bootstrap === true
          );
          const expectedSequence = state.transport.quote.stateReady
            ? state.transport.quote.stateSequence + 1
            : sharedBootstrap ? sequence : 1;
          if (!Number.isSafeInteger(sequence) || sequence <= 0 || sequence !== expectedSequence) {
            resyncQuoteState(`expected ${expectedSequence}, received invalid sequence`);
            return;
          }
          if (
            ["quote_snapshot", "quote_delta"].includes(message.type)
            && (!Number.isSafeInteger(message.revision) || message.revision < 0)
          ) {
            resyncQuoteState("rows revision invalid");
            return;
          }
          state.transport.quote.lastStateAt = Date.now();
          if (message.type === "quote_snapshot") {
            if (!Array.isArray(message.rows)) {
              resyncQuoteState("snapshot rows missing");
              return;
            }
            const subscription = message.subscription && typeof message.subscription === "object" ? message.subscription : null;
            const subscriptionRoutes = Array.isArray(subscription?.routes) ? subscription.routes : [];
            const subscriptionKeys = subscriptionRoutes.map(route => JSON.stringify([
              exactIdentityText(route?.instrument_id),
              exactIdentityText(route?.route_fingerprint),
            ]));
            const rowKeys = message.rows.map(row => JSON.stringify([
              exactIdentityText(row?.instrument_id),
              exactIdentityText(row?.route_fingerprint),
            ]));
            if (
              subscription?.interval !== requestTimeframe
              || subscriptionKeys.length !== requestedRouteSet.size
              || new Set(subscriptionKeys).size !== subscriptionKeys.length
              || subscriptionKeys.some(routeKey => !requestedRouteSet.has(routeKey))
              || rowKeys.length !== requestedRouteSet.size
              || new Set(rowKeys).size !== rowKeys.length
              || rowKeys.some(routeKey => !requestedRouteSet.has(routeKey))
            ) {
              resyncQuoteState("snapshot exact-route contract mismatch");
              return;
            }
            const warnings = message.warnings && typeof message.warnings === "object" ? message.warnings : {};
            const warning = Object.values(warnings).filter(Boolean).join(" · ");
            const degradedWarning = message.degraded ? warning : "";
            const snapshotRowsResult = handleQuoteSnapshotRows(
              message.rows,
              degradedWarning ? "degraded" : "live",
              {
                renderChart: false,
                mode: "snapshot",
                scopeKeys: requestedRouteSet,
                cacheEpoch: message.cache_epoch,
                cacheGeneration: message.cache_generation,
                allowEpochChange: true,
                commitPerf: quoteCommitPerf,
                paintPerf: quotePaintPerf,
              },
            );
            if (!snapshotRowsResult.admitted) {
              resyncQuoteState("snapshot cache revision rejected");
              return;
            }
            state.transport.quote.alertRuntimeWarning = String(warnings.price_alert_runtime || "");
            if (
              Object.prototype.hasOwnProperty.call(message, "option_targets")
              && !applyStreamOptionTargets(
                message.option_targets,
                "shared quote bootstrap",
              )
            ) return;
            if (
              Object.prototype.hasOwnProperty.call(message, "fast_indicators")
              && !applyStreamFastIndicators(
                message.fast_indicators,
                "shared quote bootstrap",
              )
            ) return;
            if (
              Object.prototype.hasOwnProperty.call(message, "price_alert_runtime")
              && !applyStreamPriceAlertRuntime(
                message.price_alert_runtime,
                "shared quote bootstrap",
              )
            ) return;
            state.transport.quote.degradedWarning = degradedWarning;
            if (stateNode) stateNode.title = state.transport.quote.degradedWarning;
            state.transport.quote.stateSequence = sequence;
            state.transport.quote.stateReady = true;
            setScreenerStateLabel();
            return;
          }
          if (!state.transport.quote.stateReady || message.interval !== requestTimeframe) {
            resyncQuoteState("delta lane received before matching snapshot");
            return;
          }
          const hasPriceAlertRuntime = Object.prototype.hasOwnProperty.call(
            message,
            "price_alert_runtime",
          );
          if (hasPriceAlertRuntime && message.type !== "quote_heartbeat") {
            resyncQuoteState("price alert runtime envelope mismatch");
            return;
          }
          if (
            hasPriceAlertRuntime
            && !applyStreamPriceAlertRuntime(
              message.price_alert_runtime,
              "price alert runtime stream",
            )
          ) return;
          if (message.type === "quote_delta") {
            if (
              exactIdentityText(message.cache_epoch)
              !== exactIdentityText(state.transport.quote.cacheEpoch)
            ) {
              resyncQuoteState("delta cache epoch mismatch");
              return;
            }
            if (!Array.isArray(message.rows)) {
              resyncQuoteState("delta rows missing");
              return;
            }
            const rowKeys = message.rows.map(row => JSON.stringify([
              exactIdentityText(row?.instrument_id),
              exactIdentityText(row?.route_fingerprint),
            ]));
            if (
              new Set(rowKeys).size !== rowKeys.length
              || rowKeys.some(routeKey => !requestedRouteSet.has(routeKey))
            ) {
              resyncQuoteState("delta exact-route contract mismatch");
              return;
            }
            if (window.mcTelemetryInc) window.mcTelemetryInc("quote_ws.dirty_rows", message.rows.length);
            const deltaRowsResult = handleQuoteSnapshotRows(
              message.rows,
              state.transport.quote.degradedWarning ? "degraded" : "live",
              {
                renderChart: false,
                mode: "delta",
                cacheEpoch: message.cache_epoch,
                cacheGeneration: message.cache_generation,
                commitPerf: quoteCommitPerf,
                paintPerf: quotePaintPerf,
              },
            );
            if (!deltaRowsResult.admitted) {
              resyncQuoteState("delta cache revision rejected");
              return;
            }
          } else if (message.type === "option_targets_snapshot") {
            if (!applyStreamOptionTargets(message.option_targets, "option targets stream")) return;
          } else if (message.type === "fast_indicators_snapshot") {
            if (!applyStreamFastIndicators(message.scopes, "fast indicators stream")) return;
          } else if (message.type === "quote_aux_status") {
            const warnings = message.warnings && typeof message.warnings === "object" ? message.warnings : {};
            state.transport.quote.alertRuntimeWarning = String(warnings.price_alert_runtime || "");
            const warning = Object.values(warnings).filter(Boolean).join(" · ");
            state.transport.quote.degradedWarning = message.degraded ? warning : "";
            setScreenerStateLabel();
            if (stateNode) stateNode.title = state.transport.quote.degradedWarning;
          } else if (message.type === "quote_heartbeat") {
            setScreenerStateLabel();
            if (stateNode && state.transport.quote.degradedWarning) {
              stateNode.title = state.transport.quote.degradedWarning;
            }
            applyCurrentScreenerQuote();
            updateDocumentTitleFromScreenerQuote();
            renderInstruments({
              dirtyIdentityKeys: new Set(
                (state.screener || []).map(row => screenerRowIdentityKey(row)).filter(Boolean),
              ),
            });
          }
          state.transport.quote.stateSequence = sequence;
        } catch (error) {
          if (!parsed && window.mcPerfEnd) window.mcPerfEnd(parsePerf, "error", 4);
          console.warn("quote stream message failed", error);
          debugStep("quote stream parse error", requestErrorMessage(error, "quote stream parse error"));
          resyncQuoteState("invalid quote message");
        }
      };
      socket.onerror = event => {
        const errorCode = String(event?.code || "QUOTE_STREAM_TRANSPORT_ERROR");
        const errorMessage = String(event?.message || "quote transport error").slice(0, 240);
        if (window.mcTelemetrySet) {
          window.mcTelemetrySet("quote_ws.last_error_code", errorCode);
          window.mcTelemetrySet("quote_ws.last_error_reason", errorMessage);
          window.mcTelemetrySet("quote_ws.last_error_fatal", event?.fatal === true);
        }
        if (event?.fatal === true) {
          state.transport.quote.errorCode = errorCode;
          state.transport.quote.errorMessage = errorMessage;
        }
        debugStep("quote stream error", `${errorCode}${event?.fatal === true ? " fatal" : ""} · ${errorMessage}`);
        setScreenerStateLabel(event?.fatal === true ? "" : "retry");
      };
      socket.onclose = event => {
        if (state.transport.quote.socket !== socket) return;
        const closeCode = Number.isFinite(Number(event?.code)) ? Number(event.code) : 1006;
        const closeReason = String(event?.reason || "quote transport closed").slice(0, 240);
        if (window.mcTelemetrySet) {
          window.mcTelemetrySet("quote_ws.last_close_code", closeCode);
          window.mcTelemetrySet("quote_ws.last_close_reason", closeReason);
          window.mcTelemetrySet("quote_ws.last_close_restart", event?.restart === true);
          window.mcTelemetrySet("quote_ws.last_close_lease_expired", event?.leaseExpired === true);
        }
        debugStep(
          "quote stream closed",
          `${closeCode}${event?.restart === true ? " restart" : ""}${event?.leaseExpired === true ? " lease-expired" : ""} · ${closeReason}`,
        );
        state.transport.quote.socket = null;
        state.transport.quote.key = "";
        state.transport.quote.openedAt = 0;
        state.transport.quote.lastMessageAt = 0;
        state.transport.quote.lastStateAt = 0;
        state.transport.quote.candleSequence = 0;
        state.transport.quote.stateSequence = 0;
        state.transport.quote.stateReady = false;
        state.transport.quote.alertRuntimeReady = false;
        state.transport.quote.alertRuntimeWarning = "";
        state.alerts.runtimeByScope.clear();
        state.transport.quote.degradedWarning = "";
        state.transport.quote.commitPerf = null;
        state.transport.quote.paintPerf = null;
        state.transport.quote.gatewayPaintAtMs = 0;
        setScreenerStateLabel(state.transport.quote.errorCode ? "" : "retry");
        if (!state.serverSleeping) {
          state.transport.quote.retryTimer = window.setTimeout(() => connectQuoteStream(true), QUOTE_STREAM_RECONNECT_MS);
        }
      };
    }

    const CHART_STREAM_RECONNECT_MAX_MS = 30000;
    let chartStreamReconnectAttempt = 0;
    let chartStreamReconnectKey = "";
    let chartRouteRefreshTimer = null;

    function closeChartStream(options = {}) {
      window.clearTimeout(chartRouteRefreshTimer);
      chartRouteRefreshTimer = null;
      window.clearTimeout(state.transport.chart.retryTimer);
      window.clearTimeout(state.transport.chart.staleTimer);
      state.transport.chart.retryTimer = null;
      state.transport.chart.staleTimer = null;
      window.clearTimeout(state.indicatorAnalysisRefreshTimer);
      state.indicatorAnalysisRefreshTimer = null;
      state.indicatorAnalysisRefreshBar = "";
      resetIndicatorAnalysisRefreshSchedulers();
      closeWebSocketQuietly(state.transport.chart.socket, "chart stream close");
      state.transport.chart.socket = null;
      state.transport.chart.key = "";
      state.transport.chart.openedAt = 0;
      state.transport.chart.lastMessageAt = 0;
      state.transport.chart.lastBarsAt = 0;
      state.transport.chart.hasLiveBar = false;
      state.transport.chart.epoch = Number(state.transport.chart.epoch || 0) + 1;
      state.transport.chart.generation = 0;
      state.transport.chart.sequence = 0;
      state.transport.chart.expectedLiveSlot = "";
      state.transport.chart.expectedLiveClose = "";
      if (options.resetBackoff !== false) {
        chartStreamReconnectAttempt = 0;
        chartStreamReconnectKey = "";
      }
      state.chartBarQueue = {
        key: "",
        barsByTs: new Map(),
        anonymous: [],
        flushScheduled: false,
        suppressAnalysis: true,
        chartDataQuality: null,
        historyCoverage: null,
        futureAxis: null,
        futureAxisCanonicalRevision: 0,
        chartQualityRevision: 0,
        canonicalRevision: 0,
        expectedLiveSlot: undefined,
        expectedLiveClose: undefined,
      };
    }

    function isDeepHistoryRange(range = state.range, interval = state.timeframe) {
      const steps = rangeStepsFor(interval);
      const currentIndex = steps.indexOf(range);
      const deepIndex = steps.indexOf("31d") >= 0 ? steps.indexOf("31d") : steps.indexOf("1mo");
      return deepIndex >= 0 && currentIndex >= deepIndex;
    }

    function chartStreamAllowedForRange(range = state.range, interval = state.timeframe) {
      void range;
      void interval;
      return true;
    }

    function clearResolvedHistoryNoticeAfterRecovery(message) {
      const coverage = message?.history_coverage;
      const repair = coverage?.bar_repair;
      if (
        message?.recovery_complete !== true
        || !coverage
        || typeof coverage !== "object"
        || Array.isArray(coverage)
        || String(coverage.requested_range || "") !== String(state.range || "")
        || !repair
        || typeof repair !== "object"
        || Array.isArray(repair)
      ) return false;
      const phase = String(repair.phase || "").trim().toLowerCase();
      const status = String(repair.status || "").trim().toLowerCase();
      const resolved = phase === "terminal" || ["not_needed", "not_supported"].includes(status);
      if (!resolved) return false;
      const pendingCleared = clearUiNotice("history", "HISTORY_REPAIR_PENDING");
      const loadedCleared = clearUiNotice("history", "HISTORY_LOADED");
      return pendingCleared || loadedCleared;
    }

    function connectChartStream(force = false) {
      if (!providerCapability(state.dataSource, "chart_stream")) return;
      if (state.serverSleeping) {
        closeChartStream();
        return;
      }
      if (effectiveDataQuality(state.snapshot?.meta || {}).market_closed) {
        closeChartStream();
        return;
      }
      const currentFingerprint = instrumentRouteFingerprint();
      const key = JSON.stringify([state.instrumentId, currentFingerprint, state.timeframe]);
      if (!state.instrumentId || !currentFingerprint) return;
      if (chartStreamReconnectKey !== key) {
        chartStreamReconnectKey = key;
        chartStreamReconnectAttempt = 0;
      }
      if (state.transport.chart.key === key && chartStreamOpen()) {
        return;
      }
      if (state.transport.chart.socket && state.transport.chart.socket.readyState <= WebSocket.OPEN && state.transport.chart.key === key) {
        return;
      }
      closeChartStream({ resetBackoff: false });
      const streamEpoch = state.transport.chart.epoch;
      const candleKey = JSON.stringify([state.instrumentId, currentFingerprint, state.timeframe, "chart", streamEpoch]);
      const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      const params = new URLSearchParams({
        instrument_id: state.instrumentId,
        expected_route_fingerprint: currentFingerprint,
        interval: state.timeframe,
        range: initialRangeFor(state.timeframe),
      });
      const snapshotBars = Array.isArray(state.snapshot?.bars) ? state.snapshot.bars : [];
      const confirmedSnapshotTs = state.snapshot?.meta?.preview?.confirmed_latest_ts || state.snapshot?.meta?.analysis_ts || "";
      const lastConfirmedBar = latestConfirmedIndicatorBar({ bars: snapshotBars });
      const recoveryCursor = confirmedSnapshotTs || lastConfirmedBar?.ts || "";
      if (recoveryCursor) params.set("since_ts", recoveryCursor);
      const socket = new WebSocket(`${protocol}//${window.location.host}/ws/chart?${params.toString()}`);
      state.transport.chart.socket = socket;
      state.transport.chart.key = key;
      state.transport.chart.openedAt = Date.now();
      state.transport.chart.lastMessageAt = state.transport.chart.openedAt;
      state.transport.chart.lastBarsAt = 0;
      state.transport.chart.hasLiveBar = false;
      state.transport.chart.generation = 0;
      state.transport.chart.sequence = 0;
      let chartStaleTimer = null;
      let lastChartTransportAt = Date.now();
      const clearChartStaleTimer = () => {
        window.clearTimeout(chartStaleTimer);
        if (state.transport.chart.staleTimer === chartStaleTimer) state.transport.chart.staleTimer = null;
        chartStaleTimer = null;
      };
      const markChartStreamAlive = () => {
        lastChartTransportAt = Date.now();
        state.transport.chart.lastMessageAt = lastChartTransportAt;
        chartStreamReconnectAttempt = 0;
      };
      const recoverStaleChartStream = (reason, options = {}) => {
        if (state.transport.chart.socket !== socket) return;
        debugStep("chart stream stale", reason);
        setUiNotice(
          "chart_stream",
          "CHART_STREAM_STALLED",
          "Chart stream stalled. Reconnecting broker chart feed...",
          { state: "stale" },
        );
        if (options.refreshRoute) {
          const transitionInstrumentId = exactIdentityText(state.instrumentId);
          const transitionTimeframe = String(state.timeframe || "");
          const previousFingerprint = currentFingerprint;
          window.clearTimeout(chartRouteRefreshTimer);
          const routeRefreshTimer = window.setTimeout(async () => {
            if (chartRouteRefreshTimer !== routeRefreshTimer) return;
            chartRouteRefreshTimer = null;
            if (
              exactIdentityText(state.instrumentId) !== transitionInstrumentId
              || String(state.timeframe || "") !== transitionTimeframe
              || instrumentRouteFingerprint() !== previousFingerprint
            ) return;
            try {
              if (typeof loadInstruments === "function") await loadInstruments();
              const nextFingerprint = instrumentRouteFingerprint();
              if (
                exactIdentityText(state.instrumentId) === transitionInstrumentId
                && String(state.timeframe || "") === transitionTimeframe
                && nextFingerprint
                && nextFingerprint !== previousFingerprint
              ) {
                await load({
                  force: true,
                  queueAnalysis: true,
                  requireLatestAnalysis: true,
                });
              }
            } catch (error) {
              debugStep("chart route transition", requestErrorMessage(error, "route refresh failed"));
            }
          }, 0);
          chartRouteRefreshTimer = routeRefreshTimer;
        }
        try {
          socket.close(4000, "chart stream stale");
        } catch (_) {
          // noop
        }
      };
      const armChartStaleTimer = () => {
        clearChartStaleTimer();
        chartStaleTimer = window.setTimeout(() => {
          const staleFor = Date.now() - lastChartTransportAt;
          if (staleFor >= CHART_STREAM_STALE_MS) {
            recoverStaleChartStream(`${Math.round(staleFor / 1000)}s without transport heartbeat`);
          } else {
            armChartStaleTimer();
          }
        }, CHART_STREAM_STALE_MS);
        state.transport.chart.staleTimer = chartStaleTimer;
      };
      socket.onopen = () => {
        if (state.transport.chart.socket !== socket || state.transport.chart.key !== key) return;
        debugStep("chart stream", `open ${state.symbol} ${state.timeframe}`);
        lastChartTransportAt = Date.now();
        state.transport.chart.openedAt = lastChartTransportAt;
        state.transport.chart.lastMessageAt = lastChartTransportAt;
        state.transport.chart.lastBarsAt = 0;
        armChartStaleTimer();
        if (gexLayerVisible()) loadGexContext({ refresh: false, bypassCache: true, refreshPoll: true });
      };
      socket.onmessage = event => {
        const parsePerf = window.mcPerfStart ? window.mcPerfStart("chartWsParse") : null;
        let parsed = false;
        try {
          const message = JSON.parse(event.data);
          parsed = true;
          if (window.mcPerfEnd) window.mcPerfEnd(parsePerf, String(message?.type || "unknown"), 4);
          if (window.mcPerfEnabled?.() && streamTextEncoder && window.mcTelemetryMax) {
            const payloadBytes = streamTextEncoder.encode(String(event.data || "")).byteLength;
            window.mcTelemetryMax("chart_ws.payload_bytes_max", payloadBytes);
            if (window.mcTelemetryInc) window.mcTelemetryInc("chart_ws.payload_samples");
          }
          if (
            state.transport.chart.socket !== socket
            || state.transport.chart.key !== key
            || state.transport.chart.epoch !== streamEpoch
          ) return;
          if (
            exactIdentityText(state.instrumentId) !== exactIdentityText(message?.instrument_id)
            || instrumentRouteFingerprint() !== currentFingerprint
          ) {
            recoverStaleChartStream("active chart scope changed");
            return;
          }
          const admission = chartStreamMessageAdmission(message);
          if (!admission.ok) {
            recoverStaleChartStream(`invalid chart frame: ${admission.reasonCode}`);
            return;
          }
          const {
            messageType,
            streamGeneration,
            streamSeq,
            unsequenced,
          } = admission;
          if (messageType === "chart_status" && unsequenced) {
            markChartStreamAlive();
            debugStep("chart stream status", message.message || "waiting");
            if (message.requires_resubscribe) {
              recoverStaleChartStream(message.message || "provider route changed", { refreshRoute: true });
            }
            return;
          }
          if (streamGeneration < Number(state.transport.chart.generation || 0)) return;
          if (streamGeneration > Number(state.transport.chart.generation || 0)) {
            state.transport.chart.generation = streamGeneration;
            state.transport.chart.sequence = 0;
            state.chartBarQueue = {
              key: candleKey,
              barsByTs: new Map(),
              anonymous: [],
              flushScheduled: false,
              suppressAnalysis: true,
              chartDataQuality: null,
              historyCoverage: null,
              futureAxis: null,
              futureAxisCanonicalRevision: 0,
              chartQualityRevision: 0,
              canonicalRevision: 0,
              expectedLiveSlot: undefined,
              expectedLiveClose: undefined,
            };
          }
          if (streamSeq <= Number(state.transport.chart.sequence || 0)) return;
          state.transport.chart.sequence = streamSeq;
          if (messageType === "chart_status") {
            markChartStreamAlive();
            debugStep("chart stream status", message.message || "waiting");
            if (message.requires_resubscribe) {
              recoverStaleChartStream(message.message || "provider route changed", { refreshRoute: true });
            }
            return;
          }
          if (messageType === "heartbeat") {
            markChartStreamAlive();
            return;
          }
          if (message.recovery) {
            debugStep(
              message.recovery_complete ? "chart stream recovery complete" : "chart stream recovery",
              `${message.bars.length} bars`,
            );
          }
          clearResolvedHistoryNoticeAfterRecovery(message);
          const providerLive = message.bars.some(bar => (
            !barIsGapPlaceholder(bar)
            && bar?.preview_kind === "provider_bar"
          ));
          if (providerLive) state.transport.chart.hasLiveBar = true;
          const receivedAt = Date.now();
          if (providerLive) {
            state.transport.chart.lastBarsAt = receivedAt;
          }
          lastChartTransportAt = receivedAt;
          state.transport.chart.lastMessageAt = receivedAt;
          chartStreamReconnectAttempt = 0;
          armChartStaleTimer();
          const provisionalOnly = message.bars.length > 0 && message.bars.every(bar => bar?.authoritative === false);
          queueChartBars(message.bars, candleKey, {
            suppressAnalysis: Boolean(message.recovery || provisionalOnly),
            streamEpoch,
            streamGeneration,
            streamSeq,
            canonicalRevision: message.canonical_revision ?? 0,
            chartDataQuality: message.chart_data_quality,
            historyCoverage: message.history_coverage,
            gapRepair: message.gap_repair,
            futureAxis: message.future_axis,
            futureAxisCanonicalRevision: message.canonical_revision ?? 0,
            chartQualityRevision: message.chart_quality_revision ?? 0,
            expectedLiveSlot: message.expected_live_slot,
            expectedLiveClose: message.expected_live_close,
          });
        } catch (error) {
          if (!parsed && window.mcPerfEnd) window.mcPerfEnd(parsePerf, "error", 4);
          console.warn("chart stream message failed", error);
          debugStep("chart stream parse error", requestErrorMessage(error, "chart stream parse error"));
          recoverStaleChartStream("malformed chart stream frame");
        }
      };
      socket.onerror = () => {
        debugStep("chart stream", "socket error");
      };
      socket.onclose = () => {
        clearChartStaleTimer();
        if (state.transport.chart.socket !== socket) return;
        state.transport.chart.socket = null;
        state.transport.chart.key = "";
        state.transport.chart.openedAt = 0;
        state.transport.chart.lastMessageAt = 0;
        state.transport.chart.lastBarsAt = 0;
        state.transport.chart.hasLiveBar = false;
        state.transport.chart.generation = 0;
        state.transport.chart.sequence = 0;
        state.transport.chart.expectedLiveSlot = "";
        state.transport.chart.expectedLiveClose = "";
        state.transport.chart.epoch = Number(state.transport.chart.epoch || 0) + 1;
        state.chartBarQueue = {
          key: "",
          barsByTs: new Map(),
          anonymous: [],
          flushScheduled: false,
          suppressAnalysis: true,
          chartDataQuality: null,
          historyCoverage: null,
          futureAxis: null,
          futureAxisCanonicalRevision: 0,
          chartQualityRevision: 0,
          canonicalRevision: 0,
          expectedLiveSlot: undefined,
          expectedLiveClose: undefined,
        };
        if (!state.serverSleeping) {
          const reconnectDelay = Math.min(
            QUOTE_STREAM_RECONNECT_MS * (2 ** Math.min(chartStreamReconnectAttempt, 5)),
            CHART_STREAM_RECONNECT_MAX_MS,
          );
          chartStreamReconnectAttempt += 1;
          state.transport.chart.retryTimer = window.setTimeout(() => {
            state.transport.chart.retryTimer = null;
            connectChartStream(true);
          }, reconnectDelay);
        }
      };
    }
