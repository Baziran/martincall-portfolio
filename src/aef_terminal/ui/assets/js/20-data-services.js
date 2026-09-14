
    const PAPER_JOURNAL_REFRESH_DEBOUNCE_MS = 1500;
    const CLIENT_CANONICAL_TAIL_BARS = 512;
    const MARKET_LOCAL_HISTORY_TIMEOUT_MS = 10000;
    const MARKET_LIVE_TAIL_TIMEOUT_MS = 15000;
    const MARKET_CHART_TRANSITION_RETRY_MAX_ATTEMPTS = 3;
    const MARKET_CHART_TRANSITION_RETRY_BASE_DELAY_MS = 150;
    const MARKET_CHART_TRANSITION_RETRY_CODES = new Set([
      "MARKET_CHART_GENERATION_CHANGED",
      "CHART_BARS_WRITE_IN_PROGRESS",
    ]);
    const MARKET_ANALYSIS_LEASE_HEARTBEAT_MS = 20000;
    const MARKET_ANALYSIS_ACTIVE_STATUSES = new Set(["queued", "running", "ready"]);
    const MARKET_ANALYSIS_POLL_MAX_ATTEMPTS = 30;
    const MARKET_ANALYSIS_MISSING_MAX_ATTEMPTS = 6;
    let paperJournalRefreshTimer = null;
    let canonicalLiveTailStore = {
      key: "",
      bars: [],
      snapshot: null,
      verified: false,
    };

    function settleHistoryPresetNotice(options, failure = null) {
      if (options.historyPreset !== true) return false;
      if (state.notices?.history?.code !== "HISTORY_PRESET_LOADING") return false;
      if (!failure) return clearUiNotice("history");
      setUiNotice("history", failure.code, failure.message, {
        state: failure.state || "error",
      });
      return true;
    }

    function projectMarketChartLoadFailure(options, code, message) {
      setUiNotice("market", code, message, { state: "error" });
      settleHistoryPresetNotice(options, { code, message });
      if (!state.snapshot && typeof renderChartTransitionFailureCanvases === "function") {
        renderChartTransitionFailureCanvases(code, message);
      }
    }

    async function fetchMarketSnapshotWithTransitionRetry(
      url,
      fetchOptions,
      requestOwnsExactScope,
    ) {
      let attempt = 0;
      while (true) {
        try {
          return await fetchJson(url, fetchOptions);
        } catch (error) {
          const code = requestErrorCode(error);
          if (
            !MARKET_CHART_TRANSITION_RETRY_CODES.has(code)
            || attempt >= MARKET_CHART_TRANSITION_RETRY_MAX_ATTEMPTS
          ) throw error;
          if (!requestOwnsExactScope()) {
            throw new DOMException("Market request superseded", "AbortError");
          }
          attempt += 1;
          await new Promise(resolve => {
            window.setTimeout(resolve, MARKET_CHART_TRANSITION_RETRY_BASE_DELAY_MS * attempt);
          });
          if (!requestOwnsExactScope()) {
            throw new DOMException("Market request superseded", "AbortError");
          }
        }
      }
    }

    function paperJournalRefreshOptions(snapshot) {
      const setup = snapshot?.trade_setup;
      const action = String(setup?.action || "").toUpperCase();
      return tradeSetupExecutionAuthority(snapshot).ready
        && setup?.ok === true
        && action === "GO"
        ? { force: true }
        : {};
    }

    function paperJournalRefreshActive() {
      return paperPollingActive({ poll: true });
    }

    function refreshPaperJournal(_snapshot, options = {}) {
      if (!paperJournalRefreshActive()) {
        if (paperJournalRefreshTimer) {
          clearTimeout(paperJournalRefreshTimer);
          paperJournalRefreshTimer = null;
        }
        return Promise.resolve(null);
      }
      if (options.force) {
        if (paperJournalRefreshTimer) {
          clearTimeout(paperJournalRefreshTimer);
          paperJournalRefreshTimer = null;
        }
        return loadPaperStats({ force: true });
      }
      if (paperJournalRefreshTimer) clearTimeout(paperJournalRefreshTimer);
      paperJournalRefreshTimer = setTimeout(() => {
        paperJournalRefreshTimer = null;
        if (!paperJournalRefreshActive()) return;
        loadPaperStats({ poll: true });
      }, PAPER_JOURNAL_REFRESH_DEBOUNCE_MS);
      return Promise.resolve(null);
    }

    function abortMarketAnalysis() {
      if (state.requests.analysis.pollTimer) {
        clearTimeout(state.requests.analysis.pollTimer);
        state.requests.analysis.pollTimer = null;
      }
      if (state.requests.analysis.abort) {
        try {
          state.requests.analysis.abort.abort();
        } catch (_) {
          // noop
        }
      }
      state.requests.analysis.abort = null;
      state.requests.analysis.loading = false;
      state.requests.analysis.key = "";
      state.requests.analysis.pollKey = "";
      state.requests.analysis.pollAttempt = 0;
      state.analysisMissingRefreshKey = "";
    }

    function clearMarketAnalysisLeaseState() {
      if (state.requests.analysis.leaseTimer) {
        clearTimeout(state.requests.analysis.leaseTimer);
      }
      state.requests.analysis.leaseTimer = null;
      state.requests.analysis.leaseKey = "";
      state.requests.analysis.leaseInstrumentId = "";
      state.requests.analysis.leaseRouteFingerprint = "";
      state.requests.analysis.leaseActiveSequence = 0;
    }

    function releaseMarketAnalysisLease(options = {}) {
      const leaseSequence = Math.max(
        Number(state.requests.analysis.leaseSequence || 0),
        Number(state.requests.analysis.leaseActiveSequence || 0),
      );
      clearMarketAnalysisLeaseState();
      if (!Number.isSafeInteger(leaseSequence) || leaseSequence <= 0) return false;
      const params = new URLSearchParams({
        analysis_client_id: state.clientId,
        analysis_lease_sequence: String(leaseSequence),
      });
      const url = `/api/market/analysis/release?${params.toString()}`;
      if (options.beacon && typeof navigator.sendBeacon === "function") {
        return navigator.sendBeacon(url);
      }
      void fetch(url, { method: "POST", keepalive: true }).catch(() => {});
      return true;
    }

    function scheduleMarketAnalysisLeaseRenewal() {
      if (state.requests.analysis.leaseTimer) {
        clearTimeout(state.requests.analysis.leaseTimer);
      }
      state.requests.analysis.leaseTimer = setTimeout(() => {
        state.requests.analysis.leaseTimer = null;
        void renewMarketAnalysisLease();
      }, MARKET_ANALYSIS_LEASE_HEARTBEAT_MS);
    }

    async function renewMarketAnalysisLease() {
      const key = String(state.requests.analysis.leaseKey || "").trim();
      const instrumentId = exactIdentityText(
        state.requests.analysis.leaseInstrumentId,
      );
      const routeFingerprint = exactIdentityText(
        state.requests.analysis.leaseRouteFingerprint,
      );
      const leaseSequence = Number(
        state.requests.analysis.leaseActiveSequence || 0,
      );
      if (!key || !Number.isSafeInteger(leaseSequence) || leaseSequence <= 0) return false;
      if (
        document.visibilityState !== "visible"
        || instrumentId !== exactIdentityText(state.instrumentId)
        || routeFingerprint !== instrumentRouteFingerprint()
      ) {
        releaseMarketAnalysisLease({ beacon: document.visibilityState !== "visible" });
        return false;
      }
      const params = new URLSearchParams({
        analysis_client_id: state.clientId,
        analysis_lease_sequence: String(leaseSequence),
        key,
        instrument_id: instrumentId,
        expected_route_fingerprint: routeFingerprint,
      });
      try {
        const response = await fetchJson(
          `/api/market/analysis/lease?${params.toString()}`,
          { method: "POST", timeoutMs: 5000 },
        );
        if (
          String(state.requests.analysis.leaseKey || "").trim() !== key
          || Number(state.requests.analysis.leaseActiveSequence || 0) !== leaseSequence
          || exactIdentityText(state.requests.analysis.leaseInstrumentId) !== instrumentId
          || exactIdentityText(state.requests.analysis.leaseRouteFingerprint) !== routeFingerprint
        ) {
          return false;
        }
        if (response?.renewed === true) {
          scheduleMarketAnalysisLeaseRenewal();
          return true;
        }
        clearMarketAnalysisLeaseState();
        if (document.visibilityState === "visible") {
          queueMicrotask(() => {
            if (
              exactIdentityText(state.instrumentId) === instrumentId
              && instrumentRouteFingerprint() === routeFingerprint
            ) {
              void load({
                force: true,
                queueAnalysis: true,
                requireLatestAnalysis: true,
              });
            }
          });
        }
      } catch (error) {
        if (String(state.requests.analysis.leaseKey || "").trim() === key) {
          scheduleMarketAnalysisLeaseRenewal();
        }
      }
      return false;
    }

    async function refreshRegisteredMarketAnalysis({
      requestInstrumentId,
      requestRouteFingerprint,
      requestSymbol,
      requestSource,
      requestTimeframe,
      range,
      analysisVersionKey = "",
      requireBarTs = "",
      requireCanonicalRevision = 0,
    }) {
      const key = String(state.requests.analysis.leaseKey || "").trim();
      const leaseSequence = Number(
        state.requests.analysis.leaseActiveSequence || 0,
      );
      const expectedKey = String(state.snapshot?.meta?.analysis_key || "").trim();
      if (!expectedKey || !marketAnalysisLeaseMatchesExactScope(
        requestInstrumentId,
        requestRouteFingerprint,
        expectedKey,
      )) return false;
      const params = new URLSearchParams({
        analysis_client_id: state.clientId,
        analysis_lease_sequence: String(leaseSequence),
        key,
        instrument_id: requestInstrumentId,
        expected_route_fingerprint: requestRouteFingerprint,
      });
      if (analysisVersionKey) {
        params.set("analysis_version", String(analysisVersionKey));
      }
      try {
        const response = await fetchJson(
          `/api/market/analysis/refresh?${params.toString()}`,
          { method: "POST", timeoutMs: 5000 },
        );
        if (
          response?.refreshed !== true
          || String(state.requests.analysis.leaseKey || "").trim() !== key
          || Number(state.requests.analysis.leaseActiveSequence || 0) !== leaseSequence
          || exactIdentityText(state.instrumentId) !== requestInstrumentId
          || instrumentRouteFingerprint() !== requestRouteFingerprint
          || state.dataSource !== requestSource
          || state.timeframe !== requestTimeframe
        ) {
          return false;
        }
        scheduleMarketAnalysisLeaseRenewal();
        loadMarketAnalysisSnapshot(
          key,
          requestInstrumentId,
          requestRouteFingerprint,
          requestSymbol,
          requestSource,
          requestTimeframe,
          range,
          { requireBarTs, requireCanonicalRevision },
        );
        debugStep("analysis refresh", `${requestSymbol} ${requestTimeframe} ${key}`);
        return true;
      } catch (error) {
        console.warn("market analysis refresh failed", error);
        return false;
      }
    }

    function marketAnalysisLeaseMatchesExactScope(
      instrumentId,
      routeFingerprint,
      expectedKey = "",
    ) {
      const key = String(state.requests.analysis.leaseKey || "").trim();
      const sequence = Number(state.requests.analysis.leaseActiveSequence || 0);
      const requiredKey = String(expectedKey || "").trim();
      return Boolean(key)
        && Number.isSafeInteger(sequence)
        && sequence > 0
        && exactIdentityText(state.requests.analysis.leaseInstrumentId) === exactIdentityText(instrumentId)
        && exactIdentityText(state.requests.analysis.leaseRouteFingerprint) === exactIdentityText(routeFingerprint)
        && (!requiredKey || key === requiredKey);
    }

    function activateMarketAnalysisLease(key, instrumentId, routeFingerprint, leaseSequence) {
      const exactKey = String(key || "").trim();
      const exactSequence = Number(leaseSequence || 0);
      if (
        !exactKey
        || !instrumentId
        || !routeFingerprint
        || !Number.isSafeInteger(exactSequence)
        || exactSequence <= 0
      ) return false;
      const newDemand = state.requests.analysis.leaseKey !== exactKey
        || Number(state.requests.analysis.leaseActiveSequence || 0) !== exactSequence;
      state.requests.analysis.leaseKey = exactKey;
      state.requests.analysis.leaseInstrumentId = exactIdentityText(instrumentId);
      state.requests.analysis.leaseRouteFingerprint = exactIdentityText(routeFingerprint);
      state.requests.analysis.leaseActiveSequence = exactSequence;
      if (newDemand) {
        if (state.requests.analysis.pollTimer) clearTimeout(state.requests.analysis.pollTimer);
        state.requests.analysis.pollTimer = null;
        state.requests.analysis.pollAttempt = 0;
        state.analysisMissingRefreshKey = "";
      }
      scheduleMarketAnalysisLeaseRenewal();
      return true;
    }

    function isValidMarketSnapshotPayload(snapshot) {
      if (!snapshot || typeof snapshot !== "object") return false;
      if (!snapshot.meta || typeof snapshot.meta !== "object") return false;
      if (snapshot.meta.analysis_only && !snapshot.meta.chart_only) return true;
      if (!Array.isArray(snapshot.bars)) return false;
      if (!snapshot.bars.every((bar) => Number.isFinite(Date.parse(bar?.ts || "")))) return false;
      return snapshot.future_axis === undefined
        || isValidFutureAxisPayload(snapshot.future_axis, snapshot.meta.timeframe);
    }

    function marketSnapshotMatchesExactScope(
      snapshot,
      instrumentId,
      routeFingerprint,
      timeframe,
      range,
    ) {
      const meta = snapshot?.meta || {};
      return (
        exactIdentityText(meta.instrument_id) === instrumentId
        && exactIdentityText(meta.route_fingerprint) === routeFingerprint
        && String(meta.timeframe || "") === timeframe
        && String(meta.chart_range || "") === range
      );
    }

    function isValidFutureAxisPayload(payload, timeframe = state.timeframe) {
      if (!payload || typeof payload !== "object" || Array.isArray(payload)) return false;
      if (payload.kind !== "provider_session_future_axis") return false;
      if (String(payload.timeframe || "") !== String(timeframe || "")) return false;
      if (!["continuous", "verified", "unknown"].includes(String(payload.schedule_state || ""))) return false;
      if (!Number.isSafeInteger(payload.schedule_revision) || payload.schedule_revision < 0) return false;
      const requestedSlots = payload.requested_slots;
      if (!Number.isSafeInteger(requestedSlots) || requestedSlots < 1 || requestedSlots > MAX_RIGHT_GAP_BARS) return false;
      if (typeof payload.complete !== "boolean" || !Array.isArray(payload.slots)) return false;
      if (payload.slots.length > requestedSlots) return false;
      if (payload.schedule_state === "unknown" && payload.slots.length) return false;
      const anchorTimestamp = payload.anchor_ts === null
        ? NaN
        : Date.parse(payload.anchor_ts || "");
      if (payload.slots.length && !Number.isFinite(anchorTimestamp)) return false;
      let previousTimestamp = Number.isFinite(anchorTimestamp)
        ? anchorTimestamp
        : -Infinity;
      let previousOffset = 0;
      for (const item of payload.slots) {
        if (!item || typeof item !== "object" || Array.isArray(item)) return false;
        if (["open", "high", "low", "close", "volume"].some(field => Object.hasOwn(item, field))) return false;
        const timestamp = Date.parse(item.ts || "");
        const barOffset = item.bar_offset;
        if (
          !Number.isFinite(timestamp)
          || !Number.isSafeInteger(barOffset)
          || timestamp <= previousTimestamp
          || barOffset !== previousOffset + 1
        ) return false;
        previousTimestamp = timestamp;
        previousOffset = barOffset;
      }
      return true;
    }

    function futureAxisScheduleRevision(payload) {
      const revision = Number(payload?.schedule_revision ?? 0);
      return Number.isSafeInteger(revision) && revision >= 0 ? revision : 0;
    }

    function futureAxisCanonicalRevision(snapshot) {
      const revision = Number(
        snapshot?.meta?.future_axis_canonical_revision
        ?? snapshot?.meta?.chart_canonical_revision
        ?? 0,
      );
      return Number.isSafeInteger(revision) && revision >= 0 ? revision : 0;
    }

    function futureAxisOrder(payload, canonicalRevision = 0) {
      const anchorTimestamp = Date.parse(payload?.anchor_ts || "");
      const scheduleState = String(payload?.schedule_state || "");
      const numericCanonicalRevision = Number(canonicalRevision || 0);
      return [
        Number.isSafeInteger(numericCanonicalRevision) && numericCanonicalRevision >= 0
          ? numericCanonicalRevision
          : 0,
        futureAxisScheduleRevision(payload),
        Number.isFinite(anchorTimestamp) ? anchorTimestamp : Number.NEGATIVE_INFINITY,
        ["continuous", "verified"].includes(scheduleState) ? 1 : 0,
        payload?.complete === true ? 1 : 0,
        Array.isArray(payload?.slots) ? payload.slots.length : 0,
      ];
    }

    function compareFutureAxisOrder(left, right) {
      for (let index = 0; index < Math.max(left.length, right.length); index += 1) {
        const leftValue = Number(left[index] ?? 0);
        const rightValue = Number(right[index] ?? 0);
        if (leftValue !== rightValue) return leftValue > rightValue ? 1 : -1;
      }
      return 0;
    }

    function futureAxisPayloadShouldReplace(currentAxis, incomingAxis, options = {}) {
      if (!isValidFutureAxisPayload(incomingAxis, options.timeframe || state.timeframe)) return false;
      if (!isValidFutureAxisPayload(currentAxis, options.timeframe || state.timeframe)) return true;
      const currentScheduleRevision = futureAxisScheduleRevision(currentAxis);
      const incomingScheduleRevision = futureAxisScheduleRevision(incomingAxis);
      const currentCanonicalRevision = Math.max(Number(options.currentCanonicalRevision || 0), 0);
      const incomingCanonicalRevision = Math.max(Number(options.incomingCanonicalRevision || 0), 0);
      if (
        incomingScheduleRevision < currentScheduleRevision
        || incomingCanonicalRevision < currentCanonicalRevision
      ) return false;
      if (
        incomingScheduleRevision > currentScheduleRevision
        || incomingCanonicalRevision > currentCanonicalRevision
      ) return true;
      return compareFutureAxisOrder(
        futureAxisOrder(incomingAxis, incomingCanonicalRevision),
        futureAxisOrder(currentAxis, currentCanonicalRevision),
      ) > 0;
    }

    function cloneFutureAxisPayload(payload) {
      return {
        ...payload,
        slots: payload.slots.map(item => ({ ...item })),
      };
    }

    function reconcileSnapshotFutureAxis(incomingSnapshot, currentSnapshot) {
      const incomingMeta = incomingSnapshot?.meta || {};
      const currentMeta = currentSnapshot?.meta || {};
      const sameRoute = Boolean(
        exactIdentityText(incomingMeta.instrument_id)
        && exactIdentityText(incomingMeta.instrument_id) === exactIdentityText(currentMeta.instrument_id)
        && exactIdentityText(incomingMeta.route_fingerprint) === exactIdentityText(currentMeta.route_fingerprint)
        && String(incomingMeta.timeframe || "") === String(currentMeta.timeframe || "")
      );
      const incomingAxis = incomingSnapshot?.future_axis;
      if (!sameRoute || !isValidFutureAxisPayload(incomingAxis, incomingMeta.timeframe)) {
        return incomingSnapshot;
      }
      const incomingCanonicalRevision = Math.max(Number(incomingMeta.chart_canonical_revision || 0), 0);
      const currentCanonicalRevision = futureAxisCanonicalRevision(currentSnapshot);
      if (futureAxisPayloadShouldReplace(currentSnapshot?.future_axis, incomingAxis, {
        timeframe: incomingMeta.timeframe,
        currentCanonicalRevision,
        incomingCanonicalRevision,
      })) {
        return {
          ...incomingSnapshot,
          meta: {
            ...incomingMeta,
            future_axis_canonical_revision: incomingCanonicalRevision,
          },
        };
      }
      return {
        ...incomingSnapshot,
        future_axis: cloneFutureAxisPayload(currentSnapshot.future_axis),
        meta: {
          ...incomingMeta,
          future_axis_canonical_revision: currentCanonicalRevision,
        },
      };
    }

    function manifestApiControlValue(control, group) {
      const key = control.state_key || control.key;
      const raw = group?.[key] ?? control.default;
      if (control.control_type === "toggle") return raw ? "true" : "false";
      if (control.control_type === "number") {
        const minimum = Number(control.minimum);
        const maximum = Number(control.maximum);
        const low = Number.isFinite(minimum) ? minimum : -Infinity;
        const high = Number.isFinite(maximum) ? maximum : Infinity;
        const fallback = Number(control.default);
        return String(clamp(Number.isFinite(Number(raw)) ? Number(raw) : fallback, low, high));
      }
      const value = String(raw ?? control.default ?? "");
      const options = Array.isArray(control.options) ? control.options : [];
      return options.length && !options.includes(value) ? String(control.default ?? "") : value;
    }

    function appendManifestIndicatorRequestParams(params) {
      Object.entries(indicatorRegistry()).forEach(([, spec]) => {
        const ui = spec?.ui || {};
        const group = state.indicators?.[ui.state_key];
        if (!group) return;
        if (ui.api_enabled_key) params.set(ui.api_enabled_key, group.enabled !== false ? "true" : "false");
        if (ui.api_visible_key) params.set(ui.api_visible_key, group.visible !== false ? "true" : "false");
        (spec.controls || []).forEach(control => {
          if (!control?.api_key) return;
          params.set(control.api_key, manifestApiControlValue(control, group));
        });
      });
    }

    function marketSnapshotNotice(snapshot) {
      const meta = snapshot?.meta || {};
      const structured = meta.error && typeof meta.error === "object" ? meta.error : null;
      if (structured?.message) return String(structured.message);
      if (meta.chart_only === true) return "";
      return meta.warning || "";
    }

    function mergeAnalysisSnapshot(analysisSnapshot, requestInstrumentId, requestRouteFingerprint, requestTimeframe, range, options = {}) {
      const analysisMeta = analysisSnapshot?.meta || {};
      const analysisOnly = Boolean(analysisMeta.analysis_only);
      if ((!analysisOnly && !analysisSnapshot?.bars?.length) || analysisMeta.chart_only) return false;
      compactMarketSnapshot(analysisSnapshot);
      if (state.instrumentId !== requestInstrumentId || instrumentRouteFingerprint() !== requestRouteFingerprint || state.timeframe !== requestTimeframe || state.range !== range) return false;
      if (exactIdentityText(analysisMeta.instrument_id) !== requestInstrumentId) return false;
      if (exactIdentityText(analysisMeta.route_fingerprint) !== requestRouteFingerprint) return false;
      if (!state.snapshot?.bars?.length || !snapshotMatchesCurrentRoute()) return false;
      const requireBarTs = timestampKey(options.requireBarTs || "");
      const requireCanonicalRevision = Math.max(Number(options.requireCanonicalRevision || 0), 0);
      if (
        requireCanonicalRevision > 0
        && Number(state.snapshot?.meta?.chart_canonical_revision || 0) !== requireCanonicalRevision
      ) {
        debugStep("analysis stale wait", `canonical revision ${requireCanonicalRevision} superseded`);
        return false;
      }
      if (!snapshotCoversAnalysisCanonicalRevision(analysisSnapshot, requireCanonicalRevision)) {
        debugStep("analysis merge skipped", "canonical revision not aligned");
        return false;
      }
      const alignment = analysisOnly
        ? { snapshot: analysisSnapshot, aligned: snapshotCoversAnalysisBar(analysisSnapshot, requireBarTs) }
        : alignSnapshotAnalysisForBars(analysisSnapshot, currentAnalysisBars(state.snapshot));
      if (requireBarTs && !alignment.aligned) {
        debugStep("analysis merge skipped", "bar window not aligned");
        return false;
      }
      const liveMeta = state.snapshot.meta || {};
      const liveChartGuides = state.snapshot.chart_guides;
      const alignedAnalysis = alignment.snapshot;
      const currentBars = currentAnalysisBars(state.snapshot);
      const analysisBars = Array.isArray(alignedAnalysis.bars) ? alignedAnalysis.bars : [];
      const mergedBarKey = timestampKey(
        analysisBars[analysisBars.length - 1]?.ts
        || analysisMeta.analysis_ts
        || analysisMeta.analysis_latest_ts
        || currentBars[currentBars.length - 1]?.ts
      );
      const mergedSnapshot = {
        ...alignedAnalysis,
        bars: state.snapshot.bars,
        future_axis: state.snapshot.future_axis || alignedAnalysis.future_axis,
        meta: {
          ...(alignedAnalysis.meta || analysisMeta),
          price: liveMeta.price ?? analysisMeta.price,
          bid: liveMeta.bid ?? analysisMeta.bid,
          ask: liveMeta.ask ?? analysisMeta.ask,
          last: liveMeta.last ?? analysisMeta.last,
          quote_ts: liveMeta.quote_ts ?? analysisMeta.quote_ts,
          change: liveMeta.change ?? analysisMeta.change,
          change_pct: liveMeta.change_pct ?? analysisMeta.change_pct,
          source: liveMeta.source || analysisMeta.source,
          freshness: liveMeta.freshness || analysisMeta.freshness,
          live_bar_closed: liveMeta.live_bar_closed ?? analysisMeta.live_bar_closed,
          data_quality: analysisMeta.data_quality || {},
          chart_data_quality: liveMeta.chart_data_quality || analysisMeta.chart_data_quality,
          chart_quality_revision: liveMeta.chart_quality_revision ?? analysisMeta.chart_quality_revision ?? 0,
          chart_canonical_revision: liveMeta.chart_canonical_revision ?? analysisMeta.chart_canonical_revision ?? 0,
          warning: liveMeta.warning || analysisMeta.warning || "",
          chart_range: liveMeta.chart_range || analysisMeta.chart_range,
          chart_bar_count: liveMeta.chart_bar_count || analysisMeta.chart_bar_count,
          chart_only: false,
          analysis_async: true,
          analysis_updated_at: analysisMeta.updated_at || new Date().toISOString(),
        },
      };
      delete mergedSnapshot.chart_guides;
      if (liveChartGuides) mergedSnapshot.chart_guides = liveChartGuides;
      state.snapshot = typeof applyFastIndicatorProjectionToSnapshot === "function"
        ? applyFastIndicatorProjectionToSnapshot(mergedSnapshot, { primarySnapshot: true })
        : mergedSnapshot;
      state.analysisMissingRecoveryScope = "";
      state.analysisMissingRecoveryAttempt = 0;
      if (mergedBarKey) {
        const canonicalRevision = Math.max(Number(liveMeta.chart_canonical_revision || 0), 0);
        state.lastIndicatorAnalysisMergedVersion = canonicalRevision > 0
          ? `${mergedBarKey}|r${canonicalRevision}`
          : mergedBarKey;
        state.lastLiveQuoteAnalysisConfirmedBar = mergedBarKey;
      }
      pruneClientHistoryWindow("analysis");
      renderCharts(state.snapshot, { reason: "analysis" });
      renderPanel(state.snapshot);
      renderInstruments();
      renderTimeframes();
      applyLayout();
      refreshPaperJournal(state.snapshot, paperJournalRefreshOptions(state.snapshot));
      if (gexLayerVisible()) loadGexContext({ refresh: false });
      else clearGexContext();
      if (typeof syncIndicatorModuleLifecycles === "function") {
        void syncIndicatorModuleLifecycles({
          snapshot: state.snapshot,
          reason: "analysis-merge",
        });
      }
      return true;
    }

    function snapshotIdentity(snapshot) {
      const meta = snapshot?.meta || {};
      return {
        instrumentId: exactIdentityText(meta.instrument_id),
        routeFingerprint: exactIdentityText(meta.route_fingerprint),
        timeframe: String(meta.timeframe || state.timeframe || "").trim(),
        range: String(meta.chart_range || state.range || "").trim(),
      };
    }

    function sameSnapshotIdentity(left, right) {
      const a = snapshotIdentity(left);
      const b = snapshotIdentity(right);
      return Boolean(a.instrumentId && a.instrumentId === b.instrumentId)
        && Boolean(a.routeFingerprint && a.routeFingerprint === b.routeFingerprint)
        && a.timeframe === b.timeframe
        && (!a.range || !b.range || a.range === b.range);
    }

    function sameBarWindow(leftBars, rightBars) {
      if (!Array.isArray(leftBars) || !Array.isArray(rightBars) || leftBars.length !== rightBars.length) return false;
      if (!leftBars.length) return true;
      const middle = Math.floor((leftBars.length - 1) / 2);
      return (
        timestampKey(leftBars[0]?.ts) === timestampKey(rightBars[0]?.ts)
        && timestampKey(leftBars[middle]?.ts) === timestampKey(rightBars[middle]?.ts)
        && timestampKey(leftBars[leftBars.length - 1]?.ts) === timestampKey(rightBars[rightBars.length - 1]?.ts)
      );
    }

    function currentAnalysisBars(snapshot = state.snapshot) {
      const bars = Array.isArray(snapshot?.bars) ? snapshot.bars : [];
      let end = bars.length;
      while (end > 0) {
        const bar = bars[end - 1];
        const usable = typeof barUsableForConfirmedAnalysis === "function"
          ? barUsableForConfirmedAnalysis(bar, snapshot)
          : bar?.closed !== false;
        if (!usable) {
          end -= 1;
          continue;
        }
        break;
      }
      return end === bars.length ? bars : bars.slice(0, end);
    }

    function effectiveDataQuality(meta = {}) {
      const signal = meta.data_quality || {};
      const chart = meta.chart_data_quality || {};
      const signalPresent = Object.keys(signal).length > 0;
      const chartPresent = Object.keys(chart).length > 0;
      if (!chartPresent) return signal;
      if (!signalPresent) return chart;
      const signalBlocked = signal.signals_ok === false;
      const chartBlocked = chart.signals_ok === false;
      return {
        ...signal,
        ...chart,
        analysis_status: String(signal.status || ""),
        chart_status: String(chart.status || ""),
        status: signalBlocked
          ? String(signal.status || "blocked")
          : chartBlocked
            ? String(chart.status || "blocked")
            : String(signal.status || chart.status || "ok"),
        signals_ok: !signalBlocked && !chartBlocked,
        warning: signalBlocked && signal.warning
          ? signal.warning
          : chartBlocked
            ? chart.warning || ""
            : "",
      };
    }

    function providerSpec(source = state.dataSource) {
      const key = String(source || "").trim().toLowerCase();
      return (state.providers || []).find(provider => String(provider?.key || "").trim().toLowerCase() === key) || null;
    }

    function providerCapability(source, capability) {
      const capabilities = providerSpec(source)?.capabilities;
      return Boolean(capabilities && capabilities[capability] === true);
    }

    function historyCoverageVerificationStatus(coverage, source = state.dataSource) {
      const payload = coverage && typeof coverage === "object" && !Array.isArray(coverage)
        ? coverage
        : {};
      const barRepair = payload.bar_repair
        && typeof payload.bar_repair === "object"
        && !Array.isArray(payload.bar_repair)
        ? payload.bar_repair
        : {};
      const absenceVerification = payload.absence_verification
        && typeof payload.absence_verification === "object"
        && !Array.isArray(payload.absence_verification)
        ? payload.absence_verification
        : {};
      const repairStatus = String(barRepair.status || "").trim().toLowerCase();
      const repairPhase = String(barRepair.phase || "").trim().toLowerCase();
      const repairSupported = barRepair.supported === true;
      const concreteRepair = Boolean(repairStatus)
        && ![
          "unknown",
          "not_requested",
          "not_needed",
          "not_admitted",
          "not_supported",
        ].includes(repairStatus);
      const absenceSupported = absenceVerification.supported === true;
      return {
        available: repairSupported,
        active: repairSupported && ["scheduled", "queued", "running"].includes(repairStatus),
        concreteRepair,
        repairPhase,
        repairStatus: repairStatus || (repairSupported ? "unknown" : "not_supported"),
        repairErrorCode: String(barRepair.error_code || "").trim(),
        repairRequestedFrom: String(barRepair.requested_from || "").trim(),
        repairRequestedTo: String(barRepair.requested_to || "").trim(),
        absenceSupported,
        absenceState: String(
          absenceVerification.state || (absenceSupported ? payload.state || "unknown" : "unsupported"),
        ).trim().toLowerCase(),
        absenceErrorCode: String(absenceVerification.error_code || "").trim(),
      };
    }

    function instrumentForId(instrumentId = state.instrumentId) {
      const identity = exactIdentityText(instrumentId);
      const instrument = (state.instruments || []).find(item => exactIdentityText(item?.instrument_id) === identity);
      return instrument || null;
    }

    function emptyAnalysisValue(value) {
      if (value === undefined || value === null) return true;
      if (Array.isArray(value)) return value.length === 0;
      if (typeof value === "object") return Object.keys(value).length === 0;
      return false;
    }

    function compactObjectFields(item, fields) {
      if (!item || typeof item !== "object" || Array.isArray(item)) return item;
      const out = {};
      for (const key of fields) {
        if (item[key] !== undefined) out[key] = item[key];
      }
      return out;
    }

    function compactArrayTail(value, limit) {
      if (!Array.isArray(value)) return value;
      const maxItems = Math.max(Number(limit) || 0, 0);
      return maxItems > 0 && value.length > maxItems ? value.slice(-maxItems) : value;
    }

    function compactOverlayTail(value, limit) {
      if (!Array.isArray(value)) return value;
      const maxItems = Math.max(Number(limit) || 0, 0);
      if (!(maxItems > 0) || value.length <= maxItems) return value;
      const activeIndices = new Set();
      value.forEach((item, index) => {
        if (item && typeof item === "object" && item.retention === "active") {
          activeIndices.add(index);
        }
      });
      if (!activeIndices.size) return value.slice(-maxItems);
      const historySlots = Math.max(maxItems - activeIndices.size, 0);
      const historyIndices = value
        .map((_, index) => index)
        .filter(index => !activeIndices.has(index));
      if (historySlots > 0) {
        for (const index of historyIndices.slice(-historySlots)) activeIndices.add(index);
      }
      return value.filter((_, index) => activeIndices.has(index));
    }

    function compactIndicatorArray(indicator, key, limit, fields = null) {
      if (!indicator || !Array.isArray(indicator[key])) return;
      const rows = key === "overlays"
        ? compactOverlayTail(indicator[key], limit)
        : compactArrayTail(indicator[key], limit);
      indicator[key] = Array.isArray(fields) ? rows.map(item => compactObjectFields(item, fields)) : rows;
    }

    function runtimePayloadFields(indicatorId, section) {
      const contract = window.INDICATOR_REGISTRY_MANIFEST?.[indicatorId]?.runtime_payload_contract;
      const fields = contract && Array.isArray(contract[section]) ? contract[section] : null;
      return fields?.length ? fields : null;
    }

    function runtimePayloadCompactContractFields(indicatorId, section) {
      const contract = window.INDICATOR_REGISTRY_MANIFEST?.[indicatorId]?.runtime_payload_compact_contract;
      const fields = contract && Array.isArray(contract[section]) ? contract[section] : null;
      return fields?.length ? fields : null;
    }

    function runtimePayloadCompactPolicy(indicatorId) {
      const policy = window.INDICATOR_REGISTRY_MANIFEST?.[indicatorId]?.runtime_payload_compact;
      return policy && typeof policy === "object" ? policy : null;
    }

    function runtimePayloadCompactFields(indicatorId, section, policy) {
      const fields = policy?.fields;
      if (fields === "runtime_contract") {
        const contractFields = runtimePayloadFields(indicatorId, section);
        if (!contractFields) throw new Error(`Missing runtime payload contract for ${indicatorId}.${section}`);
        return contractFields;
      }
      if (fields === "compact_contract") {
        const contractFields = runtimePayloadCompactContractFields(indicatorId, section);
        if (!contractFields) throw new Error(`Missing compact runtime payload contract for ${indicatorId}.${section}`);
        return contractFields;
      }
      if (Array.isArray(fields)) return fields;
      if (typeof fields === "string") throw new Error(`Unknown runtime payload compact fields ref for ${indicatorId}.${section}: ${fields}`);
      return null;
    }

    function recordClientOverlayCompaction(snapshot, indicatorId, before, after) {
      const dropped = Math.max(Number(before || 0) - Number(after || 0), 0);
      if (!dropped) return;
      snapshot.meta = snapshot.meta && typeof snapshot.meta === "object" ? snapshot.meta : {};
      const diagnostics = snapshot.meta.indicator_overlay_compaction
        && typeof snapshot.meta.indicator_overlay_compaction === "object"
        ? snapshot.meta.indicator_overlay_compaction
        : {};
      const previous = diagnostics[indicatorId] && typeof diagnostics[indicatorId] === "object"
        ? diagnostics[indicatorId]
        : {};
      diagnostics[indicatorId] = {
        dropped: Number(previous.dropped || 0) + dropped,
        retained: Number(after || 0),
      };
      snapshot.meta.indicator_overlay_compaction = diagnostics;
    }

    function compactMarketSnapshot(snapshot) {
      const indicators = snapshot?.indicators;
      if (!indicators || typeof indicators !== "object") return snapshot;

      for (const [indicatorId, indicator] of Object.entries(indicators)) {
        if (!indicator || typeof indicator !== "object") continue;
        const sectionPolicies = runtimePayloadCompactPolicy(indicatorId);
        if (!sectionPolicies) continue;
        for (const [section, policy] of Object.entries(sectionPolicies)) {
          if (!policy || typeof policy !== "object") continue;
          if (policy.drop === true) {
            delete indicator[section];
            continue;
          }
          const limit = Number(policy.limit || 0);
          if (!Number.isFinite(limit) || limit <= 0) continue;
          const before = Array.isArray(indicator[section]) ? indicator[section].length : 0;
          compactIndicatorArray(indicator, section, limit, runtimePayloadCompactFields(indicatorId, section, policy));
          if (section === "overlays") {
            const after = Array.isArray(indicator[section]) ? indicator[section].length : 0;
            recordClientOverlayCompaction(snapshot, indicatorId, before, after);
          }
        }
      }

      return snapshot;
    }

    function alignChartGuidesToBars(snapshot, sourceBars, targetBars = snapshot?.bars || [], fallbackGuides = null) {
      const guides = snapshot?.chart_guides;
      const source = Array.isArray(sourceBars) ? sourceBars : [];
      const target = Array.isArray(targetBars) ? targetBars : [];
      if (!guides || typeof guides !== "object" || !source.length || !target.length) return snapshot;

      const sourceIndexByTs = new Map(source.map((bar, index) => [timestampKey(bar?.ts), index]));
      const project = (values, missingValue, fallbackValues) => {
        if (!Array.isArray(values) || values.length !== source.length) return values;
        const fallback = Array.isArray(fallbackValues) && fallbackValues.length === target.length
          ? fallbackValues
          : [];
        return target.map((bar, targetIndex) => {
          const sourceIndex = sourceIndexByTs.get(timestampKey(bar?.ts));
          return sourceIndex === undefined
            ? (fallback.length
              ? fallback[targetIndex]
              : (typeof missingValue === "function" ? missingValue() : missingValue))
            : values[sourceIndex];
        });
      };
      const fallbackEma = fallbackGuides?.ema && typeof fallbackGuides.ema === "object"
        ? fallbackGuides.ema
        : {};
      const ema = guides.ema && typeof guides.ema === "object"
        ? Object.fromEntries(Object.entries(guides.ema).map(([length, values]) => [
          length,
          project(values, null, fallbackEma[length]),
        ]))
        : guides.ema;
      const vwap = project(
        guides.vwap,
        () => ({ vwap: null, sigma: null }),
        fallbackGuides?.vwap,
      );
      snapshot.chart_guides = {
        ...guides,
        ema,
        vwap,
        meta: guides.meta && typeof guides.meta === "object"
          ? { ...guides.meta, bar_count: target.length }
          : guides.meta,
      };
      return snapshot;
    }

    function mergeChartGuidesFromChartSnapshot(chartSnapshot, options = {}) {
      const current = state.snapshot;
      const incomingMeta = chartSnapshot?.meta || {};
      if (
        !chartSnapshot?.meta?.chart_only
        || !chartSnapshot?.chart_guides
        || !Array.isArray(chartSnapshot?.bars)
        || !chartSnapshot.bars.length
        || !current?.bars?.length
        || !sameSnapshotIdentity(current, chartSnapshot)
        || !snapshotMatchesCurrentRoute()
      ) return false;
      const requireBarTs = timestampKey(options.requireBarTs || "");
      const requireCanonicalRevision = Math.max(Number(options.requireCanonicalRevision || 0), 0);
      const currentCanonicalRevision = Math.max(Number(current.meta?.chart_canonical_revision || 0), 0);
      const incomingCanonicalRevision = Math.max(Number(incomingMeta.chart_canonical_revision || 0), 0);
      if (
        incomingCanonicalRevision !== currentCanonicalRevision
        || (requireCanonicalRevision > 0 && (
          currentCanonicalRevision !== requireCanonicalRevision
          || incomingCanonicalRevision !== requireCanonicalRevision
        ))
        || (requireBarTs && !chartSnapshot.bars.some(bar => timestampKey(bar?.ts) === requireBarTs))
      ) return false;
      const currentGeneration = String(current.chart_guides?.meta?.generation || "");
      const incomingGeneration = String(chartSnapshot.chart_guides?.meta?.generation || "");
      if (!incomingGeneration || incomingGeneration === currentGeneration) return false;
      const merged = {
        ...current,
        chart_guides: chartSnapshot.chart_guides,
      };
      alignChartGuidesToBars(
        merged,
        chartSnapshot.bars,
        current.bars,
        current.chart_guides,
      );
      const sourceBarKeys = new Set(chartSnapshot.bars.map(bar => timestampKey(bar?.ts)));
      current.bars.forEach((bar, index) => {
        if (
          sourceBarKeys.has(timestampKey(bar?.ts))
          || typeof barIsLivePreview !== "function"
          || !barIsLivePreview(bar)
        ) return;
        Object.values(merged.chart_guides?.ema || {}).forEach(values => {
          if (Array.isArray(values) && index < values.length) values[index] = null;
        });
        if (Array.isArray(merged.chart_guides?.vwap) && index < merged.chart_guides.vwap.length) {
          merged.chart_guides.vwap[index] = { vwap: null, sigma: null };
        }
      });
      state.snapshot = merged;
      retainCanonicalLiveTailChartGuides(merged);
      renderCharts(state.snapshot, { reason: "chart-guides" });
      return true;
    }

    function sliceKnownBarAlignedArray(value, oldLength, from, to) {
      return Array.isArray(value) && value.length === oldLength
        ? value.slice(from, to)
        : value;
    }

    function sliceChartGuidesAlignedRange(guides, oldLength, from, to) {
      if (!guides || typeof guides !== "object" || Array.isArray(guides)) return guides;
      const ema = guides.ema && typeof guides.ema === "object" && !Array.isArray(guides.ema)
        ? Object.fromEntries(Object.entries(guides.ema).map(([length, values]) => [
          length,
          sliceKnownBarAlignedArray(values, oldLength, from, to),
        ]))
        : guides.ema;
      const vwap = sliceKnownBarAlignedArray(guides.vwap, oldLength, from, to);
      return {
        ...guides,
        ema,
        vwap,
        meta: guides.meta && typeof guides.meta === "object"
          ? { ...guides.meta, bar_count: to - from }
          : guides.meta,
      };
    }

    function sliceVsaVolumeAlignedRange(context, oldLength, from, to) {
      if (!context || typeof context !== "object" || Array.isArray(context)) return context;
      const series = sliceKnownBarAlignedArray(context.series, oldLength, from, to);
      return {
        ...context,
        series,
        status: context.status && typeof context.status === "object"
          ? { ...context.status, series_count: Array.isArray(series) ? series.length : 0 }
          : context.status,
      };
    }

    function sliceSnapshotAlignedRange(snapshot, start, end) {
      const bars = snapshot?.bars || [];
      const oldLength = bars.length;
      const from = clamp(Math.floor(Number(start) || 0), 0, oldLength);
      const to = clamp(Math.ceil(Number(end) || oldLength), from, oldLength);
      if (!oldLength || (from <= 0 && to >= oldLength)) return snapshot;
      const next = {
        ...snapshot,
        bars: bars.slice(from, to),
      };
      if (Object.prototype.hasOwnProperty.call(snapshot, "chart_guides")) {
        next.chart_guides = sliceChartGuidesAlignedRange(snapshot.chart_guides, oldLength, from, to);
      }
      if (Object.prototype.hasOwnProperty.call(snapshot, "vsa_volume")) {
        next.vsa_volume = sliceVsaVolumeAlignedRange(snapshot.vsa_volume, oldLength, from, to);
      }
      const meta = next.meta || {};
      next.meta = {
        ...meta,
        client_pruned_before: Number(meta.client_pruned_before || 0) + from,
        client_pruned_after: Number(meta.client_pruned_after || 0) + (oldLength - to),
        client_bar_count: next.bars.length,
        server_bar_count: Number(meta.server_bar_count || meta.chart_bar_count || oldLength),
      };
      return next;
    }

    function canonicalLiveTailKey(meta = {}) {
      const instrumentId = exactIdentityText(meta.instrument_id);
      const routeFingerprint = exactIdentityText(meta.route_fingerprint);
      const timeframe = String(meta.timeframe ?? "");
      return instrumentId && routeFingerprint && timeframe
        ? JSON.stringify([instrumentId, routeFingerprint, timeframe])
        : "";
    }

    function snapshotCoversCanonicalLiveTail(snapshot = state.snapshot) {
      const key = canonicalLiveTailKey(snapshot?.meta || {});
      if (
        !key
        || key !== canonicalLiveTailStore.key
        || !canonicalLiveTailStore.verified
        || !snapshot?.bars?.length
        || !canonicalLiveTailStore.bars.length
      ) return false;
      const retainedLatestKey = timestampKey(canonicalLiveTailStore.bars[canonicalLiveTailStore.bars.length - 1]?.ts);
      return Boolean(retainedLatestKey && snapshot.bars.some(bar => timestampKey(bar?.ts) === retainedLatestKey));
    }

    function retainCanonicalLiveTail(bars, meta = {}, options = {}) {
      const key = canonicalLiveTailKey(meta);
      if (!key || !Array.isArray(bars) || !bars.length) return 0;
      const instrumentId = exactIdentityText(meta.instrument_id);
      const routeFingerprint = exactIdentityText(meta.route_fingerprint);
      const timeframe = String(meta.timeframe ?? "");
      const incoming = bars
        .map(bar => {
          if (!bar || typeof bar !== "object") return bar;
          return {
            ...bar,
            ...(Object.prototype.hasOwnProperty.call(bar, "instrument_id") ? {} : { instrument_id: instrumentId }),
            ...(Object.prototype.hasOwnProperty.call(bar, "route_fingerprint") ? {} : { route_fingerprint: routeFingerprint }),
            ...(Object.prototype.hasOwnProperty.call(bar, "timeframe") ? {} : { timeframe }),
          };
        })
        .filter(bar => (
          Number.isFinite(Date.parse(bar?.ts || ""))
          && exactIdentityText(bar?.instrument_id) === instrumentId
          && exactIdentityText(bar?.route_fingerprint) === routeFingerprint
          && String(bar?.timeframe ?? "") === timeframe
        ))
        .map(bar => ({ ...bar }))
        .sort((left, right) => Date.parse(left.ts) - Date.parse(right.ts));
      if (!incoming.length) return 0;
      const previous = canonicalLiveTailStore.key === key ? canonicalLiveTailStore : {
        key,
        bars: [],
        snapshot: null,
        verified: false,
      };
      const replacing = options.replace === true;
      const mergedByTs = new Map();
      const resolve = (existing, candidate) => (
        typeof resolveStreamBar === "function"
          ? resolveStreamBar(existing, candidate)
          : candidate || existing
      );
      if (!replacing) {
        previous.bars.forEach(bar => mergedByTs.set(timestampKey(bar?.ts), { ...bar }));
      }
      incoming.forEach(bar => {
        const barKey = timestampKey(bar.ts);
        mergedByTs.set(barKey, resolve(mergedByTs.get(barKey), bar));
      });
      if (replacing && previous.bars.length) {
        const incomingStartMs = Date.parse(incoming[0].ts);
        previous.bars.forEach(bar => {
          const barMs = Date.parse(bar?.ts || "");
          if (!Number.isFinite(barMs) || barMs < incomingStartMs) return;
          const barKey = timestampKey(bar.ts);
          mergedByTs.set(barKey, resolve(mergedByTs.get(barKey), bar));
        });
      }
      const mergedBars = Array.from(mergedByTs.values())
        .sort((left, right) => Date.parse(left.ts) - Date.parse(right.ts))
        .slice(-CLIENT_CANONICAL_TAIL_BARS);
      let retainedSnapshot = null;
      if (options.snapshot?.bars?.length) {
        const source = options.snapshot;
        retainedSnapshot = sliceSnapshotAlignedRange(
          source,
          Math.max(source.bars.length - CLIENT_CANONICAL_TAIL_BARS, 0),
          source.bars.length,
        );
      } else if (previous.snapshot?.bars?.length) {
        retainedSnapshot = previous.snapshot;
      }
      if (retainedSnapshot?.bars?.length) {
        const sourceBars = retainedSnapshot.bars.slice();
        retainedSnapshot = {
          ...retainedSnapshot,
          bars: mergedBars.map(bar => ({ ...bar })),
          meta: {
            ...(retainedSnapshot.meta || {}),
            ...meta,
            client_live_tail: true,
            client_bar_count: mergedBars.length,
          },
        };
        alignChartGuidesToBars(retainedSnapshot, sourceBars, retainedSnapshot.bars);
      }
      canonicalLiveTailStore = {
        key,
        bars: mergedBars.map(bar => ({ ...bar })),
        snapshot: retainedSnapshot,
        verified: Boolean(options.verified || previous.verified),
      };
      return mergedBars.length;
    }

    function cloneChartGuidePayload(value) {
      if (Array.isArray(value)) return value.map(item => cloneChartGuidePayload(item));
      if (!value || typeof value !== "object") return value;
      return Object.fromEntries(
        Object.entries(value).map(([key, item]) => [key, cloneChartGuidePayload(item)]),
      );
    }

    function retainCanonicalLiveTailChartGuides(snapshot) {
      const key = canonicalLiveTailKey(snapshot?.meta || {});
      const sourceBars = Array.isArray(snapshot?.bars) ? snapshot.bars : [];
      const retainedBars = canonicalLiveTailStore.bars.map(bar => ({ ...bar }));
      const retainedLatestKey = timestampKey(retainedBars[retainedBars.length - 1]?.ts);
      if (
        !key
        || key !== canonicalLiveTailStore.key
        || !canonicalLiveTailStore.verified
        || !snapshot?.chart_guides
        || !sourceBars.length
        || !retainedBars.length
        || !retainedLatestKey
        || !sourceBars.some(bar => timestampKey(bar?.ts) === retainedLatestKey)
      ) return false;
      const previous = canonicalLiveTailStore.snapshot;
      const retainedSnapshot = {
        ...(previous?.bars?.length ? previous : snapshot),
        bars: retainedBars,
        chart_guides: snapshot.chart_guides,
      };
      alignChartGuidesToBars(
        retainedSnapshot,
        sourceBars,
        retainedBars,
        previous?.chart_guides,
      );
      retainedSnapshot.chart_guides = cloneChartGuidePayload(retainedSnapshot.chart_guides);
      canonicalLiveTailStore = {
        ...canonicalLiveTailStore,
        bars: retainedBars.map(bar => ({ ...bar })),
        snapshot: retainedSnapshot,
      };
      return true;
    }

    function restoreCanonicalLiveTail() {
      const key = canonicalLiveTailKey({
        instrument_id: state.instrumentId,
        route_fingerprint: instrumentRouteFingerprint(),
        timeframe: state.timeframe,
      });
      if (
        !key
        || canonicalLiveTailStore.key !== key
        || !canonicalLiveTailStore.verified
        || !canonicalLiveTailStore.bars.length
      ) return false;
      const base = canonicalLiveTailStore.snapshot || state.snapshot;
      if (!base) return false;
      const sourceBars = Array.isArray(base.bars) ? base.bars.slice() : [];
      const bars = canonicalLiveTailStore.bars.map(bar => ({ ...bar }));
      const latest = bars[bars.length - 1];
      const restored = {
        ...base,
        bars,
        meta: {
          ...(base.meta || {}),
          instrument_id: state.instrumentId,
          route_fingerprint: instrumentRouteFingerprint(),
          timeframe: state.timeframe,
          chart_range: base.meta?.chart_range || initialRangeFor(state.timeframe),
          price: Number(latest?.close),
          client_live_tail: true,
          client_live_tail_restored: true,
          client_bar_count: bars.length,
        },
      };
      alignChartGuidesToBars(restored, sourceBars, bars);
      state.snapshot = typeof normalizeSnapshotBarSlots === "function"
        ? normalizeSnapshotBarSlots(restored)
        : restored;
      if (resolveDrawingAnchorsAgainstSnapshot(state.drawing.objects, state.snapshot)) {
        touchChartUserObjectsVersion();
      }
      state.range = String(restored.meta?.chart_range || initialRangeFor(state.timeframe));
      state.requests.history.exhausted = false;
      setServerSettingValue(rangeStorageKey(state.instrumentId, state.timeframe), state.range);
      touchChartBarsVersion();
      return true;
    }

    function alignSnapshotAnalysisForBars(snapshot, bars) {
      const sourceBars = snapshot?.bars || [];
      const targetBars = Array.isArray(bars) ? bars : [];
      if (!sourceBars.length || !targetBars.length) return { snapshot, aligned: false };
      if (sourceBars.length === targetBars.length) {
        return {
          snapshot,
          aligned: sameBarWindow(sourceBars, targetBars),
        };
      }
      const firstTs = targetBars[0]?.ts;
      const lastTs = targetBars[targetBars.length - 1]?.ts;
      const start = sourceBars.findIndex(bar => bar?.ts === firstTs);
      if (start < 0) return { snapshot, aligned: false };
      const end = start + targetBars.length;
      if (end > sourceBars.length || sourceBars[end - 1]?.ts !== lastTs) return { snapshot, aligned: false };
      return { snapshot: sliceSnapshotAlignedRange(snapshot, start, end), aligned: true };
    }

    function snapshotCoversAnalysisBar(snapshot, barKey) {
      const key = timestampKey(barKey);
      if (!key) return true;
      const meta = snapshot?.meta || {};
      return [meta.analysis_ts, meta.analysis_latest_ts].some(ts => timestampKey(ts) === key);
    }

    function snapshotCoversAnalysisCanonicalRevision(snapshot, canonicalRevision) {
      const required = Number(canonicalRevision);
      if (!Number.isSafeInteger(required) || required <= 0) return true;
      const actual = Number(snapshot?.meta?.analysis_parent_canonical_revision);
      return Number.isSafeInteger(actual) && actual === required;
    }

    function clientHistoryWindowLimit(snapshot = state.snapshot) {
      const visibleCount = Math.max(Number(view.barsVisible) || DEFAULT_BARS_VISIBLE, DEFAULT_BARS_VISIBLE);
      const activeRange = String(snapshot?.meta?.chart_range || state.range || "");
      if (
        snapshot?.meta?.client_history_paged === true
        || (typeof isDeepHistoryRange === "function" && isDeepHistoryRange(activeRange, state.timeframe))
      ) {
        return clamp(Math.round(Math.max(visibleCount * 24, 12000)), 12000, 60000);
      }
      return clamp(Math.round(Math.max(visibleCount * 4, 1200)), 1200, 2600);
    }

    function pruneClientHistoryWindow(reason = "viewport") {
      const snapshot = state.snapshot;
      const bars = snapshot?.bars || [];
      if (!bars.length) return snapshot;
      const limit = clientHistoryWindowLimit(snapshot);
      if (bars.length <= limit + 120) return snapshot;
      const visible = visibleBars(snapshot);
      const visibleCount = Math.max(visible.end - visible.start, 1);
      const visibleAnchorTs = timestampKey(visible.bars[0]?.ts);
      const buffer = clamp(Math.round(Math.max(visibleCount * 1.25, 240)), 240, 900);
      let keepStart = Math.max(0, visible.start - buffer);
      let keepEnd = Math.min(bars.length, visible.end + buffer);
      if (view.followLatest) {
        keepEnd = bars.length;
        keepStart = Math.max(0, keepEnd - limit);
      } else if (keepEnd - keepStart > limit) {
        const center = (visible.start + visible.end) / 2;
        keepStart = clamp(Math.round(center - limit / 2), 0, Math.max(bars.length - limit, 0));
        keepEnd = Math.min(bars.length, keepStart + limit);
      }
      if (keepStart <= 0 && keepEnd >= bars.length) return snapshot;
      const active = sliceSnapshotAlignedRange(snapshot, keepStart, keepEnd);
      state.snapshot = active;
      if (resolveDrawingAnchorsAgainstSnapshot(state.drawing.objects, active)) {
        touchChartUserObjectsVersion();
      }
      touchChartBarsVersion();
      const anchorIndex = historyAnchorIndex(active.bars, visibleAnchorTs);
      if (anchorIndex >= 0) {
        view.offset = clamp(active.bars.length - (anchorIndex + visibleCount), 0, maxOffsetFor(active));
      } else {
        view.offset = clamp(Math.round(Number(view.offset) || 0), 0, maxOffsetFor(active));
      }
      view.smoothOffsetBars = 0;
      clampView(active);
      debugStep("client history pruned", `${reason}: ${active.bars.length}/${bars.length} active bars`);
      return active;
    }

    function mergeHistoryPageIntoSnapshot(payload) {
      const current = state.snapshot;
      const pageBars = Array.isArray(payload?.bars) ? payload.bars : [];
      const beforeMs = Date.parse(payload?.before_ts || "");
      const nextBeforeMs = Date.parse(payload?.next_before_ts || "");
      if (
        !current?.bars?.length
        || !Number.isFinite(beforeMs)
        || exactIdentityText(payload?.instrument_id) !== exactIdentityText(state.instrumentId)
        || exactIdentityText(payload?.route_fingerprint) !== instrumentRouteFingerprint()
        || String(payload?.timeframe || "") !== state.timeframe
        || Number(payload?.canonical_generation) !== Number(current.meta?.chart_canonical_revision || 0)
        || (payload?.has_more === true && (!Number.isFinite(nextBeforeMs) || nextBeforeMs >= beforeMs))
        || pageBars.some(bar => !barIsConfirmedClosed(bar) || Date.parse(bar?.ts || "") >= beforeMs)
      ) return null;
      const previousBars = current.bars.slice();
      const mergedByTs = new Map();
      pageBars.forEach(bar => mergedByTs.set(timestampKey(bar?.ts), { ...bar }));
      previousBars.forEach(bar => {
        const key = timestampKey(bar?.ts);
        const existing = mergedByTs.get(key);
        mergedByTs.set(
          key,
          typeof resolveStreamBar === "function" ? resolveStreamBar(existing, bar) : (bar || existing),
        );
      });
      const bars = Array.from(mergedByTs.values())
        .filter(bar => Number.isFinite(Date.parse(bar?.ts || "")))
        .sort((left, right) => Date.parse(left.ts) - Date.parse(right.ts));
      if (bars.length <= previousBars.length) return current;
      const merged = {
        ...current,
        bars,
        meta: {
          ...(current.meta || {}),
          client_history_paged: true,
          client_history_has_more: payload.has_more === true,
          client_history_next_before_ts: payload.next_before_ts || null,
          client_bar_count: bars.length,
        },
      };
      alignChartGuidesToBars(merged, previousBars, bars, current.chart_guides);
      return typeof normalizeSnapshotBarSlots === "function"
        ? normalizeSnapshotBarSlots(merged)
        : merged;
    }

    async function loadHistoryPage(options = {}) {
      const requestInstrumentId = exactIdentityText(state.instrumentId);
      const requestRouteFingerprint = instrumentRouteFingerprint();
      const requestTimeframe = state.timeframe;
      const current = state.snapshot;
      const beforeMs = Date.parse(options.beforeTs || current?.bars?.find(bar => barIsConfirmedClosed(bar))?.ts || "");
      const beforeTs = Number.isFinite(beforeMs) ? new Date(beforeMs).toISOString() : "";
      const expectedGeneration = Number(current?.meta?.chart_canonical_revision || 0);
      if (!requestInstrumentId || !requestRouteFingerprint || !beforeTs || !Number.isSafeInteger(expectedGeneration)) return 0;
      const pageLimit = clamp(Math.round(Number(options.limit) || 600), 100, 1000);
      const params = new URLSearchParams({
        instrument_id: requestInstrumentId,
        expected_route_fingerprint: requestRouteFingerprint,
        interval: requestTimeframe,
        before_ts: beforeTs,
        limit: String(pageLimit),
        expected_canonical_generation: String(expectedGeneration),
      });
      const controller = new AbortController();
      state.requests.history.abort = controller;
      try {
        const payload = await fetchJson(`/api/market/history-page?${params.toString()}`, {
          signal: controller.signal,
          timeoutMs: MARKET_LOCAL_HISTORY_TIMEOUT_MS,
        });
        if (
          state.requests.history.abort !== controller
          || state.instrumentId !== requestInstrumentId
          || instrumentRouteFingerprint() !== requestRouteFingerprint
          || state.timeframe !== requestTimeframe
        ) return 0;
        const previousSnapshot = state.snapshot;
        const merged = mergeHistoryPageIntoSnapshot(payload);
        if (!merged) return 0;
        if (merged === previousSnapshot) {
          state.requests.history.exhausted = payload?.has_more !== true;
          if (payload?.has_more === true && payload?.next_before_ts) {
            state.snapshot = {
              ...previousSnapshot,
              meta: {
                ...(previousSnapshot.meta || {}),
                client_history_paged: true,
                client_history_has_more: true,
                client_history_next_before_ts: payload.next_before_ts,
              },
            };
          }
          return 0;
        }
        state.snapshot = merged;
        applyHistoryViewportAnchor(merged, {
          previousSnapshot,
          previousOffset: Number(view.offset) || 0,
        });
        if (resolveDrawingAnchorsAgainstSnapshot(state.drawing.objects, merged)) {
          touchChartUserObjectsVersion();
        }
        touchChartBarsVersion();
        state.requests.history.exhausted = payload.has_more !== true;
        clampView(merged);
        const active = pruneClientHistoryWindow("history-page");
        const historyNotice = setUiNotice(
          "history",
          state.requests.history.exhausted ? "HISTORY_RANGE_EXHAUSTED" : "HISTORY_PAGE_LOADED",
          state.requests.history.exhausted
            ? "All available local history is loaded."
            : `Loaded ${payload.bars.length} older bars from local DB.`,
          { state: "ready" },
        );
        if (!state.requests.history.exhausted) scheduleHistorySuccessNoticeClear(historyNotice);
        renderPanel(active);
        renderCharts(active, { reason: "history page" });
        return payload.bars.length;
      } catch (error) {
        if (error?.name === "AbortError") return 0;
        setUiNotice(
          "history",
          "HISTORY_PAGE_FAILED",
          error?.message || "Local history page could not be loaded.",
          { state: "degraded" },
        );
        return 0;
      } finally {
        if (state.requests.history.abort === controller) state.requests.history.abort = null;
      }
    }

    function preserveAnalysisForChartOnlySnapshot(chartSnapshot, options = {}) {
      if (!chartSnapshot?.meta?.chart_only || !chartSnapshot?.bars?.length) return chartSnapshot;
      const previous = state.snapshot;
      const previousMeta = previous?.meta || {};
      const previousHasAnalysis = !previousMeta.chart_only || previousMeta.analysis_preserved;
      if (!previous?.bars?.length || !previousHasAnalysis || !sameSnapshotIdentity(previous, chartSnapshot)) return chartSnapshot;
      const sameWindow = sameBarWindow(previous.bars, chartSnapshot.bars);
      const preserveStaleAnalysis = Boolean(options.preserveStaleAnalysis);
      if (!sameWindow && !preserveStaleAnalysis) return chartSnapshot;
      const preserved = {
        ...previous,
        ...chartSnapshot,
        bars: chartSnapshot.bars,
        meta: {
          ...previousMeta,
          ...(chartSnapshot.meta || {}),
          chart_only: true,
          analysis_preserved: true,
          analysis_preserved_stale: !sameWindow,
        },
      };
      const preserveKeys = sameWindow
        ? ["indicators", "features", "levels", "candidates", "decision", "direction_sentiment", "command", "trade_setup", "gex_dynamics", "vsa_volume"]
        : ["indicators", "decision", "direction_sentiment", "command", "trade_setup", "gex_dynamics", "vsa_volume"];
      for (const key of preserveKeys) {
        if (!emptyAnalysisValue(previous[key]) && emptyAnalysisValue(chartSnapshot[key])) preserved[key] = previous[key];
      }
      return preserved;
    }

    function snapshotAnalysisPreservedStale(snapshot) {
      return snapshot?.meta?.analysis_preserved === true
        && snapshot?.meta?.analysis_preserved_stale === true;
    }

    function marketAnalysisCanPoll(snapshot = state.snapshot) {
      return MARKET_ANALYSIS_ACTIVE_STATUSES.has(String(snapshot?.meta?.analysis_status || ""))
        && Boolean(String(snapshot?.meta?.analysis_key || "").trim());
    }

    function projectMarketAnalysisLifecycle(
      payload,
      requestInstrumentId,
      requestRouteFingerprint,
      statusOverride = "",
    ) {
      if (
        !state.snapshot?.meta
        || exactIdentityText(state.instrumentId) !== exactIdentityText(requestInstrumentId)
        || instrumentRouteFingerprint() !== exactIdentityText(requestRouteFingerprint)
      ) return false;
      const runtimeStatus = String(payload?.status || "").trim().toLowerCase();
      const status = String(statusOverride || runtimeStatus || "degraded").trim().toLowerCase();
      state.snapshot.meta.analysis_status = status;
      state.snapshot.meta.analysis_runtime_status = runtimeStatus || status;
      const reason = payload?.error && typeof payload.error === "object" && !Array.isArray(payload.error)
        ? payload.error
        : null;
      if (reason) state.snapshot.meta.analysis_reason = { ...reason };
      else delete state.snapshot.meta.analysis_reason;
      if (typeof renderMarketHealthStatus === "function") {
        renderMarketHealthStatus(state.snapshot);
      }
      if (typeof renderIndicatorRuntimeStatus === "function") {
        renderIndicatorRuntimeStatus(state.snapshot);
      }
      return true;
    }

    function scheduleAnalysisRefreshAfterMissing(requestInstrumentId, requestRouteFingerprint, requestSource, requestTimeframe, range, options = {}) {
      const expectedAnalysisKey = String(state.snapshot?.meta?.analysis_key || "").trim();
      if (!expectedAnalysisKey) return false;
      const refreshKey = JSON.stringify([
        requestInstrumentId,
        requestRouteFingerprint,
        requestSource,
        requestTimeframe,
        String(range || ""),
        expectedAnalysisKey,
        Boolean(options.requireLatestAnalysis),
      ]);
      if (state.analysisMissingRefreshKey === refreshKey) return false;
      const recoveryScope = JSON.stringify([
        requestInstrumentId,
        requestRouteFingerprint,
        requestSource,
        requestTimeframe,
        String(range || ""),
        expectedAnalysisKey,
      ]);
      if (state.analysisMissingRecoveryScope !== recoveryScope) {
        state.analysisMissingRecoveryScope = recoveryScope;
        state.analysisMissingRecoveryAttempt = 0;
      }
      const attempt = Math.max(Number(state.analysisMissingRecoveryAttempt || 0), 0) + 1;
      if (attempt > MARKET_ANALYSIS_MISSING_MAX_ATTEMPTS) return false;
      state.analysisMissingRecoveryAttempt = attempt;
      state.analysisMissingRefreshKey = refreshKey;
      const delayMs = Math.min(1000 * (2 ** (attempt - 1)), 15000);
      if (state.requests.analysis.pollTimer) clearTimeout(state.requests.analysis.pollTimer);
      state.requests.analysis.pollTimer = setTimeout(async () => {
        state.requests.analysis.pollTimer = null;
        try {
          if (
            state.analysisMissingRefreshKey !== refreshKey
            || state.serverSleeping
            || state.instrumentId !== requestInstrumentId
            || instrumentRouteFingerprint() !== requestRouteFingerprint
            || state.dataSource !== requestSource
            || state.timeframe !== requestTimeframe
          ) {
            return;
          }
          const exactLeaseActive = marketAnalysisLeaseMatchesExactScope(
            requestInstrumentId,
            requestRouteFingerprint,
            expectedAnalysisKey,
          );
          const refreshed = exactLeaseActive
            ? await refreshRegisteredMarketAnalysis({
                requestInstrumentId,
                requestRouteFingerprint,
                requestSymbol: state.symbol,
                requestSource,
                requestTimeframe,
                range,
                analysisVersionKey: String(options.analysisVersionKey || ""),
                requireBarTs: String(options.requireBarTs || ""),
                requireCanonicalRevision: Math.max(Number(options.requireCanonicalRevision || 0), 0),
              })
            : false;
          if (!refreshed) {
            await load({
              splitAnalysis: true,
              queueAnalysis: true,
              analysisOnly: true,
              requireLatestAnalysis: Boolean(options.requireLatestAnalysis),
              analysisVersionKey: String(options.analysisVersionKey || ""),
              analysisRequireBarTs: String(options.requireBarTs || ""),
              analysisCanonicalRevision: Math.max(Number(options.requireCanonicalRevision || 0), 0),
              analysisMissingRecovery: true,
              range,
            });
          }
        } finally {
          if (state.analysisMissingRefreshKey === refreshKey) state.analysisMissingRefreshKey = "";
        }
      }, delayMs);
      return true;
    }

    async function loadMarketAnalysisSnapshot(analysisKey, requestInstrumentId, requestRouteFingerprint, requestSymbol, requestSource, requestTimeframe, range, options = {}) {
      const key = String(analysisKey || "").trim();
      if (!key) return false;
      const requireBarTs = timestampKey(options.requireBarTs || "");
      const requireCanonicalRevision = Math.max(Number(options.requireCanonicalRevision || 0), 0);
      if (
        requireCanonicalRevision > 0
        && Number(state.snapshot?.meta?.chart_canonical_revision || 0) !== requireCanonicalRevision
      ) {
        debugStep("analysis stale wait", `canonical revision ${requireCanonicalRevision} superseded`);
        return false;
      }
      if (
        state.snapshot?.meta?.analysis_async
        && state.snapshot?.meta?.analysis_preserved !== true
        && String(state.snapshot?.meta?.analysis_key || "").trim() === key
        && (!requireBarTs || snapshotCoversAnalysisBar(state.snapshot, requireBarTs))
        && snapshotCoversAnalysisCanonicalRevision(state.snapshot, requireCanonicalRevision)
      ) {
        debugStep("analysis already merged", key);
        return false;
      }
      if (state.requests.analysis.loading && state.requests.analysis.key === key) {
        debugStep("analysis already waiting", key);
        return false;
      }
      if (state.requests.analysis.pollKey && state.requests.analysis.pollKey !== key) {
        if (state.requests.analysis.pollTimer) clearTimeout(state.requests.analysis.pollTimer);
        state.requests.analysis.pollTimer = null;
        state.requests.analysis.pollAttempt = 0;
      }
      if (state.requests.analysis.abort) {
        try {
          state.requests.analysis.abort.abort();
        } catch (_) {
          // noop
        }
      }
      const controller = new AbortController();
      state.requests.analysis.abort = controller;
      state.requests.analysis.key = key;
      state.requests.analysis.pollKey = key;
      state.requests.analysis.loading = true;
      try {
        debugStep("analysis wait", `${requestSymbol} ${requestTimeframe} ${range} ${key}`);
        if (state.serverSleeping && !options.allowWhenSleeping) {
          debugStep("analysis paused", "server sleeping");
          return false;
        }
        if (
          state.requests.analysis.key !== key
          || state.instrumentId !== requestInstrumentId
          || instrumentRouteFingerprint() !== requestRouteFingerprint
          || state.dataSource !== requestSource
          || state.timeframe !== requestTimeframe
        ) {
          debugStep("analysis stale wait", `${requestSymbol} ignored; current ${state.symbol}`);
          return false;
        }
        const analysisParams = new URLSearchParams({
          key,
          instrument_id: requestInstrumentId,
          expected_route_fingerprint: requestRouteFingerprint,
          vsa_render_hours: String(vsaVolumeRenderHours()),
        });
        const analysisSnapshot = await fetchJson(`/api/market/analysis?${analysisParams.toString()}`, {
          signal: controller.signal,
          cache: "no-store",
          sharedTtlMs: 0,
        });
        if (
          exactIdentityText(analysisSnapshot?.instrument_id ?? analysisSnapshot?.meta?.instrument_id) !== requestInstrumentId
          || exactIdentityText(analysisSnapshot?.route_fingerprint ?? analysisSnapshot?.meta?.route_fingerprint) !== requestRouteFingerprint
        ) {
          projectMarketAnalysisLifecycle(
            analysisSnapshot,
            requestInstrumentId,
            requestRouteFingerprint,
            "error",
          );
          debugStep("analysis payload dropped", "instrument identity mismatch");
          return false;
        }
        if (window.mcRecordBackendTiming) {
          window.mcRecordBackendTiming("chart_guides", analysisSnapshot?.snapshot?.chart_guides?.meta?.compute_ms, `${requestSymbol} ${requestTimeframe} analysis`);
        }
        if (analysisSnapshot?.status === "running" || analysisSnapshot?.status === "queued") {
          state.requests.analysis.pollAttempt = Math.min(
            Number(state.requests.analysis.pollAttempt || 0) + 1,
            MARKET_ANALYSIS_POLL_MAX_ATTEMPTS,
          );
          const pollingExhausted = (
            state.requests.analysis.pollAttempt >= MARKET_ANALYSIS_POLL_MAX_ATTEMPTS
          );
          projectMarketAnalysisLifecycle(
            analysisSnapshot,
            requestInstrumentId,
            requestRouteFingerprint,
            pollingExhausted ? "degraded" : analysisSnapshot.status,
          );
          const pollDelayMs = Math.min(250 * (2 ** Math.min(state.requests.analysis.pollAttempt - 1, 3)), 2000);
          if (!pollingExhausted) {
            if (state.requests.analysis.pollTimer) clearTimeout(state.requests.analysis.pollTimer);
            state.requests.analysis.pollTimer = setTimeout(() => {
              state.requests.analysis.pollTimer = null;
              if (
                state.serverSleeping
                || state.requests.analysis.pollKey !== key
                || state.instrumentId !== requestInstrumentId
                || instrumentRouteFingerprint() !== requestRouteFingerprint
                || state.dataSource !== requestSource
                || state.timeframe !== requestTimeframe
              ) {
                return;
              }
              loadMarketAnalysisSnapshot(key, requestInstrumentId, requestRouteFingerprint, requestSymbol, requestSource, requestTimeframe, range, options);
            }, pollDelayMs);
          }
          debugStep("analysis revalidate pending", `${key} retry ${state.requests.analysis.pollAttempt}`);
          return false;
        }
        if (analysisSnapshot?.status === "missing") {
          state.requests.analysis.pollAttempt = Math.min(
            Number(state.requests.analysis.pollAttempt || 0) + 1,
            MARKET_ANALYSIS_MISSING_MAX_ATTEMPTS,
          );
          const scheduled = scheduleAnalysisRefreshAfterMissing(requestInstrumentId, requestRouteFingerprint, requestSource, requestTimeframe, range, {
            requireLatestAnalysis: Boolean(requireBarTs),
            requireBarTs,
            requireCanonicalRevision,
          });
          projectMarketAnalysisLifecycle(
            analysisSnapshot,
            requestInstrumentId,
            requestRouteFingerprint,
            scheduled ? "repairing" : "degraded",
          );
          debugStep("analysis cache missing", scheduled ? "bounded refresh queued" : "refresh stopped");
          return false;
        }
        if (analysisSnapshot?.status === "error") {
          state.requests.analysis.pollAttempt = 0;
          projectMarketAnalysisLifecycle(
            analysisSnapshot,
            requestInstrumentId,
            requestRouteFingerprint,
          );
          debugStep("analysis server error", apiErrorMessage(analysisSnapshot, analysisSnapshot.message || "error"));
          return false;
        }
        if (!isValidMarketSnapshotPayload(analysisSnapshot)) {
          projectMarketAnalysisLifecycle(
            {
              status: "error",
              error: {
                code: "MARKET_ANALYSIS_PAYLOAD_INVALID",
                category: "analysis",
                retryable: true,
                message: "Analysis response did not satisfy the typed snapshot contract.",
              },
            },
            requestInstrumentId,
            requestRouteFingerprint,
            "degraded",
          );
          debugStep("analysis payload dropped", "invalid snapshot shape");
          return false;
        }
        if (requireBarTs && !snapshotCoversAnalysisBar(analysisSnapshot, requireBarTs)) {
          projectMarketAnalysisLifecycle(
            {
              status: "missing",
              error: {
                code: "MARKET_ANALYSIS_BAR_NOT_READY",
                category: "analysis",
                retryable: true,
                message: "Analysis has not reached the required confirmed bar.",
              },
            },
            requestInstrumentId,
            requestRouteFingerprint,
            "repairing",
          );
          debugStep("analysis stale payload", `${requireBarTs} not ready`);
          return false;
        }
        if (
          requireCanonicalRevision > 0
          && Number(state.snapshot?.meta?.chart_canonical_revision || 0) !== requireCanonicalRevision
        ) {
          debugStep("analysis stale payload", `canonical revision ${requireCanonicalRevision} superseded`);
          return false;
        }
        const normalizedSnapshot = normalizeSnapshotBarSlots(analysisSnapshot);
        const merged = mergeAnalysisSnapshot(
          normalizedSnapshot,
          requestInstrumentId,
          requestRouteFingerprint,
          requestTimeframe,
          range,
          { requireBarTs, requireCanonicalRevision },
        );
        state.requests.analysis.pollAttempt = 0;
        state.requests.analysis.pollKey = "";
        debugStep("analysis response", merged ? "merged server cache" : "ignored");
        return merged;
      } catch (error) {
        if (error?.name !== "AbortError") {
          projectMarketAnalysisLifecycle(
            {
              status: "error",
              error: {
                code: "MARKET_ANALYSIS_TRANSPORT_FAILED",
                category: "analysis",
                retryable: true,
                message: requestErrorMessage(error, "market analysis failed"),
              },
            },
            requestInstrumentId,
            requestRouteFingerprint,
            "degraded",
          );
          console.warn("market analysis poll failed", error);
          debugStep("analysis error", requestErrorMessage(error, "market analysis failed"));
        }
        return false;
      } finally {
        if (state.requests.analysis.key === key) {
          state.requests.analysis.loading = false;
          state.requests.analysis.abort = null;
          state.requests.analysis.key = "";
          runPendingGexMarketRecalc();
        }
      }
    }

    function marketRefreshFailureNotice(error, options = {}) {
      if (error?.name === "TimeoutError" && options.historyLoad) {
        return `Local history read timed out after ${Math.round(MARKET_LOCAL_HISTORY_TIMEOUT_MS / 1000)}s. Retry the same range.`;
      }
      if (error?.name === "TimeoutError" && options.liveTailRefresh) {
        return `Current market tail timed out after ${Math.round(MARKET_LIVE_TAIL_TIMEOUT_MS / 1000)}s. Retry Go Live.`;
      }
      const hasExistingBars = Array.isArray(state.snapshot?.bars) && state.snapshot.bars.length > 0;
      const prefix = hasExistingBars ? "Market refresh failed" : "No history";
      return `${prefix}: ${requestErrorMessage(error, "market refresh failed")}`;
    }

    async function load(options = {}) {
      const perfToken = window.mcPerfStart ? window.mcPerfStart("loadMarket") : null;
      if (state.serverSleeping && !options.allowWhenSleeping) {
        closeQuoteStream();
        closeChartStream();
        abortMarketAnalysis();
        releaseMarketAnalysisLease();
        setUiNotice(
          "system",
          "SERVER_SLEEPING",
          "Server sleeping: IBKR refresh is paused.",
          { state: "sleeping", routeScoped: false },
        );
        settleHistoryPresetNotice(options, {
          code: "SERVER_SLEEPING",
          message: "Server sleeping: chart history load is paused.",
          state: "sleeping",
        });
        if (state.snapshot) {
          renderPanel(state.snapshot);
          renderInstruments();
          renderCharts(state.snapshot);
        }
        debugStep("market skipped", "server sleeping");
        return 0;
      }
      if (!options.historyLoad && providerCapability(instrumentForId()?.provider, "chart_stream")) {
        connectChartStream(false);
      }
      const requestSymbol = state.symbol;
      const requestInstrumentId = exactIdentityText(state.instrumentId);
      const requestRouteFingerprint = instrumentRouteFingerprint();
      if (!requestInstrumentId || !requestRouteFingerprint) {
        setUiNotice(
          "market",
          "MARKET_ROUTE_REQUIRED",
          "Qualified instrument identity is required for market load",
          { state: "blocked", routeScoped: false },
        );
        settleHistoryPresetNotice(options, {
          code: "MARKET_ROUTE_REQUIRED",
          message: "Qualified instrument identity is required for market load",
          state: "blocked",
        });
        return 0;
      }
      if (state.gexContext && exactIdentityText(state.gexContext.route_fingerprint) !== requestRouteFingerprint) clearGexContext();
      const requestSource = String(instrumentForId()?.provider || "").trim().toLowerCase();
      state.dataSource = requestSource;
      const requestTimeframe = state.timeframe;
      const requestedRange = options.range || state.range || defaultRangeFor(state.timeframe);
      const range = requestedRange;
      const saveRangeState = () => {
        try {
          setServerSettingValue(rangeStorageKey(state.instrumentId, state.timeframe), state.range);
        } catch (error) {
          console.warn("range preference save failed", error);
        }
      };
      if (
        options.analysisOnly
        && options.reuseAnalysisDemand === true
        && await refreshRegisteredMarketAnalysis({
          requestInstrumentId,
          requestRouteFingerprint,
          requestSymbol,
          requestSource,
          requestTimeframe,
          range,
          analysisVersionKey: options.analysisVersionKey,
          requireBarTs: options.analysisRequireBarTs,
          requireCanonicalRevision: options.analysisCanonicalRevision,
        })
      ) {
        return 0;
      }
      const params = new URLSearchParams({
        instrument_id: requestInstrumentId,
        expected_route_fingerprint: requestRouteFingerprint,
        interval: requestTimeframe,
        range,
        signal_range: normalizeSignalsRange(state.settings.signalsRange),
        global_atr_len: String(clamp(numericValueOrFallback(state.indicators.globalDefaults.atrLen, 14), 2, 100)),
        global_rvol_len: String(clamp(numericValueOrFallback(state.indicators.globalDefaults.rvolLen, 30), 2, 200)),
        global_ema_pullback: String(clamp(numericValueOrFallback(state.indicators.globalDefaults.emaPullback, 20), 5, 300)),
        global_ema_fast: String(clamp(numericValueOrFallback(state.indicators.globalDefaults.emaFast, 21), 5, 300)),
        global_ema_slow: String(clamp(numericValueOrFallback(state.indicators.globalDefaults.emaSlow, 55), 10, 400)),
        global_ema_magnet: String(clamp(numericValueOrFallback(state.indicators.globalDefaults.emaMagnet, 233), 50, 1000)),
        global_score_pre: String(clamp(numericValueOrFallback(state.indicators.globalDefaults.scorePre, 42), 0, 99)),
        global_score_watch: String(clamp(numericValueOrFallback(state.indicators.globalDefaults.scoreWatch, 58), 0, 99)),
        global_score_arm: String(clamp(numericValueOrFallback(state.indicators.globalDefaults.scoreArm, 70), 0, 99)),
        global_score_go: String(clamp(numericValueOrFallback(state.indicators.globalDefaults.scoreGo, 78), 0, 99)),
        global_rvol_low: String(clamp(numericValueOrFallback(state.indicators.globalDefaults.rvolLow, 0.85), 0, 5)),
        global_rvol_elevated: String(clamp(numericValueOrFallback(state.indicators.globalDefaults.rvolElevated, 1.10), 0, 5)),
        global_rvol_high: String(clamp(numericValueOrFallback(state.indicators.globalDefaults.rvolHigh, 1.20), 0, 5)),
        global_rvol_climax: String(clamp(numericValueOrFallback(state.indicators.globalDefaults.rvolClimax, 1.60), 0, 10)),
        vsa_render_hours: String(vsaVolumeRenderHours()),
        strategy_mode: state.strategyMode || "balanced",
        signal_min_rr: String([0.75, 1.25, 1.5, 2].includes(Number(state.settings.paperMinRr)) ? Number(state.settings.paperMinRr) : 1.25),
        gex_context_active: gexLayerVisible() ? "true" : "false",
        gex_capture_mode: effectiveGexContextMode(),
      });
      appendManifestIndicatorRequestParams(params);
      if (window.mcPerfEnabled && window.mcPerfEnabled()) params.set("debug", "true");
      const splitAnalysis = !options.historyLoad && options.splitAnalysis !== false;
      const historyAnalysis = Boolean(options.historyLoad);
      const queueAnalysis = (
        (splitAnalysis || historyAnalysis)
        && options.queueAnalysis !== false
      );
      if (queueAnalysis && options.analysisMissingRecovery !== true) {
        state.analysisMissingRecoveryScope = "";
        state.analysisMissingRecoveryAttempt = 0;
      }
      params.set("chart_only", (options.historyLoad || splitAnalysis) ? "true" : "false");
      params.set("queue_analysis", queueAnalysis ? "true" : "false");
      let analysisLeaseSequence = 0;
      if (queueAnalysis) {
        state.requests.analysis.leaseSequence += 1;
        analysisLeaseSequence = state.requests.analysis.leaseSequence;
        params.set("analysis_client_id", state.clientId);
        params.set("analysis_lease_sequence", String(analysisLeaseSequence));
      }
      if (options.analysisVersionKey) params.set("analysis_version", String(options.analysisVersionKey));
      const url = `/api/market?${params.toString()}`;
      const marketRequestKey = JSON.stringify([
        url,
        Boolean(options.historyLoad),
        Boolean(options.liveTailRefresh),
        Boolean(options.analysisOnly),
        Boolean(options.requireLatestAnalysis),
      ]);
      if (state.requests.market.loading) {
        if (options.force && state.requests.market.abort) {
          state.requests.market.abort.abort();
          state.requests.market.pending = false;
          state.requests.market.pendingOptions = null;
        } else if (state.requests.market.key === marketRequestKey) {
          debugStep("market already loading", marketRequestKey);
          return 0;
        } else {
          state.requests.market.pending = true;
          state.requests.market.pendingOptions = { ...options };
          debugStep("market queued", "previous request is still running");
          return 0;
        }
      }
      state.requests.market.loading = true;
      state.requests.market.pending = false;
      state.requests.market.pendingOptions = null;
      state.requests.market.key = marketRequestKey;
      abortMarketAnalysis();
      const controller = new AbortController();
      state.requests.market.abort = controller;
      const requestOwnsMarketState = () => state.requests.market.abort === controller;
      const requestOwnsExactMarketScope = () => (
        requestOwnsMarketState()
        && exactIdentityText(state.instrumentId) === requestInstrumentId
        && instrumentRouteFingerprint() === requestRouteFingerprint
        && state.dataSource === requestSource
        && state.timeframe === requestTimeframe
        && state.range === range
      );
      state.lastMarketLoadAt = Date.now();
      let historyAdvanced = false;
      let historyCovered = false;
      try {
        debugStep("market request", `${requestSymbol} ${requestTimeframe} ${range} via ${requestSource}`);
        const marketTimeoutMs = options.historyLoad
            ? MARKET_LOCAL_HISTORY_TIMEOUT_MS
            : options.liveTailRefresh
              ? MARKET_LIVE_TAIL_TIMEOUT_MS
              : 0;
        const rawSnapshot = await fetchMarketSnapshotWithTransitionRetry(
          url,
          {
            signal: controller.signal,
            timeoutMs: marketTimeoutMs,
          },
          () => requestOwnsExactMarketScope() && !state.requests.market.pending,
        );
        if (!requestOwnsExactMarketScope()) {
          const ownsMarketState = requestOwnsMarketState();
          if (ownsMarketState) settleHistoryPresetNotice(options);
          if (ownsMarketState && !state.requests.market.pending) {
            state.requests.market.pending = true;
            state.requests.market.pendingOptions = { queueAnalysis: false };
          }
          debugStep("market superseded response", `${requestSymbol} ignored; chart scope changed`);
          return 0;
        }
        if (window.mcRecordBackendTiming) {
          window.mcRecordBackendTiming("chart_guides", rawSnapshot?.chart_guides?.meta?.compute_ms, `${requestSymbol} ${requestTimeframe}`);
        }
        if (!isValidMarketSnapshotPayload(rawSnapshot)) {
          console.warn("market payload dropped: invalid snapshot shape");
          debugStep("market payload dropped", "invalid snapshot shape");
          const invalidCode = "MARKET_SNAPSHOT_INVALID";
          const invalidMessage = "Market snapshot is invalid for the requested chart scope.";
          projectMarketChartLoadFailure(options, invalidCode, invalidMessage);
          return 0;
        }
        const normalizePerf = window.mcPerfStart ? window.mcPerfStart("marketNormalizeCompact") : null;
        let snapshot;
        try {
          snapshot = compactMarketSnapshot(normalizeSnapshotBarSlots(rawSnapshot));
        } finally {
          if (window.mcPerfEnd) {
            window.mcPerfEnd(
              normalizePerf,
              `${requestSymbol} ${requestTimeframe} ${range}`,
              80,
            );
          }
        }
        if (!marketSnapshotMatchesExactScope(
          snapshot,
          requestInstrumentId,
          requestRouteFingerprint,
          requestTimeframe,
          range,
        )) {
          const mismatchCode = "MARKET_SNAPSHOT_SCOPE_MISMATCH";
          const mismatchMessage = "Market snapshot scope does not match the exact chart request.";
          projectMarketChartLoadFailure(options, mismatchCode, mismatchMessage);
          debugStep("market scope mismatch", `${requestSymbol} ${requestTimeframe} ${range} rejected`);
          return 0;
        }
        settleHistoryPresetNotice(options);
        if (queueAnalysis) {
          const analysisStatus = String(snapshot.meta?.analysis_status || "");
          if (
            MARKET_ANALYSIS_ACTIVE_STATUSES.has(analysisStatus)
            && String(snapshot.meta?.analysis_key || "").trim()
          ) {
            activateMarketAnalysisLease(
              snapshot.meta.analysis_key,
              requestInstrumentId,
              requestRouteFingerprint,
              analysisLeaseSequence,
            );
          } else {
            releaseMarketAnalysisLease();
          }
        }
        snapshot = reconcileSnapshotFutureAxis(snapshot, state.snapshot);
        const bars = snapshot.bars || [];
        const historyCoverage = options.historyLoad && snapshot.meta?.history_coverage && typeof snapshot.meta.history_coverage === "object"
          ? snapshot.meta.history_coverage
          : {};
        const historyCoverageState = ["complete", "partial", "unknown", "failed"].includes(String(historyCoverage.state || ""))
          ? String(historyCoverage.state)
          : "unknown";
        const historyDisplayState = ["loaded", "empty"].includes(String(historyCoverage.display_state || ""))
          ? String(historyCoverage.display_state)
          : "unknown";
        const historyLoadedFromMs = Date.parse(historyCoverage.loaded_from || "");
        const historyLoadedToMs = Date.parse(historyCoverage.loaded_to || "");
        const historyDisplayMatchesBars = historyDisplayState === "empty"
          ? bars.length === 0
          : historyDisplayState === "loaded"
            && bars.length > 0
            && Number.isFinite(historyLoadedFromMs)
            && Number.isFinite(historyLoadedToMs)
            && historyLoadedFromMs <= historyLoadedToMs;
        const historyCoverageMatchesRange = !options.historyLoad
          || String(historyCoverage.requested_range || "") === String(range);
        const historyDisplayResolved = options.historyLoad
          && historyCoverageMatchesRange
          && historyDisplayMatchesBars;
        const historyVerification = historyCoverageVerificationStatus(
          historyCoverage,
          requestSource,
        );
        const historyVerificationPending = options.historyLoad
          && ["partial", "unknown"].includes(historyCoverageState)
          && historyVerification.active;
        historyCovered = options.historyLoad
          && historyCoverageMatchesRange
          && historyCoverageState === "complete";
        debugStep("market response", `${bars.length} bars, ${snapshot.meta?.source || "unknown source"}`);
        if (options.analysisOnly && snapshot.meta?.chart_only) {
          clearUiNotice("market");
          mergeChartGuidesFromChartSnapshot(snapshot, {
            requireBarTs: options.analysisRequireBarTs,
            requireCanonicalRevision: options.analysisCanonicalRevision,
          });
          if (queueAnalysis && marketAnalysisCanPoll(snapshot)) {
            loadMarketAnalysisSnapshot(
              snapshot.meta?.analysis_key,
              requestInstrumentId,
              requestRouteFingerprint,
              requestSymbol,
              requestSource,
              requestTimeframe,
              range,
              {
                requireBarTs: options.analysisRequireBarTs,
                requireCanonicalRevision: options.analysisCanonicalRevision,
              },
            );
          }
          return 0;
        }
        if (!options.historyLoad && bars.length) {
          retainCanonicalLiveTail(
            bars.slice(-CLIENT_CANONICAL_TAIL_BARS),
            snapshot.meta,
            {
              replace: true,
              verified: true,
              snapshot,
            },
          );
        }
        if (options.liveTailRefresh) {
          if (!bars.length) {
            setUiNotice(
              "market",
              "MARKET_TAIL_UNAVAILABLE",
              marketSnapshotNotice(snapshot) || "Current chart tail is not available yet.",
              { state: "empty" },
            );
            return 0;
          }
          const tailBars = bars.slice(-CLIENT_CANONICAL_TAIL_BARS);
          state.snapshot = snapshot;
          touchChartBarsVersion();
          restoreCanonicalLiveTail();
          state.range = String(snapshot.meta?.chart_range || range);
          state.requests.history.exhausted = false;
          saveRangeState();
          clearUiNotice("market");
          clearUiNotice("history", "HISTORY_RETURNING_TO_TAIL");
          view.followLatest = true;
          view.offset = 0;
          view.historyPullOffsetBars = 0;
          clampView(state.snapshot);
          if (state.snapshot) {
            renderCharts(state.snapshot);
            renderPanel(state.snapshot);
          }
          connectChartStream(false);
          if (queueAnalysis && marketAnalysisCanPoll(snapshot)) {
            const requireBarTs = timestampKey(currentAnalysisBars(state.snapshot).slice(-1)[0]?.ts);
            loadMarketAnalysisSnapshot(
              snapshot.meta?.analysis_key,
              requestInstrumentId,
              requestRouteFingerprint,
              requestSymbol,
              requestSource,
              requestTimeframe,
              range,
              { requireBarTs },
            );
          }
          return tailBars.length;
        }
        if (!bars.length) {
          const warning = marketSnapshotNotice(snapshot) || `No ${state.dataSource.toUpperCase()} history for ${state.symbol} ${state.timeframe} ${range}`;
          if (options.historyLoad && state.snapshot?.bars?.length) {
            if (historyDisplayResolved) {
              state.range = range;
              saveRangeState();
              state.snapshot = {
                ...state.snapshot,
                meta: {
                  ...(state.snapshot.meta || {}),
                  history_coverage: historyCoverage,
                  chart_range: range,
                },
              };
            }
            if (historyCovered) {
              state.requests.history.exhausted = historyCoverage.has_older === false;
              if (state.requests.history.exhausted) view.historyPullOffsetBars = 0;
              setUiNotice(
                "history",
                state.requests.history.exhausted ? "HISTORY_RANGE_EXHAUSTED" : "HISTORY_VERIFIED",
                state.requests.history.exhausted
                  ? `Verified ${range} history; provider reports no older bars.`
                  : `Verified ${range} history.`,
                { state: "ready" },
              );
            } else {
              state.requests.history.exhausted = false;
              const historyMessage = historyDisplayResolved && historyVerificationPending
                ? `Checked local ${range} history; bar repair continues in background.`
                : historyDisplayResolved && historyVerification.concreteRepair
                  ? `Checked local ${range} history; bar repair status is ${historyVerification.repairStatus}.`
                  : historyDisplayResolved
                    ? `Checked local ${range} history.`
                    : `${warning} Local history display state is unavailable.`;
              setUiNotice(
                "history",
                historyVerificationPending
                  ? "HISTORY_REPAIR_PENDING"
                  : historyDisplayResolved ? "HISTORY_CHECKED" : "HISTORY_DISPLAY_UNAVAILABLE",
                historyMessage,
                { state: historyVerificationPending ? "degraded" : historyDisplayResolved ? "ready" : "empty" },
              );
            }
            renderPanel(state.snapshot);
            renderCharts(state.snapshot);
            return 0;
          }
          state.snapshot = snapshot;
          if (resolveDrawingAnchorsAgainstSnapshot(state.drawing.objects, snapshot)) {
            touchChartUserObjectsVersion();
          }
          touchChartBarsVersion();
          setUiNotice("market", "MARKET_HISTORY_UNAVAILABLE", warning, { state: "empty" });
          renderPanel(snapshot);
          renderInstruments();
          renderTimeframes();
          applyLayout();
          renderCharts(snapshot);
          if (providerCapability(state.dataSource, "live_quote_stream") || providerCapability(state.dataSource, "live_quote_polling")) loadLiveQuote();
          if (providerCapability(state.dataSource, "chart_stream")) connectChartStream(false);
          else closeChartStream();
          return 0;
        }

        if (options.historyLoad) {
          const previousBars = options.previousBars || 0;
          const previousLoadedFromMs = Date.parse(options.previousLoadedFrom || "");
          const nextEarliestBar = bars.find((bar) => barIsConfirmedClosed(bar));
          const nextLoadedFromMs = Number.isFinite(historyLoadedFromMs)
            ? historyLoadedFromMs
            : Date.parse(nextEarliestBar?.ts || "");
          historyAdvanced = Number.isFinite(previousLoadedFromMs) && Number.isFinite(nextLoadedFromMs)
            ? nextLoadedFromMs < previousLoadedFromMs
            : bars.length > previousBars;
          if (historyCoverageMatchesRange && (historyDisplayResolved || historyAdvanced)) {
            state.range = range;
            saveRangeState();
          }
          if (historyCovered) {
            state.requests.history.exhausted = historyCoverage.has_older === false;
            setUiNotice(
              "history",
              state.requests.history.exhausted ? "HISTORY_RANGE_EXHAUSTED" : "HISTORY_VERIFIED",
              state.requests.history.exhausted
                ? `Verified ${range} history; provider reports no older bars.`
                : `Verified ${range} history.`,
              { state: "ready" },
            );
          } else if (!historyDisplayResolved) {
            state.requests.history.exhausted = false;
            setUiNotice(
              "history",
              "HISTORY_DISPLAY_UNAVAILABLE",
              `${marketSnapshotNotice(snapshot) || `No additional ${state.dataSource.toUpperCase()} bars in ${range}`} Local history display state is unavailable.`,
              { state: "empty" },
            );
          } else {
            state.requests.history.exhausted = false;
            const historyMessage = historyVerificationPending
              ? historyAdvanced
                ? `Loaded ${range} history from local DB; bar repair continues in background.`
                : `Local ${range} history is loaded; bar repair continues in background.`
              : historyVerification.concreteRepair
                ? `Local ${range} history is loaded; bar repair status is ${historyVerification.repairStatus}.`
                : `Local ${range} history is loaded.`;
            setUiNotice(
              "history",
              historyVerificationPending ? "HISTORY_REPAIR_PENDING" : "HISTORY_LOADED",
              historyMessage,
              { state: historyVerificationPending ? "degraded" : "ready" },
            );
          }
        } else {
          state.range = range;
          setUiNotice(
            "market",
            "MARKET_SNAPSHOT_NOTICE",
            marketSnapshotNotice(snapshot) || "",
            { state: "degraded" },
          );
          saveRangeState();
        }

        const commitPerf = window.mcPerfStart ? window.mcPerfStart("marketStateCommit") : null;
        const displaySnapshot = preserveAnalysisForChartOnlySnapshot(snapshot, { preserveStaleAnalysis: true });
        if (typeof restoreLiveBarPreview === "function") restoreLiveBarPreview(displaySnapshot);
        const previousSnapshot = state.snapshot;
        state.snapshot = displaySnapshot;
        if (resolveDrawingAnchorsAgainstSnapshot(state.drawing.objects, displaySnapshot)) {
          touchChartUserObjectsVersion();
        }
        touchChartBarsVersion();
        const historyCommit = options.historyLoad
          && historyCoverageMatchesRange
          && (historyAdvanced || historyDisplayResolved);
        const resetInitialViewport = options.initialViewport === true;
        if (historyCommit && !resetInitialViewport) {
          view.followLatest = options.historyFollowLatest === true && view.followLatest;
        }
        if (resetInitialViewport) {
          resetTimeViewportToLatest(displaySnapshot);
          saveBarsVisibleForCurrent();
        } else if (!view.followLatest && previousSnapshot?.bars?.length) {
          const anchored = applyHistoryViewportAnchor(displaySnapshot, {
            ...options,
            previousSnapshot,
            previousBars: previousSnapshot.bars.length,
            previousOffset: Number(view.offset) || 0,
          });
          debugStep(
            historyCommit ? "history viewport" : "background viewport",
            anchored ? "commit anchor restored" : "request anchor fallback",
          );
        } else if (view.followLatest) {
          view.offset = 0;
          view.historyPullOffsetBars = 0;
        }
        if (state.requests.history.exhausted) view.historyPullOffsetBars = 0;
        clampView(displaySnapshot);
        const activeSnapshot = pruneClientHistoryWindow(options.historyLoad ? "history" : "load");
        if (window.mcPerfEnd) {
          window.mcPerfEnd(
            commitPerf,
            `${requestSymbol} ${requestTimeframe} ${range}`,
            80,
          );
        }
        renderPanel(activeSnapshot);
        renderInstruments();
        renderTimeframes();
        applyLayout();
        renderCharts(
          activeSnapshot,
          options.historyLoad
            ? { immediate: true, overlayImmediate: true, force: true, reason: "history commit" }
            : {},
        );
        if (resetInitialViewport) maybeLoadMoreHistory();
        if (!snapshot.meta?.chart_only) {
          refreshPaperJournal(snapshot, paperJournalRefreshOptions(snapshot));
          if (gexLayerVisible()) loadGexContext({ refresh: false });
          else clearGexContext();
          if (typeof syncIndicatorModuleLifecycles === "function") {
            void syncIndicatorModuleLifecycles({
              snapshot,
              reason: "market-commit",
            });
          }
        } else if (splitAnalysis || historyAnalysis) {
          if (gexLayerVisible()) loadGexContext({ refresh: false });
          else clearGexContext();
          if (
            queueAnalysis
            && marketAnalysisCanPoll(snapshot)
            && (!options.historyLoad || historyDisplayResolved)
          ) {
            const requireBarTs = options.requireLatestAnalysis ? timestampKey(currentAnalysisBars(snapshot).slice(-1)[0]?.ts) : "";
            loadMarketAnalysisSnapshot(snapshot.meta?.analysis_key, requestInstrumentId, requestRouteFingerprint, requestSymbol, requestSource, requestTimeframe, range, { requireBarTs });
          }
        }
        if (providerCapability(state.dataSource, "live_quote_stream") || providerCapability(state.dataSource, "live_quote_polling")) loadLiveQuote();
        if (providerCapability(state.dataSource, "chart_stream")) connectChartStream(false);
        else closeChartStream();
        return bars.length;
      } catch (error) {
        if (!requestOwnsMarketState()) {
          debugStep("market superseded error", `${requestSymbol} ignored; newer request owns chart state`);
          return 0;
        }
        if (error?.name === "AbortError") {
          settleHistoryPresetNotice(options);
          return 0;
        }
        const errorCode = requestErrorCode(error);
        if (MARKET_CHART_TRANSITION_RETRY_CODES.has(errorCode)) {
          const failureMessage = marketRefreshFailureNotice(error, options);
          console.warn("market chart transition failed", error);
          projectMarketChartLoadFailure(options, errorCode, failureMessage);
          return 0;
        }
        if (errorCode === "ROUTE_SELECTION_MISMATCH") {
          if (options.routeResyncAttempted !== true) {
            setUiNotice(
              "market",
              errorCode,
              "Instrument route changed. Refreshing the qualified watchlist route...",
              { state: "stale" },
            );
            try {
              await loadInstruments({ activate: false });
            } catch (routeError) {
              console.warn("market route resync failed", routeError);
            }
            if (requestOwnsMarketState() && instrumentForId(state.instrumentId)) {
              state.requests.market.pending = true;
              state.requests.market.pendingOptions = {
                ...options,
                force: true,
                routeResyncAttempted: true,
              };
              return 0;
            }
          }
          const failureMessage = marketRefreshFailureNotice(error, options);
          projectMarketChartLoadFailure(options, errorCode, failureMessage);
          return 0;
        }
        console.warn("market refresh failed", error);
        const failureMessage = marketRefreshFailureNotice(error, options);
        setUiNotice(
          "market",
          "MARKET_REFRESH_FAILED",
          failureMessage,
          { state: "error" },
        );
        settleHistoryPresetNotice(options, {
          code: errorCode || "MARKET_REFRESH_FAILED",
          message: failureMessage,
        });
        if (state.snapshot) {
          renderPanel(state.snapshot);
          renderCharts(state.snapshot);
        }
        return 0;
      } finally {
        if (window.mcPerfEnd) window.mcPerfEnd(perfToken, `${requestSymbol} ${requestTimeframe} ${range}`, 350);
        if (state.requests.market.abort === controller) {
          const pendingOptions = state.requests.market.pendingOptions;
          const hasPendingLoad = state.requests.market.pending;
          state.requests.market.abort = null;
          state.requests.market.loading = false;
          state.requests.market.key = "";
          state.requests.market.pending = false;
          state.requests.market.pendingOptions = null;
          if (hasPendingLoad) {
            void load(pendingOptions || {});
          }
          runPendingGexMarketRecalc();
        }
      }
    }
