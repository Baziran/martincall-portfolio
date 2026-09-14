let gexOptionUniverseExpiryTimer = 0;
let gexOptionUniverseExpiryGeneration = 0;

    function currentGexManualRefresh() {
      state.gexManualRefresh = state.gexManualRefresh || {};
      return state.gexManualRefresh;
    }

    function gexContextKey(captureMode = effectiveGexContextMode(), identity = {}) {
      const instrumentId = exactIdentityText(identity.instrument_id ?? state.instrumentId);
      const routeFingerprint = exactIdentityText(
        identity.route_fingerprint ?? instrumentRouteFingerprint(),
      );
      if (!instrumentId || !routeFingerprint) return "";
      const mode = ["request", "live"].includes(captureMode) ? captureMode : "";
      if (!mode) return "";
      return JSON.stringify([instrumentId, routeFingerprint, mode]);
    }

    function activeGexContext(snapshot = state.snapshot) {
      if (!gexLayerVisible()) return null;
      const instrumentId = exactIdentityText(state.instrumentId);
      const routeFingerprint = instrumentRouteFingerprint();
      const captureMode = effectiveGexContextMode();
      const candidates = [snapshot?.gex, state.gexContext, state.gexContexts?.[gexContextKey()]];
      for (const candidate of candidates) {
        if (gexPayloadMatchesRequest(candidate, instrumentId, routeFingerprint, captureMode)) {
          return candidate;
        }
      }
      return null;
    }

    function gexStatusContext(snapshot = state.snapshot) {
      if (!gexLayerVisible()) return null;
      const captureMode = effectiveGexContextMode();
      const instrumentId = exactIdentityText(state.instrumentId);
      const routeFingerprint = instrumentRouteFingerprint();
      const candidates = [snapshot?.gex, state.gexContext, state.gexContexts?.[gexContextKey()]];
      for (const candidate of candidates) {
        if (gexStatusPayloadMatchesRequest(
          candidate,
          instrumentId,
          routeFingerprint,
          captureMode,
        )) return candidate;
        if (
          candidate?.local_only === true
          && gexPayloadIdentityMatchesRequest(candidate, instrumentId, routeFingerprint)
          && candidate.capture_mode === captureMode
          && candidate.source === GEX_SOURCE_BY_CAPTURE_MODE[captureMode]
          && ["error", "loading", "timeout"].includes(candidate.status)
          && typeof candidate.message === "string"
          && Array.isArray(candidate.levels)
          && candidate.levels.length === 0
        ) return candidate;
      }
      return null;
    }

    function gexRenderKey(snapshot = state.snapshot) {
      const gex = activeGexContext(snapshot);
      if (!gex) return "";
      const history = Array.isArray(gex.history) ? gex.history : [];
      const firstHistory = history[0] || {};
      const lastHistory = history[history.length - 1] || {};
      return [
        gex.instrument_id || "",
        gex.route_fingerprint || "",
        gex.provider_symbol || "",
        gex.market_data_entitlement || "",
        gex.capture_mode === "live" ? "" : gex.captured_at || "",
        history.length,
        gex.history_revision || "",
        firstHistory.captured_at || "",
        firstHistory.capture_mode || "",
        lastHistory.captured_at || "",
        lastHistory.capture_mode || "",
        gexHistoryView(state.indicators?.gexContext?.historyView),
      ].join(":");
    }

    if (typeof window !== "undefined") {
      window.chartRenderKeyProviders = window.chartRenderKeyProviders || [];
      window.chartRenderKeyProviders.push({ id: "gex", renderKey: gexRenderKey });
    }

    // This exact key belongs only to the dedicated GEX canvases. Feeding live
    // level facts into gexRenderKey would invalidate the full chart/background
    // guard on every option frame.
    function gexSidebarOffVisualKey(snapshot = state.snapshot) {
      const gex = activeGexContext(snapshot);
      if (!gex) return "";
      const cfg = state.indicators?.gexContext || {};
      const cachedAdmission = gexPayloadAdmissionCache.get(gex);
      let levelsKey = cachedAdmission?.levelsRenderKey;
      if (typeof levelsKey !== "string") {
        levelsKey = JSON.stringify(gex.levels || []);
        if (cachedAdmission) cachedAdmission.levelsRenderKey = levelsKey;
      }
      return JSON.stringify([
        gex.instrument_id || "",
        gex.route_fingerprint || "",
        gex.provider_symbol || "",
        gex.market_data_entitlement || "",
        gex.capture_mode === "live" ? "" : gex.captured_at || "",
        gex.status || "",
        gex.stale === true,
        gex.decision_authoritative === true,
        gex.global_gamma_regime || "",
        gex.gamma_flip ?? null,
        gex.history_status || "",
        Array.isArray(gex.history) ? gex.history.length : 0,
        gex.history_revision || "",
        gexZoneMode(cfg.zoneStyle),
        GEX_DISPLAY_LEVEL_COUNTS.includes(cfg.displayLevels)
          ? cfg.displayLevels
          : GEX_DEFAULT_DISPLAY_LEVEL_COUNT,
        levelsKey,
      ]);
    }

    function currentGexLiveState() {
      state.gexLive = state.gexLive || {};
      state.gexLive.motionEvents = state.gexLive.motionEvents || {};
      if (!Number.isSafeInteger(state.gexLive.lifecycleGeneration)) {
        state.gexLive.lifecycleGeneration = 0;
      }
      return state.gexLive;
    }

    function gexLiveModeRequested() {
      return String(state.indicators?.gexContext?.mode || "request") === "live";
    }

    function clearGexLiveBlock() {
      const live = currentGexLiveState();
      live.blockedReason = "";
      live.blockedAt = 0;
      live.statusPayload = null;
    }

    function gexLiveBlockReason() {
      return String(currentGexLiveState().blockedReason || "").trim();
    }

    async function setGexContextMode(value, options = {}) {
      const next = String(value || "request") === "live" ? "live" : "request";
      state.indicators.gexContext.mode = next;
      if (options.persist !== false) {
        saveInstrumentIndicatorSetting("gexContextMode", next);
      }
      const modeSelect = document.getElementById("gex-context-mode");
      if (modeSelect) modeSelect.value = next;
      clearGexContext();
      clearGexMotionEvents();
      clearGexLiveBlock();
      if (next === "live" && gexLayerVisible()) connectGexLiveStream();
      else await stopGexLiveContext();
      if (gexLayerVisible() && options.load !== false) await loadGexContext({ refresh: false, force: true, bypassCache: true });
      if (
        options.reloadAnalysis !== false
        && gexLayerVisible()
        && snapshotMatchesCurrentRoute()
      ) {
        reloadCurrentAnalysisOnly("gex-capture-mode", { renderImmediate: false });
      }
      if (state.snapshot) renderCharts(state.snapshot, { overlayImmediate: true });
      if (typeof renderGexContextModeButton === "function") renderGexContextModeButton();
      if (options.apply !== false && typeof applySettings === "function") applySettings();
    }

    function gexLiveModeActive() {
      return gexLiveModeRequested();
    }

    function effectiveGexContextMode() {
      return gexLiveModeRequested() ? "live" : "request";
    }

    function clearLocalGexLiveRuntime() {
      const live = currentGexLiveState();
      clearGexMotionEvents();
      if (live.retryTimer) {
        window.clearTimeout(live.retryTimer);
        live.retryTimer = null;
      }
      if (live.staleTimer) {
        window.clearTimeout(live.staleTimer);
        live.staleTimer = null;
      }
      live.lastMessageAt = 0;
    }

    function clearGexMotionEvents() {
      const live = currentGexLiveState();
      live.motionEvents = {};
      live.seenMotionEvents = {};
      live.motionScopeKey = "";
      if (live.motionTimer) {
        window.clearTimeout(live.motionTimer);
        live.motionTimer = null;
      }
    }

    function gexMotionPriceKey(level) {
      const expiry = typeof level?.expiry === "string" ? level.expiry : "";
      const price = expiry
        ? gexNullableNumber(level?.strike)
        : gexNullableNumber(level?.price);
      const priceKey = price !== null ? String(Math.round(price * 1000) / 1000) : String(level?.kind || "");
      const kind = typeof level?.kind_class === "string"
        ? level.kind_class
        : typeof level?.kind === "string"
          ? level.kind
          : "";
      return [priceKey, expiry, kind].filter(Boolean).join("|");
    }

    function gexMotionLevelKey(level, side = "net") {
      const exactSide = ["call", "put", "net"].includes(side) ? side : "net";
      return `${gexContextKey()}|${gexMotionPriceKey(level)}|${exactSide}`;
    }

    function gexOptionVolumeContext(row) {
      return row?.option_volume_context && typeof row.option_volume_context === "object"
        ? row.option_volume_context
        : null;
    }

    function gexOptionVolumeSection(row, section) {
      const context = gexOptionVolumeContext(row);
      const value = context?.[section];
      return value && typeof value === "object" ? value : null;
    }

    function gexNullableNumber(value) {
      return typeof value === "number" && Number.isFinite(value) ? value : null;
    }

    function gexOptionVolumeNumber(row, section, key) {
      const raw = gexOptionVolumeSection(row, section)?.[key];
      return gexNullableNumber(raw);
    }

    function gexOptionVolumeInteraction(row) {
      const interaction = gexOptionVolumeSection(row, "event")?.interaction;
      return typeof interaction === "string" ? interaction : "";
    }

    function gexOptionVolumeInteractionLabel(row) {
      const interaction = gexOptionVolumeInteraction(row);
      const label = {
        price_near_level: "NEAR",
        spot_moved_away_with_activity: "AWAY",
        price_stationary_near_activity: "TEST",
        price_approaching_with_activity: "APPROACH",
        price_crossed_with_activity: "CROSS",
        two_sided_activity_near_level: "2-WAY",
      }[interaction] || "";
      if (interaction !== "price_crossed_with_activity" || !label) return label;
      const direction = gexOptionVolumeSection(row, "event")?.cross_direction;
      return `${label} ${direction === "up" ? "↑" : direction === "down" ? "↓" : ""}`.trim();
    }

    function gexOptionVolumeDominantSide(row) {
      const liveBias = gexOptionVolumeNumber(row, "event", "bias");
      const callPart = gexOptionVolumeNumber(row, "event", "call_participation");
      const putPart = gexOptionVolumeNumber(row, "event", "put_participation");
      if ((liveBias !== null && liveBias >= 0.25) || (callPart !== null && callPart >= 0.58)) return "call";
      if ((liveBias !== null && liveBias <= -0.25) || (putPart !== null && putPart >= 0.58)) return "put";
      return "net";
    }

    function gexOptionVolumeIntensity(row) {
      const turnover = Math.max(0, gexOptionVolumeNumber(row, "event", "turnover") || 0);
      const acceleration = Math.max(0, gexOptionVolumeNumber(row, "event", "acceleration") || 0);
      const totalDelta = Math.max(0, gexOptionVolumeNumber(row, "event", "total_volume_delta") || 0);
      const turnoverScore = turnover > 0 ? clamp(turnover / 0.10, 0, 1) : 0;
      const accelerationScore = acceleration > 0 ? clamp(acceleration / 3.0, 0, 1) : 0;
      const volumeScore = totalDelta > 0 ? clamp(Math.log10(totalDelta + 1) / 4.0, 0, 1) : 0;
      return clamp(Math.max(turnoverScore, accelerationScore, volumeScore), 0, 1);
    }

    function gexOptionVolumeCompactLabel(row) {
      const event = gexOptionVolumeSection(row, "event");
      const materialActivity = event?.material === true;
      if (materialActivity) {
        const interactionLabel = gexOptionVolumeInteractionLabel(row);
        if (interactionLabel) return interactionLabel;
        const turnover = Math.max(0, gexOptionVolumeNumber(row, "event", "turnover") || 0);
        const acceleration = Math.max(0, gexOptionVolumeNumber(row, "event", "acceleration") || 0);
        if (turnover >= 0.05) return `TURN ${Math.round(turnover * 100)}%`;
        if (acceleration >= 1.8) return `ACC ×${acceleration.toFixed(acceleration >= 10 ? 0 : 1)}`;
        const callPart = gexOptionVolumeNumber(row, "event", "call_participation");
        const putPart = gexOptionVolumeNumber(row, "event", "put_participation");
        if (callPart !== null && callPart >= 0.58) return `CΔ ${Math.round(callPart * 100)}%`;
        if (putPart !== null && putPart >= 0.58) return `PΔ ${Math.round(putPart * 100)}%`;
        const totalDelta = Math.max(0, gexOptionVolumeNumber(row, "event", "total_volume_delta") || 0);
        const compactDelta = totalDelta >= 1000
          ? `${(totalDelta / 1000).toFixed(totalDelta >= 10000 ? 0 : 1)}K`
          : String(Math.round(totalDelta));
        if (totalDelta > 0) return `2W Δ${compactDelta}`;
      }
      const current = gexOptionVolumeSection(row, "current");
      const turnoverSinceOpen = gexOptionVolumeNumber(row, "current", "turnover");
      if (turnoverSinceOpen !== null) {
        return `VOL ${turnoverSinceOpen.toFixed(turnoverSinceOpen >= 10 ? 0 : 2)}×`;
      }
      return "";
    }

    function gexOptionVolumeFrameMs(row) {
      const intensity = gexOptionVolumeIntensity(row);
      return Math.round(clamp(360 - intensity * 240, 120, 360));
    }

    function gexOptionVolumeMotionType(row, side) {
      const processState = gexOptionVolumeInteraction(row);
      if (!processState) return "";
      if (processState === "price_crossed_with_activity") return side === "put" ? "live_cross_put" : side === "call" ? "live_cross_call" : "live_cross";
      if (processState === "price_approaching_with_activity") return side === "put" ? "live_approach_put" : side === "call" ? "live_approach_call" : "live_approach";
      if (processState === "price_stationary_near_activity" || processState === "spot_moved_away_with_activity" || processState === "price_near_level") return "live_near";
      if (processState === "two_sided_activity_near_level") return "live_two_sided";
      if (side === "put") return "live_activity_put";
      if (side === "call") return "live_activity_call";
      return "live_activity_two_sided";
    }

    function gexOptionVolumeMotionEvent(row) {
      const event = gexOptionVolumeSection(row, "event");
      const processState = gexOptionVolumeInteraction(row);
      const totalDelta = gexOptionVolumeNumber(row, "event", "total_volume_delta");
      if (event?.material !== true || !processState || !(totalDelta > 0)) return null;
      const side = gexOptionVolumeDominantSide(row);
      const type = gexOptionVolumeMotionType(row, side);
      if (!type) return null;
      const intensity = gexOptionVolumeIntensity(row);
      return {
        type,
        side,
        frameMs: gexOptionVolumeFrameMs(row),
        intensity,
        processState,
        crossDirection: typeof event.cross_direction === "string" ? event.cross_direction : "",
        flowState: typeof event.state === "string" ? event.state : "",
        totalDelta,
        turnoverDelta: gexOptionVolumeNumber(row, "event", "turnover"),
        acceleration: gexOptionVolumeNumber(row, "event", "acceleration"),
        liveBias: gexOptionVolumeNumber(row, "event", "bias"),
        windowSeconds: gexOptionVolumeNumber(row, "event", "window_seconds"),
      };
    }

    function recordGexMotionEvent(level, type, detail = {}) {
      const side = ["call", "put", "net"].includes(detail.side) ? detail.side : "net";
      const key = gexMotionLevelKey(level, side);
      const now = Date.now();
      const live = currentGexLiveState();
      const motionScopeKey = gexContextKey();
      if (!motionScopeKey) return;
      if (live.motionScopeKey !== motionScopeKey) {
        clearGexMotionEvents();
        live.motionScopeKey = motionScopeKey;
      }
      live.seenMotionEvents = live.seenMotionEvents || {};
      const signature = [
        type,
        side,
        detail.from,
        detail.to,
        detail.fromPrice,
        detail.toPrice,
        detail.windowSeconds,
        detail.processState,
        detail.crossDirection,
        detail.flowState,
        detail.frameMs,
        Number.isFinite(Number(detail.intensity)) ? Number(detail.intensity).toFixed(2) : "",
      ].map(value => String(value ?? "")).join("|");
      if (live.seenMotionEvents[key] === signature) return;
      live.seenMotionEvents[key] = signature;
      live.motionEvents[key] = {
        key,
        side,
        type,
        signature,
        startedAt: now,
        expiresAt: now + GEX_MOTION_TTL_MS,
        ...detail,
      };
      scheduleGexMotionAnimation();
    }

    function updateGexMotionEvents(payload, previousPayload) {
      if (!gexLiveModeActive()) return;
      if (!state.indicators?.gexContext?.profile) return;
      if (!payload?.live?.active) return;
      const rows = [
        ...(Array.isArray(payload.levels) ? payload.levels : []),
      ];
      const seen = new Set();
      rows.forEach(row => {
        const motion = row?.motion && typeof row.motion === "object" ? row.motion : null;
        if (motion) {
          for (const side of ["call", "put", "net"]) {
            const event = motion[side];
            const type = String(event?.type || "");
            if (!type) continue;
            const key = `${gexMotionLevelKey(row, side)}|${type}`;
            if (seen.has(key)) continue;
            seen.add(key);
            recordGexMotionEvent(row, type, {
              side,
              from: event.from,
              to: event.to,
              delta: event.delta,
              deltaPct: event.delta_pct,
              fromPrice: event.from_price,
              toPrice: event.to_price,
              windowSeconds: event.window_seconds,
            });
          }
        }
        const optionVolumeEvent = gexOptionVolumeMotionEvent(row);
        if (!optionVolumeEvent) return;
        const eventKey = `${gexMotionLevelKey(row, optionVolumeEvent.side)}|${optionVolumeEvent.type}`;
        if (seen.has(eventKey)) return;
        seen.add(eventKey);
        recordGexMotionEvent(row, optionVolumeEvent.type, {
          side: optionVolumeEvent.side,
          delta: optionVolumeEvent.totalDelta,
          intensity: optionVolumeEvent.intensity,
          frameMs: optionVolumeEvent.frameMs,
          processState: optionVolumeEvent.processState,
          crossDirection: optionVolumeEvent.crossDirection,
          flowState: optionVolumeEvent.flowState,
          turnoverDelta: optionVolumeEvent.turnoverDelta,
          acceleration: optionVolumeEvent.acceleration,
          liveBias: optionVolumeEvent.liveBias,
          windowSeconds: optionVolumeEvent.windowSeconds,
        });
      });
    }

    function pruneGexMotionEvents() {
      const live = currentGexLiveState();
      const now = Date.now();
      let active = false;
      Object.entries(live.motionEvents || {}).forEach(([key, event]) => {
        if (!event || Number(event.expiresAt || 0) <= now) delete live.motionEvents[key];
        else active = true;
      });
      return active;
    }

    function gexMotionAnimationFrameMs() {
      const live = currentGexLiveState();
      const frames = Object.values(live.motionEvents || {})
        .map(event => Number(event?.frameMs))
        .filter(value => Number.isFinite(value) && value > 0);
      const fastest = frames.length ? Math.min(...frames) : GEX_MOTION_FRAME_MS;
      return Math.round(clamp(fastest, 120, GEX_MOTION_FRAME_MS));
    }

    function scheduleGexMotionAnimation() {
      const live = currentGexLiveState();
      const cfg = state.indicators?.gexContext || {};
      const renderScopeKey = live.motionScopeKey || gexContextKey();
      if (
        !renderScopeKey
        || gexContextKey() !== renderScopeKey
        || !gexLayerVisible()
        || !cfg.profile
      ) {
        if (live.motionTimer) {
          window.clearTimeout(live.motionTimer);
          live.motionTimer = null;
        }
        return;
      }
      if (live.motionTimer) return;
      const lifecycleGeneration = live.lifecycleGeneration;
      const motionTimer = window.setTimeout(() => {
        if (
          live.lifecycleGeneration !== lifecycleGeneration
          || live.motionTimer !== motionTimer
        ) return;
        live.motionTimer = null;
        const frameCfg = state.indicators?.gexContext || {};
        if (
          live.motionScopeKey !== renderScopeKey
          || gexContextKey() !== renderScopeKey
          || !gexLayerVisible()
          || !frameCfg.profile
        ) return;
        const active = pruneGexMotionEvents();
        const frontCanvas = document.getElementById("price-gex-front");
        if (state.snapshot && frontCanvas && typeof renderPriceGexFrontFromGeometry === "function") {
          const { w, h } = canvasContext("price-gex-front");
          renderPriceGexFrontFromGeometry(state.snapshot, priceRenderGeometry(state.snapshot, w, h));
        }
        if (active) scheduleGexMotionAnimation();
      }, gexMotionAnimationFrameMs());
      live.motionTimer = motionTimer;
    }

    function gexMotionEventForLevel(level, side = "net") {
      const live = currentGexLiveState();
      const event = live.motionEvents?.[gexMotionLevelKey(level, side)] || null;
      if (!event || Number(event.expiresAt || 0) <= Date.now()) return null;
      return event;
    }

    function connectGexLiveStream() {
      if (!gexLiveModeRequested() || !gexLayerVisible() || state.serverSleeping) return;
      const live = currentGexLiveState();
      const requestInstrumentId = exactIdentityText(state.instrumentId);
      const requestRouteFingerprint = instrumentRouteFingerprint();
      const requestKey = gexContextKey();
      if (!requestInstrumentId || !requestRouteFingerprint || !requestKey) return;
      const requestIdentity = {
        instrument_id: requestInstrumentId,
        route_fingerprint: requestRouteFingerprint,
      };
      const streamKey = requestKey;
      const lastMessageAt = Number(live.lastMessageAt || 0);
      const existingSocketFresh = !lastMessageAt || Date.now() - lastMessageAt < GEX_STREAM_STALE_MS;
      if (live.socket && live.socket.readyState <= WebSocket.OPEN && live.streamKey === streamKey && existingSocketFresh) return;
      if (live.socket || live.retryTimer || live.staleTimer) {
        void stopGexLiveContext({
          pruneRouteCaches: live.streamKey !== streamKey,
          preserveCurrentRoute: true,
          reason: "gex stream replace",
        });
      }
      const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      const params = new URLSearchParams({
        instrument_id: requestInstrumentId,
        expected_route_fingerprint: requestRouteFingerprint,
      });
      const socket = new WebSocket(`${protocol}//${window.location.host}/ws/gex?${params.toString()}`);
      live.socket = socket;
      live.streamKey = streamKey;
      live.acceptedSessionId = "";
      live.acceptedFrameSeq = 0;
      live.pendingHistory = null;
      const lifecycleGeneration = live.lifecycleGeneration;
      let staleTimer = null;
      let lastTransportAt = Date.now();
      const clearGexStaleTimer = () => {
        window.clearTimeout(staleTimer);
        if (live.staleTimer === staleTimer) live.staleTimer = null;
        staleTimer = null;
      };
      const markGexStreamAlive = () => {
        lastTransportAt = Date.now();
        live.lastMessageAt = lastTransportAt;
      };
      const armGexStaleTimer = () => {
        clearGexStaleTimer();
        staleTimer = window.setTimeout(() => {
          if (
            live.lifecycleGeneration !== lifecycleGeneration
            || live.socket !== socket
          ) return;
          const staleFor = Date.now() - lastTransportAt;
          if (staleFor >= GEX_STREAM_STALE_MS) {
            if (live.socket !== socket) return;
            live.blockedReason = `GEX stream stalled for ${Math.round(staleFor / 1000)}s; reconnecting.`;
            renderSystemHealth();
            try { socket.close(4000, "gex stream stale"); } catch (_) { /* noop */ }
          } else {
            armGexStaleTimer();
          }
        }, GEX_STREAM_STALE_MS);
        live.staleTimer = staleTimer;
      };
      live.lastMessageAt = lastTransportAt;
      armGexStaleTimer();
      socket.onopen = () => {
        if (
          live.lifecycleGeneration !== lifecycleGeneration
          || live.socket !== socket
        ) return;
        clearGexLiveBlock();
        markGexStreamAlive();
        armGexStaleTimer();
        debugStep("gex stream", `open ${requestInstrumentId}`);
      };
      socket.onmessage = event => {
        try {
          if (
            live.lifecycleGeneration !== lifecycleGeneration
            || live.socket !== socket
          ) return;
          let payload = JSON.parse(event.data);
          if (
            exactIdentityText(state.instrumentId) !== requestInstrumentId
            || instrumentRouteFingerprint() !== requestRouteFingerprint
            || !gexPayloadIdentityMatchesRequest(
              payload,
              requestInstrumentId,
              requestRouteFingerprint,
            )
          ) return;
          const knownMessageType = [
            "gex_heartbeat",
            "gex_status",
            "gex_history_snapshot",
            "gex_history_delta",
            "gex_frame",
          ].includes(payload.type);
          let attachedPendingHistory = false;
          if (payload.type === "gex_frame") {
            const previous = state.gexContexts[requestKey];
            if (!Array.isArray(payload.history) && Array.isArray(previous?.history)) {
              payload.history = previous.history;
            }
            if (!Number.isInteger(payload.history_revision) && Number.isInteger(previous?.history_revision)) {
              payload.history_revision = previous.history_revision;
            }
            if (!payload.history_status && previous?.history_status) payload.history_status = previous.history_status;
            const pendingHistory = live.pendingHistory;
            if (pendingHistory) {
              const pendingRevision = pendingHistory.history_revision;
              const frameRevision = payload.history_revision;
              const frameEntitlement = gexMarketDataEntitlement(payload);
              const frameCanAdmitHistory = Boolean(
                frameEntitlement
                && frameEntitlement !== "unknown"
              );
              if (
                Number.isInteger(pendingRevision)
                && Number.isInteger(frameRevision)
                && frameRevision === pendingRevision
                && frameCanAdmitHistory
              ) {
                const attached = mergeGexHistoryMessage(
                  {
                    type: "gex_history_snapshot",
                    provider_symbol: pendingHistory.provider_symbol,
                    instrument_id: pendingHistory.instrument_id,
                    route_fingerprint: pendingHistory.route_fingerprint,
                    history_revision: pendingRevision,
                    history_status: pendingHistory.history_status,
                    history: pendingHistory.history,
                  },
                  payload,
                  requestIdentity,
                );
                if (!attached) {
                  live.blockedReason = "GEX history payload failed canonical admission; reconnecting.";
                  renderSystemHealth();
                  try { socket.close(4001, "gex history admission failed"); } catch (_) { /* noop */ }
                  return;
                }
                payload = attached;
                attachedPendingHistory = true;
              } else if (
                Number.isInteger(pendingRevision)
                && Number.isInteger(frameRevision)
                && frameRevision > pendingRevision
              ) {
                live.blockedReason = "GEX history revision gap; reconnecting for a fresh snapshot.";
                renderSystemHealth();
                try { socket.close(4001, "gex history revision gap"); } catch (_) { /* noop */ }
                return;
              }
            }
          }
          const frameIsCanonical = payload.type !== "gex_frame" || gexPayloadMatchesRequest(
            payload,
            requestInstrumentId,
            requestRouteFingerprint,
            "live",
          );
          if (!knownMessageType || !frameIsCanonical) return;
          if (attachedPendingHistory) live.pendingHistory = null;
          markGexStreamAlive();
          armGexStaleTimer();
          if (payload.type === "gex_heartbeat") return;
          if (payload.type === "gex_status") {
            const statusPayload = {
              ...payload,
              source: GEX_SOURCE_BY_CAPTURE_MODE.live,
              capture_mode: "live",
              levels: [],
              expiry_profile: [],
            };
            if (gexStatusPayloadMatchesRequest(
              statusPayload,
              requestInstrumentId,
              requestRouteFingerprint,
              "live",
            )) {
              live.blockedReason = String(
                payload.message || payload.status || "GEX stream unavailable",
              );
              live.statusPayload = statusPayload;
              renderGexContextModeButton();
              const current = state.gexContexts[requestKey] || null;
              if (
                gexPayloadMatchesRequest(
                  current,
                  requestInstrumentId,
                  requestRouteFingerprint,
                  "live",
                )
                && statusPayload.ok === false
              ) {
                const preserved = preservedGexContextWithStaleNotice(
                  current,
                  statusPayload,
                  {
                    displaySource: current.display_context_source === "history"
                      ? "history"
                      : "previous",
                  },
                );
                state.gexContexts[requestKey] = preserved;
                setActiveGexContext(preserved, { suppressMarketRecalc: true });
                shareGexReadModel(preserved);
                renderGexSnapshot({
                  overlayImmediate: true,
                  reason: "gex-stream-status",
                });
              } else if (!gexPayloadMatchesRequest(current, requestInstrumentId, requestRouteFingerprint, "live")) {
                state.gexContexts[requestKey] = statusPayload;
                setActiveGexContext(statusPayload, { suppressMarketRecalc: true });
                shareGexReadModel(statusPayload);
                renderGexSnapshot({ overlayImmediate: true, reason: "gex-stream-status" });
              }
              renderSystemHealth();
            }
            return;
          }
          if (["gex_history_snapshot", "gex_history_delta"].includes(payload.type)) {
            const current = state.gexContexts[requestKey];
            const currentEntitlement = gexMarketDataEntitlement(current);
            const currentCanAdmitHistory = Boolean(
              !live.pendingHistory
              && currentEntitlement
              && currentEntitlement !== "unknown"
              && gexPayloadMatchesRequest(
                current,
                requestInstrumentId,
                requestRouteFingerprint,
                "live",
              )
            );
            const mergeBase = live.pendingHistory || (
              currentCanAdmitHistory ? current : null
            );
            const merged = mergeGexHistoryMessage(
              payload,
              mergeBase,
              requestIdentity,
            );
            if (!merged) {
              live.blockedReason = "GEX history revision gap; reconnecting for a fresh snapshot.";
              renderSystemHealth();
              try { socket.close(4001, "gex history revision gap"); } catch (_) { /* noop */ }
              return;
            }
            if (!currentCanAdmitHistory || live.pendingHistory) {
              live.pendingHistory = merged;
              const historyContext = lastUsableGexHistoryContext(merged, "live");
              if (gexContextHasDisplayData(historyContext)) {
                const transportStatus = gexStatusPayloadMatchesRequest(
                  live.statusPayload,
                  requestInstrumentId,
                  requestRouteFingerprint,
                  "live",
                )
                  ? live.statusPayload
                  : {
                      ok: false,
                      source: GEX_SOURCE_BY_CAPTURE_MODE.live,
                      capture_mode: "live",
                      status: "stale",
                      message: "No current GEX stream frame; showing the last durable live snapshot.",
                    };
                const preserved = preservedGexContextWithStaleNotice(
                  historyContext,
                  transportStatus,
                  { displaySource: "history" },
                );
                state.gexContexts[requestKey] = preserved;
                shareGexReadModel(preserved);
                if (
                  exactIdentityText(state.instrumentId) === requestInstrumentId
                  && instrumentRouteFingerprint() === requestRouteFingerprint
                  && gexContextKey() === requestKey
                ) {
                  setActiveGexContext(preserved, { suppressMarketRecalc: true });
                  renderGexSnapshot({
                    overlayImmediate: true,
                    reason: "gex-history-stream",
                  });
                }
              }
              return;
            }
            if (!gexPayloadMatchesRequest(
              merged,
              requestInstrumentId,
              requestRouteFingerprint,
              "live",
            )) {
              live.blockedReason = "GEX history payload failed canonical admission; reconnecting.";
              renderSystemHealth();
              try { socket.close(4001, "gex history admission failed"); } catch (_) { /* noop */ }
              return;
            }
            state.gexContexts[requestKey] = merged;
            shareGexReadModel(merged);
            if (exactIdentityText(state.instrumentId) === requestInstrumentId && instrumentRouteFingerprint() === requestRouteFingerprint && gexContextKey() === requestKey) {
              setActiveGexContext(merged, { suppressMarketRecalc: true });
              renderGexSnapshot({ overlayImmediate: true, reason: "gex-history-stream" });
            }
            return;
          }
          if (payload.type !== "gex_frame") return;
          const sessionId = payload.live.session_id;
          const frameSeq = payload.live.frame_seq;
          const recoversTypedStatus = gexStatusPayloadMatchesRequest(
            live.statusPayload,
            requestInstrumentId,
            requestRouteFingerprint,
            "live",
          );
          if (!gexLiveFrameMayAdvance(
            sessionId,
            frameSeq,
            live.acceptedSessionId,
            live.acceptedFrameSeq,
            recoversTypedStatus,
          )) return;
          if (sessionId !== live.acceptedSessionId) {
            live.acceptedSessionId = sessionId;
            live.acceptedFrameSeq = 0;
          }
          live.acceptedFrameSeq = frameSeq;
          clearGexLiveBlock();
          if (recoversTypedStatus) {
            renderGexContextModeButton();
            renderSystemHealth();
          }
          state.gexContexts[requestKey] = payload;
          shareGexReadModel(payload);
          if (exactIdentityText(state.instrumentId) === requestInstrumentId && instrumentRouteFingerprint() === requestRouteFingerprint && gexContextKey() === requestKey) {
            setActiveGexContext(payload);
            renderGexSnapshot({ overlayImmediate: true, reason: "gex-stream" });
          }
        } catch (error) {
          console.warn("gex stream message failed", error);
        }
      };
      socket.onerror = () => {
        if (
          live.lifecycleGeneration !== lifecycleGeneration
          || live.socket !== socket
        ) return;
        live.blockedReason = "GEX stream socket error";
      };
      socket.onclose = () => {
        clearGexStaleTimer();
        if (
          live.lifecycleGeneration !== lifecycleGeneration
          || live.socket !== socket
        ) return;
        live.socket = null;
        live.streamKey = "";
        live.lastMessageAt = 0;
        if (!gexLiveModeRequested() || state.serverSleeping) return;
        const retryTimer = window.setTimeout(() => {
          if (
            live.lifecycleGeneration !== lifecycleGeneration
            || live.retryTimer !== retryTimer
          ) return;
          live.retryTimer = null;
          connectGexLiveStream();
        }, QUOTE_STREAM_RECONNECT_MS);
        live.retryTimer = retryTimer;
      };
    }

    const sharedGexReadModelThrottleByScope = new Map();

    function shareGexReadModel(payload) {
      const captureMode = ["request", "live"].includes(payload?.capture_mode)
        ? payload.capture_mode
        : "";
      const scopeKey = gexContextKey(captureMode, {
        instrument_id: exactIdentityText(payload?.instrument_id),
        route_fingerprint: exactIdentityText(payload?.route_fingerprint),
      });
      if (!scopeKey) return;
      const now = Date.now();
      if (captureMode === "live") {
        let throttle = sharedGexReadModelThrottleByScope.get(scopeKey);
        if (!throttle) {
          throttle = { lastSharedAt: 0, pendingPayload: null, timer: 0 };
          sharedGexReadModelThrottleByScope.set(scopeKey, throttle);
        }
        const remainingMs = 1000 - (now - throttle.lastSharedAt);
        if (remainingMs > 0) {
          throttle.pendingPayload = payload;
          if (!throttle.timer) {
            const lifecycleGeneration = currentGexLiveState().lifecycleGeneration;
            const timer = window.setTimeout(() => {
              if (
                currentGexLiveState().lifecycleGeneration !== lifecycleGeneration
                || throttle.timer !== timer
                || sharedGexReadModelThrottleByScope.get(scopeKey) !== throttle
              ) return;
              throttle.timer = 0;
              const trailingPayload = throttle.pendingPayload;
              throttle.pendingPayload = null;
              if (trailingPayload) shareGexReadModel(trailingPayload);
            }, remainingMs);
            throttle.timer = timer;
          }
          return;
        }
        throttle.lastSharedAt = now;
        throttle.pendingPayload = null;
        if (throttle.timer) {
          window.clearTimeout(throttle.timer);
          throttle.timer = 0;
        }
      }
      const sharedPayload = Object.fromEntries(Object.entries(payload || {}).filter(([field]) => (
        !["history", "history_revision", "history_status"].includes(field)
      )));
      shareReadModel("gex_context", {
        instrument_id: exactIdentityText(payload?.instrument_id),
        route_fingerprint: exactIdentityText(payload?.route_fingerprint),
        capture_mode: captureMode,
      }, sharedPayload);
    }

    function queueSharedGexContextMessage(message) {
      const scope = message.scope;
      if (message.type === "read_model_requested") {
        const instrumentId = exactIdentityText(scope.instrument_id);
        const routeFingerprint = exactIdentityText(scope.route_fingerprint);
        const captureMode = ["request", "live"].includes(scope.capture_mode)
          ? scope.capture_mode
          : "";
        const requestKey = gexContextKey(captureMode, {
          instrument_id: instrumentId,
          route_fingerprint: routeFingerprint,
        });
        if (!requestKey) return;
        const cached = state.gexContexts[requestKey] || null;
        const cachedIsSnapshot = gexPayloadMatchesRequest(
          cached,
          instrumentId,
          routeFingerprint,
          captureMode,
        );
        const cachedIsStatus = gexStatusPayloadMatchesRequest(
          cached,
          instrumentId,
          routeFingerprint,
          captureMode,
        );
        const ownsReadModel = acquireSharedStreamOwner("gex-read", requestKey);
        if (cachedIsSnapshot || cachedIsStatus) shareGexReadModel(cached);
        const requestMatchesCurrent = exactIdentityText(state.instrumentId) === instrumentId
          && instrumentRouteFingerprint() === routeFingerprint
          && effectiveGexContextMode() === captureMode
          && gexLayerVisible();
        if (cachedIsSnapshot || !requestMatchesCurrent) return;
        if (captureMode === "live") {
          connectGexLiveStream();
        } else if (ownsReadModel) {
          loadGexContext({ force: true, bypassCache: true, sharedRequest: true });
        }
        return;
      }
      if (message.type !== "read_model_snapshot") return;
      const payload = message.payload;
      const instrumentId = exactIdentityText(scope.instrument_id);
      const routeFingerprint = exactIdentityText(scope.route_fingerprint);
      const captureMode = ["request", "live"].includes(scope.capture_mode)
        ? scope.capture_mode
        : "";
      if (
        exactIdentityText(payload?.instrument_id) !== instrumentId
        || exactIdentityText(payload?.route_fingerprint) !== routeFingerprint
        || payload?.capture_mode !== captureMode
      ) return;
      const requestKey = gexContextKey(captureMode, {
        instrument_id: instrumentId,
        route_fingerprint: routeFingerprint,
      });
      if (!requestKey) return;
      const payloadIsSnapshot = gexPayloadMatchesRequest(
        payload,
        instrumentId,
        routeFingerprint,
        captureMode,
      );
      const payloadIsStatus = gexStatusPayloadMatchesRequest(
        payload,
        instrumentId,
        routeFingerprint,
        captureMode,
      );
      if (!payloadIsSnapshot && !payloadIsStatus) return;
      const current = state.gexContexts[requestKey] || null;
      const recoversTypedStatus = payloadIsSnapshot && gexStatusPayloadMatchesRequest(
        current,
        instrumentId,
        routeFingerprint,
        captureMode,
      );
      const currentMatchesSnapshotContract = gexPayloadMatchesRequest(
        current,
        instrumentId,
        routeFingerprint,
        captureMode,
      );
      const cachedCurrentAdmission = typeof gexPayloadAdmissionCache === "undefined"
        ? null
        : gexPayloadAdmissionCache.get(current);
      const currentWasAdmittedSnapshot = Boolean(
        currentMatchesSnapshotContract
        || (
          cachedCurrentAdmission
          && Object.isFrozen(current)
          && cachedCurrentAdmission.instrumentId === instrumentId
          && cachedCurrentAdmission.routeFingerprint === routeFingerprint
          && cachedCurrentAdmission.expectedMode === captureMode
        )
      );
      const sameRevisionImmutableFactsMatch = Boolean(
        payloadIsSnapshot
        && currentWasAdmittedSnapshot
        && JSON.stringify([
          gexPayloadProviderSymbol(payload),
          payload?.source,
          payload?.capture_mode,
          gexPayloadRevision(payload),
          payload?.captured_at,
          payload?.option_universe_expires_at,
          payload?.market_data_entitlement,
          payload?.open_interest_as_of,
          payload?.frame_complete,
          payload?.spot,
          payload?.global_gamma_regime,
          payload?.gamma_flip,
          payload?.comparison_scope,
          payload?.levels,
          payload?.expiry_profile,
          payload?.visibility_summary,
        ]) === JSON.stringify([
          gexPayloadProviderSymbol(current),
          current?.source,
          current?.capture_mode,
          gexPayloadRevision(current),
          current?.captured_at,
          current?.option_universe_expires_at,
          current?.market_data_entitlement,
          current?.open_interest_as_of,
          current?.frame_complete,
          current?.spot,
          current?.global_gamma_regime,
          current?.gamma_flip,
          current?.comparison_scope,
          current?.levels,
          current?.expiry_profile,
          current?.visibility_summary,
        ])
      );
      const appliesSameRevisionAuthorityDowngrade = Boolean(
        payloadIsSnapshot
        && payload?.decision_authoritative === false
        && currentWasAdmittedSnapshot
        && current?.decision_authoritative === true
        && gexPayloadRevision(payload) === gexPayloadRevision(current)
        && sameRevisionImmutableFactsMatch
        && (
          captureMode !== "live"
          || (
            payload?.live?.session_id === current?.live?.session_id
            && Number(payload?.live?.frame_seq || 0)
              === Number(current?.live?.frame_seq || 0)
          )
        )
      );
      let appliedPayload = payload;
      if (
        payloadIsStatus
        && currentWasAdmittedSnapshot
      ) return;
      if (payloadIsSnapshot && currentWasAdmittedSnapshot) {
        const nextRevisionMs = Date.parse(gexPayloadRevision(payload));
        const currentRevisionMs = Date.parse(gexPayloadRevision(current));
        if (nextRevisionMs < currentRevisionMs) return;
        if (
          captureMode === "live"
          && nextRevisionMs === currentRevisionMs
          && !appliesSameRevisionAuthorityDowngrade
        ) {
          const nextSessionMs = Date.parse(payload?.live?.session_id || "");
          const currentSessionMs = Date.parse(current?.live?.session_id || "");
          if (
            nextSessionMs < currentSessionMs
            || (
              nextSessionMs === currentSessionMs
              && Number(payload?.live?.frame_seq || 0) <= Number(current?.live?.frame_seq || 0)
            )
          ) return;
        } else if (
          nextRevisionMs === currentRevisionMs
          && !appliesSameRevisionAuthorityDowngrade
        ) {
          appliedPayload = current;
        }
      }
      if (
        appliedPayload === payload
        && payloadIsSnapshot
        && !Object.prototype.hasOwnProperty.call(payload, "history")
        && Array.isArray(current?.history)
      ) {
        appliedPayload = {
          ...payload,
          history: current.history,
          ...(Number.isInteger(current.history_revision) ? { history_revision: current.history_revision } : {}),
          ...(current.history_status ? { history_status: current.history_status } : {}),
        };
      }
      if (
        payloadIsSnapshot
        && !gexPayloadMatchesRequest(
          appliedPayload,
          instrumentId,
          routeFingerprint,
          captureMode,
        )
      ) return;
      clearGexReadModelHandoff(requestKey);
      state.gexContexts[requestKey] = appliedPayload;
      if (
        exactIdentityText(state.instrumentId) === instrumentId
        && instrumentRouteFingerprint() === routeFingerprint
        && effectiveGexContextMode() === captureMode
      ) {
        setActiveGexContext(appliedPayload);
        renderGexSnapshot({ overlayImmediate: true, reason: "gex-shared-read-model" });
        if (payloadIsStatus || recoversTypedStatus) renderSystemHealth();
      }
      if (window.mcTelemetryInc) window.mcTelemetryInc("gex_read_model.applied");
    }

    async function stopGexLiveContext(options = {}) {
      const live = currentGexLiveState();
      live.lifecycleGeneration += 1;
      clearLocalGexLiveRuntime();
      const socket = live.socket;
      const stoppedStreamKey = live.streamKey;
      const refresh = currentGexManualRefresh();
      const releasedReadOwnerKeys = new Set();
      const releasedAcquireOwnerKeys = new Set();
      live.socket = null;
      live.streamKey = "";
      live.acceptedSessionId = "";
      live.acceptedFrameSeq = 0;
      live.lastMessageAt = 0;
      live.blockedReason = "";
      live.blockedAt = 0;
      live.statusPayload = null;
      live.pendingHistory = null;
      if (socket) {
        closeWebSocketQuietly(
          socket,
          String(options.reason || "gex mode disabled"),
        );
      }
      if (refresh.pollTimer) window.clearTimeout(refresh.pollTimer);
      refresh.pollTimer = null;
      for (const [key, timer] of Object.entries(state.gexReadModelHandoffTimers || {})) {
        if (timer) window.clearTimeout(timer);
        delete state.gexReadModelHandoffTimers[key];
      }
      for (const throttle of sharedGexReadModelThrottleByScope.values()) {
        if (throttle?.timer) window.clearTimeout(throttle.timer);
        if (throttle) {
          throttle.timer = 0;
          throttle.pendingPayload = null;
        }
      }
      for (const [key, acquire] of Object.entries(state.gexInitialAcquireKeys || {})) {
        if (acquire?.timer) window.clearTimeout(acquire.timer);
        delete state.gexInitialAcquireKeys[key];
        releaseSharedStreamOwner("gex-acquire", key);
        releasedAcquireOwnerKeys.add(key);
      }
      for (const key of Object.keys(state.loadingGexKeys || {})) {
        delete state.loadingGexKeys[key];
        releaseSharedStreamOwner("gex-read", key);
        releasedReadOwnerKeys.add(key);
      }
      if (stoppedStreamKey && !releasedReadOwnerKeys.has(stoppedStreamKey)) {
        releaseSharedStreamOwner("gex-read", stoppedStreamKey);
        releasedReadOwnerKeys.add(stoppedStreamKey);
      }
      state.loadingGex = false;
      if (gexSnapshotRenderTimer) window.clearTimeout(gexSnapshotRenderTimer);
      if (gexSnapshotRenderFrame) window.cancelAnimationFrame(gexSnapshotRenderFrame);
      gexSnapshotRenderTimer = 0;
      gexSnapshotRenderFrame = 0;
      gexSnapshotRenderScopeKey = "";
      gexSnapshotSidebarOffVisualKey = "";
      if (options.pruneRouteCaches === true) {
        const retainedKeys = new Set();
        if (options.preserveCurrentRoute === true) {
          for (const captureMode of ["request", "live"]) {
            const key = gexContextKey(captureMode);
            if (key) retainedKeys.add(key);
          }
        }
        const activePayload = state.gexContext || state.snapshot?.gex || null;
        const activeKey = activePayload
          ? gexContextKey(activePayload.capture_mode, activePayload)
          : "";
        const retiredKeys = new Set([
          ...Object.keys(state.gexContexts || {}),
          ...Object.keys(state.gexReadModelHandoffTimers || {}),
          ...Object.keys(state.gexInitialAcquireKeys || {}),
          ...Object.keys(state.loadingGexKeys || {}),
          ...sharedGexReadModelThrottleByScope.keys(),
          ...[stoppedStreamKey, refresh.requestKey, activeKey].filter(Boolean),
        ]);
        for (const [key, throttle] of sharedGexReadModelThrottleByScope) {
          if (retainedKeys.has(key)) continue;
          sharedGexReadModelThrottleByScope.delete(key);
        }
        for (const key of Object.keys(state.gexContexts || {})) {
          if (!retainedKeys.has(key)) delete state.gexContexts[key];
        }
        for (const key of retiredKeys) {
          if (retainedKeys.has(key)) continue;
          if (!releasedReadOwnerKeys.has(key)) {
            releaseSharedStreamOwner("gex-read", key);
            releasedReadOwnerKeys.add(key);
          }
          if (!releasedAcquireOwnerKeys.has(key)) {
            releaseSharedStreamOwner("gex-acquire", key);
            releasedAcquireOwnerKeys.add(key);
          }
        }
        if (!retainedKeys.has(refresh.requestKey)) {
          refresh.active = false;
          refresh.requestKey = "";
        }
        if (activePayload && !retainedKeys.has(activeKey)) {
          state.gexContext = null;
          if (state.snapshot?.gex) delete state.snapshot.gex;
        }
        state.loadingGex = Boolean(state.loadingGexKeys[gexContextKey()]);
      }
      if (
        refresh.active
        && refresh.requestKey
        && refresh.requestKey === gexContextKey("request")
        && gexLayerVisible()
        && !state.serverSleeping
      ) {
        scheduleGexManualRefreshPoll(refresh.requestKey);
      }
    }

    let gexSnapshotRenderFrame = 0;
    let gexSnapshotRenderTimer = 0;
    let gexSnapshotRenderScopeKey = "";
    let gexSnapshotSidebarOffVisualKey = "";

    function renderGexSnapshot(options = {}) {
      if (!state.snapshot) return;
      if (typeof renderGexSidebarChrome === "function") renderGexSidebarChrome();
      const renderScopeKey = gexContextKey();
      if (gexSnapshotRenderScopeKey && gexSnapshotRenderScopeKey !== renderScopeKey) {
        if (gexSnapshotRenderTimer) window.clearTimeout(gexSnapshotRenderTimer);
        if (gexSnapshotRenderFrame) window.cancelAnimationFrame(gexSnapshotRenderFrame);
        gexSnapshotRenderTimer = 0;
        gexSnapshotRenderFrame = 0;
        gexSnapshotRenderScopeKey = "";
        gexSnapshotSidebarOffVisualKey = "";
      }
      const cfg = state.indicators?.gexContext || {};
      const visualFrameVisible = gexLayerVisible()
        && (Boolean(cfg.profile) || gexZoneMode(cfg.zoneStyle) !== "off");
      if (
        !visualFrameVisible
        && ["gex-stream", "gex-shared-read-model", "gex-history-stream"].includes(options.reason)
      ) {
        if (gexSnapshotRenderScopeKey === renderScopeKey) {
          if (gexSnapshotRenderTimer) window.clearTimeout(gexSnapshotRenderTimer);
          if (gexSnapshotRenderFrame) window.cancelAnimationFrame(gexSnapshotRenderFrame);
          gexSnapshotRenderTimer = 0;
          gexSnapshotRenderFrame = 0;
          gexSnapshotRenderScopeKey = "";
        }
        gexSnapshotSidebarOffVisualKey = "";
        return;
      }
      // Live-stream and shared-read-model updates carry updated GEX levels in the
      // dedicated GEX surfaces only; the background, general indicators, user
      // objects, trading, and interaction canvases have not changed.
      const isLiveOverlayOnly = options.reason === "gex-stream"
        || options.reason === "gex-shared-read-model";
      if (isLiveOverlayOnly && typeof renderPriceGexLayers === "function") {
        if (!renderScopeKey) return;
        const sidebarOffVisualKey = !cfg.profile ? gexSidebarOffVisualKey(state.snapshot) : "";
        if (
          sidebarOffVisualKey
          && sidebarOffVisualKey === gexSnapshotSidebarOffVisualKey
        ) {
          if (window.mcTelemetryInc) window.mcTelemetryInc("gex.render.skipped_unchanged");
          return;
        }
        if (gexSnapshotRenderTimer || gexSnapshotRenderFrame) return;
        gexSnapshotRenderScopeKey = renderScopeKey;
        const lifecycleGeneration = currentGexLiveState().lifecycleGeneration;
        const renderTimer = window.setTimeout(() => {
          if (
            currentGexLiveState().lifecycleGeneration !== lifecycleGeneration
            || gexSnapshotRenderTimer !== renderTimer
          ) return;
          gexSnapshotRenderTimer = 0;
          const liveCfg = state.indicators?.gexContext || {};
          if (
            !state.snapshot
            || gexSnapshotRenderScopeKey !== renderScopeKey
            || gexContextKey() !== renderScopeKey
            || !gexLayerVisible()
            || (!liveCfg.profile && gexZoneMode(liveCfg.zoneStyle) === "off")
          ) {
            gexSnapshotRenderScopeKey = "";
            return;
          }
          const renderFrame = window.requestAnimationFrame(() => {
            if (
              currentGexLiveState().lifecycleGeneration !== lifecycleGeneration
              || gexSnapshotRenderFrame !== renderFrame
            ) return;
            gexSnapshotRenderFrame = 0;
            const frameCfg = state.indicators?.gexContext || {};
            if (
              !state.snapshot
              || gexSnapshotRenderScopeKey !== renderScopeKey
              || gexContextKey() !== renderScopeKey
              || !gexLayerVisible()
              || (!frameCfg.profile && gexZoneMode(frameCfg.zoneStyle) === "off")
            ) {
              gexSnapshotRenderScopeKey = "";
              return;
            }
            const frameVisualKey = !frameCfg.profile ? gexSidebarOffVisualKey(state.snapshot) : "";
            if (
              frameVisualKey
              && frameVisualKey === gexSnapshotSidebarOffVisualKey
            ) {
              gexSnapshotRenderScopeKey = "";
              if (window.mcTelemetryInc) window.mcTelemetryInc("gex.render.skipped_unchanged");
              return;
            }
            renderPriceGexLayers(state.snapshot);
            gexSnapshotSidebarOffVisualKey = frameVisualKey;
            gexSnapshotRenderScopeKey = "";
          });
          gexSnapshotRenderFrame = renderFrame;
        }, 120);
        gexSnapshotRenderTimer = renderTimer;
      } else {
        if (gexSnapshotRenderTimer) window.clearTimeout(gexSnapshotRenderTimer);
        if (gexSnapshotRenderFrame) window.cancelAnimationFrame(gexSnapshotRenderFrame);
        gexSnapshotRenderTimer = 0;
        gexSnapshotRenderFrame = 0;
        gexSnapshotRenderScopeKey = "";
        gexSnapshotSidebarOffVisualKey = "";
        renderCharts(state.snapshot, options);
      }
    }

    function setGexManualRefresh(status, requestKey, message = "", payload = null, options = {}) {
      const refresh = currentGexManualRefresh();
      const now = Date.now();
      if (refresh.pollTimer) {
        window.clearTimeout(refresh.pollTimer);
        refresh.pollTimer = null;
      }
      refresh.status = status;
      refresh.message = message;
      refresh.requestKey = requestKey || refresh.requestKey || gexContextKey();
      refresh.active = status === "requesting" || status === "running" || status === "queued";
      if (status === "requesting") refresh.startedAt = now;
      if (!refresh.active) refresh.finishedAt = now;
      if (payload) refresh.capturedAt = gexContextCapturedAt(payload) || refresh.capturedAt || "";
      applySettings();
      if (options.render !== false) renderGexSnapshot({ overlayImmediate: true });
    }

    function scheduleGexManualRefreshPoll(requestKey) {
      const refresh = currentGexManualRefresh();
      if (!refresh.active || refresh.requestKey !== requestKey || refresh.pollTimer) return;
      const lifecycleGeneration = currentGexLiveState().lifecycleGeneration;
      const pollTimer = window.setTimeout(async () => {
        if (
          currentGexLiveState().lifecycleGeneration !== lifecycleGeneration
          || refresh.pollTimer !== pollTimer
        ) return;
        refresh.pollTimer = null;
        if (!refresh.active || refresh.requestKey !== requestKey) return;
        if (gexContextKey("request") !== requestKey) {
          setGexManualRefresh("cancelled", requestKey, "Stopped tracking GEX refresh after the qualified instrument route changed.");
          return;
        }
        await loadGexContext({ refresh: false, bypassCache: true, refreshPoll: true });
      }, 1800);
      refresh.pollTimer = pollTimer;
    }

    function syncGexManualRefreshFromPayload(payload, requestKey, options = {}) {
      const refresh = currentGexManualRefresh();
      const refreshStatus = typeof payload?.refresh_status === "string" ? payload.refresh_status : "";
      if (options.refresh) {
        if (!payload?.ok && !payload?.refresh_running && !payload?.refresh_queued && refreshStatus !== "unavailable") {
          setGexManualRefresh("error", requestKey, apiErrorMessage(payload, "GEX refresh failed."), payload, { render: options.renderManualRefresh !== false });
          return;
        }
        if (payload?.refresh_running || payload?.refresh_queued) {
          setGexManualRefresh("running", requestKey, payload?.message || "GEX refresh is running.", payload, { render: options.renderManualRefresh !== false });
          scheduleGexManualRefreshPoll(requestKey);
          return;
        }
        const outcome = refreshStatus || (payload?.ok === false ? "error" : "published");
        setGexManualRefresh(outcome, requestKey, payload?.refresh_error || payload?.message || "GEX refresh finished.", payload, { render: options.renderManualRefresh !== false });
        return;
      }
      if (!refresh.active || refresh.requestKey !== requestKey) return;
      if (payload?.refresh_running) {
        setGexManualRefresh("running", requestKey, payload?.message || "GEX refresh is running.", payload, { render: options.renderManualRefresh !== false });
        scheduleGexManualRefreshPoll(requestKey);
        return;
      }
      if (["blocked", "cancelled", "error", "rejected", "unavailable"].includes(refreshStatus)) {
        setGexManualRefresh(refreshStatus, requestKey, payload?.refresh_error || payload?.message || "GEX refresh did not publish a new snapshot.", payload, { render: options.renderManualRefresh !== false });
        return;
      }
      setGexManualRefresh(
        refreshStatus === "published" ? "published" : payload?.ok === false ? "error" : "rejected",
        requestKey,
        payload?.refresh_error || payload?.message || (payload?.ok === false ? apiErrorMessage(payload, "GEX refresh failed.") : "GEX refresh did not publish a new snapshot."),
        payload,
        { render: options.renderManualRefresh !== false },
      );
    }

    function runPendingGexMarketRecalc() {
      if (!state.pendingGexRecalcCapturedAt || state.serverSleeping || !snapshotMatchesCurrentRoute()) return;
      if (state.requests.market.loading || state.requests.analysis.loading) {
        debugStep("gex recalc deferred", "market or analysis is already loading");
        return;
      }
      const capturedAt = state.pendingGexRecalcCapturedAt;
      state.pendingGexRecalcCapturedAt = "";
      debugStep("gex recalc", `new snapshot ${capturedAt}`);
      reloadCurrentAnalysisOnly("gex-context-update", { renderImmediate: false });
    }

    function scheduleGexMarketRecalc(capturedAt) {
      state.pendingGexRecalcCapturedAt = capturedAt;
      debugStep("gex recalc queued", `new snapshot ${capturedAt}`);
      runPendingGexMarketRecalc();
    }

    function maybeRecalculateMarketForGexUpdate(payload, previousPayload) {
      const nextCapturedAt = gexPayloadRevision(payload);
      const previousCapturedAt = gexPayloadRevision(previousPayload);
      const authorityDowngraded = Boolean(
        previousPayload?.decision_authoritative === true
        && payload?.decision_authoritative === false
        && nextCapturedAt
        && nextCapturedAt === previousCapturedAt
      );
      if (
        (!payload?.ok && !authorityDowngraded)
        || !nextCapturedAt
        || (nextCapturedAt === previousCapturedAt && !authorityDowngraded)
      ) return;
      if (!previousCapturedAt && !payload?.live?.active) return;
      if (!gexLayerVisible() || !snapshotMatchesCurrentRoute()) return;
      if (
        state.lastGexRecalcCapturedAt === nextCapturedAt
        && !authorityDowngraded
      ) return;
      state.lastGexRecalcCapturedAt = nextCapturedAt;
      scheduleGexMarketRecalc(nextCapturedAt);
    }

    function clearGexOptionUniverseExpiryTimer() {
      gexOptionUniverseExpiryGeneration += 1;
      if (gexOptionUniverseExpiryTimer) {
        window.clearTimeout(gexOptionUniverseExpiryTimer);
        gexOptionUniverseExpiryTimer = 0;
      }
    }

    function scheduleGexOptionUniverseExpiry(payload) {
      clearGexOptionUniverseExpiryTimer();
      if (payload?.decision_authoritative !== true) return;
      const expiryMs = gexOptionUniverseExpiryMs(payload);
      if (expiryMs === null) return;
      const expectedGeneration = gexOptionUniverseExpiryGeneration;
      const expectedRevision = gexPayloadRevision(payload);
      const expectedExpiryAt = payload.option_universe_expires_at;
      const delayMs = Math.max(0, expiryMs - Date.now());
      gexOptionUniverseExpiryTimer = window.setTimeout(() => {
        if (
          gexOptionUniverseExpiryGeneration !== expectedGeneration
          || state.gexContext !== payload
        ) return;
        gexOptionUniverseExpiryTimer = 0;
        if (Date.now() < expiryMs) {
          scheduleGexOptionUniverseExpiry(payload);
          return;
        }
        const current = state.gexContext;
        if (
          current?.decision_authoritative !== true
          || gexPayloadRevision(current) !== expectedRevision
          || current?.option_universe_expires_at !== expectedExpiryAt
        ) return;
        setActiveGexContext(current, { suppressMarketRecalc: true });
        renderGexSnapshot({
          overlayImmediate: true,
          reason: "gex-shared-read-model",
        });
        renderSystemHealth();
        debugStep("gex authority expired", expectedExpiryAt);
      }, Math.min(delayMs, 2_147_483_647));
    }

    function setActiveGexContext(payload, options = {}) {
      const previousPayload = state.gexContext;
      let nextPayload = payload || null;
      let authorityDowngraded = false;
      if (nextPayload?.decision_authoritative === true) {
        const expiryMs = gexOptionUniverseExpiryMs(nextPayload);
        if (expiryMs === null || expiryMs <= Date.now()) {
          const expiryAt = expiryMs === null
            ? null
            : nextPayload.option_universe_expires_at;
          const normalized = expiryAt === null
            ? { ...nextPayload, option_universe_expires_at: null }
            : nextPayload;
          const message = expiryAt === null
            ? "GEX option universe has no exact active provider expiry; showing levels as display-only."
            : `GEX option universe expired at ${expiryAt}; showing levels as display-only.`;
          nextPayload = preservedGexContextWithStaleNotice(normalized, {
            status: "stale",
            message,
            latest_attempt: {
              source: normalized.source,
              capture_mode: normalized.capture_mode,
              captured_at: normalized.captured_at,
              status: "stale",
              message,
              diagnostics: {
                reason: expiryAt === null
                  ? "option_universe_expiry_unknown"
                  : "option_universe_expired",
                option_universe_expires_at: expiryAt,
              },
            },
          });
          authorityDowngraded = true;
        }
      }
      const authorityTransitioned = Boolean(
        previousPayload?.decision_authoritative === true
        && nextPayload?.decision_authoritative === false
        && gexPayloadRevision(previousPayload)
        && gexPayloadRevision(previousPayload) === gexPayloadRevision(nextPayload)
      );
      updateGexMotionEvents(nextPayload, previousPayload);
      state.gexContext = nextPayload;
      if (authorityDowngraded || authorityTransitioned) {
        const requestKey = gexContextKey(nextPayload.capture_mode, nextPayload);
        if (requestKey) state.gexContexts[requestKey] = nextPayload;
        shareGexReadModel(nextPayload);
      }
      scheduleGexOptionUniverseExpiry(state.gexContext);
      if (!state.snapshot) return;
      if (nextPayload) state.snapshot.gex = nextPayload;
      else if (state.snapshot.gex) delete state.snapshot.gex;
      if (
        !options.suppressMarketRecalc
        || authorityDowngraded
        || authorityTransitioned
      ) {
        maybeRecalculateMarketForGexUpdate(nextPayload, previousPayload);
      }
    }

    function gexContextHasDisplayData(payload) {
      if (!payload) return false;
      return Array.isArray(payload.levels) && payload.levels.length > 0;
    }

    function gexContextMatchesRequestKey(payload, requestKey) {
      return requestKey === gexContextKey()
        && exactIdentityText(payload?.instrument_id) === exactIdentityText(state.instrumentId)
        && exactIdentityText(payload?.route_fingerprint) === instrumentRouteFingerprint()
        && Boolean(gexPayloadProviderSymbol(payload));
    }

    function gexContextMatchesCaptureMode(payload, captureMode) {
      const expectedMode = typeof captureMode === "string" ? captureMode : "";
      if (!["request", "live"].includes(expectedMode)) return false;
      const expectedSource = GEX_SOURCE_BY_CAPTURE_MODE[expectedMode];
      return payload?.capture_mode === expectedMode
        && payload?.source === expectedSource;
    }

    function lastUsableGexHistoryContext(payload, captureMode) {
      const history = Array.isArray(payload?.history) ? payload.history : [];
      const instrumentId = exactIdentityText(payload?.instrument_id);
      const routeFingerprint = exactIdentityText(payload?.route_fingerprint);
      const providerSymbol = gexPayloadProviderSymbol(payload);
      if (!instrumentId || !routeFingerprint || !providerSymbol) return null;
      for (let index = history.length - 1; index >= 0; index -= 1) {
        const candidate = history[index];
        if (
          gexHistoryRowIsCanonical(candidate)
          && gexContextHasDisplayData(candidate)
          && gexContextMatchesCaptureMode(candidate, captureMode)
        ) return {
          ...candidate,
          instrument_id: instrumentId,
          route_fingerprint: routeFingerprint,
          provider_symbol: providerSymbol,
          expiry_profile: [],
          decision_authoritative: false,
        };
      }
      return null;
    }

    function preservedGexContextWithStaleNotice(previous, payload = {}, options = {}) {
      const typedPrevious = { ...(previous || {}) };
      delete typedPrevious.raw;
      const status = String(payload?.status || "empty").trim() || "empty";
      const message = String(payload?.message || payload?.error?.message || status).trim();
      const capturedAt = gexContextCapturedAt(previous);
      const displaySource = String(options.displaySource || "previous");
      const displayLabel = displaySource === "history" ? "last usable history context" : "previous context";
      const rawAttempt = payload?.latest_attempt && typeof payload.latest_attempt === "object"
        ? payload.latest_attempt
        : payload;
      const latestAttempt = {
        source: typeof rawAttempt?.source === "string" ? rawAttempt.source : null,
        capture_mode: typeof rawAttempt?.capture_mode === "string" ? rawAttempt.capture_mode : null,
        captured_at: typeof rawAttempt?.captured_at === "string" ? rawAttempt.captured_at : null,
        status: typeof rawAttempt?.status === "string" ? rawAttempt.status : status,
        message: typeof rawAttempt?.message === "string" ? rawAttempt.message : message,
        diagnostics: rawAttempt?.diagnostics && typeof rawAttempt.diagnostics === "object" ? rawAttempt.diagnostics : null,
      };
      return {
        ...typedPrevious,
        ok: gexContextHasDisplayData(previous),
        instrument_id: previous?.instrument_id,
        route_fingerprint: previous?.route_fingerprint,
        status: "stale",
        stale: true,
        decision_authoritative: false,
        preserved_context: true,
        display_context_source: displaySource,
        display_context_captured_at: capturedAt,
        preserved_reason: status,
        latest_attempt: latestAttempt,
        message: `Latest GEX update returned ${status}; showing ${displayLabel}${capturedAt ? ` from ${capturedAt}` : ""}. ${message}`,
      };
    }

    function preserveUsableGexContextOnBackgroundPoll(payload, requestKey, options = {}) {
      if (gexContextHasDisplayData(payload)) return false;
      const captureMode = typeof options.captureMode === "string" ? options.captureMode : "";
      const cached = state.gexContexts[requestKey] || null;
      const active = gexContextMatchesRequestKey(state.gexContext, requestKey) ? state.gexContext : null;
      const previous = gexContextHasDisplayData(cached) && gexContextMatchesCaptureMode(cached, captureMode)
        ? cached
        : gexContextHasDisplayData(active) && gexContextMatchesCaptureMode(active, captureMode)
          ? active
          : null;
      const historyContext = lastUsableGexHistoryContext(payload, captureMode);
      const reusable = gexContextHasDisplayData(previous) ? previous : historyContext;
      if (!gexContextHasDisplayData(reusable)) return false;
      debugStep("gex poll kept previous", String(payload?.status || payload?.message || "empty context"));
      const preserved = preservedGexContextWithStaleNotice(reusable, payload, {
        displaySource: reusable === historyContext ? "history" : "previous",
      });
      state.gexContexts[requestKey] = preserved;
      if (gexContextKey() === requestKey) setActiveGexContext(preserved, { ...options, suppressMarketRecalc: true });
      return true;
    }

    function gexContextPollingActive(options = {}) {
      if (options.refresh || options.refreshPoll) return true;
      if (gexLiveModeActive()) return false;
      return acquireSharedStreamOwner("gex-read", gexContextKey("request"));
    }

    function clearGexReadModelHandoff(requestKey) {
      const timer = state.gexReadModelHandoffTimers[requestKey];
      if (timer) window.clearTimeout(timer);
      delete state.gexReadModelHandoffTimers[requestKey];
    }

    function queueGexReadModelHandoff(requestKey) {
      if (!requestKey || state.gexReadModelHandoffTimers[requestKey]) return;
      const lifecycleGeneration = currentGexLiveState().lifecycleGeneration;
      const owner = readSharedStreamOwner("gex-read", requestKey);
      const delayMs = Math.max(Number(owner.expires || 0) - Date.now() + 25, 25);
      const timer = window.setTimeout(() => {
        if (state.gexReadModelHandoffTimers[requestKey] !== timer) return;
        delete state.gexReadModelHandoffTimers[requestKey];
        if (currentGexLiveState().lifecycleGeneration !== lifecycleGeneration) return;
        if (gexContextKey("request") !== requestKey || !gexLayerVisible() || state.serverSleeping) return;
        const pending = state.gexContexts[requestKey];
        if (pending?.local_only !== true || pending.status !== "loading") return;
        loadGexContext({ force: true, bypassCache: true, leaseHandoff: true });
      }, Math.min(delayMs, SHARED_STREAM_OWNER_TTL_MS + 25));
      state.gexReadModelHandoffTimers[requestKey] = timer;
    }

    function queueInitialGexAcquire(requestKey) {
      if (!requestKey || state.gexInitialAcquireKeys[requestKey]) return;
      if (!acquireSharedStreamOwner("gex-acquire", requestKey)) return;
      const lifecycleGeneration = currentGexLiveState().lifecycleGeneration;
      const acquireToken = { lifecycleGeneration, timer: null };
      state.gexInitialAcquireKeys[requestKey] = acquireToken;
      const timer = window.setTimeout(() => {
        if (state.gexInitialAcquireKeys[requestKey] !== acquireToken) return;
        acquireToken.timer = null;
        if (
          currentGexLiveState().lifecycleGeneration !== lifecycleGeneration
          || gexContextKey("request") !== requestKey
          || !gexLayerVisible()
          || state.serverSleeping
        ) {
          delete state.gexInitialAcquireKeys[requestKey];
          releaseSharedStreamOwner("gex-acquire", requestKey);
          return;
        }
        void loadGexContext({ refresh: true, bypassCache: true, initialAcquire: true }).finally(() => {
          if (state.gexInitialAcquireKeys[requestKey] !== acquireToken) return;
          delete state.gexInitialAcquireKeys[requestKey];
          releaseSharedStreamOwner("gex-acquire", requestKey);
        });
      }, 0);
      acquireToken.timer = timer;
    }

    async function loadGexContext(options = {}) {
      if (state.serverSleeping) {
        if (gexLiveModeActive()) stopGexLiveContext();
        clearGexContext();
        renderGexSnapshot();
        debugStep("gex skipped", "server sleeping");
        return null;
      }
      if (!gexLayerVisible()) {
        if (gexLiveModeActive()) stopGexLiveContext();
        clearGexContext();
        renderGexSnapshot();
        return null;
      }
      const requestInstrumentId = exactIdentityText(state.instrumentId);
      const requestRouteFingerprint = instrumentRouteFingerprint();
      const requestCaptureMode = options.refresh || options.refreshPoll ? "request" : effectiveGexContextMode();
      const requestKey = gexContextKey(requestCaptureMode);
      if (!requestKey) {
        clearGexContext();
        renderGexSnapshot();
        return null;
      }
      if (requestCaptureMode === "live") {
        connectGexLiveStream();
        return state.gexContexts[requestKey] || state.gexContext;
      }
      if (!options.refresh && !options.bypassCache && state.gexContexts[requestKey]) {
        setActiveGexContext(state.gexContexts[requestKey]);
        renderGexSnapshot();
        return state.gexContext;
      }
      if (!gexContextPollingActive(options)) {
        shareReadModel("gex_context", {
          instrument_id: requestInstrumentId,
          route_fingerprint: requestRouteFingerprint,
          capture_mode: requestCaptureMode,
        });
        const cached = state.gexContexts[requestKey] || null;
        if (
          gexPayloadMatchesRequest(cached, requestInstrumentId, requestRouteFingerprint, requestCaptureMode)
          || gexStatusPayloadMatchesRequest(cached, requestInstrumentId, requestRouteFingerprint, requestCaptureMode)
        ) return cached;
        const pending = cached?.local_only === true && cached.status === "loading" ? cached : {
          ok: false,
          enabled: true,
          local_only: true,
          provider_symbol: exactIdentityText(instrumentForId(requestInstrumentId)?.provider_symbol),
          instrument_id: requestInstrumentId,
          route_fingerprint: requestRouteFingerprint,
          source: GEX_SOURCE_BY_CAPTURE_MODE[requestCaptureMode],
          capture_mode: requestCaptureMode,
          status: "loading",
          message: "Waiting for the shared GEX read model from the producing tab.",
          levels: [],
          expiry_profile: [],
        };
        state.gexContexts[requestKey] = pending;
        queueGexReadModelHandoff(requestKey);
        if (gexContextKey() === requestKey) setActiveGexContext(pending, { suppressMarketRecalc: true });
        renderGexSnapshot({ overlayImmediate: true });
        return pending;
      }
      if (state.loadingGexKeys[requestKey]) return state.gexContexts[requestKey] || state.gexContext;
      const requestLifecycleGeneration = currentGexLiveState().lifecycleGeneration;
      const loadToken = { lifecycleGeneration: requestLifecycleGeneration };
      if (options.refresh) {
        setGexManualRefresh(
          "requesting",
          requestKey,
          options.initialAcquire ? "Acquiring the first GEX option-chain snapshot..." : "Sending GEX refresh request...",
        );
      }
      state.loadingGexKeys[requestKey] = loadToken;
      state.loadingGex = Boolean(state.loadingGexKeys[gexContextKey()]);
      applySettings();
      const params = new URLSearchParams({
        instrument_id: requestInstrumentId,
        expected_route_fingerprint: requestRouteFingerprint,
        enabled: "true",
        refresh: options.refresh ? "true" : "false",
      });
      try {
        const payload = await fetchJson(`/api/gex?${params.toString()}`, {
          cache: "no-store",
          sharedTtlMs: options.refresh ? 0 : 2500,
          timeoutMs: options.refresh ? 75000 : 8000,
        });
        if (
          currentGexLiveState().lifecycleGeneration
          !== requestLifecycleGeneration
        ) return null;
        if (gexStatusPayloadMatchesRequest(
          payload,
          requestInstrumentId,
          requestRouteFingerprint,
          "request",
        )) {
          clearGexReadModelHandoff(requestKey);
          if (preserveUsableGexContextOnBackgroundPoll(payload, requestKey, { ...options, captureMode: requestCaptureMode })) {
            syncGexManualRefreshFromPayload(payload, requestKey, { ...options, renderManualRefresh: false });
            shareGexReadModel(state.gexContexts[requestKey]);
            renderGexSnapshot();
            renderSystemHealth();
            return state.gexContexts[requestKey] || state.gexContext;
          }
          state.gexContexts[requestKey] = payload;
          syncGexManualRefreshFromPayload(payload, requestKey, { ...options, renderManualRefresh: false });
          shareGexReadModel(payload);
          if (
            exactIdentityText(state.instrumentId) === requestInstrumentId
            && instrumentRouteFingerprint() === requestRouteFingerprint
            && gexContextKey() === requestKey
          ) setActiveGexContext(payload, { suppressMarketRecalc: true });
          renderGexSnapshot({ overlayImmediate: true });
          renderSystemHealth();
          if (
            payload.status === "missing"
            && payload.refresh_running !== true
            && payload.refresh_queued !== true
            && !options.refresh
            && !options.refreshPoll
          ) queueInitialGexAcquire(requestKey);
          return payload;
        }
        if (!gexPayloadMatchesRequest(payload, requestInstrumentId, requestRouteFingerprint, "request")) {
          clearGexReadModelHandoff(requestKey);
          const mismatchPayload = gexRouteMismatchPayload(payload, requestInstrumentId, requestRouteFingerprint, "request");
          if (preserveUsableGexContextOnBackgroundPoll(mismatchPayload, requestKey, { ...options, bypassCache: true, captureMode: requestCaptureMode })) {
            syncGexManualRefreshFromPayload(mismatchPayload, requestKey, { ...options, renderManualRefresh: false });
            renderGexSnapshot();
            renderSystemHealth();
            return state.gexContexts[requestKey] || state.gexContext;
          }
          state.gexContexts[requestKey] = mismatchPayload;
          syncGexManualRefreshFromPayload(mismatchPayload, requestKey, { ...options, renderManualRefresh: false });
          if (
            exactIdentityText(state.instrumentId) !== requestInstrumentId
            || instrumentRouteFingerprint() !== requestRouteFingerprint
            || gexContextKey() !== requestKey
          ) return mismatchPayload;
          setActiveGexContext(mismatchPayload, { suppressMarketRecalc: Boolean(options.refresh) });
          renderGexSnapshot();
          renderSystemHealth();
          return mismatchPayload;
        }
        if (preserveUsableGexContextOnBackgroundPoll(payload, requestKey, { ...options, captureMode: requestCaptureMode })) {
          syncGexManualRefreshFromPayload(payload, requestKey, { ...options, renderManualRefresh: false });
          renderGexSnapshot();
          renderSystemHealth();
          return state.gexContexts[requestKey] || state.gexContext;
        }
        state.gexContexts[requestKey] = payload;
        clearGexReadModelHandoff(requestKey);
        shareGexReadModel(payload);
        syncGexManualRefreshFromPayload(payload, requestKey, { ...options, renderManualRefresh: false });
        if (
          exactIdentityText(state.instrumentId) !== requestInstrumentId
          || instrumentRouteFingerprint() !== requestRouteFingerprint
          || gexContextKey() !== requestKey
        ) return payload;
        setActiveGexContext(payload, { suppressMarketRecalc: Boolean(options.refresh) });
        renderGexSnapshot();
        renderSystemHealth();
        return payload;
      } catch (error) {
        if (
          currentGexLiveState().lifecycleGeneration
          !== requestLifecycleGeneration
        ) return null;
        const aborted = error?.name === "AbortError";
        const payload = {
          ok: false,
          enabled: true,
          local_only: true,
          provider_symbol: exactIdentityText(instrumentForId(requestInstrumentId)?.provider_symbol),
          instrument_id: requestInstrumentId,
          route_fingerprint: requestRouteFingerprint,
          status: aborted ? "timeout" : "error",
          message: aborted
            ? "GEX request timed out in the browser; broker refresh may still finish on the server."
            : requestErrorMessage(error, "GEX request failed"),
          levels: [],
          expiry_profile: [],
          source: GEX_SOURCE_BY_CAPTURE_MODE[requestCaptureMode],
          capture_mode: requestCaptureMode,
        };
        if (preserveUsableGexContextOnBackgroundPoll(payload, requestKey, { ...options, captureMode: requestCaptureMode })) {
          syncGexManualRefreshFromPayload(payload, requestKey, { ...options, renderManualRefresh: false });
          showBrowserToast(apiErrorMessage(payload, "GEX refresh is still running; keeping previous levels"));
          renderGexSnapshot();
          renderSystemHealth();
          return state.gexContexts[requestKey] || state.gexContext;
        }
        state.gexContexts[requestKey] = payload;
        clearGexReadModelHandoff(requestKey);
        syncGexManualRefreshFromPayload(payload, requestKey, { ...options, renderManualRefresh: false });
        if (
          exactIdentityText(state.instrumentId) !== requestInstrumentId
          || instrumentRouteFingerprint() !== requestRouteFingerprint
          || gexContextKey() !== requestKey
        ) return payload;
        setActiveGexContext(payload, { suppressMarketRecalc: Boolean(options.refresh) });
        renderGexSnapshot();
        return state.gexContext;
      } finally {
        if (state.loadingGexKeys[requestKey] === loadToken) {
          delete state.loadingGexKeys[requestKey];
          state.loadingGex = Boolean(state.loadingGexKeys[gexContextKey()]);
          applySettings();
        }
      }
    }
