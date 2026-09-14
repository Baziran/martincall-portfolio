    const OPTION_TARGET_LIVE_REFRESH_MS = 6000;
    const OPTION_TARGET_QUOTE_MAX_AGE_MS = 30000;
    const OPTION_TARGET_SPINNER_CYCLE_MS = OPTION_TARGET_LIVE_REFRESH_MS;
    const OPTION_TARGET_RENDER_MS = 250;
    const OPTION_TARGET_DELETE_TOMBSTONE_MS = 60000;
    const OPTION_TARGET_CHOICES_PER_EXPIRY = 7;

    function optionTargetPositiveNumber(value) {
      return typeof value === "number" && Number.isFinite(value) && value > 0
        ? value
        : null;
    }

    function optionTargetScopeKey(instrumentId, routeFingerprint) {
      const exactInstrumentId = exactIdentityText(instrumentId);
      const exactRouteFingerprint = exactIdentityText(routeFingerprint);
      return exactInstrumentId && exactRouteFingerprint
        ? JSON.stringify([exactInstrumentId, exactRouteFingerprint])
        : "";
    }

    function optionTargetIdentityKey(item) {
      const id = item?.id;
      const scopeKey = optionTargetScopeKey(item?.instrument_id, item?.route_fingerprint);
      return scopeKey && typeof id === "string" && id
        ? JSON.stringify([exactIdentityText(item.instrument_id), exactIdentityText(item.route_fingerprint), id])
        : "";
    }

    function fastIndicatorScopeKey(instrumentId, routeFingerprint, timeframe) {
      const scopeKey = optionTargetScopeKey(instrumentId, routeFingerprint);
      const exactTimeframe = String(timeframe || "");
      return scopeKey && exactTimeframe
        ? JSON.stringify([exactIdentityText(instrumentId), exactIdentityText(routeFingerprint), exactTimeframe])
        : "";
    }

    function fastIndicatorResultValid(indicator) {
      if (!indicator || typeof indicator !== "object" || Array.isArray(indicator)) return false;
      return indicator.execution?.lane === "fast"
        && indicator.execution?.authority === "advisory_only"
        && indicator.execution?.trigger === "option_target_sample";
    }

    function applyFastIndicatorProjectionToSnapshot(snapshot, options = {}) {
      if (!snapshot || typeof snapshot !== "object") return snapshot;
      const scopeKey = fastIndicatorScopeKey(
        state.instrumentId,
        instrumentRouteFingerprint(),
        state.timeframe,
      );
      if (!scopeKey) return snapshot;
      const indicators = { ...(snapshot.indicators || {}) };
      const snapshotOptionReversal = indicators.option_reversal;
      const primaryByScope = state.fastIndicators.primaryOptionReversalByScope
        || (state.fastIndicators.primaryOptionReversalByScope = {});
      if (options.primarySnapshot === true) {
        if (snapshotOptionReversal && snapshotOptionReversal.execution?.lane !== "fast") {
          primaryByScope[scopeKey] = snapshotOptionReversal;
        } else {
          delete primaryByScope[scopeKey];
        }
      } else if (
        snapshotOptionReversal
        && snapshotOptionReversal.execution?.lane !== "fast"
      ) {
        primaryByScope[scopeKey] = snapshotOptionReversal;
      }
      const projection = state.fastIndicators.aggregateByScope[scopeKey];
      if (fastIndicatorResultValid(projection?.indicator)) {
        indicators.option_reversal = projection.indicator;
      } else if (Object.prototype.hasOwnProperty.call(primaryByScope, scopeKey)) {
        indicators.option_reversal = primaryByScope[scopeKey];
      } else {
        delete indicators.option_reversal;
      }
      return { ...snapshot, indicators };
    }

    function clearFastIndicatorSnapshotsForRoutes(routeSet, timeframe) {
      const routes = routeSet instanceof Set ? routeSet : new Set();
      const exactTimeframe = String(timeframe || "");
      let currentRemoved = false;
      Object.keys(state.fastIndicators?.aggregateByScope || {}).forEach(scopeKey => {
        let scope;
        try { scope = JSON.parse(scopeKey); } catch { scope = null; }
        if (
          !Array.isArray(scope)
          || scope.length !== 3
          || scope[2] !== exactTimeframe
          || !routes.has(JSON.stringify([scope[0], scope[1]]))
        ) return;
        if (
          scope[0] === exactIdentityText(state.instrumentId)
          && scope[1] === instrumentRouteFingerprint()
          && scope[2] === String(state.timeframe || "")
        ) currentRemoved = true;
        delete state.fastIndicators.aggregateByScope[scopeKey];
        delete state.fastIndicators.revisionsByScope[scopeKey];
      });
      Object.keys(state.fastIndicators?.optionReversalByTargetIdentity || {}).forEach(identityKey => {
        let identity;
        try { identity = JSON.parse(identityKey); } catch { identity = null; }
        if (
          Array.isArray(identity)
          && identity.length === 3
          && routes.has(JSON.stringify([identity[0], identity[1]]))
        ) delete state.fastIndicators.optionReversalByTargetIdentity[identityKey];
      });
      if (currentRemoved && state.snapshot) {
        state.snapshot = applyFastIndicatorProjectionToSnapshot(state.snapshot);
      }
    }

    function applyFastIndicatorSnapshots(scopes, options = {}) {
      if (!Array.isArray(scopes)) throw new Error("fast indicator scopes must be an array");
      const requestedRouteSet = options.requestedRouteSet instanceof Set
        ? options.requestedRouteSet
        : new Set();
      const timeframe = String(options.timeframe || "");
      const seenScopeKeys = new Set();
      let currentChanged = false;
      for (const scope of scopes) {
        const instrumentId = exactIdentityText(scope?.instrument_id);
        const routeFingerprint = exactIdentityText(scope?.route_fingerprint);
        const routeKey = JSON.stringify([instrumentId, routeFingerprint]);
        const scopeTimeframe = String(scope?.timeframe || "");
        const barTimeframe = String(scope?.bar_timeframe || "");
        const barCanonicalGeneration = scope?.bar_canonical_generation;
        const revision = Number(scope?.revision);
        const byTargetId = scope?.by_target_id;
        if (
          !scope
          || typeof scope !== "object"
          || Array.isArray(scope)
          || !instrumentId
          || !routeFingerprint
          || !requestedRouteSet.has(routeKey)
          || scopeTimeframe !== timeframe
          || !barTimeframe
          || !Number.isSafeInteger(barCanonicalGeneration)
          || barCanonicalGeneration < 0
          || !Number.isSafeInteger(revision)
          || revision <= 0
          || scope.indicator_id !== "option_reversal"
          || scope.authority !== "advisory_only"
          || !byTargetId
          || typeof byTargetId !== "object"
          || Array.isArray(byTargetId)
          || !(scope.indicator === null || fastIndicatorResultValid(scope.indicator))
        ) throw new Error("fast indicator scope violates exact-route contract");
        const scopeKey = fastIndicatorScopeKey(instrumentId, routeFingerprint, scopeTimeframe);
        seenScopeKeys.add(scopeKey);
        const previousRevision = Number(state.fastIndicators.revisionsByScope[scopeKey] || 0);
        if (revision < previousRevision) throw new Error("fast indicator scope revision regressed");
        if (revision === previousRevision) continue;

        const nextTargetEntries = {};
        for (const [targetId, indicator] of Object.entries(byTargetId)) {
          if (typeof targetId !== "string" || !targetId || !fastIndicatorResultValid(indicator)) {
            throw new Error("fast indicator target projection is invalid");
          }
          const target = allOptionTargets().find(item => (
            item?.id === targetId
            && exactIdentityText(item?.instrument_id) === instrumentId
            && exactIdentityText(item?.route_fingerprint) === routeFingerprint
            && String(item?.timeframe || "") === scopeTimeframe
          ));
          if (!target) throw new Error("fast indicator target is not in the committed Option Point snapshot");
          nextTargetEntries[optionTargetIdentityKey(target)] = indicator;
        }
        Object.keys(state.fastIndicators.optionReversalByTargetIdentity).forEach(identityKey => {
          let identity;
          try { identity = JSON.parse(identityKey); } catch { identity = null; }
          if (
            Array.isArray(identity)
            && identity[0] === instrumentId
            && identity[1] === routeFingerprint
          ) delete state.fastIndicators.optionReversalByTargetIdentity[identityKey];
        });
        Object.assign(state.fastIndicators.optionReversalByTargetIdentity, nextTargetEntries);
        state.fastIndicators.revisionsByScope[scopeKey] = revision;
        state.fastIndicators.aggregateByScope[scopeKey] = {
          revision,
          barTimeframe,
          barCanonicalGeneration,
          indicator: scope.indicator,
          updatedAt: String(scope.updated_at || ""),
        };
        if (
          instrumentId === exactIdentityText(state.instrumentId)
          && routeFingerprint === instrumentRouteFingerprint()
          && scopeTimeframe === String(state.timeframe || "")
        ) currentChanged = true;
      }
      const missingRouteKeys = new Set();
      Object.keys(state.fastIndicators.aggregateByScope).forEach(scopeKey => {
        let identity;
        try { identity = JSON.parse(scopeKey); } catch { identity = null; }
        if (!Array.isArray(identity) || identity.length !== 3) return;
        const routeKey = JSON.stringify([identity[0], identity[1]]);
        if (
          identity[2] !== timeframe
          || !requestedRouteSet.has(routeKey)
          || seenScopeKeys.has(scopeKey)
        ) return;
        delete state.fastIndicators.aggregateByScope[scopeKey];
        delete state.fastIndicators.revisionsByScope[scopeKey];
        missingRouteKeys.add(routeKey);
        if (
          identity[0] === exactIdentityText(state.instrumentId)
          && identity[1] === instrumentRouteFingerprint()
          && identity[2] === String(state.timeframe || "")
        ) currentChanged = true;
      });
      if (missingRouteKeys.size) {
        Object.keys(state.fastIndicators.optionReversalByTargetIdentity).forEach(identityKey => {
          let identity;
          try { identity = JSON.parse(identityKey); } catch { identity = null; }
          if (
            Array.isArray(identity)
            && identity.length === 3
            && missingRouteKeys.has(JSON.stringify([identity[0], identity[1]]))
          ) delete state.fastIndicators.optionReversalByTargetIdentity[identityKey];
        });
      }
      if (currentChanged && state.snapshot) {
        state.snapshot = applyFastIndicatorProjectionToSnapshot(state.snapshot);
        renderCharts(state.snapshot, { reason: options.reason || "fast-indicators" });
        renderPanel(state.snapshot);
        renderOptionTargetActiveList();
        requestObjectLayerRedraw(options.reason || "fast indicators updated");
      }
      return currentChanged;
    }

    function optionTargetFastIndicator(item) {
      const identityKey = optionTargetIdentityKey(item);
      return identityKey
        ? state.fastIndicators?.optionReversalByTargetIdentity?.[identityKey] || null
        : null;
    }

    function optionTargetFastStatusText(item) {
      if (!optionTargetsFastStatusEnabled()) return "";
      const indicator = optionTargetFastIndicator(item);
      const latest = indicator?.latest;
      if (!latest || typeof latest !== "object") return "";
      const action = String(latest.action || latest.state || "").toUpperCase();
      const score = typeof latest.score === "number" && Number.isFinite(latest.score)
        ? latest.score
        : null;
      return action
        ? `${action}${score !== null ? ` Q${Math.round(score)}` : ""}`
        : "";
    }

    function optionTargetPayloadMatchesRequestScope(payload, requestScope) {
      const requestedTargetMs = Date.parse(requestScope?.targetPoint?.ts || "");
      const payloadTargetMs = Date.parse(payload?.target_ts || "");
      const requestedBarSlot = requestScope?.targetPoint?.barSlot;
      const payloadBarSlot = payload?.target_bar_slot;
      return Boolean(
        payload
        && requestScope
        && exactIdentityText(payload.instrument_id) === requestScope.instrumentId
        && exactIdentityText(payload.route_fingerprint) === requestScope.routeFingerprint
        && Boolean(exactIdentityText(payload.provider_symbol))
        && String(payload.timeframe || "") === requestScope.timeframe
        && Number.isSafeInteger(payloadBarSlot)
        && (
          !Number.isSafeInteger(requestedBarSlot)
          || payloadBarSlot === requestedBarSlot
        )
        && (
          !Number.isFinite(payloadTargetMs)
          || payloadTargetMs === requestedTargetMs
        )
      );
    }

    function admitOptionTargetPricingResponse(payload, requestScope, targetDte) {
      if (payload?.ok === false) {
        return {
          ...payload,
          target_dte: payload.target_dte || targetDte,
        };
      }
      if (optionTargetPayloadMatchesRequestScope(payload, requestScope)) return payload;
      const message = "Option pricing response does not match the exact chart route.";
      return {
        ok: false,
        status: "route_mismatch",
        target_dte: targetDte,
        message,
        error: {
          code: "OPTION_TARGET_RESPONSE_SCOPE_MISMATCH",
          category: "options",
          retryable: true,
          message,
        },
      };
    }

    function optionTargetRequestScopeIsCurrent(requestScope) {
      return Boolean(
        requestScope
        && exactIdentityText(state.instrumentId) === requestScope.instrumentId
        && instrumentRouteFingerprint() === requestScope.routeFingerprint
        && String(state.timeframe || "") === requestScope.timeframe
      );
    }

    function optionTargetRequestScopeForPoint(point) {
      const chartPrice = finiteSeriesNumber(point?.price);
      const pointMs = Date.parse(point?.ts || "");
      if (
        chartPrice === null
        || chartPrice <= 0
        || !Number.isFinite(pointMs)
      ) {
        throw new Error(point?.future
          ? "IBKR trading schedule does not cover this future candle yet. Refresh the contract schedule and try again."
          : "The provider trading schedule does not cover the target candle time.");
      }
      const instrumentId = exactIdentityText(state.instrumentId);
      const configuredPremiumCap = optionTargetPositiveNumber(
        state.settings.optionCaps?.[instrumentId],
      );
      if (!instrumentId || configuredPremiumCap === null) {
        throw new Error(optionTargetDteErrorMessage({
          code: "OPTION_PREMIUM_CAP_REQUIRED",
          instrument_id: instrumentId,
        }));
      }
      const routeFingerprint = instrumentRouteFingerprint();
      if (!routeFingerprint) {
        throw new Error("Option pricing route is unavailable for the current instrument.");
      }
      const timeframe = String(state.timeframe || "");
      if (!timeframe) {
        throw new Error("Option Point requires the current chart timeframe.");
      }
      return Object.freeze({
        instrumentId,
        routeFingerprint,
        displaySymbol: String(state.symbol || ""),
        timeframe,
        targetPoint: Object.freeze({
          ts: new Date(pointMs).toISOString(),
          price: chartPrice,
          future: Boolean(point?.future),
        }),
      });
    }

    function optionTargetConfirmedAnchorPoint(point, bar = null) {
      if (
        barIsConfirmedClosed(bar)
        && bar?.authoritative !== false
      ) return point;
      const confirmed = latestConfirmedIndicatorBar(state.snapshot);
      const price = finiteSeriesNumber(point?.price);
      if (!confirmed?.ts || price === null) {
        throw new Error("Option Point requires one confirmed provider candle.");
      }
      return { ts: confirmed.ts, price };
    }

    function optionTargetRequestScopeWithServerAxis(requestScope, payload) {
      if (!optionTargetPayloadMatchesRequestScope(payload, requestScope)) {
        throw new Error("Option pricing response does not match the exact chart route.");
      }
      return Object.freeze({
        ...requestScope,
        targetPoint: Object.freeze({
          ...requestScope.targetPoint,
          barSlot: payload.target_bar_slot,
        }),
      });
    }

    function optionTargetByIdentityKey(identityKey, items = state.optionTargets?.items || []) {
      return typeof identityKey === "string" && identityKey
        ? (items || []).find(item => optionTargetIdentityKey(item) === identityKey) || null
        : null;
    }

    function pruneOptionTargetDeletes(now = Date.now()) {
      const deleted = state.optionTargets?.deletedIds;
      if (!deleted || typeof deleted !== "object") return;
      Object.keys(deleted).forEach(identityKey => {
        if (now - Number(deleted[identityKey] || 0) > OPTION_TARGET_DELETE_TOMBSTONE_MS) delete deleted[identityKey];
      });
    }

    function optionTargetDeleted(item) {
      const key = optionTargetIdentityKey(item);
      if (!key) return false;
      state.optionTargets.deletedIds = state.optionTargets.deletedIds || {};
      pruneOptionTargetDeletes();
      return Boolean(state.optionTargets.deletedIds[key]);
    }

    function markOptionTargetDeleted(item) {
      const key = optionTargetIdentityKey(item);
      if (!key) return;
      state.optionTargets.deletedIds = state.optionTargets.deletedIds || {};
      state.optionTargets.deletedIds[key] = Date.now();
      pruneOptionTargetDeletes();
    }

    function optionTargetDeleteBroadcastKey() {
      return "martincall:option-target-delete";
    }

    function broadcastOptionTargetDeleted(item) {
      if (!optionTargetIdentityKey(item)) return;
      try {
        rawLocalStorageSetItem(optionTargetDeleteBroadcastKey(), JSON.stringify({
          instrument_id: item.instrument_id,
          route_fingerprint: item.route_fingerprint,
          id: item.id,
          at: Date.now(),
        }));
      } catch (error) {
        console.warn("option target delete broadcast failed", error);
      }
    }

    function applyOptionTargetDeleted(item, options = {}) {
      const key = optionTargetIdentityKey(item);
      if (!key) return false;
      const previousDraggingId = state.optionTargets.draggingId;
      const previousSelectedId = state.optionTargets.selectedId;
      markOptionTargetDeleted(item);
      if (state.optionTargets.draggingId === key) state.optionTargets.draggingId = null;
      if (state.optionTargets.refreshStartedAt) delete state.optionTargets.refreshStartedAt[key];
      const before = state.optionTargets.items.length;
      state.optionTargets.items = allOptionTargets().filter(candidate => optionTargetIdentityKey(candidate) !== key);
      if (state.optionTargets.selectedId === key) state.optionTargets.selectedId = null;
      const changed = before !== state.optionTargets.items.length;
      const visualStateChanged = changed
        || previousDraggingId !== state.optionTargets.draggingId
        || previousSelectedId !== state.optionTargets.selectedId;
      if (visualStateChanged) touchChartUserObjectsVersion();
      if (visualStateChanged || options.forceRender) {
        renderOptionTargetActiveList();
        applyDrawingUi();
        requestObjectLayerRedraw("option target deleted", { force: true });
      }
      return changed;
    }

    function optionTargetExpiryLabel(payload) {
      const expiry = String(payload?.expiry || "");
      if (expiry.length >= 8) return `${expiry.slice(4, 6)}/${expiry.slice(6, 8)}`;
      return String(payload?.target_dte || "").toUpperCase();
    }

    function optionTargetGroupLabel(payload) {
      const label = String(payload?.target_dte || "").toUpperCase() || "DTE";
      return label;
    }

    // Option Points are pricing anchors for a planned counter-trend limit at a channel boundary.
    // A CALL below spot and a PUT above spot are intentional: they price the expected bounce/rejection option at the target point.
    function optionTargetLabelModel(payload) {
      const right = payload?.right === "P" ? "P" : payload?.right === "C" ? "C" : "";
      const strike = optionTargetPositiveNumber(payload?.strike);
      const rawFair = payload?.fair_price;
      const displayFair = payload?.fair_price_status === "stale"
        ? optionTargetPositiveNumber(payload?.display_fair_price)
        : null;
      const rawDelta = payload?.target_delta;
      const delta = typeof rawDelta === "number" && Number.isFinite(rawDelta) ? rawDelta : null;
      const liveQuoteTs = typeof payload?.live_quote_ts === "string"
        && /(?:Z|[+-]\d{2}:\d{2})$/i.test(payload.live_quote_ts)
        ? Date.parse(payload.live_quote_ts)
        : Number.NaN;
      const liveQuoteAgeMs = Date.now() - liveQuoteTs;
      const liveQuoteClockReady = payload?.reference_option_price_source === "bid_ask_mid"
        && payload?.live_quote_time_basis === "client_receive";
      const liveQuoteReady = payload?.live_quote_status === "ok"
        && payload?.live_quote_entitlement === "live"
        && liveQuoteClockReady
        && Number.isFinite(liveQuoteTs)
        && liveQuoteAgeMs >= -1000
        && liveQuoteAgeMs <= OPTION_TARGET_QUOTE_MAX_AGE_MS;
      const underlyingQuoteTs = typeof payload?.underlying_quote_ts === "string"
        && /(?:Z|[+-]\d{2}:\d{2})$/i.test(payload.underlying_quote_ts)
        ? Date.parse(payload.underlying_quote_ts)
        : Number.NaN;
      const underlyingQuoteAgeMs = Date.now() - underlyingQuoteTs;
      const underlyingQuoteClockReady = (payload?.underlying_quote_price_source === "last"
          && payload?.underlying_quote_time_basis === "provider_event")
        || (payload?.underlying_quote_price_source === "bid_ask_mid"
          && payload?.underlying_quote_time_basis === "client_receive");
      const underlyingQuoteReady = payload?.underlying_quote_status === "live"
        && payload?.underlying_quote_entitlement === "live"
        && underlyingQuoteClockReady
        && optionTargetPositiveNumber(payload?.live_underlying_price) !== null
        && Number.isFinite(underlyingQuoteTs)
        && underlyingQuoteAgeMs >= -1000
        && underlyingQuoteAgeMs <= OPTION_TARGET_QUOTE_MAX_AGE_MS
        && Math.abs(liveQuoteTs - underlyingQuoteTs) <= OPTION_TARGET_QUOTE_MAX_AGE_MS;
      const isStaticCandidate = !(payload && "live_quote_status" in payload);
      const fairLabel = isStaticCandidate && payload?.pricing_basis === "theoretical_chain"
        ? "theo"
        : "fair";
      const fairReady = isStaticCandidate || (payload?.fair_price_status === "ok"
        && liveQuoteReady
        && underlyingQuoteReady);
      const finiteFair = fairReady && typeof rawFair === "number" && Number.isFinite(rawFair)
        ? rawFair
        : null;
      const authoritativeFair = fairReady ? optionTargetPositiveNumber(rawFair) : null;
      const theo = authoritativeFair ?? displayFair;
      const fairDisplayOnly = authoritativeFair === null && displayFair !== null;
      const bid = liveQuoteReady ? optionTargetPositiveNumber(payload?.live_bid) : null;
      const ask = liveQuoteReady ? optionTargetPositiveNumber(payload?.live_ask) : null;
      const reference = liveQuoteReady
        ? optionTargetPositiveNumber(payload?.reference_option_price)
        : null;
      const expiry = optionTargetExpiryLabel(payload);
      const strikeText = strike !== null ? fmt(strike) : "";
      const liveMarketPositive = bid !== null || ask !== null || reference !== null;
      const invalidLiveFair = finiteFair !== null && finiteFair <= 0 && liveMarketPositive;
      const fairStale = Boolean(payload?.reprice_error)
        || invalidLiveFair
        || (["ok", "stale", "unavailable"].includes(String(payload?.fair_price_status || "").toLowerCase()) && theo === null);
      const fairText = theo !== null && !invalidLiveFair
        ? `${fairDisplayOnly ? "~" : ""}${theo.toFixed(2)}`
        : (fairStale ? "!" : "-");
      const deltaText = delta !== null ? `${payload?.estimated_greeks ? "~" : ""}${Math.abs(delta).toFixed(2)}` : "-";
      const overCapBy = optionTargetPositiveNumber(payload?.over_cap_by);
      const capText = overCapBy !== null ? `cap+${overCapBy.toFixed(2)}` : "";
      const bidText = bid !== null ? bid.toFixed(2) : "-";
      const askText = ask !== null ? ask.toFixed(2) : "-";
      const liveText = bid !== null || ask !== null ? `B/A ${bidText}/${askText}` : "";
      const contractText = [right, expiry, strikeText].filter(Boolean).join(" ");
      return {
        right,
        expiry,
        strikeText,
        contractText,
        fairText,
        fairLabel,
        deltaText,
        capText,
        bidText,
        askText,
        liveText,
        hasLive: bid !== null || ask !== null,
        fairStale,
        fairDisplayOnly,
        errorText: String(payload?.reprice_error || "").trim(),
        fairMessage: String(payload?.fair_price_message || "").trim(),
      };
    }

    function optionTargetMarketLine(model) {
      return model?.hasLive
        ? String(model.liveText || "")
        : String(model?.fairMessage || "");
    }

    function optionTargetLabelText(payload) {
      const model = optionTargetLabelModel(payload);
      if (model.errorText) {
        return [
          model.contractText,
          `${model.fairLabel} ${model.fairText}`,
          model.errorText,
        ].join("\n");
      }
      return [
        model.contractText,
        `${model.fairLabel} ${model.fairText} d-${model.deltaText}${model.capText ? ` ${model.capText}` : ""}`,
        optionTargetMarketLine(model),
      ].filter(Boolean).join("\n");
    }

    function optionTargetErrorMessage(error, payload = null) {
      if (payload && typeof payload === "object") return apiErrorMessage(payload, "Option target pricing failed.");
      return requestErrorMessage(error, "Option target pricing failed.");
    }

    function optionTargetSortPrice(payload) {
      return optionTargetPositiveNumber(payload?.reference_option_price)
        ?? Number.POSITIVE_INFINITY;
    }

    function sameOptionTargetContract(base, payload) {
      if (!base || !payload) return false;
      const baseConId = base.con_id;
      const nextConId = payload.con_id;
      const baseStrike = base.strike;
      const nextStrike = payload.strike;
      return Number.isInteger(baseConId)
        && baseConId > 0
        && nextConId === baseConId
        && ["OPT", "FOP"].includes(base.sec_type)
        && payload.sec_type === base.sec_type
        && ["C", "P"].includes(base.right)
        && payload.right === base.right
        && typeof base.expiry === "string"
        && /^\d{8}$/.test(base.expiry)
        && payload.expiry === base.expiry
        && typeof base.expiry_at === "string"
        && Number.isFinite(Date.parse(base.expiry_at))
        && payload.expiry_at === base.expiry_at
        && typeof base.exchange === "string"
        && base.exchange.length > 0
        && payload.exchange === base.exchange
        && exactIdentityText(base.trading_class)
        && payload.trading_class === base.trading_class
        && exactIdentityText(base.currency)
        && payload.currency === base.currency
        && typeof base.multiplier === "number"
        && Number.isFinite(base.multiplier)
        && base.multiplier > 0
        && payload.multiplier === base.multiplier
        && Number.isFinite(baseStrike)
        && Number.isFinite(nextStrike)
        && Math.abs(baseStrike - nextStrike) <= 0.000001;
    }

    function optionTargetPersistentItem(item) {
      const payload = optionTargetPayload(item);
      if (!payload || !sameOptionTargetContract(payload, payload)) {
        throw new Error("option target exact provider contract is required");
      }
      if (!exactIdentityText(payload.provider_symbol)) {
        throw new Error("option target provider_symbol transport metadata is required");
      }
      if (!["conservative", "normal", "aggressive"].includes(payload.mode)) {
        throw new Error("option target mode is invalid");
      }
      if (!["0dte", "1dte"].includes(payload.target_dte)) {
        throw new Error("option target DTE is invalid");
      }
      if (payload.local_symbol !== undefined && payload.local_symbol !== null && typeof payload.local_symbol !== "string") {
        throw new Error("option target local_symbol must be display text");
      }
      if (!exactIdentityText(payload.trading_class) || !exactIdentityText(payload.currency)) {
        throw new Error("option target exact provider series metadata is required");
      }
      if (!(typeof payload.multiplier === "number" && Number.isFinite(payload.multiplier) && payload.multiplier > 0)) {
        throw new Error("option target exact provider multiplier is required");
      }
      if (typeof payload.estimated_greeks !== "boolean") {
        throw new Error("option target estimated_greeks must be boolean");
      }
      const persistentIntent = {
        provider_symbol: payload.provider_symbol,
        mode: payload.mode,
        right: payload.right,
        target_dte: payload.target_dte,
        sec_type: payload.sec_type,
        con_id: payload.con_id,
        local_symbol: payload.local_symbol || "",
        exchange: payload.exchange,
        expiry: payload.expiry,
        expiry_at: payload.expiry_at,
        strike: payload.strike,
        trading_class: payload.trading_class,
        multiplier: payload.multiplier,
        currency: payload.currency,
        estimated_greeks: payload.estimated_greeks,
        target_delta: typeof payload.target_delta === "number" && Number.isFinite(payload.target_delta)
          ? payload.target_delta
          : null,
      };
      return {
        id: item?.id,
        instrument_id: item?.instrument_id,
        route_fingerprint: item?.route_fingerprint,
        symbol: item?.symbol,
        timeframe: item?.timeframe,
        point: item?.point,
        created_at_ms: item?.created_at_ms,
        payload: { intent: persistentIntent },
      };
    }

    function mergeOptionTargetRuntimeFields(localItem, serverItem, options = {}) {
      const localPayload = optionTargetPayload(localItem);
      const serverPayload = optionTargetPayload(serverItem);
      const preserveLocalIntent = Boolean(options.preserveLocalIntent);
      if (!localPayload || !serverPayload || !sameOptionTargetContract(localPayload, serverPayload)) {
        return preserveLocalIntent ? localItem : serverItem;
      }
      const localContainer = localItem?.payload;
      const serverContainer = serverItem?.payload;
      if (!localContainer?.intent || !serverContainer?.intent || !serverContainer?.market_sample) {
        return preserveLocalIntent ? localItem : serverItem;
      }
      const payload = {
        intent: preserveLocalIntent ? { ...localContainer.intent } : { ...serverContainer.intent },
        market_sample: { ...serverContainer.market_sample },
      };
      return {
        ...(preserveLocalIntent ? { ...serverItem, ...localItem } : serverItem),
        payload,
        text: optionTargetLabelText({ ...payload.intent, ...payload.market_sample }),
      };
    }

    function optionTargetsEnabled() {
      return state.indicators?.optionTargets?.enabled !== false;
    }

    function optionTargetsFastStatusEnabled() {
      return optionTargetsEnabled() && state.indicators?.optionTargets?.fastStatus !== false;
    }

    function optionTargetsPulseEnabled() {
      return uiMotionEnabled() && optionTargetsEnabled() && state.indicators?.optionTargets?.pulse !== false;
    }

    function optionTargetPulseItemActive(item, now = Date.now()) {
      const key = optionTargetIdentityKey(item);
      if (!key) return false;
      const marked = Number(state.optionTargets?.refreshStartedAt?.[key] || 0);
      return Number.isFinite(marked) && marked > 0 && now - marked < OPTION_TARGET_SPINNER_CYCLE_MS;
    }

    function optionTargetsPulseActive(now = Date.now()) {
      if (!optionTargetsPulseEnabled()) return false;
      return currentOptionTargets().some(item => optionTargetPulseItemActive(item, now));
    }

    function optionTargetRefreshStartedAt(item, now = Date.now()) {
      const key = optionTargetIdentityKey(item);
      state.optionTargets.refreshStartedAt = state.optionTargets.refreshStartedAt || {};
      const marked = Number(key ? state.optionTargets.refreshStartedAt[key] : 0);
      if (Number.isFinite(marked) && marked > 0) return marked;
      const created = Number(item?.created_at_ms || optionTargetPayload(item)?.created_at_ms || 0);
      const started = Number.isFinite(created) && created > 0 ? created : now;
      if (key) state.optionTargets.refreshStartedAt[key] = started;
      return started;
    }

    function markOptionTargetRefreshStarted(item, now = Date.now()) {
      const key = optionTargetIdentityKey(item);
      if (!key) return;
      state.optionTargets.refreshStartedAt = state.optionTargets.refreshStartedAt || {};
      const marked = Number(state.optionTargets.refreshStartedAt[key] || 0);
      if (Number.isFinite(marked) && marked > 0 && now - marked < OPTION_TARGET_SPINNER_CYCLE_MS) {
        scheduleOptionTargetPulseRender();
        return;
      }
      state.optionTargets.refreshStartedAt[key] = now;
      scheduleOptionTargetPulseRender();
    }

    function optionTargetRefreshPhase(item, now = Date.now()) {
      const started = optionTargetRefreshStartedAt(item, now);
      return Math.min(Math.max((now - started) / Math.max(OPTION_TARGET_SPINNER_CYCLE_MS, 1), 0), 0.995);
    }

    function drawOptionTargetRefreshSpinner(ctx, screen, item, selected, now = Date.now()) {
      const phase = optionTargetRefreshPhase(item, now);
      const color = selected ? "gold" : "blue";
      const radius = selected ? 8 : 7;
      const start = -Math.PI / 2;
      const head = start + phase * Math.PI * 2;
      const tailPulse = 0.45 + 0.55 * Math.sin(phase * Math.PI);
      const tail = Math.PI * (0.18 + tailPulse * 0.36);
      const headWidth = 0.85 + tailPulse * (selected ? 1.55 : 1.3);
      const tailTipWidth = selected ? 0.34 : 0.3;
      const headX = screen.x + Math.cos(head) * radius;
      const headY = screen.y + Math.sin(head) * radius;
      ctx.save();
      ctx.lineCap = "round";
      ctx.shadowColor = themeColor(color, selected ? 0.42 : 0.34);
      ctx.shadowBlur = selected ? 7 : 5;
      ctx.strokeStyle = themeColor(color, 0.16);
      ctx.lineWidth = 0.8;
      ctx.beginPath();
      ctx.arc(screen.x, screen.y, radius, 0, Math.PI * 2);
      ctx.stroke();
      const segments = 16;
      for (let index = 0; index < segments; index += 1) {
        const from = index / segments;
        const to = (index + 1) / segments;
        const ease = to * to * (3 - 2 * to);
        ctx.strokeStyle = themeColor(color, 0.14 + ease * (0.56 + tailPulse * 0.18));
        ctx.lineWidth = tailTipWidth + ease * (headWidth - tailTipWidth);
        ctx.beginPath();
        ctx.arc(screen.x, screen.y, radius, head - tail + tail * from, head - tail + tail * to);
        ctx.stroke();
      }
      ctx.shadowBlur = selected ? 9 : 6;
      const gradient = ctx.createRadialGradient(headX - 1.5, headY - 1.8, 0.8, headX, headY, headWidth * 1.75);
      gradient.addColorStop(0, "rgba(255,255,255,0.92)");
      gradient.addColorStop(0.34, themeColor(color, selected ? 0.98 : 0.92));
      gradient.addColorStop(1, themeColor(color, 0.45));
      ctx.fillStyle = gradient;
      ctx.beginPath();
      ctx.ellipse(headX, headY, headWidth * 1.28, headWidth * 0.92, head + Math.PI / 2, 0, Math.PI * 2);
      ctx.fill();
      ctx.restore();
    }

    function currentOptionTargets() {
      return allOptionTargets().filter(item =>
        exactIdentityText(item?.instrument_id) === exactIdentityText(state.instrumentId)
        && exactIdentityText(item?.route_fingerprint) === instrumentRouteFingerprint()
        && String(item?.timeframe || "") === String(state.timeframe || "")
      );
    }

    function allOptionTargets() {
      pruneOptionTargetDeletes();
      return (state.optionTargets?.items || []).filter(item => !optionTargetDeleted(item));
    }

    function optionTargetPayload(item) {
      const payload = item?.payload;
      const intent = payload?.intent;
      const marketSample = payload?.market_sample;
      if (!intent || typeof intent !== "object" || Array.isArray(intent)) return null;
      if (!marketSample || typeof marketSample !== "object" || Array.isArray(marketSample)) return null;
      return { ...intent, ...marketSample };
    }

    function optionTargetItemsSignature(items) {
      try {
        return JSON.stringify(items || []);
      } catch {
        return `${Date.now()}`;
      }
    }

    function optionTargetDrawingPayload(_object) {
      return null;
    }
    function clearOptionTargetRuntime(_id) {
      return;
    }

    function selectedOptionTarget() {
      const identityKey = state.optionTargets?.selectedId;
      return identityKey ? optionTargetByIdentityKey(identityKey, allOptionTargets()) : null;
    }

    function selectOptionTarget(identityKey) {
      const key = typeof identityKey === "string" ? identityKey : "";
      if (!key || !optionTargetByIdentityKey(key, allOptionTargets())) return false;
      setChartObjectSelection("option-target", key);
      applyDrawingUi();
      renderDrawingOverlay();
      renderOptionTargetActiveList();
      return true;
    }

    function optionTargetPoint(item) {
      const point = item?.point;
      if (!point || typeof point !== "object") return null;
      if (finiteSeriesNumber(point.price) === null) return null;
      return point;
    }

    const OPTION_TARGETS_MEMORY_TTL_MS = 5000;
    const optionTargetsMemoryByScope = new Map();

    function optionTargetCurrentScopeKey() {
      return optionTargetScopeKey(state.instrumentId, instrumentRouteFingerprint());
    }

    function optionTargetMutationPending(item = null) {
      const pending = state.optionTargets?.pendingMutationKeys;
      if (!pending || typeof pending !== "object") return false;
      if (item) {
        const identityKey = optionTargetIdentityKey(item);
        return Boolean(identityKey && pending[identityKey]);
      }
      const scopeKey = optionTargetCurrentScopeKey();
      return Boolean(scopeKey && Object.keys(pending).some(identityKey => {
        try {
          const identity = JSON.parse(identityKey);
          return Array.isArray(identity)
            && identity.length === 3
            && optionTargetScopeKey(identity[0], identity[1]) === scopeKey;
        } catch {
          return false;
        }
      }));
    }

    function beginOptionTargetMutation(item) {
      const identityKey = optionTargetIdentityKey(item);
      if (!identityKey) throw new Error("option target mutation identity is required");
      const generation = createBrowserUuidV4();
      state.optionTargets.pendingMutationKeys = state.optionTargets.pendingMutationKeys || {};
      state.optionTargets.pendingMutationKeys[identityKey] = generation;
      return { identityKey, generation };
    }

    function finishOptionTargetMutation(mutation) {
      if (!mutation?.identityKey || !mutation?.generation) return;
      const pending = state.optionTargets?.pendingMutationKeys;
      if (pending?.[mutation.identityKey] === mutation.generation) {
        delete pending[mutation.identityKey];
      }
    }

    function mergeOptionTargetsFromServer(serverItems, options = {}) {
      const localItems = allOptionTargets();
      const localByKey = new Map(localItems.map(item => [optionTargetIdentityKey(item), item]));
      const draggingKey = typeof state.optionTargets.draggingId === "string" ? state.optionTargets.draggingId : "";
      const affectedScopes = new Set(
        Array.isArray(options.scopeKeys)
          ? options.scopeKeys.filter(scopeKey => typeof scopeKey === "string" && scopeKey)
          : typeof options.scopeKey === "string" && options.scopeKey
            ? [options.scopeKey]
            : (serverItems || []).map(item => optionTargetScopeKey(item?.instrument_id, item?.route_fingerprint)).filter(Boolean)
      );
      const merged = [];
      const seen = new Set();
      const responseIdentityKeys = new Set();

      for (const serverItem of serverItems || []) {
        const identityKey = optionTargetIdentityKey(serverItem);
        if (!identityKey) throw new Error("option target response contains an invalid exact identity");
        if (responseIdentityKeys.has(identityKey)) {
          throw new Error("option target response contains a duplicate exact identity");
        }
        responseIdentityKeys.add(identityKey);
      }

      const preferLocal = (local, serverItem) => {
        const identityKey = optionTargetIdentityKey(local || serverItem);
        const mutationPending = Boolean(local && optionTargetMutationPending(local));
        if (!local) return serverItem;
        return mergeOptionTargetRuntimeFields(local, serverItem, {
          preserveLocalIntent: Boolean(identityKey && (identityKey === draggingKey || mutationPending)),
        });
      };

      for (const serverItem of serverItems || []) {
        const identityKey = optionTargetIdentityKey(serverItem);
        const scopeKey = optionTargetScopeKey(serverItem?.instrument_id, serverItem?.route_fingerprint);
        if (!identityKey || !affectedScopes.has(scopeKey) || optionTargetDeleted(serverItem)) continue;
        seen.add(identityKey);
        merged.push(preferLocal(localByKey.get(identityKey), serverItem));
      }
      for (const local of localItems) {
        const identityKey = optionTargetIdentityKey(local);
        const scopeKey = optionTargetScopeKey(local?.instrument_id, local?.route_fingerprint);
        if (!identityKey || seen.has(identityKey) || optionTargetDeleted(local)) continue;
        if (!affectedScopes.has(scopeKey) || optionTargetMutationPending(local) || identityKey === draggingKey) merged.push(local);
      }
      return merged;
    }

    function applyOptionTargetsFromServer(serverItems, options = {}) {
      const previousByKey = new Map(allOptionTargets().map(item => [optionTargetIdentityKey(item), optionTargetItemsSignature(item)]));
      const previousSignature = optionTargetItemsSignature(state.optionTargets.items);
      const previousSelectedId = state.optionTargets.selectedId;
      state.optionTargets.items = mergeOptionTargetsFromServer(serverItems, options);
      const changed = previousSignature !== optionTargetItemsSignature(state.optionTargets.items);
      if (typeof options.scopeKey === "string" && options.scopeKey) {
        optionTargetsMemoryByScope.set(options.scopeKey, { at: Date.now(), promise: null });
      }
      if (state.optionTargets.selectedId && !optionTargetByIdentityKey(state.optionTargets.selectedId, allOptionTargets())) {
        state.optionTargets.selectedId = null;
      }
      const selectionChanged = previousSelectedId !== state.optionTargets.selectedId;
      renderOptionTargetActiveList();
      applyDrawingUi();
      if (changed || selectionChanged) {
        touchChartUserObjectsVersion();
        (serverItems || []).forEach(item => {
          const identityKey = optionTargetIdentityKey(item);
          if (identityKey && previousByKey.has(identityKey) && previousByKey.get(identityKey) !== optionTargetItemsSignature(item)) {
            markOptionTargetRefreshStarted(item);
          }
        });
        requestObjectLayerRedraw(options.reason || "option targets updated");
      }
      scheduleOptionTargetPulseRender();
      return state.optionTargets.items;
    }

    async function fetchOptionTargets(options = {}) {
      const now = Date.now();
      const requestInstrumentId = exactIdentityText(state.instrumentId);
      const requestRouteFingerprint = instrumentRouteFingerprint();
      const requestScopeKey = optionTargetScopeKey(requestInstrumentId, requestRouteFingerprint);
      if (!requestScopeKey) return state.optionTargets.items;
      const memory = optionTargetsMemoryByScope.get(requestScopeKey) || { at: 0, promise: null };
      if (!options.force) {
        if (memory.promise) return memory.promise;
        if (now - Number(memory.at || 0) <= OPTION_TARGETS_MEMORY_TTL_MS) {
          return state.optionTargets.items;
        }
        if (optionTargetMutationPending()) {
          return state.optionTargets.items;
        }
      }
      const params = new URLSearchParams({
        instrument_id: requestInstrumentId,
        route_fingerprint: requestRouteFingerprint,
      });
      const request = fetchJson(`/api/option-targets?${params.toString()}`, { cache: "no-store", sharedTtlMs: options.force ? 0 : OPTION_TARGETS_MEMORY_TTL_MS })
        .then(payload => {
          if (
            exactIdentityText(payload?.instrument_id) !== requestInstrumentId
            || exactIdentityText(payload?.route_fingerprint) !== requestRouteFingerprint
          ) return state.optionTargets.items;
          const items = payload?.ok && Array.isArray(payload.items) ? payload.items : [];
          return applyOptionTargetsFromServer(items, {
            ...options,
            reason: "option targets fetched",
            scopeKey: requestScopeKey,
          });
        })
        .catch(error => {
          const currentMemory = optionTargetsMemoryByScope.get(requestScopeKey);
          if (currentMemory?.promise === request) optionTargetsMemoryByScope.delete(requestScopeKey);
          throw error;
        });
      optionTargetsMemoryByScope.set(requestScopeKey, { at: now, promise: request });
      return request;
    }

    async function saveOptionTarget(item, options = {}) {
      const create = options.operation === "create";
      const update = options.operation === "update";
      if (!create && !update) throw new Error("option target save operation is required");
      if (update && optionTargetDeleted(item)) return null;
      const requestInstrumentId = exactIdentityText(item?.instrument_id);
      const requestRouteFingerprint = exactIdentityText(item?.route_fingerprint);
      if (!requestInstrumentId || !requestRouteFingerprint) {
        throw new Error("option target qualified identity is required");
      }
      const mutation = beginOptionTargetMutation(item);
      const url = update ? `/api/option-targets/${encodeURIComponent(item.id)}` : "/api/option-targets";
      try {
        const payload = await fetchJson(url, {
          method: update ? "PUT" : "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(optionTargetPersistentItem(item)),
          cache: "no-store",
          sharedTtlMs: 0,
        });
        if (payload?.ok === false) throw new Error(apiErrorMessage(payload, "option target save failed"));
        if (exactIdentityText(payload?.instrument_id) !== requestInstrumentId) throw new Error("option target instrument_id mismatch");
        if (exactIdentityText(payload?.route_fingerprint) !== requestRouteFingerprint) throw new Error("option target route_fingerprint mismatch");
        const saved = payload.item || item;
        if (state.instrumentId !== requestInstrumentId || instrumentRouteFingerprint() !== requestRouteFingerprint) return null;
        if (state.optionTargets.pendingMutationKeys?.[mutation.identityKey] !== mutation.generation) return null;
        const savedIdentityKey = optionTargetIdentityKey(saved);
        if (!savedIdentityKey || optionTargetDeleted(saved)) return null;
        const evictedIds = new Set(
          Array.isArray(payload.evicted_ids)
            ? payload.evicted_ids.filter(id => exactIdentityText(id))
            : [],
        );
        const next = allOptionTargets().filter(row => (
          optionTargetIdentityKey(row) !== savedIdentityKey
          && !evictedIds.has(row?.id)
        ));
        next.push(saved);
        state.optionTargets.items = next;
        touchChartUserObjectsVersion();
        markOptionTargetRefreshStarted(saved);
        renderOptionTargetActiveList();
        requestObjectLayerRedraw("option target saved", { force: true });
        return saved;
      } finally {
        finishOptionTargetMutation(mutation);
      }
    }

    async function addOptionTargetFromExactContract(contract, requestScope) {
      if (!optionTargetRequestScopeIsCurrent(requestScope)) return null;
      if (!sameOptionTargetContract(contract, contract)) {
        throw new Error("option target exact provider contract is required");
      }
      if (
        !exactIdentityText(contract.provider_symbol)
        || !["conservative", "normal", "aggressive"].includes(contract.mode)
        || !["0dte", "1dte"].includes(contract.target_dte)
        || typeof contract.estimated_greeks !== "boolean"
      ) {
        throw new Error("option target exact intent metadata is required");
      }
      const targetPoint = requestScope.targetPoint;
      const now = Date.now();
      const item = {
        id: `opt-${createBrowserUuidV4()}`,
        instrument_id: requestScope.instrumentId,
        route_fingerprint: requestScope.routeFingerprint,
        symbol: requestScope.displaySymbol,
        timeframe: requestScope.timeframe,
        point: { ...targetPoint },
        payload: {
          intent: {
            provider_symbol: contract.provider_symbol,
            mode: contract.mode,
            right: contract.right,
            target_dte: contract.target_dte,
            sec_type: contract.sec_type,
            con_id: contract.con_id,
            local_symbol: contract.local_symbol || "",
            exchange: contract.exchange,
            expiry: contract.expiry,
            expiry_at: contract.expiry_at,
            strike: contract.strike,
            trading_class: contract.trading_class,
            multiplier: contract.multiplier,
            currency: contract.currency,
            estimated_greeks: contract.estimated_greeks,
            target_delta: typeof contract.target_delta === "number" && Number.isFinite(contract.target_delta)
              ? contract.target_delta
              : null,
          },
          market_sample: {},
        },
        text: optionTargetLabelText(contract),
        created_at_ms: now,
      };
      const saved = await saveOptionTarget(item, { operation: "create" });
      if (!saved || !optionTargetRequestScopeIsCurrent(requestScope)) return null;
      selectOptionTarget(optionTargetIdentityKey(saved));
      ensureOptionTargetPulseRender();
      renderDrawingOverlay();
      return saved;
    }

    async function deleteOptionTarget(identityKey) {
      const item = optionTargetByIdentityKey(identityKey, state.optionTargets?.items || []);
      if (!item) return;
      const params = new URLSearchParams({
        instrument_id: item.instrument_id,
        route_fingerprint: item.route_fingerprint,
      });
      try {
        const payload = await fetchJson(`/api/option-targets/${encodeURIComponent(item.id)}?${params.toString()}`, {
          method: "DELETE",
          cache: "no-store",
          sharedTtlMs: 0,
        });
        if (payload?.ok === false || payload?.deleted !== true) {
          throw new Error(apiErrorMessage(payload, "option target delete failed"));
        }
        if (
          exactIdentityText(payload?.instrument_id) !== exactIdentityText(item.instrument_id)
          || exactIdentityText(payload?.route_fingerprint) !== exactIdentityText(item.route_fingerprint)
        ) throw new Error("option target delete exact identity mismatch");
        applyOptionTargetDeleted(item, { forceRender: true });
        broadcastOptionTargetDeleted(item);
      } catch (error) {
        console.warn("option target delete failed", requestErrorMessage(error, "option target delete failed"));
        await fetchOptionTargets({ force: true, reason: "option target delete rejected" }).catch(() => null);
      }
    }

    function ensureOptionTargetPulseRender() {
      scheduleOptionTargetPulseRender();
    }

    function stopOptionTargetPulseRender() {
      if (!state.optionTargets?.renderTimer) return;
      window.clearTimeout(state.optionTargets.renderTimer);
      state.optionTargets.renderTimer = null;
    }

    function scheduleOptionTargetPulseRender() {
      if (!state.optionTargets || state.optionTargets.renderTimer || !optionTargetsPulseActive()) return;
      state.optionTargets.renderTimer = window.setTimeout(() => {
        state.optionTargets.renderTimer = null;
        if (document.visibilityState !== "visible" || !optionTargetsPulseActive()) return;
        renderDrawingOverlay({ animate: true });
        scheduleOptionTargetPulseRender();
      }, OPTION_TARGET_RENDER_MS);
    }

    function optionTargetChoicePayloads(payload, requestScope) {
      const rows = [];
      if (Array.isArray(payload?.dte_payloads)) {
        payload.dte_payloads.forEach(item => rows.push(...optionTargetChoicePayloads(item, requestScope)));
        return rows;
      }
      if (payload?.ok) rows.push(payload);
      if (Array.isArray(payload?.alternatives)) rows.push(...payload.alternatives);
      const seen = new Set();
      return rows
        .filter(item => item && typeof item === "object")
        .filter(item => {
          if (!optionTargetPayloadMatchesRequestScope(item, requestScope)) return false;
          if (!sameOptionTargetContract(item, item)) return false;
          if (!exactIdentityText(item.provider_symbol)) return false;
          if (!["conservative", "normal", "aggressive"].includes(item.mode)) return false;
          if (!["0dte", "1dte"].includes(item.target_dte)) return false;
          if (typeof item.estimated_greeks !== "boolean") return false;
          const conId = item.con_id;
          if (seen.has(conId)) return false;
          seen.add(conId);
          return true;
        })
        .sort((a, b) => (optionTargetPositiveNumber(a?.strike) ?? Number.POSITIVE_INFINITY)
          - (optionTargetPositiveNumber(b?.strike) ?? Number.POSITIVE_INFINITY)
          || optionTargetSortPrice(a) - optionTargetSortPrice(b))
        .slice(0, OPTION_TARGET_CHOICES_PER_EXPIRY);
    }

    function optionTargetChoiceGroups(payload, requestScope) {
      if (!Array.isArray(payload?.dte_payloads)) {
        return [{ label: "", rows: optionTargetChoicePayloads(payload, requestScope) }].filter(group => group.rows.length);
      }
      return payload.dte_payloads
        .map(item => ({ label: optionTargetGroupLabel(item), rows: optionTargetChoicePayloads(item, requestScope), message: item?.message || "" }))
        .filter(group => group.rows.length);
    }

    function optionTargetDteErrorMessage(payload) {
      const dtePayloads = Array.isArray(payload?.dte_payloads) ? payload.dte_payloads : [];
      if (
        payload?.code === "OPTION_PREMIUM_CAP_REQUIRED"
        || dtePayloads.some(item => item?.code === "OPTION_PREMIUM_CAP_REQUIRED")
      ) {
        return "Option premium cap is required for this instrument. Open Settings, expand Option Points, and set Premium cap for the current instrument before pricing an option point.";
      }
      if (!Array.isArray(payload?.dte_payloads)) return optionTargetErrorMessage(null, payload);
      const messages = dtePayloads
        .map(item => {
          const label = String(item?.target_dte || "").toUpperCase() || "DTE";
          const details = item?.available_expiries?.length
            ? `${item.message || "No matching contracts."} Available: ${item.available_expiries.join(", ")}`
            : (item?.message || "No matching contracts.");
          return `${label}: ${details}`;
        })
        .filter(Boolean);
      const uniqueMessages = [...new Set(messages)];
      return uniqueMessages.length ? uniqueMessages.join("\n") : optionTargetErrorMessage(null, payload);
    }

    function renderOptionTargetChoices(payload, requestScope) {
      const node = document.getElementById("chart-context-menu");
      const targetPoint = requestScope?.targetPoint;
      const chartPrice = finiteSeriesNumber(targetPoint?.price);
      const groups = optionTargetChoiceGroups(payload, requestScope);
      if (!node || !optionTargetRequestScopeIsCurrent(requestScope) || !targetPoint || chartPrice === null || groups.length === 0) {
        return false;
      }
      node.querySelector(".option-target-choices")?.remove();
      const choices = document.createElement("div");
      choices.className = "option-target-choices";
      choices.innerHTML = `
        <div class="option-target-note">${escapeHtml(payload.message || "Choose option contract:")}</div>
        <div class="option-target-groups"></div>`;
      const groupBox = choices.querySelector(".option-target-groups");
      let firstButton = null;
      groups.forEach(group => {
        const column = document.createElement("div");
        column.className = "option-target-group";
        column.innerHTML = `<div class="option-target-group-title">${escapeHtml(group.label || "Options")}</div>`;
        group.rows.forEach(alternative => {
          const button = document.createElement("button");
          button.type = "button";
          button.className = "tool-button option-target-choice";
          button.textContent = optionTargetLabelText(alternative);
          button.title = `Use ${alternative.local_symbol || alternative.label || "option variant"}`;
          button.addEventListener("click", async () => {
            if (!optionTargetRequestScopeIsCurrent(requestScope)) {
              closeContextMenu();
              return;
            }
            try {
              const canonicalScope = optionTargetRequestScopeWithServerAxis(
                requestScope,
                alternative,
              );
              const saved = await addOptionTargetFromExactContract(
                alternative,
                canonicalScope,
              );
              if (!saved) return;
              closeContextMenu();
            } catch (error) {
              console.warn("option target add failed", requestErrorMessage(error, "option target add failed"));
              window.alert(`Option target add failed: ${optionTargetErrorMessage(error)}`);
            }
          });
          column.appendChild(button);
          if (!firstButton) firstButton = button;
        });
        groupBox.appendChild(column);
      });
      node.appendChild(choices);
      if (firstButton) firstButton.focus({ preventScroll: true });
      const menuW = node.offsetWidth || 280;
      const menuH = node.offsetHeight || 260;
      node.style.left = `${clamp(state.contextMenu.x, 8, window.innerWidth - menuW - 8)}px`;
      node.style.top = `${clamp(state.contextMenu.y, 8, window.innerHeight - menuH - 8)}px`;
      return true;
    }

    async function addOptionTargetLabelFromContext() {
      const { bar, price, point } = state.contextMenu || {};
      const chartPrice = finiteSeriesNumber(price);
      if (chartPrice === null || !point) return;
      let requestScope;
      try {
        requestScope = optionTargetRequestScopeForPoint(optionTargetConfirmedAnchorPoint({
          ...point,
          price: chartPrice,
        }, bar));
      } catch (error) {
        window.alert(optionTargetErrorMessage(error));
        return;
      }
      const requestInstrumentId = requestScope.instrumentId;
      const requestRouteFingerprint = requestScope.routeFingerprint;
      const pointMs = Date.parse(requestScope.targetPoint.ts);
      const params = new URLSearchParams({
        instrument_id: requestInstrumentId,
        expected_route_fingerprint: requestRouteFingerprint,
        target_price: String(chartPrice),
        target_ts: new Date(pointMs).toISOString(),
        timeframe: requestScope.timeframe,
        right: "auto",
        mode: "normal",
      });
      const button = document.querySelector('[data-context-action="option-target"]');
      const buttonLabel = button?.querySelector(".context-action-label");
      if (buttonLabel) buttonLabel.textContent = "Pricing...";
      try {
        const dtePayloads = await Promise.all(["0dte", "1dte"].map(dte => {
          const dteParams = new URLSearchParams(params);
          dteParams.set("dte", dte);
          return fetchJson(`/api/options/target-price?${dteParams.toString()}`, { cache: "no-store", sharedTtlMs: 0 })
            .then(payload => admitOptionTargetPricingResponse(payload, requestScope, dte));
        }));
        if (!optionTargetRequestScopeIsCurrent(requestScope)) return;
        const payload = {
          ok: dtePayloads.some(item => item?.ok || Array.isArray(item?.alternatives)),
          message: "Choose option contract:",
          dte_payloads: dtePayloads,
        };
        if (!renderOptionTargetChoices(payload, requestScope)) window.alert(optionTargetDteErrorMessage(payload));
      } catch (error) {
        console.warn("option target pricing failed", requestErrorMessage(error, "option target pricing failed"));
        window.alert(`Option target pricing failed: ${optionTargetErrorMessage(error)}`);
      } finally {
        if (buttonLabel) buttonLabel.textContent = "Option price";
      }
    }

    function optionTargetSummaryLine(item) {
      const payload = optionTargetPayload(item) || item;
      const scope = `${String(item?.symbol || "").toUpperCase()} ${String(item?.timeframe || "")}`.trim();
      const fastText = optionTargetFastStatusText(item);
      return `${scope} · ${optionTargetLabelText(payload).replace(/\n/g, " · ")}${fastText ? ` · ${fastText}` : ""}`;
    }

    function renderOptionTargetActiveList() {
      const node = document.getElementById("option-target-active-list");
      if (!node) return;
      const rows = allOptionTargets();
      if (!rows.length) {
        node.innerHTML = `<div class="muted">No active option points.</div>`;
        return;
      }
      node.innerHTML = rows.slice().reverse().slice(0, 24).map(item => `
        <div class="option-target-active-row ${optionTargetIdentityKey(item) === state.optionTargets.selectedId ? "active" : ""}">
          <button class="option-target-select" type="button" data-option-target-select="${escapeHtml(optionTargetIdentityKey(item))}" aria-pressed="${optionTargetIdentityKey(item) === state.optionTargets.selectedId ? "true" : "false"}">
            <span>${escapeHtml(optionTargetSummaryLine(item))}</span>
          </button>
          <button class="tool-button option-target-delete" type="button" data-option-target-delete="${escapeHtml(optionTargetIdentityKey(item))}" aria-label="Delete option target ${escapeHtml(optionTargetSummaryLine(item))}">×</button>
        </div>`).join("");
      node.querySelectorAll("[data-option-target-select]").forEach(button => {
        button.addEventListener("click", () => {
          selectOptionTarget(button.dataset.optionTargetSelect);
        });
      });
      node.querySelectorAll("[data-option-target-delete]").forEach(button => {
        button.addEventListener("click", event => {
          event.stopPropagation();
          deleteOptionTarget(button.dataset.optionTargetDelete);
        });
      });
    }

    function optionTargetScreenPoint(item, axis) {
      const point = optionTargetPoint(item);
      if (!point) return null;
      const screenIndex = screenIndexForTimestampPoint(point, axis);
      if (!Number.isFinite(screenIndex)) return null;
      return {
        x: axis.x(screenIndex),
        y: axis.y(point.price),
        price: point.price,
        ts: point.ts,
      };
    }

    function optionTargetLabelLines(model) {
      if (model.errorText) return [model.contractText, `fair ${model.fairText}`, model.errorText.slice(0, 32)];
      return [
        model.contractText,
        `fair ${model.fairText} d-${model.deltaText}`,
        optionTargetMarketLine(model),
        model.fastText,
      ].filter(Boolean).slice(0, 4);
    }

    function drawOptionTargetLabelModel(ctx, model, x0, y0, selected) {
      const baseFont = "750 10.5px ui-monospace, SFMono-Regular, Menlo, monospace";
      const strongFont = "950 10.5px ui-monospace, SFMono-Regular, Menlo, monospace";
      const baseColor = selected ? themeColor("gold", 1) : themeColor("up", 1);
      ctx.font = baseFont;
      ctx.fillStyle = baseColor;
      const charW = ctx.measureText("0").width || 6.4;
      const lineH = 11;
      ctx.fillText(model.right, x0, y0 + lineH);
      ctx.fillText(model.expiry, x0 + charW * 3, y0 + lineH);
      ctx.font = strongFont;
      ctx.fillText(model.strikeText, x0 + charW * 10, y0 + lineH);
      ctx.font = baseFont;
      if (model.errorText) {
        ctx.fillText(`fair ${model.fairText}`, x0, y0 + lineH * 2);
        ctx.fillText(model.errorText.slice(0, 32), x0, y0 + lineH * 3);
        return;
      }
      ctx.fillText("fair", x0, y0 + lineH * 2);
      ctx.fillStyle = model.fairDisplayOnly
        ? themeColor("neutral", 0.72)
        : themeColor("gold", 1);
      ctx.fillText(String(model.fairText).padStart(5, " "), x0 + charW * 5, y0 + lineH * 2);
      ctx.fillStyle = baseColor;
      ctx.fillText(`d-${model.deltaText}`, x0 + charW * 11.8, y0 + lineH * 2);
      if (model.hasLive) {
        const bidText = String(model.bidText).padStart(5, " ");
        const bidX = x0 + charW * 5;
        ctx.fillText("B/A", x0, y0 + lineH * 3);
        ctx.fillText(bidText, bidX, y0 + lineH * 3);
        ctx.fillText(`/${model.askText}`, bidX + ctx.measureText(bidText).width, y0 + lineH * 3);
      } else {
        const marketLine = optionTargetMarketLine(model);
        if (marketLine) ctx.fillText(marketLine, x0, y0 + lineH * 3);
      }
      if (model.fastText) {
        ctx.fillStyle = themeColor("gold", 0.96);
        ctx.fillText(model.fastText, x0, y0 + lineH * 4);
      }
    }

    function drawOptionTargets(ctx, visible, pad, priceH, xStep, x, y, width) {
      if (!optionTargetsEnabled()) return;
      const bars = visible?.bars || [];
      if (!bars.length) return;
      const axis = {
        bars,
        allBars: visible.allBars || bars,
        futureAxis: state.snapshot?.future_axis,
        start: visible.start || 0,
        xStep,
        x,
        y,
        slotStep: intervalMinutesFromState(),
        width,
        pad,
        barsVersion: chartRenderVersions.bars,
      };
      ctx.save();
      ctx.font = "700 10.5px -apple-system, BlinkMacSystemFont, sans-serif";
      ctx.textBaseline = "alphabetic";
      for (const item of currentOptionTargets()) {
        const screen = optionTargetScreenPoint(item, axis);
        const payload = optionTargetPayload(item);
        if (!screen || !payload) continue;
        const selected = optionTargetIdentityKey(item) === state.optionTargets.selectedId;
        const fastText = optionTargetFastStatusText(item);
        const model = optionTargetLabelModel(payload);
        model.fastText = fastText;
        const text = optionTargetLabelText(payload);
        const tooltipText = `${text}${fastText ? `\n${fastText}` : ""}`;
        const lines = optionTargetLabelLines(model);
        const lineH = 11;
        const x0 = screen.x + 7;
        const y0 = screen.y - (lines.length * lineH) - 4;
        const pulseNow = Date.now();
        ctx.fillStyle = selected ? themeColor("gold", 0.98) : themeColor("blue", 0.95);
        ctx.beginPath();
        ctx.arc(screen.x, screen.y, selected ? 4 : 3, 0, Math.PI * 2);
        ctx.fill();
        if (optionTargetsPulseEnabled() && optionTargetPulseItemActive(item, pulseNow)) {
          drawOptionTargetRefreshSpinner(ctx, screen, item, selected, pulseNow);
        }
        drawOptionTargetLabelModel(ctx, model, x0, y0, selected);
        registerCanvasTooltip("price", x0, y0, Math.max(90, ...lines.map(line => ctx.measureText(line).width)), lines.length * lineH + 4, tooltipText);
      }
      ctx.restore();
    }

    function hitTestOptionTarget(localX, localY) {
      if (!optionTargetsEnabled()) return null;
      const geo = priceChartGeometry();
      if (!geo) return null;
      const axis = drawingAxisFromGeo(geo);
      const targets = currentOptionTargets();
      for (let index = targets.length - 1; index >= 0; index -= 1) {
        const item = targets[index];
        const screen = optionTargetScreenPoint(item, axis);
        if (screen && Math.hypot(localX - screen.x, localY - screen.y) <= 12) return optionTargetIdentityKey(item);
      }
      return null;
    }

    function advanceOptionTargetFutureDrag(localX, localY) {
      const geo = priceChartGeometry();
      if (!geo || Number(view.offset || 0) !== 0) return null;
      const plotRight = Number(geo.rect?.width) - Number(geo.pad?.right || 0);
      const edgeZone = clamp(Number(geo.xStep || 0) * 1.5, 18, 48);
      if (!Number.isFinite(plotRight) || !Number.isFinite(localX) || localX < plotRight - edgeZone) {
        return null;
      }
      const futureSlots = providerFutureAxisIndex(drawingAxisFromGeo(geo)).slots;
      const lastFuture = futureSlots[futureSlots.length - 1];
      const visibleLastAbsoluteIndex = Number(geo.start || 0) + geo.bars.length - 1;
      const availableGap = Number.isSafeInteger(lastFuture?.absoluteIndex)
        ? Math.max(lastFuture.absoluteIndex - visibleLastAbsoluteIndex, 0)
        : 0;
      const currentGap = rightGapBars();
      const nextGap = clampRightGapBars(Math.min(currentGap + 1, availableGap));
      if (nextGap <= currentGap) return null;
      view.rightGapBars = nextGap;
      view.rightGapManual = true;
      view.smoothOffsetBars = 0;
      clampView(state.snapshot);
      updateChartNavigationControls();
      renderCharts(state.snapshot, { overlayImmediate: true });
      const updatedGeo = priceChartGeometry();
      if (!updatedGeo) return null;
      const updatedPlotRight = Number(updatedGeo.rect?.width) - Number(updatedGeo.pad?.right || 0);
      const targetX = updatedPlotRight - Math.max(Number(updatedGeo.xStep || 0) * 0.25, 0.5);
      return optionTargetPointFromLocal(targetX, localY);
    }

    function optionTargetPointFromLocal(localX, localY) {
      const point = chartPointFromLocal(localX, localY);
      const price = finiteSeriesNumber(point?.price);
      if (!point || price === null) return null;
      return {
        ts: point.ts,
        barSlot: point.barSlot,
        price,
        future: Boolean(point.future),
      };
    }

    function updateOptionTargetPointLocal(identityKey, point) {
      const key = typeof identityKey === "string" ? identityKey : "";
      if (!key || !point) return null;
      let nextItem = null;
      state.optionTargets.items = allOptionTargets().map(item => {
        if (optionTargetIdentityKey(item) !== key) return item;
        nextItem = {
          ...item,
          point: { ...item.point, ...point },
        };
        return nextItem;
      });
      if (nextItem) {
        renderDrawingOverlay();
        renderOptionTargetActiveList();
      }
      return nextItem;
    }

    function beginOptionTargetDrag(identityKey) {
      const key = typeof identityKey === "string" ? identityKey : "";
      if (!selectOptionTarget(key)) return false;
      state.optionTargets.draggingId = key;
      const item = optionTargetByIdentityKey(key, allOptionTargets());
      return true;
    }

    async function finishOptionTargetDrag(identityKey, point) {
      const key = typeof identityKey === "string" ? identityKey : "";
      if (!key) return null;
      const existing = optionTargetByIdentityKey(key, state.optionTargets?.items || []);
      if (!existing || optionTargetDeleted(existing)) {
        state.optionTargets.draggingId = null;
        return null;
      }
      const item = point ? updateOptionTargetPointLocal(key, point) : optionTargetByIdentityKey(key, allOptionTargets());
      if (!item) {
        state.optionTargets.draggingId = null;
        return null;
      }
      try {
        await saveOptionTarget(item, { operation: "update" });
        applyDrawingUi();
        return optionTargetByIdentityKey(key, allOptionTargets()) || item;
      } catch (error) {
        state.optionTargets.draggingId = null;
        await fetchOptionTargets({ force: true, reason: "option target update rejected" }).catch(() => null);
        throw error;
      } finally {
        state.optionTargets.draggingId = null;
        renderDrawingOverlay({ immediate: true });
        renderOptionTargetActiveList();
      }
    }
