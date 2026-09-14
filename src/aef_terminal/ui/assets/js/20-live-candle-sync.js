    function timestampKey(ts) {
      return drawingTimestampIdentity(ts);
    }

    function isValidChartStreamPayload(message) {
      if (!message || message.type !== "chart_bars" || !Array.isArray(message.bars)) return false;
      if (
        message.consumer_role !== "primary"
        || Object.prototype.hasOwnProperty.call(message, "secondary_tail_membership")
      ) return false;
      if (
        exactIdentityText(message.instrument_id) !== exactIdentityText(state.instrumentId)
        || exactIdentityText(message.route_fingerprint) !== instrumentRouteFingerprint()
        || message.interval !== state.timeframe
      ) return false;
      if (
        message.future_axis !== undefined
        && !isValidFutureAxisPayload(message.future_axis, message.interval)
      ) return false;
      const optionalObjects = [
        message.chart_data_quality,
        message.history_coverage,
        message.gap_repair,
      ];
      if (optionalObjects.some(value => (
        value !== undefined
        && value !== null
        && (typeof value !== "object" || Array.isArray(value))
      ))) return false;
      if (
        !Number.isSafeInteger(message.canonical_revision ?? 0)
        || (message.canonical_revision ?? 0) < 0
        || !Number.isSafeInteger(message.chart_quality_revision ?? 0)
        || (message.chart_quality_revision ?? 0) < 0
        || typeof message.recovery !== "boolean"
        || typeof message.recovery_complete !== "boolean"
      ) return false;
      const expectedLiveSlot = message.expected_live_slot;
      const expectedLiveClose = message.expected_live_close;
      if (
        ![expectedLiveSlot, expectedLiveClose].every(value => (
          value === undefined
          || value === null
          || (typeof value === "string" && Number.isFinite(Date.parse(value)))
        ))
        || (
          typeof expectedLiveSlot === "string"
          && typeof expectedLiveClose === "string"
          && Date.parse(expectedLiveClose) <= Date.parse(expectedLiveSlot)
        )
      ) return false;
      return message.bars.every((bar) => (
        !barIsGapPlaceholder(bar)
        && exactIdentityText(bar?.instrument_id) === exactIdentityText(state.instrumentId)
        && exactIdentityText(bar?.route_fingerprint) === instrumentRouteFingerprint()
        && String(bar?.timeframe ?? "") === String(state.timeframe ?? "")
        && Number.isFinite(Date.parse(bar?.ts || ""))
        && normalizeStreamBar(bar) !== null
      ));
    }

    function chartStreamMessageAdmission(message) {
      if (!message || typeof message !== "object" || Array.isArray(message)) {
        return { ok: false, reasonCode: "payload_invalid" };
      }
      if (
        message.consumer_role !== "primary"
        || Object.prototype.hasOwnProperty.call(message, "secondary_tail_membership")
      ) return { ok: false, reasonCode: "consumer_scope_mismatch" };
      if (
        exactIdentityText(message.instrument_id) !== exactIdentityText(state.instrumentId)
        || exactIdentityText(message.route_fingerprint) !== instrumentRouteFingerprint()
      ) return { ok: false, reasonCode: "identity_mismatch" };
      const messageType = String(message.type || "");
      let payloadValid = false;
      if (messageType === "chart_status") {
        payloadValid = Boolean(
          typeof message.message === "string"
          && message.message.trim()
          && (message.interval === undefined || message.interval === state.timeframe)
          && (
            message.requires_resubscribe === undefined
            || typeof message.requires_resubscribe === "boolean"
          )
        );
      } else if (messageType === "heartbeat") {
        payloadValid = Boolean(
          message.interval === state.timeframe
          && typeof message.source === "string"
          && message.source.trim()
          && Number.isFinite(Date.parse(message.ts || ""))
        );
      } else if (messageType === "chart_bars") {
        payloadValid = isValidChartStreamPayload(message);
      }
      if (!payloadValid) return { ok: false, reasonCode: "payload_invalid" };
      const streamGeneration = message.stream_generation ?? 0;
      const streamSeq = message.stream_seq ?? 0;
      if (messageType === "chart_status" && streamGeneration === 0 && streamSeq === 0) {
        return {
          ok: true,
          messageType,
          streamGeneration,
          streamSeq,
          unsequenced: true,
        };
      }
      if (
        !Number.isSafeInteger(streamGeneration)
        || streamGeneration <= 0
        || !Number.isSafeInteger(streamSeq)
        || streamSeq <= 0
      ) return { ok: false, reasonCode: "sequence_invalid" };
      return {
        ok: true,
        messageType,
        streamGeneration,
        streamSeq,
        unsequenced: false,
      };
    }

    function normalizeChartStreamBarForScope(bar, expectedScope, streamOrder = null) {
      if (barIsGapPlaceholder(bar)) return null;
      const expectedInstrumentId = exactIdentityText(expectedScope?.instrumentId);
      const expectedRouteFingerprint = exactIdentityText(expectedScope?.routeFingerprint);
      const expectedTimeframe = String(expectedScope?.timeframe || "").trim();
      if (!expectedInstrumentId || !expectedRouteFingerprint || !expectedTimeframe) return null;
      const instrumentId = exactIdentityText(bar?.instrument_id);
      const routeFingerprint = exactIdentityText(bar?.route_fingerprint);
      const providerSymbol = exactIdentityText(bar?.provider_symbol);
      if (
        !instrumentId
        || instrumentId !== expectedInstrumentId
        || !routeFingerprint
        || routeFingerprint !== expectedRouteFingerprint
        || !providerSymbol
        || String(bar?.timeframe ?? "") !== expectedTimeframe
      ) return null;
      const parsedTs = Date.parse(bar?.ts || "");
      const rawOhlc = [bar?.open, bar?.high, bar?.low, bar?.close];
      if (rawOhlc.some(value => typeof value !== "number" || !Number.isFinite(value))) return null;
      const [open, high, low, close] = rawOhlc;
      if (!Number.isFinite(parsedTs) || ![open, high, low, close].every(Number.isFinite)) return null;
      const bodyHigh = Math.max(open, close);
      const bodyLow = Math.min(open, close);
      if (high < bodyHigh || low > bodyLow || high < low) return null;
      const rawVolume = bar?.volume;
      if (typeof rawVolume !== "number" || !Number.isFinite(rawVolume) || rawVolume < 0) return null;
      const volume = rawVolume;
      const source = typeof bar?.source === "string" ? bar.source.trim() : "";
      const closed = bar?.closed;
      const lifecycleState = typeof bar?.state === "string" ? bar.state : "";
      if (!source || typeof closed !== "boolean") return null;
      if (!["forming", "awaiting_provider_confirmation", "confirmed"].includes(lifecycleState)) return null;
      if (closed !== (lifecycleState === "confirmed")) return null;
      if (typeof bar?.authoritative !== "boolean" || typeof bar?.commit_pending !== "boolean") {
        return null;
      }
      const rawBarSlot = bar?.bar_slot;
      if (
        rawBarSlot !== null
        && rawBarSlot !== undefined
        && (typeof rawBarSlot !== "number" || !Number.isFinite(rawBarSlot))
      ) return null;
      const sequenceValues = {
        lastTradeSequence: bar?.last_trade_sequence ?? 0,
        eventSequence: bar?.event_sequence ?? 0,
        streamEpoch: streamOrder?.streamEpoch ?? bar?.stream_epoch ?? 0,
        streamGeneration: streamOrder?.streamGeneration ?? bar?.stream_generation ?? 0,
        streamSeq: streamOrder?.streamSeq ?? bar?.stream_seq ?? 0,
        canonicalRevision: streamOrder?.canonicalRevision ?? bar?.canonical_revision ?? 0,
      };
      if (Object.values(sequenceValues).some(value => (
        !Number.isSafeInteger(value) || value < 0
      ))) return null;
      const ts = String(bar.ts);
      const expectedCloseMs = Date.parse(bar?.expected_close || "");
      const normalized = {
        symbol: String(bar?.symbol || ""),
        instrument_id: instrumentId,
        provider_symbol: providerSymbol,
        route_fingerprint: routeFingerprint,
        ts,
        open,
        high,
        low,
        close,
        volume,
        timeframe: expectedTimeframe,
        source,
        closed,
        state: lifecycleState,
        preview_kind: bar?.preview_kind || null,
        preview_ts: bar?.preview_ts || null,
        gateway_ts: bar?.gateway_ts || null,
        last_trade_preview_ts: bar?.last_trade_preview_ts || null,
        last_trade_gateway_ts: bar?.last_trade_gateway_ts || null,
        last_trade_sequence: sequenceValues.lastTradeSequence,
        event_sequence: sequenceValues.eventSequence,
        stream_epoch: sequenceValues.streamEpoch,
        stream_generation: sequenceValues.streamGeneration,
        stream_seq: sequenceValues.streamSeq,
        canonical_revision: sequenceValues.canonicalRevision,
        authoritative: bar.authoritative,
        commit_pending: bar.commit_pending,
        provider_state: String(bar?.provider_state || ""),
        availability_state: String(bar?.availability_state || ""),
        session_key: bar?.session_key || null,
        bar_slot: rawBarSlot,
        bar_slot_authoritative: bar?.bar_slot_authoritative === true,
        bar_slot_schedule_state: String(bar?.bar_slot_schedule_state || "unknown"),
      };
      if (Number.isFinite(expectedCloseMs) && expectedCloseMs > parsedTs) {
        normalized.expected_close = String(bar.expected_close);
      }
      return normalized;
    }

    function normalizeStreamBar(bar, streamOrder = null) {
      return normalizeChartStreamBarForScope(
        bar,
        {
          instrumentId: state.instrumentId,
          routeFingerprint: instrumentRouteFingerprint(),
          timeframe: state.timeframe,
        },
        streamOrder,
      );
    }

    function streamBarKind(bar) {
      if (barIsConfirmedClosed(bar)) return "confirmed";
      if (bar?.preview_kind === "broker_trade") return "trade";
      return "provider";
    }

    function streamBarRevisionMs(bar) {
      const previewMs = Date.parse(bar?.preview_ts || "");
      const tradeMs = Date.parse(bar?.last_trade_preview_ts || "");
      return Math.max(
        Number.isFinite(previewMs) ? previewMs : 0,
        Number.isFinite(tradeMs) ? tradeMs : 0,
      );
    }

    function streamBarOrder(bar) {
      return [
        Math.max(Number(bar?.stream_epoch || 0), 0),
        Math.max(Number(bar?.stream_generation || 0), 0),
        Math.max(Number(bar?.event_sequence || bar?.stream_seq || 0), 0),
      ];
    }

    function streamBarOrderIsOlder(incoming, existing) {
      const incomingOrder = streamBarOrder(incoming);
      const existingOrder = streamBarOrder(existing);
      for (let index = 0; index < incomingOrder.length; index += 1) {
        if (incomingOrder[index] !== existingOrder[index]) return incomingOrder[index] < existingOrder[index];
      }
      return streamBarRevisionMs(incoming) < streamBarRevisionMs(existing);
    }

    function providerBarWithNewerTrade(providerBar, tradeBar, canonicalSlot) {
      const lastTradeMs = Date.parse(providerBar?.last_trade_preview_ts || "");
      const providerRevisionMs = Math.max(
        streamBarRevisionMs(providerBar),
        Number.isFinite(lastTradeMs) ? lastTradeMs : 0,
      );
      const incomingTradeMs = streamBarRevisionMs(tradeBar);
      const priorTradeSequence = providerBar?.last_trade_sequence ?? 0;
      const incomingTradeSequence = tradeBar?.event_sequence ?? 0;
      if (
        incomingTradeMs < providerRevisionMs
        || (incomingTradeMs === providerRevisionMs && incomingTradeSequence <= priorTradeSequence)
      ) return providerBar;
      return {
        ...providerBar,
        high: Math.max(providerBar.high, tradeBar.high, tradeBar.close),
        low: Math.min(providerBar.low, tradeBar.low, tradeBar.close),
        close: tradeBar.close,
        volume: Math.max(providerBar.volume, tradeBar.volume),
        last_trade_preview_ts: tradeBar.preview_ts,
        last_trade_gateway_ts: tradeBar.gateway_ts || tradeBar.preview_ts,
        last_trade_sequence: tradeBar.event_sequence,
        bar_slot: Number.isFinite(canonicalSlot) ? canonicalSlot : undefined,
      };
    }

    function resolveStreamBar(existing, incoming) {
      if (!existing) return { ...incoming };
      if (!incoming) return existing;
      const existingSlot = existing.bar_slot;
      const incomingSlot = incoming.bar_slot;
      const canonicalSlot = Number.isFinite(incomingSlot) ? Number(incomingSlot) : existingSlot;
      const existingKind = streamBarKind(existing);
      const incomingKind = streamBarKind(incoming);
      if (existingKind === "confirmed" && incomingKind === "confirmed") {
        const existingEpoch = Math.max(Number(existing.stream_epoch || 0), 0);
        const incomingEpoch = Math.max(Number(incoming.stream_epoch || 0), 0);
        const existingGeneration = Math.max(Number(existing.stream_generation || 0), 0);
        const incomingGeneration = Math.max(Number(incoming.stream_generation || 0), 0);
        if (incomingEpoch !== existingEpoch) {
          if (incomingEpoch < existingEpoch) return existing;
        } else if (incomingGeneration !== existingGeneration) {
          if (incomingGeneration < existingGeneration) return existing;
        } else if (streamBarOrderIsOlder(incoming, existing)) {
          return existing;
        }
        const existingCanonicalRevision = Math.max(Number(existing.canonical_revision || 0), 0);
        const incomingCanonicalRevision = Math.max(Number(incoming.canonical_revision || 0), 0);
        if (
          incomingEpoch === existingEpoch
          && incomingGeneration === existingGeneration
          && incomingCanonicalRevision < existingCanonicalRevision
        ) return existing;
        if (incomingCanonicalRevision === existingCanonicalRevision) {
          if (streamBarOrderIsOlder(incoming, existing)) return existing;
        }
        return {
          ...incoming,
          bar_slot: Number.isFinite(canonicalSlot) ? Number(canonicalSlot) : undefined,
        };
      }
      if (existingKind === "confirmed") return existing;
      if (incomingKind === "confirmed") {
        return {
          ...incoming,
          bar_slot: Number.isFinite(canonicalSlot) ? Number(canonicalSlot) : undefined,
        };
      }
      if (existingKind === "provider" && incomingKind === "trade") {
        return providerBarWithNewerTrade(existing, incoming, canonicalSlot);
      }
      if (existingKind === "trade" && incomingKind === "provider") {
        const provider = {
          ...incoming,
          bar_slot: Number.isFinite(canonicalSlot) ? Number(canonicalSlot) : undefined,
        };
        return providerBarWithNewerTrade(provider, existing, canonicalSlot);
      }
      if (existingKind === "provider" && incomingKind === "provider" && existing.last_trade_preview_ts) {
        const existingTradeMs = Date.parse(existing.last_trade_preview_ts);
        const incomingProviderMs = Date.parse(incoming.preview_ts || "");
        if (Number.isFinite(existingTradeMs) && (!Number.isFinite(incomingProviderMs) || incomingProviderMs < existingTradeMs)) {
          return providerBarWithNewerTrade(
            incoming,
            {
              ...existing,
              preview_ts: existing.last_trade_preview_ts,
              gateway_ts: existing.last_trade_gateway_ts || existing.last_trade_preview_ts,
              event_sequence: existing.last_trade_sequence,
            },
            canonicalSlot,
          );
        }
      }
      if (streamBarOrderIsOlder(incoming, existing)) return existing;
      return {
        ...incoming,
        bar_slot: Number.isFinite(canonicalSlot) ? Number(canonicalSlot) : undefined,
      };
    }

    function intervalMinutesFromState() {
      if (state.timeframe.endsWith("m")) return Math.max(Number(state.timeframe.slice(0, -1)) || 1, 1);
      if (state.timeframe.endsWith("h")) return Math.max((Number(state.timeframe.slice(0, -1)) || 1) * 60, 1);
      return 5;
    }

    function intervalMsFromState() {
      return intervalMinutesFromState() * 60000;
    }

    function barUsableForConfirmedAnalysis(bar) {
      return Boolean(
        bar
        && bar.closed === true
        && bar.authoritative !== false
        && !barIsLivePreview(bar)
      );
    }

    function candleCountdownText(now = Date.now()) {
      const expectedOpenMs = Date.parse(state.transport.chart.expectedLiveSlot || "");
      const expectedCloseMs = Date.parse(state.transport.chart.expectedLiveClose || "");
      if (
        !Number.isFinite(expectedOpenMs)
        || !Number.isFinite(expectedCloseMs)
        || expectedCloseMs <= expectedOpenMs
      ) return "";
      if (now < expectedOpenMs || now >= expectedCloseMs) return "";
      const remainingSeconds = Math.max(0, Math.ceil((expectedCloseMs - now) / 1000));
      const minutes = Math.floor(remainingSeconds / 60);
      const seconds = remainingSeconds % 60;
      if (minutes >= 60) {
        const hours = Math.floor(minutes / 60);
        const restMinutes = minutes % 60;
        return `${hours}:${String(restMinutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
      }
      return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
    }

    function latestBarLagsCurrentSlot() {
      if (!providerCapability(state.dataSource, "chart_stream") || !state.snapshot?.bars?.length) return false;
      const quality = effectiveDataQuality(state.snapshot?.meta || {});
      if (quality.market_closed || quality.session_unknown) return false;
      const expectedMs = Date.parse(state.transport.chart.expectedLiveSlot || "");
      if (!Number.isFinite(expectedMs)) return false;
      let latestProvider = null;
      for (let index = state.snapshot.bars.length - 1; index >= 0; index -= 1) {
        const candidate = state.snapshot.bars[index];
        if (!barIsConfirmedClosed(candidate) && streamBarKind(candidate) !== "provider") continue;
        latestProvider = candidate;
        break;
      }
      const latestMs = Date.parse(latestProvider?.ts || "");
      return !Number.isFinite(latestMs) || expectedMs > latestMs;
    }

    function currentInstrumentSessionClosed() {
      const quality = effectiveDataQuality(state.snapshot?.meta || {});
      return Boolean(quality.market_closed);
    }

    function chartStreamFreshForRender(now = Date.now()) {
      return chartStreamOpen()
        && now - Number(state.transport.chart.lastBarsAt || state.transport.chart.openedAt || 0) < CHART_STREAM_STALE_MS;
    }

    function passiveChartRenderDue(now = Date.now()) {
      if (!state.snapshot?.bars?.length) return false;
      const guard = chartRenderGuardState(state.snapshot, {});
      if (guard.skip) return false;
      if (chartStreamFreshForRender(now)) return true;
      const interval = currentInstrumentSessionClosed() ? CLOSED_SESSION_CHART_RENDER_MS : IDLE_CHART_RENDER_MS;
      return now - Number(state.lastPassiveChartRenderAt || 0) >= interval;
    }

    function ensureCurrentLiveCandle() {
      if (!latestBarLagsCurrentSlot()) return false;
      const now = Date.now();
      const lastTransportAt = Number(state.transport.chart.lastMessageAt || state.transport.chart.openedAt || 0);
      const transportFresh = chartStreamOpen() && now - lastTransportAt < CHART_STREAM_STALE_MS;
      if (transportFresh) {
        loadLiveQuote();
        return false;
      }
      if (
        !state.serverSleeping
        && !state.transport.chart.socket
        && !state.transport.chart.retryTimer
        && !state.requests.history.loading
      ) {
        connectChartStream(false);
      }
      return false;
    }

    function normalizeSnapshotBarSlots(snapshot = state.snapshot) {
      const bars = snapshot?.bars;
      if (!Array.isArray(bars) || !bars.length) return snapshot;
      for (let index = bars.length - 1; index >= 0; index -= 1) {
        if (barIsGapPlaceholder(bars[index])) bars.splice(index, 1);
      }
      let lastSlot = null;
      bars.forEach((bar) => {
        const rawSlot = bar.bar_slot;
        if (!Number.isFinite(rawSlot) || (lastSlot !== null && rawSlot <= lastSlot)) {
          delete bar.bar_slot;
          return;
        }
        const slot = Number(rawSlot);
        bar.bar_slot = slot;
        lastSlot = slot;
      });
      return snapshot;
    }

    function ensureSnapshotBarIndex(snapshot = state.snapshot) {
      const bars = Array.isArray(snapshot?.bars) ? snapshot.bars : [];
      if (state.chartBarIndexBars === bars && state.chartBarIndexLength === bars.length) {
        return state.chartBarIndex;
      }
      const index = new Map();
      bars.forEach((bar, position) => {
        const key = timestampKey(bar?.ts);
        if (!key) return;
        index.set(key, position);
      });
      state.chartBarIndex = index;
      state.chartBarIndexBars = bars;
      state.chartBarIndexLength = bars.length;
      return index;
    }

    function liveBarPreviewKey(timeframe = state.timeframe) {
      return JSON.stringify([state.instrumentId, instrumentRouteFingerprint(), String(timeframe ?? "")]);
    }

    function rememberLiveBarPreview(bar = state.snapshot?.bars?.[state.snapshot.bars.length - 1]) {
      if (!bar) return;
      const key = liveBarPreviewKey(bar.timeframe || state.timeframe);
      const cached = Array.isArray(state.liveBarPreviewCache[key]) ? state.liveBarPreviewCache[key] : [];
      const remaining = cached.filter(item => timestampKey(item?.ts) !== timestampKey(bar.ts));
      if (barIsLivePreview(bar)) remaining.push({ ...bar });
      remaining.sort((left, right) => Date.parse(left?.ts || "") - Date.parse(right?.ts || ""));
      if (remaining.length) state.liveBarPreviewCache[key] = remaining.slice(-3);
      else delete state.liveBarPreviewCache[key];
    }

    function latestConfirmedIndicatorBar(snapshot = state.snapshot) {
      const bars = Array.isArray(snapshot?.bars) ? snapshot.bars : [];
      for (let index = bars.length - 1; index >= 0; index -= 1) {
        const bar = bars[index];
        if (!barUsableForConfirmedAnalysis(bar, snapshot)) continue;
        return bar;
      }
      return null;
    }

    function reloadCurrentAnalysisOnly(reason = "settings", options = {}) {
      if (state.snapshot && options.renderImmediate !== false) renderCharts(state.snapshot, { overlayImmediate: true });
      const latest = latestConfirmedIndicatorBar(state.snapshot);
      const latestKey = timestampKey(latest?.ts || state.snapshot?.meta?.analysis_ts || "");
      const versionKey = String(options.versionKey || [reason, latestKey, Date.now()].join("|"));
      load({
        force: true,
        splitAnalysis: true,
        queueAnalysis: true,
        analysisOnly: true,
        requireLatestAnalysis: true,
        analysisVersionKey: versionKey,
        analysisRequireBarTs: latestKey,
      });
    }

    function historyRepairPreviewState(snapshot = state.snapshot) {
      const meta = snapshot?.meta || {};
      const direct = meta.gap_repair && typeof meta.gap_repair === "object"
        ? meta.gap_repair
        : null;
      const coverageRepair = meta.history_coverage?.bar_repair
        && typeof meta.history_coverage.bar_repair === "object"
        ? meta.history_coverage.bar_repair
        : null;
      const repair = direct || coverageRepair || {};
      const status = String(repair.status || "").trim().toLowerCase();
      const code = String(repair.code || repair.error_code || "").trim();
      const pending = [
        "scheduled",
        "queued",
        "running",
      ].includes(status);
      const terminalSuccess = ["committed", "no_change", "not_needed"].includes(status);
      const idle = ["", "unknown", "not_requested", "not_supported"].includes(status);
      const failed = !pending && !terminalSuccess && !idle && Boolean(status || code);
      return {
        visible: pending || failed,
        pending,
        failed,
        status,
        code,
        requestedFrom: String(repair.requested_from || ""),
        requestedTo: String(repair.requested_to || ""),
      };
    }

    function syncSnapshotPreviewMeta(snapshot = state.snapshot) {
      if (!snapshot?.meta) return;
      const bars = Array.isArray(snapshot.bars) ? snapshot.bars : [];
      const latest = bars[bars.length - 1] || null;
      const candidates = Array.isArray(snapshot.candidates) ? snapshot.candidates : [];
      const existing = snapshot.meta.preview && typeof snapshot.meta.preview === "object" ? snapshot.meta.preview : {};
      const provisionalBar = barIsLivePreview(latest);
      const repair = historyRepairPreviewState(snapshot);
      const previewCandidates = candidates.filter(candidate => {
        const status = candidate?.status && typeof candidate.status === "object" ? candidate.status : {};
        const details = candidate?.details && typeof candidate.details === "object" ? candidate.details : {};
        return status.stage === "preview" || status.execution_candidate === false || details.execution_candidate === false;
      }).length;
      const executionCandidates = candidates.filter(candidate => {
        const status = candidate?.status && typeof candidate.status === "object" ? candidate.status : {};
        const details = candidate?.details && typeof candidate.details === "object" ? candidate.details : {};
        return status.stage === "execution" || (status.execution_candidate !== false && details.execution_candidate !== false);
      }).length;
      let latestConfirmed = null;
      for (let index = bars.length - 1; index >= 0; index -= 1) {
        if (!barUsableForConfirmedAnalysis(bars[index])) continue;
        latestConfirmed = bars[index];
        break;
      }
      snapshot.meta.preview = {
        ...existing,
        active: Boolean(repair.visible || provisionalBar || previewCandidates),
        bar_closed: latest ? !provisionalBar : existing.bar_closed !== false,
        provisional_bar: provisionalBar,
        gap_active: repair.visible,
        gap_repair_pending: repair.pending,
        gap_repair_failed: repair.failed,
        gap_repair_status: repair.status,
        gap_repair_code: repair.code,
        missing_slot_ts: repair.requestedFrom,
        missing_range_to: repair.requestedTo,
        latest_kind: repair.failed
          ? "history_repair_failed"
          : repair.pending
            ? "history_repair"
            : provisionalBar
              ? streamBarKind(latest)
              : "confirmed",
        candidate_count: candidates.length ? previewCandidates : Number(existing.candidate_count || 0),
        execution_candidate_count: candidates.length ? executionCandidates : Number(existing.execution_candidate_count || 0),
        latest_ts: latest?.ts || existing.latest_ts || "",
        confirmed_latest_ts: latestConfirmed?.ts || snapshot.meta.analysis_ts || existing.confirmed_latest_ts || "",
        source: snapshot.meta.source || latest?.source || existing.source || "",
        policy: existing.policy || "preview_only_until_exchange_confirmed",
        execution_uses_confirmed: true,
      };
    }

    function analysisRefreshBusy() {
      return document.visibilityState !== "visible"
        || !snapshotMatchesCurrentRoute()
        || state.serverSleeping
        || state.requests.market.loading
        || state.requests.analysis.loading;
    }

    const indicatorAnalysisRefreshSchedulerRegistry = new Map();

    function registerIndicatorAnalysisRefreshScheduler(indicatorId, scheduler) {
      const key = String(indicatorId || "").trim();
      if (
        !indicatorSpec(key)?.id
        || !scheduler
        || typeof scheduler !== "object"
        || typeof scheduler.schedule !== "function"
        || typeof scheduler.reset !== "function"
      ) {
        throw new Error(`Invalid indicator analysis refresh scheduler: ${key || "missing"}`);
      }
      if (indicatorAnalysisRefreshSchedulerRegistry.has(key)) {
        throw new Error(`Duplicate indicator analysis refresh scheduler: ${key}`);
      }
      indicatorAnalysisRefreshSchedulerRegistry.set(key, scheduler);
    }

    function maybeScheduleIndicatorAnalysisRefreshExtension(reason = "chart-stream") {
      const schedulers = Array.from(indicatorAnalysisRefreshSchedulerRegistry.entries())
        .sort((left, right) => {
          const leftOrder = numericValueOrFallback(indicatorUiSpec(left[0])?.runtime_order, 500);
          const rightOrder = numericValueOrFallback(indicatorUiSpec(right[0])?.runtime_order, 500);
          return leftOrder - rightOrder || left[0].localeCompare(right[0]);
        });
      for (const [indicatorId, scheduler] of schedulers) {
        if (!indicatorCalcForId(indicatorId)) continue;
        try {
          if (scheduler.schedule(reason) === true) return true;
        } catch (error) {
          console.error(`Indicator analysis refresh scheduler failed: ${indicatorId}`, error);
        }
      }
      return false;
    }

    function resetIndicatorAnalysisRefreshSchedulers() {
      indicatorAnalysisRefreshSchedulerRegistry.forEach((scheduler, indicatorId) => {
        try {
          scheduler.reset();
        } catch (error) {
          console.error(`Indicator analysis refresh reset failed: ${indicatorId}`, error);
        }
      });
    }

    function scheduleAnalysisRefreshOnce({
      latestKey,
      activeBarStateKey,
      timerStateKey,
      lastRefreshStateKey,
      delayMs,
      reason,
      debugLabel,
      loadOptions,
      ownerKey = "",
      canRun = () => true,
      retry = null,
      onRun = null,
      tracker = state,
    }) {
      if (!latestKey) return false;
      if (tracker[activeBarStateKey] === latestKey && tracker[timerStateKey]) return false;
      if (tracker[timerStateKey]) window.clearTimeout(tracker[timerStateKey]);
      const scheduledScopeKey = JSON.stringify([
        exactIdentityText(state.instrumentId),
        instrumentRouteFingerprint(),
        String(state.timeframe || ""),
        String(state.range || ""),
      ]);
      tracker[activeBarStateKey] = latestKey;
      tracker[timerStateKey] = window.setTimeout(() => {
        tracker[timerStateKey] = null;
        const currentScopeKey = JSON.stringify([
          exactIdentityText(state.instrumentId),
          instrumentRouteFingerprint(),
          String(state.timeframe || ""),
          String(state.range || ""),
        ]);
        if (currentScopeKey !== scheduledScopeKey) {
          tracker[activeBarStateKey] = "";
          return;
        }
        if (analysisRefreshBusy() || !canRun()) {
          tracker[activeBarStateKey] = "";
          const retryable = document.visibilityState === "visible"
            && !state.serverSleeping
            && snapshotMatchesCurrentRoute();
          if (retryable && typeof retry === "function") retry();
          return;
        }
        if (ownerKey && !acquireSharedStreamOwner("analysis", ownerKey)) return;
        tracker[lastRefreshStateKey] = Date.now();
        if (typeof onRun === "function") onRun();
        debugStep(debugLabel, `${reason} ${latestKey}`);
        Promise.resolve(load(loadOptions)).finally(() => {
          if (ownerKey) releaseSharedStreamOwner("analysis", ownerKey);
        });
      }, delayMs);
      return true;
    }

    function maybeScheduleIndicatorAnalysisRefresh(reason = "chart-stream") {
      if (document.visibilityState !== "visible") return false;
      if (state.serverSleeping || !providerCapability(state.dataSource, "chart_stream") || !state.snapshot?.bars?.length || !snapshotMatchesCurrentRoute()) return false;
      if (typeof marketAnalysisCanPoll === "function" && !marketAnalysisCanPoll(state.snapshot)) return false;
      const canonicalRevision = Math.max(Number(state.snapshot?.meta?.chart_canonical_revision || 0), 0);
      if (effectiveDataQuality(state.snapshot?.meta || {}).market_closed && canonicalRevision <= 0) return false;
      const analysisKey = String(state.snapshot?.meta?.analysis_key || "").trim();
      if (!analysisKey) return false;
      const latest = latestConfirmedIndicatorBar(state.snapshot);
      const latestKey = timestampKey(latest?.ts);
      const analysisVersion = canonicalRevision > 0 ? `${latestKey}|r${canonicalRevision}` : latestKey;
      if (!latestKey || analysisVersion === state.lastIndicatorAnalysisMergedVersion) return false;
      const ownerKey = JSON.stringify([
        state.instrumentId,
        instrumentRouteFingerprint(),
        state.timeframe,
        state.range,
        analysisVersion,
      ]);
      const elapsed = Date.now() - Number(state.lastIndicatorAnalysisRefreshAt || 0);
      const delayMs = Math.max(650, 2200 - elapsed);
      return scheduleAnalysisRefreshOnce({
        latestKey: analysisVersion,
        activeBarStateKey: "indicatorAnalysisRefreshBar",
        timerStateKey: "indicatorAnalysisRefreshTimer",
        lastRefreshStateKey: "lastIndicatorAnalysisRefreshAt",
        delayMs,
        reason,
        debugLabel: "indicator analysis refresh",
        ownerKey,
        retry: () => maybeScheduleIndicatorAnalysisRefresh("busy"),
        loadOptions: {
          force: true,
          splitAnalysis: true,
          queueAnalysis: true,
          analysisOnly: true,
          analysisVersionKey: analysisVersion,
          analysisRequireBarTs: latestKey,
          analysisCanonicalRevision: canonicalRevision,
        },
      });
    }

    function maybeScheduleLiveQuoteAnalysisRefresh(reason = "live-quote") {
      if (document.visibilityState !== "visible") return false;
      if (
        state.serverSleeping
        || (
          !providerCapability(state.dataSource, "live_quote_stream")
          && !providerCapability(state.dataSource, "live_quote_polling")
        )
        || !state.snapshot?.bars?.length
        || !snapshotMatchesCurrentRoute()
      ) return false;
      if (effectiveDataQuality(state.snapshot?.meta || {}).market_closed) return false;
      const analysisKey = String(state.snapshot?.meta?.analysis_key || "").trim();
      if (!analysisKey) return false;
      const bars = state.snapshot.bars || [];
      const latest = bars[bars.length - 1];
      const confirmedLatest = latestConfirmedIndicatorBar(state.snapshot);
      const confirmedLatestKey = timestampKey(confirmedLatest?.ts);
      const quoteKey = timestampKey(state.snapshot.meta?.quote_ts) || String(state.snapshot.meta?.price ?? latest?.close ?? "");
      if (!confirmedLatestKey || !quoteKey) return false;
      const refreshKey = `${confirmedLatestKey}|${quoteKey}`;
      const ownerKey = JSON.stringify([
        state.instrumentId,
        instrumentRouteFingerprint(),
        state.timeframe,
        state.range,
        "live",
        confirmedLatestKey,
      ]);
      const sameConfirmedBar = state.lastLiveQuoteAnalysisConfirmedBar === confirmedLatestKey;
      if (sameConfirmedBar) return false;
      return scheduleAnalysisRefreshOnce({
        latestKey: refreshKey,
        activeBarStateKey: "liveQuoteAnalysisRefreshKey",
        timerStateKey: "liveQuoteAnalysisRefreshTimer",
        lastRefreshStateKey: "lastLiveQuoteAnalysisRefreshAt",
        delayMs: 900,
        reason,
        debugLabel: "live quote analysis refresh",
        ownerKey,
        canRun: () => state.lastLiveQuoteAnalysisConfirmedBar !== confirmedLatestKey,
        retry: () => maybeScheduleLiveQuoteAnalysisRefresh("busy"),
        onRun: () => { state.lastLiveQuoteAnalysisConfirmedBar = confirmedLatestKey; },
        loadOptions: {
          force: true,
          splitAnalysis: true,
          queueAnalysis: true,
          analysisOnly: true,
          reuseAnalysisDemand: true,
          analysisVersionKey: refreshKey,
          analysisRequireBarTs: confirmedLatestKey,
        },
      });
    }

    function restoreLiveBarPreview(snapshot) {
      if (!snapshot?.bars?.length) return snapshot;
      const key = liveBarPreviewKey(snapshot.meta?.timeframe || state.timeframe);
      const cachedBars = Array.isArray(state.liveBarPreviewCache?.[key])
        ? state.liveBarPreviewCache[key]
        : [];
      if (!cachedBars.length) return snapshot;
      const expectedLiveSlotMs = Date.parse(state.transport.chart.expectedLiveSlot || "");
      const expectedLiveSlotKey = Number.isFinite(expectedLiveSlotMs)
        ? timestampKey(state.transport.chart.expectedLiveSlot)
        : "";
      const expectedLiveCloseMs = Date.parse(state.transport.chart.expectedLiveClose || "");
      const expectedLiveSlotClosed = Number.isFinite(expectedLiveSlotMs)
        && Number.isFinite(expectedLiveCloseMs)
        && expectedLiveCloseMs > expectedLiveSlotMs
        && expectedLiveCloseMs <= Date.now();
      const now = Date.now();
      const bars = snapshot.bars;
      const chartGuideBars = bars.slice();
      const retained = [];
      for (const cached of cachedBars) {
        if (barIsGapPlaceholder(cached)) continue;
        const isAwaiting = cached?.state === "awaiting_provider_confirmation";
        const cachedKey = timestampKey(cached?.ts);
        const previewRevisionMs = streamBarRevisionMs(cached);
        const previewFresh = Number.isFinite(previewRevisionMs)
          && previewRevisionMs > 0
          && now >= previewRevisionMs
          && now - previewRevisionMs <= CHART_STREAM_STALE_MS;
        const matchesExpectedSlot = Boolean(expectedLiveSlotKey && cachedKey === expectedLiveSlotKey);
        const cachedSlotMs = Date.parse(cached?.ts || "");
        const advancesStaleExpectedSlot = Boolean(
          expectedLiveSlotKey
          && expectedLiveSlotClosed
          && previewFresh
          && Number.isFinite(cachedSlotMs)
          && cachedSlotMs > expectedLiveSlotMs
        );
        if (!isAwaiting && !(matchesExpectedSlot || advancesStaleExpectedSlot || (!expectedLiveSlotKey && previewFresh))) {
          continue;
        }
        const existingIndex = bars.findIndex(bar => timestampKey(bar.ts) === timestampKey(cached.ts));
        if (existingIndex >= 0) {
          const existing = bars[existingIndex];
          const resolved = resolveStreamBar(existing, cached);
          bars[existingIndex] = resolved;
          if (barIsLivePreview(resolved)) retained.push(resolved);
        } else {
          bars.push({ ...cached });
          bars.sort((left, right) => Date.parse(left?.ts || "") - Date.parse(right?.ts || ""));
          retained.push(cached);
        }
      }
      if (retained.length) state.liveBarPreviewCache[key] = retained;
      else {
        delete state.liveBarPreviewCache[key];
        return snapshot;
      }
      normalizeSnapshotBarSlots(snapshot);
      alignChartGuidesToBars(snapshot, chartGuideBars, bars);
      state.chartBarIndexBars = null;
      snapshot.meta.live_bar_closed = false;
      const latestCached = retained[retained.length - 1];
      if (latestCached?.preview_kind === "broker_trade") {
        snapshot.meta.source = `${state.dataSource || "provider"}:db-cache+trade-preview`;
      } else if (chartStreamOpen()) {
        snapshot.meta.source = `${state.dataSource || "provider"}:db-cache+chart-stream`;
      }
      syncSnapshotPreviewMeta(snapshot);
      return snapshot;
    }

    function applyChartBarsToSnapshot(streamBars, options = {}) {
      if (!providerCapability(state.dataSource, "chart_stream")) return false;
      let qualityOnlyChanged = false;
      const incomingCanonicalRevision = Math.max(Number(options.canonicalRevision || 0), 0);
      const acceptedCanonicalRevision = state.snapshot?.meta && snapshotMatchesCurrentRoute()
        ? Math.max(Number(state.snapshot?.meta?.chart_canonical_revision || 0), 0)
        : 0;
      const canonicalRevisionAdvanced = incomingCanonicalRevision > acceptedCanonicalRevision;
      const incomingChartQuality = options.chartDataQuality && typeof options.chartDataQuality === "object"
        ? options.chartDataQuality
        : null;
      const incomingHistoryCoverage = options.historyCoverage && typeof options.historyCoverage === "object"
        ? options.historyCoverage
        : null;
      const incomingGapRepair = options.gapRepair && typeof options.gapRepair === "object"
        ? options.gapRepair
        : null;
      const incomingFutureAxis = options.futureAxis !== undefined
        && isValidFutureAxisPayload(options.futureAxis, state.timeframe)
        ? options.futureAxis
        : null;
      const incomingFutureAxisCanonicalRevision = Math.max(
        Number(options.futureAxisCanonicalRevision ?? incomingCanonicalRevision ?? 0),
        0,
      );
      const incomingChartQualityRevision = Math.max(
        Number(options.chartQualityRevision || incomingCanonicalRevision || 0),
        0,
      );
      const hasExpectedLiveSlot = options.expectedLiveSlot !== undefined;
      const hasExpectedLiveClose = options.expectedLiveClose !== undefined;
      if (hasExpectedLiveSlot || hasExpectedLiveClose) {
        const expectedLiveSlot = String(options.expectedLiveSlot || "");
        const expectedLiveClose = String(options.expectedLiveClose || "");
        const expectedLiveSlotMs = Date.parse(expectedLiveSlot);
        const expectedLiveCloseMs = Date.parse(expectedLiveClose);
        const currentExpectedLiveSlotMs = Date.parse(state.transport.chart.expectedLiveSlot || "");
        const clearExpectedBounds = !expectedLiveSlot && !expectedLiveClose;
        const validExpectedBounds = (
          hasExpectedLiveSlot
          && hasExpectedLiveClose
          && Number.isFinite(expectedLiveSlotMs)
          && Number.isFinite(expectedLiveCloseMs)
          && expectedLiveCloseMs > expectedLiveSlotMs
        );
        if (clearExpectedBounds || !validExpectedBounds) {
          state.transport.chart.expectedLiveSlot = "";
          state.transport.chart.expectedLiveClose = "";
        } else if (
          !Number.isFinite(currentExpectedLiveSlotMs)
          || expectedLiveSlotMs >= currentExpectedLiveSlotMs
        ) {
          state.transport.chart.expectedLiveSlot = expectedLiveSlot;
          state.transport.chart.expectedLiveClose = expectedLiveClose;
        }
      }
      if (state.snapshot?.meta && snapshotMatchesCurrentRoute()) {
        if (incomingFutureAxis) {
          const acceptedFutureAxisCanonicalRevision = futureAxisCanonicalRevision(state.snapshot);
          if (futureAxisPayloadShouldReplace(
            state.snapshot.future_axis,
            incomingFutureAxis,
            {
              timeframe: state.timeframe,
              currentCanonicalRevision: acceptedFutureAxisCanonicalRevision,
              incomingCanonicalRevision: incomingFutureAxisCanonicalRevision,
            },
          )) {
            state.snapshot.future_axis = {
              ...incomingFutureAxis,
              slots: incomingFutureAxis.slots.map(item => ({ ...item })),
            };
            state.snapshot.meta.future_axis_canonical_revision = incomingFutureAxisCanonicalRevision;
            qualityOnlyChanged = true;
          }
        }
        if (incomingCanonicalRevision > Number(state.snapshot.meta.chart_canonical_revision || 0)) {
          state.snapshot.meta.chart_canonical_revision = incomingCanonicalRevision;
        }
        const acceptedChartQualityRevision = Math.max(
          Number(state.snapshot.meta.chart_quality_revision || 0),
          0,
        );
        const acceptedCanonicalRevision = Math.max(
          Number(state.snapshot.meta.chart_canonical_revision || 0),
          0,
        );
        if (
          incomingChartQuality
          && incomingChartQualityRevision >= acceptedChartQualityRevision
          && incomingChartQualityRevision >= acceptedCanonicalRevision
        ) {
          state.snapshot.meta.chart_data_quality = { ...incomingChartQuality };
          state.snapshot.meta.chart_quality_revision = incomingChartQualityRevision;
          qualityOnlyChanged = true;
        }
        const requestedHistoryRange = String(incomingHistoryCoverage?.requested_range || "");
        const activeHistoryRange = String(state.range || state.snapshot.meta.chart_range || "");
        if (incomingHistoryCoverage && requestedHistoryRange && requestedHistoryRange === activeHistoryRange) {
          state.snapshot.meta.history_coverage = { ...incomingHistoryCoverage };
          qualityOnlyChanged = true;
        }
        if (incomingGapRepair) {
          state.snapshot.meta.gap_repair = { ...incomingGapRepair };
          qualityOnlyChanged = true;
        }
      }
      let incoming = streamBars;
      if (options.normalizedBars !== true) {
        const incomingByTs = new Map();
        streamBars.forEach(rawBar => {
          const bar = normalizeStreamBar(rawBar);
          if (!bar) return;
          const key = timestampKey(bar.ts);
          incomingByTs.set(key, resolveStreamBar(incomingByTs.get(key), bar));
        });
        incoming = Array.from(incomingByTs.values())
          .sort((a, b) => Date.parse(a.ts) - Date.parse(b.ts));
      }
      if (!incoming.length) {
        if (qualityOnlyChanged && document.visibilityState === "visible") {
          renderLiveMarketVisuals(state.snapshot, { forcePanel: true });
        }
        return qualityOnlyChanged;
      }
      if (typeof retainCanonicalLiveTail === "function") {
        retainCanonicalLiveTail(incoming, {
          instrument_id: state.instrumentId,
          route_fingerprint: instrumentRouteFingerprint(),
          timeframe: state.timeframe,
          chart_data_quality: incomingChartQuality,
          chart_quality_revision: incomingChartQualityRevision,
          chart_canonical_revision: incomingCanonicalRevision,
        }, { verified: true });
      }
      const receivedNowMs = Date.now();
      let earliestAppliedPreviewTs = "";
      let earliestAppliedPreviewMs = Number.POSITIVE_INFINITY;
      if (!state.snapshot?.bars?.length) {
        const initialIncoming = incoming;
        if (!initialIncoming.length) return qualityOnlyChanged;
        for (const bar of initialIncoming) {
          if (!(barIsLivePreview(bar) || bar?.preview_kind === "provider_bar")) continue;
          const candidateTs = bar?.last_trade_gateway_ts
            || bar?.gateway_ts
            || bar?.last_trade_preview_ts
            || bar?.preview_ts;
          const candidateMs = Date.parse(candidateTs || "");
          if (
            Number.isFinite(candidateMs)
            && candidateMs <= receivedNowMs
            && candidateMs < earliestAppliedPreviewMs
          ) {
            earliestAppliedPreviewTs = candidateTs;
            earliestAppliedPreviewMs = candidateMs;
          }
        }
        const latest = initialIncoming[initialIncoming.length - 1];
        const previous = initialIncoming.length > 1
          ? initialIncoming[initialIncoming.length - 2]
          : latest;
        const price = Number(latest.close);
        const rawReferencePrice = Number(previous.close ?? latest.open ?? price);
        const referencePrice = Number.isFinite(rawReferencePrice) ? rawReferencePrice : price;
        const change = price - referencePrice;
        const changePct = referencePrice !== 0 ? (change / Math.abs(referencePrice)) * 100 : 0;
        state.snapshot = normalizeSnapshotBarSlots({
          bars: initialIncoming,
          future_axis: incomingFutureAxis || undefined,
          meta: {
            symbol: state.symbol,
            provider_symbol: latest.provider_symbol,
            instrument_id: state.instrumentId,
            route_fingerprint: instrumentRouteFingerprint(),
            timeframe: state.timeframe,
            source: latest.preview_kind === "broker_trade"
              ? `${state.dataSource || "provider"}:trade-preview`
              : `${state.dataSource || "provider"}:chart-stream`,
            chart_only: true,
            price,
            change,
            change_pct: changePct,
            live_bar_closed: !barIsLivePreview(latest),
            data_quality: {},
            chart_data_quality: incomingChartQuality ? { ...incomingChartQuality } : {},
            gap_repair: incomingGapRepair ? { ...incomingGapRepair } : {},
            chart_quality_revision: incomingChartQuality ? incomingChartQualityRevision : 0,
            chart_canonical_revision: incomingCanonicalRevision,
          },
        });
        state.chartBarIndexBars = null;
        ensureSnapshotBarIndex(state.snapshot);
        if (resolveDrawingAnchorsAgainstSnapshot(state.drawing.objects, state.snapshot)) {
          touchChartUserObjectsVersion();
        }
        touchChartBarsVersion();
        initialIncoming.forEach(rememberLiveBarPreview);
        clearUiNotice("chart_stream");
        clearUiNotice("market", "MARKET_TAIL_UNAVAILABLE");
        state.lastPrice = price;
        if (earliestAppliedPreviewTs) {
          const pendingMs = Date.parse(state.pendingLiveCandlePaintAt || "");
          state.pendingLiveCandlePaintAt = Number.isFinite(pendingMs)
            && pendingMs <= receivedNowMs
            && pendingMs < earliestAppliedPreviewMs
            ? state.pendingLiveCandlePaintAt
            : earliestAppliedPreviewTs;
        }
        if (view.followLatest) view.offset = 0;
        clampView(state.snapshot);
        if (document.visibilityState === "visible") {
          renderLiveMarketVisuals(state.snapshot, { forcePanel: true });
        }
        return true;
      }
      if (!snapshotMatchesCurrentRoute()) return false;
      const bars = state.snapshot.bars;
      const byTs = ensureSnapshotBarIndex(state.snapshot);
      const browsingHistory = !view.followLatest;
      const displayFirstMs = Date.parse(bars[0]?.ts || "");
      const displayLastMs = Date.parse(bars[bars.length - 1]?.ts || "");
      let chartGuideBars = null;
      let viewportAnchorTs = "";
      let viewportCount = 0;
      let structuralChange = false;
      let confirmedDrawingSeriesChanged = false;
      let changed = false;
      let slotsNeedNormalization = false;
      const resolvedKeys = new Set();
      for (const bar of incoming) {
        if (barIsGapPlaceholder(bar)) continue;
        const barKey = timestampKey(bar.ts);
        const candidatePreviewTs = bar?.last_trade_gateway_ts
          || bar?.gateway_ts
          || bar?.last_trade_preview_ts
          || bar?.preview_ts;
        const candidatePreviewMs = Date.parse(candidatePreviewTs || "");
        const measurableLivePreview = (
          (barIsLivePreview(bar) || bar?.preview_kind === "provider_bar")
          && Number.isFinite(candidatePreviewMs)
          && candidatePreviewMs <= receivedNowMs
        );
        const existingIndex = byTs.get(barKey);
        if (existingIndex !== undefined) {
          const existing = bars[existingIndex];
          const resolved = resolveStreamBar(existing, bar);
          if (resolved !== existing) {
            const existingSlot = existing.bar_slot;
            const resolvedSlot = resolved.bar_slot;
            if (
              (barIsConfirmedClosed(existing) && existing?.authoritative !== false)
              !== (barIsConfirmedClosed(resolved) && resolved?.authoritative !== false)
            ) confirmedDrawingSeriesChanged = true;
            bars[existingIndex] = resolved;
            if (resolvedSlot !== existingSlot) {
              slotsNeedNormalization = true;
            }
            changed = true;
            resolvedKeys.add(barKey);
            if (measurableLivePreview && candidatePreviewMs < earliestAppliedPreviewMs) {
              earliestAppliedPreviewTs = candidatePreviewTs;
              earliestAppliedPreviewMs = candidatePreviewMs;
            }
          }
          continue;
        }
        const last = bars[bars.length - 1];
        const barMs = Date.parse(bar.ts);
        const lastMs = Date.parse(last.ts);
        if (!Number.isFinite(barMs) || !Number.isFinite(lastMs)) continue;
        if (
          browsingHistory
          && (!Number.isFinite(displayFirstMs) || barMs < displayFirstMs || barMs > displayLastMs)
        ) continue;
        const incomingSlot = bar.bar_slot;
        let insertIndex = bars.length;
        if (barMs <= lastMs) {
          insertIndex = bars.findIndex(item => Date.parse(item?.ts || "") > barMs);
          if (insertIndex < 0) continue;
        }
        if (!structuralChange) {
          chartGuideBars = bars.slice();
          if (browsingHistory) {
            const viewport = visibleBars(state.snapshot);
            viewportAnchorTs = timestampKey(viewport?.bars?.[0]?.ts);
            viewportCount = Math.max(
              Number(viewport?.end || 0) - Number(viewport?.start || 0),
              1,
            );
          }
          structuralChange = true;
        }
        const inserted = { ...bar };
        if (Number.isFinite(incomingSlot)) {
          inserted.bar_slot = Number(incomingSlot);
        } else {
          delete inserted.bar_slot;
        }
        if (insertIndex < bars.length) {
          bars.splice(insertIndex, 0, inserted);
          for (let index = insertIndex; index < bars.length; index += 1) {
            byTs.set(timestampKey(bars[index].ts), index);
          }
        } else {
          bars.push(inserted);
          byTs.set(timestampKey(bar.ts), bars.length - 1);
        }
        state.chartBarIndexLength = bars.length;
        slotsNeedNormalization = true;
        if (barIsConfirmedClosed(bar) && bar?.authoritative !== false) {
          confirmedDrawingSeriesChanged = true;
        }
        changed = true;
        resolvedKeys.add(barKey);
        if (measurableLivePreview && candidatePreviewMs < earliestAppliedPreviewMs) {
          earliestAppliedPreviewTs = candidatePreviewTs;
          earliestAppliedPreviewMs = candidatePreviewMs;
        }
      }
      if (qualityOnlyChanged) changed = true;
      if (!changed) return false;
      if (slotsNeedNormalization) normalizeSnapshotBarSlots(state.snapshot);
      if (structuralChange && chartGuideBars) {
        alignChartGuidesToBars(state.snapshot, chartGuideBars, bars);
      }
      const drawingProjectionStale = (state.drawing.objects || []).some(object => (
        object?.anchorProjection
        && Number(object.anchorProjection.canonicalGeneration)
          !== Number(state.snapshot?.meta?.chart_canonical_revision)
      ));
      if (
        (confirmedDrawingSeriesChanged || drawingProjectionStale)
        && resolveDrawingAnchorsAgainstSnapshot(
          state.drawing.objects,
          state.snapshot,
        )
      ) {
        touchChartUserObjectsVersion();
      }
      touchChartBarsVersion();
      const quoteDisplay = currentChartQuoteDisplay();
      const streamLatest = bars[bars.length - 1];
      const price = Number(quoteDisplay?.price ?? streamLatest.close);
      resolvedKeys.forEach(key => {
        const index = byTs.get(key);
        if (index !== undefined) rememberLiveBarPreview(bars[index]);
      });
      const latest = bars[bars.length - 1];
      const prev = bars.length > 1 ? bars[bars.length - 2] : latest;
      const rawReferencePrice = Number(prev.close ?? latest.open ?? price);
      const referencePrice = Number.isFinite(rawReferencePrice) ? rawReferencePrice : price;
      const change = price - referencePrice;
      const changePct = referencePrice !== 0 ? (change / Math.abs(referencePrice)) * 100 : 0;
      if (quoteDisplay) {
        state.snapshot.meta = {
          ...state.snapshot.meta,
          ...liveQuoteMetaProjection(quoteDisplay),
        };
      } else {
        state.snapshot.meta = {
          ...state.snapshot.meta,
          ...unavailableLiveQuoteMetaProjection(price, "confirmed_close"),
        };
      }
      state.snapshot.meta.change = change;
      state.snapshot.meta.change_pct = changePct;
      const brokerTradePreview = latest.preview_kind === "broker_trade";
      state.snapshot.meta.source = brokerTradePreview
        ? quoteDisplay
          ? `${state.dataSource || "provider"}:db-cache+trade-preview+quote`
          : `${state.dataSource || "provider"}:db-cache+trade-preview`
        : quoteDisplay
          ? `${state.dataSource || "provider"}:db-cache+chart-stream+quote`
          : `${state.dataSource || "provider"}:db-cache+chart-stream`;
      state.snapshot.meta.live_bar_closed = !barIsLivePreview(latest);
      syncSnapshotPreviewMeta(state.snapshot);
      state.lastPrice = price;
      if (earliestAppliedPreviewTs) {
        const pendingMs = Date.parse(state.pendingLiveCandlePaintAt || "");
        state.pendingLiveCandlePaintAt = Number.isFinite(pendingMs)
          && pendingMs <= receivedNowMs
          && pendingMs < earliestAppliedPreviewMs
          ? state.pendingLiveCandlePaintAt
          : earliestAppliedPreviewTs;
      }
      if (structuralChange) {
        if (view.followLatest) {
          view.offset = 0;
        } else if (viewportAnchorTs) {
          const anchorIndex = historyAnchorIndex(bars, viewportAnchorTs);
          if (anchorIndex >= 0) {
            view.offset = clamp(bars.length - (anchorIndex + viewportCount), 0, maxOffsetFor(state.snapshot));
          }
        }
        clampView(state.snapshot);
      }
      if (document.visibilityState === "visible") renderLiveMarketVisuals(state.snapshot);
      if (!options.suppressAnalysis || canonicalRevisionAdvanced) {
        const refreshReason = canonicalRevisionAdvanced ? "canonical-revision" : "chart-stream";
        const indicatorRefreshScheduled = maybeScheduleIndicatorAnalysisRefresh(refreshReason);
        if (!indicatorRefreshScheduled) {
          maybeScheduleIndicatorAnalysisRefreshExtension(refreshReason);
        }
      }
      return true;
    }

    function flushQueuedChartBars() {
      const queue = state.chartBarQueue || {};
      if (
        !(queue.barsByTs instanceof Map)
        || (
          !queue.barsByTs.size
          && !queue.chartDataQuality
          && !queue.historyCoverage
          && !queue.gapRepair
          && !queue.futureAxis
          && queue.expectedLiveSlot === undefined
          && queue.expectedLiveClose === undefined
        )
      ) {
        queue.flushScheduled = false;
        return false;
      }
      queue.flushScheduled = false;
      const payload = Array.from(queue.barsByTs.values());
      payload.sort((left, right) => Date.parse(left?.ts || "") - Date.parse(right?.ts || ""));
      queue.barsByTs.clear();
      const suppressAnalysis = Boolean(queue.suppressAnalysis);
      const chartDataQuality = queue.chartDataQuality;
      const historyCoverage = queue.historyCoverage;
      const gapRepair = queue.gapRepair;
      const futureAxis = queue.futureAxis;
      const futureAxisCanonicalRevision = Number(queue.futureAxisCanonicalRevision || 0);
      const chartQualityRevision = Number(queue.chartQualityRevision || 0);
      const canonicalRevision = Number(queue.canonicalRevision || 0);
      const expectedLiveSlot = queue.expectedLiveSlot;
      const expectedLiveClose = queue.expectedLiveClose;
      queue.suppressAnalysis = true;
      queue.chartDataQuality = null;
      queue.historyCoverage = null;
      queue.gapRepair = null;
      queue.futureAxis = null;
      queue.futureAxisCanonicalRevision = 0;
      queue.chartQualityRevision = 0;
      queue.canonicalRevision = 0;
      queue.expectedLiveSlot = undefined;
      queue.expectedLiveClose = undefined;
      const applyOptions = {
        suppressAnalysis,
        chartDataQuality,
        historyCoverage,
        gapRepair,
        futureAxis,
        futureAxisCanonicalRevision,
        chartQualityRevision,
        canonicalRevision,
        normalizedBars: true,
      };
      if (expectedLiveSlot !== undefined) applyOptions.expectedLiveSlot = expectedLiveSlot;
      if (expectedLiveClose !== undefined) applyOptions.expectedLiveClose = expectedLiveClose;
      return applyChartBarsToSnapshot(payload, applyOptions);
    }

    function queueChartBars(streamBars, streamKey = "", options = {}) {
      if (!Array.isArray(streamBars)) return;
      if (!state.chartBarQueue || !(state.chartBarQueue.barsByTs instanceof Map)) {
        state.chartBarQueue = {
          key: "",
          barsByTs: new Map(),
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
      }
      const queue = state.chartBarQueue;
      if (queue.key !== streamKey) {
        queue.key = streamKey;
        queue.barsByTs.clear();
        queue.suppressAnalysis = true;
        queue.chartDataQuality = null;
        queue.historyCoverage = null;
        queue.gapRepair = null;
        queue.futureAxis = null;
        queue.futureAxisCanonicalRevision = 0;
        queue.chartQualityRevision = 0;
        queue.canonicalRevision = 0;
        queue.expectedLiveSlot = undefined;
        queue.expectedLiveClose = undefined;
      }
      let promotedExpectedBounds = null;
      streamBars.forEach(rawBar => {
        const normalized = normalizeStreamBar(rawBar, options);
        if (!normalized) return;
        const key = timestampKey(normalized.ts);
        if (!key) return;
        const existing = queue.barsByTs.get(key);
        const resolved = existing
          ? resolveStreamBar(existing, normalized)
          : normalized;
        queue.barsByTs.set(key, resolved);
        if (options.promoteExpectedBounds === true && resolved?.expected_close) {
          const expectedOpenMs = Date.parse(resolved.ts || "");
          const expectedCloseMs = Date.parse(resolved.expected_close || "");
          if (
            Number.isFinite(expectedOpenMs)
            && Number.isFinite(expectedCloseMs)
            && expectedCloseMs > expectedOpenMs
            && (
              !promotedExpectedBounds
              || expectedOpenMs >= promotedExpectedBounds.openMs
            )
          ) {
            promotedExpectedBounds = {
              openMs: expectedOpenMs,
              open: String(resolved.ts),
              close: String(resolved.expected_close),
            };
          }
        }
      });
      while (queue.barsByTs.size > 512) {
        const entries = Array.from(queue.barsByTs.entries());
        const removable = entries.find(([, bar]) => !barIsConfirmedClosed(bar)) || entries[0];
        if (!removable) break;
        queue.barsByTs.delete(removable[0]);
        if (window.mcTelemetryInc) window.mcTelemetryInc("live_candle.queue_drop");
      }
      queue.suppressAnalysis = queue.suppressAnalysis && Boolean(options.suppressAnalysis);
      queue.canonicalRevision = Math.max(
        Number(queue.canonicalRevision || 0),
        Number(options.canonicalRevision || 0),
      );
      if (options.chartDataQuality && typeof options.chartDataQuality === "object") {
        const incomingRevision = Math.max(Number(options.chartQualityRevision || 0), 0);
        if (!queue.chartDataQuality || incomingRevision >= Number(queue.chartQualityRevision || 0)) {
          queue.chartDataQuality = { ...options.chartDataQuality };
          queue.chartQualityRevision = incomingRevision;
        }
      }
      if (options.historyCoverage && typeof options.historyCoverage === "object") {
        queue.historyCoverage = { ...options.historyCoverage };
      }
      if (options.gapRepair && typeof options.gapRepair === "object") {
        queue.gapRepair = { ...options.gapRepair };
      }
      if (
        options.futureAxis !== undefined
        && isValidFutureAxisPayload(options.futureAxis, state.timeframe)
      ) {
        const incomingFutureAxisCanonicalRevision = Math.max(
          Number(options.futureAxisCanonicalRevision ?? options.canonicalRevision ?? 0),
          0,
        );
        if (futureAxisPayloadShouldReplace(queue.futureAxis, options.futureAxis, {
          timeframe: state.timeframe,
          currentCanonicalRevision: Number(queue.futureAxisCanonicalRevision || 0),
          incomingCanonicalRevision: incomingFutureAxisCanonicalRevision,
        })) {
          queue.futureAxis = {
            ...options.futureAxis,
            slots: options.futureAxis.slots.map(item => ({ ...item })),
          };
          queue.futureAxisCanonicalRevision = incomingFutureAxisCanonicalRevision;
        }
      }
      const optionHasExpectedLiveSlot = options.expectedLiveSlot !== undefined;
      const optionHasExpectedLiveClose = options.expectedLiveClose !== undefined;
      if (optionHasExpectedLiveSlot || optionHasExpectedLiveClose) {
        queue.expectedLiveSlot = String(options.expectedLiveSlot || "");
        queue.expectedLiveClose = String(options.expectedLiveClose || "");
      } else if (promotedExpectedBounds) {
        const queuedExpectedOpenMs = Date.parse(queue.expectedLiveSlot || "");
        const currentExpectedOpenMs = Date.parse(state.transport.chart.expectedLiveSlot || "");
        if (
          (!Number.isFinite(queuedExpectedOpenMs) || promotedExpectedBounds.openMs >= queuedExpectedOpenMs)
          && (!Number.isFinite(currentExpectedOpenMs) || promotedExpectedBounds.openMs >= currentExpectedOpenMs)
        ) {
          queue.expectedLiveSlot = promotedExpectedBounds.open;
          queue.expectedLiveClose = promotedExpectedBounds.close;
        }
      }
      if (!queue.flushScheduled) {
        queue.flushScheduled = true;
        queueMicrotask(flushQueuedChartBars);
      } else if (window.mcTelemetryInc) {
        window.mcTelemetryInc("live_candle.queue_coalesced");
      }
    }

    function renderLoadingCanvases(message) {
      if (typeof resetChartRenderScope === "function") resetChartRenderScope();
      renderEmptyCanvas("price-chart", "LOADING HISTORY", message, { skeleton: true });
      renderEmptyCanvas("volume-chart", "LOADING VOLUME", "Waiting for confirmed market data.", { skeleton: true });
      const status = document.getElementById("chart-accessible-status");
      if (status) {
        status.dataset.chartState = "loading";
        status.textContent = `${String(state.symbol || "Instrument")} ${String(state.timeframe || "")} chart loading.`;
      }
    }

    function renderChartTransitionFailureCanvases(code, message) {
      if (typeof resetChartRenderScope === "function") resetChartRenderScope();
      const exactCode = String(code || "MARKET_REFRESH_FAILED");
      const detail = String(message || "Market history is unavailable for this chart scope.");
      renderEmptyCanvas("price-chart", "CHART UNAVAILABLE", detail);
      renderEmptyCanvas("volume-chart", "VOLUME UNAVAILABLE", exactCode);
      const status = document.getElementById("chart-accessible-status");
      if (status) {
        status.dataset.chartState = "error";
        status.textContent = `${String(state.symbol || "Instrument")} ${String(state.timeframe || "")} chart unavailable. ${exactCode}.`;
      }
    }

    function applyLiveQuoteToSnapshot(quote) {
      if (
        !quote?.ok
        || (
          !providerCapability(state.dataSource, "live_quote_stream")
          && !providerCapability(state.dataSource, "live_quote_polling")
        )
        || quote.route_fingerprint !== instrumentRouteFingerprint()
      ) return false;
      const quoteDisplay = quoteDisplayFromPayload(quote, {
        allowStale: true,
        allowDelayed: true,
        maxAgeMs: quote.status === "live" ? LIVE_QUOTE_DISPLAY_TTL_MS : null,
      });
      if (!quoteDisplay) return false;
      const price = quoteDisplay.price;
      state.liveQuote = quote;
      state.lastQuoteAt = Date.now();
      state.lastPrice = price;
      if (!state.snapshot?.bars?.length) return false;
      if (!snapshotMatchesCurrentRoute(state.snapshot)) {
        debugStep("quote patch skipped", `${state.snapshot?.meta?.symbol || "-"} != ${state.symbol}`);
        return false;
      }
      const bars = state.snapshot.bars;
      const latest = bars[bars.length - 1];
      const prev = bars.length > 1 ? bars[bars.length - 2] : latest;
      const rawReferencePrice = Number(prev.close ?? latest.open ?? price);
      const referencePrice = Number.isFinite(rawReferencePrice) ? rawReferencePrice : price;
      const change = price - referencePrice;
      const changePct = referencePrice !== 0 ? (change / Math.abs(referencePrice)) * 100 : 0;
      const quoteOnlySession = Boolean(effectiveDataQuality(state.snapshot.meta || {}).market_closed);
      state.snapshot.meta = {
        ...state.snapshot.meta,
        ...liveQuoteMetaProjection(quoteDisplay),
      };
      const brokerCandleLive = chartStreamOpen() && barIsLivePreview(latest) && !barIsGapPlaceholder(latest);
      const brokerTradePreview = latest.preview_kind === "broker_trade";
      state.snapshot.meta.source = brokerTradePreview
        ? `${state.dataSource || "provider"}:db-cache+trade-preview+quote`
        : brokerCandleLive
        ? `${state.dataSource || "provider"}:db-cache+chart-stream+quote`
        : `${state.dataSource || "provider"}:db-cache+quote`;
      state.snapshot.meta.quote_only = quoteOnlySession;
      state.snapshot.meta.market_state = quoteOnlySession ? "quote_only" : (brokerCandleLive ? "live_bar" : "quote_preview");
      state.snapshot.meta.change = change;
      state.snapshot.meta.change_pct = changePct;
      state.snapshot.meta.live_bar_closed = !barIsLivePreview(latest);
      syncSnapshotPreviewMeta(state.snapshot);
      renderLiveMarketVisuals(state.snapshot, { skipChart: true });
      if (!quoteDisplay.displayOnly) maybeScheduleLiveQuoteAnalysisRefresh();
      return true;
    }

    async function loadLiveQuote() {
      if (
        (
          !providerCapability(state.dataSource, "live_quote_stream")
          && !providerCapability(state.dataSource, "live_quote_polling")
        )
        || !state.symbol
      ) return;
      if (state.serverSleeping) return;
      syncLiveQuoteFromScreenerRow(currentQuoteRow());
      updateDocumentTitleFromScreenerQuote();
    }

    function updateMarketContext(snapshot) {
      const backendContext = snapshot?.chart_guides?.context;
      state.marketContext = backendContext && typeof backendContext === "object"
        ? backendContext
        : {};
      globalThis.AEF_MARKET_CONTEXT = state.marketContext;
      return state.marketContext;
    }
