    const MTF_LENS_CONSUMER_ROLE = "secondary_candles";
    const MTF_LENS_TAIL_BARS = 300;
    const MTF_LENS_MAX_BARS = 500;
    const MTF_LENS_CANVAS_PIXEL_BUDGET = 10000000;
    const MTF_LENS_MIN_WIDTH = 320;
    const MTF_LENS_MIN_HEIGHT = 190;
    const MTF_LENS_MAX_PARENT_RATIO = 0.7;
    const MTF_LENS_WIDTH_SETTING = "aef:mtfLensWidth";
    const MTF_LENS_HEIGHT_SETTING = "aef:mtfLensHeight";
    const MTF_LENS_ANCHOR_SETTING = "aef:mtfLensAnchor";
    const MTF_LENS_DRAWING_REFRESH_MAX_ATTEMPTS = 4;
    const MTF_LENS_DRAWING_REFRESH_BASE_DELAY_MS = 160;
    const MTF_LENS_ANCHORS = Object.freeze([
      "bottom-left",
      "bottom-right",
      "top-right",
      "top-left",
    ]);

    const mtfLensRuntime = {
      open: false,
      interval: "",
      scope: null,
      epoch: 0,
      connectionEpoch: 0,
      generation: 0,
      sequence: 0,
      canonicalRevision: 0,
      barsVersion: 0,
      chartFrameReady: false,
      socket: null,
      reconnectTimer: null,
      staleTimer: null,
      reconnectAttempt: 0,
      reconnectBlocked: false,
      reconnectBlockedForSleep: false,
      blockedSleepObserved: false,
      barsByTs: new Map(),
      orderedBars: [],
      renderFrame: 0,
      interactionFrame: 0,
      resizeFrame: 0,
      resizePointer: null,
      resizeObserver: null,
      geometry: null,
      crosshair: { visible: false, x: 0, y: 0 },
      drawings: [],
      drawingsHydrated: false,
      drawingExactRefreshPending: false,
      drawingRequest: null,
      drawingRequestEpoch: 0,
      drawingReloadQueued: false,
      drawingRefreshTimer: null,
      drawingRefreshAttempts: 0,
      drawingRefreshScopeKey: "",
    };

    function mtfLensScope(interval = mtfLensRuntime.interval) {
      const instrumentId = exactIdentityText(state.instrumentId);
      const routeFingerprint = instrumentRouteFingerprint();
      const timeframe = String(interval || "").trim();
      if (!instrumentId || !routeFingerprint || !timeframe) return null;
      return Object.freeze({
        instrumentId,
        routeFingerprint,
        timeframe,
        consumerRole: MTF_LENS_CONSUMER_ROLE,
        range: "1d",
        tailBars: MTF_LENS_TAIL_BARS,
        symbol: String(state.symbol || "").trim(),
      });
    }

    function mtfLensScopeEquals(left, right) {
      return Boolean(left && right)
        && left.instrumentId === right.instrumentId
        && left.routeFingerprint === right.routeFingerprint
        && left.timeframe === right.timeframe
        && left.consumerRole === right.consumerRole
        && left.range === right.range
        && left.tailBars === right.tailBars;
    }

    function mtfLensIntervalMinutes(interval) {
      const match = /^(\d+)([mh])$/.exec(String(interval || "").trim());
      if (!match) return 1;
      const value = Math.max(Number(match[1]) || 1, 1);
      return match[2] === "h" ? value * 60 : value;
    }

    function mtfLensDrawingScopeKey(scope = mtfLensRuntime.scope) {
      if (!scope) return "";
      return JSON.stringify([
        exactIdentityText(scope.instrumentId),
        exactIdentityText(scope.routeFingerprint),
        String(scope.timeframe || ""),
      ]);
    }

    function mtfLensDrawingsExpectedScope(scope = mtfLensRuntime.scope) {
      if (!scope) return null;
      return {
        instrumentId: exactIdentityText(scope.instrumentId),
        routeFingerprint: exactIdentityText(scope.routeFingerprint),
        timeframe: String(scope.timeframe || ""),
      };
    }

    function mtfLensDrawingSnapshot(runtime = mtfLensRuntime) {
      const scope = runtime.scope;
      if (!scope || !runtime.chartFrameReady) return null;
      return {
        bars: runtime.orderedBars,
        future_axis: null,
        meta: {
          instrument_id: scope.instrumentId,
          route_fingerprint: scope.routeFingerprint,
          timeframe: scope.timeframe,
          chart_canonical_revision: runtime.canonicalRevision,
        },
      };
    }

    function mtfLensCompleteDrawingProjectionRefresh(runtime = mtfLensRuntime) {
      if (runtime.drawingRefreshTimer) {
        window.clearTimeout(runtime.drawingRefreshTimer);
      }
      runtime.drawingRefreshTimer = null;
      runtime.drawingRefreshAttempts = 0;
      runtime.drawingRefreshScopeKey = "";
    }

    function mtfLensScheduleDrawingProjectionRefresh(runtime = mtfLensRuntime) {
      const scope = runtime.scope;
      if (
        !runtime.open
        || state.serverSleeping
        || !scope
        || !runtime.chartFrameReady
      ) return false;
      const refreshScopeKey = JSON.stringify([
        mtfLensDrawingScopeKey(scope),
        runtime.epoch,
        runtime.connectionEpoch,
        runtime.canonicalRevision,
      ]);
      if (runtime.drawingRefreshScopeKey !== refreshScopeKey) {
        if (runtime.drawingRefreshTimer) {
          window.clearTimeout(runtime.drawingRefreshTimer);
        }
        runtime.drawingRefreshTimer = null;
        runtime.drawingRefreshAttempts = 0;
        runtime.drawingRefreshScopeKey = refreshScopeKey;
      }
      if (
        runtime.drawingRefreshTimer
        || runtime.drawingRefreshAttempts >= MTF_LENS_DRAWING_REFRESH_MAX_ATTEMPTS
      ) return false;
      const delay = Math.min(
        MTF_LENS_DRAWING_REFRESH_BASE_DELAY_MS * (2 ** runtime.drawingRefreshAttempts),
        2000,
      );
      const scopeEpoch = runtime.epoch;
      runtime.drawingRefreshTimer = window.setTimeout(() => {
        runtime.drawingRefreshTimer = null;
        if (
          !runtime.open
          || state.serverSleeping
          || runtime.epoch !== scopeEpoch
          || runtime.drawingRefreshScopeKey !== refreshScopeKey
          || !mtfLensScopeEquals(runtime.scope, scope)
        ) return;
        runtime.drawingRefreshAttempts += 1;
        void mtfLensLoadDrawings(scope, scopeEpoch, {
          force: true,
          reason: "mtf_lens_projection_refresh",
        });
      }, delay);
      return true;
    }

    function mtfLensResolveDrawings(drawings = mtfLensRuntime.drawings, options = {}) {
      const snapshot = mtfLensDrawingSnapshot();
      const expectedScope = mtfLensDrawingsExpectedScope();
      if (!snapshot || !expectedScope || !Array.isArray(drawings)) return false;
      let projectionRefreshRequired = false;
      const changed = resolveDrawingAnchorsAgainstSnapshot(drawings, snapshot, {
        expectedScope: expectedScope,
        onProjectionRefreshRequired: () => {
          projectionRefreshRequired = true;
        },
      });
      if (projectionRefreshRequired) {
        mtfLensScheduleDrawingProjectionRefresh();
      } else if (options.completeRefresh === true && mtfLensRuntime.drawingsHydrated) {
        mtfLensCompleteDrawingProjectionRefresh();
      }
      return changed;
    }

    function mtfLensCancelDrawingRuntime(options = {}, runtime = mtfLensRuntime) {
      if (runtime.drawingRefreshTimer) {
        window.clearTimeout(runtime.drawingRefreshTimer);
      }
      runtime.drawingRefreshTimer = null;
      runtime.drawingRefreshAttempts = 0;
      runtime.drawingRefreshScopeKey = "";
      runtime.drawingRequestEpoch += 1;
      runtime.drawingReloadQueued = false;
      const request = runtime.drawingRequest;
      runtime.drawingRequest = null;
      request?.controller?.abort();
      runtime.drawingsHydrated = false;
      runtime.drawingExactRefreshPending = false;
      if (options.clear !== false) runtime.drawings = [];
    }

    function mtfLensDrawingsForScope(payload, scope) {
      if (
        !payload
        || typeof payload !== "object"
        || Array.isArray(payload)
        || exactIdentityText(payload.instrument_id) !== scope.instrumentId
        || exactIdentityText(payload.route_fingerprint) !== scope.routeFingerprint
        || String(payload.interval || "") !== scope.timeframe
        || !Array.isArray(payload.drawings)
      ) throw new TypeError("MTF_LENS_DRAWING_SCOPE_MISMATCH");
      const drawings = normalizeDrawingObjects(payload.drawings);
      for (const drawing of drawings) {
        if (
          exactIdentityText(drawing.instrument_id) !== scope.instrumentId
          || exactIdentityText(drawing.route_fingerprint) !== scope.routeFingerprint
        ) throw new TypeError(`MTF_LENS_DRAWING_SCOPE_MISMATCH: ${drawing.id}`);
      }
      return drawings;
    }

    function mtfLensLoadDrawings(scope, scopeEpoch, options = {}) {
      const runtime = mtfLensRuntime;
      if (
        !runtime.open
        || state.serverSleeping
        || runtime.epoch !== scopeEpoch
        || !mtfLensScopeEquals(runtime.scope, scope)
      ) return Promise.resolve(false);
      const scopeKey = mtfLensDrawingScopeKey(scope);
      const inFlight = runtime.drawingRequest;
      if (
        inFlight
        && inFlight.scopeKey === scopeKey
        && inFlight.scopeEpoch === scopeEpoch
      ) {
        if (options.force === true) runtime.drawingReloadQueued = true;
        return inFlight.promise;
      }
      if (inFlight) {
        runtime.drawingRequestEpoch += 1;
        runtime.drawingRequest = null;
        inFlight.controller?.abort();
      }
      const controller = new AbortController();
      const requestEpoch = runtime.drawingRequestEpoch + 1;
      runtime.drawingRequestEpoch = requestEpoch;
      const entry = {
        scopeKey,
        scopeEpoch,
        requestEpoch,
        controller,
        promise: null,
      };
      const request = fetchServerStorage(scope.instrumentId, scope.timeframe, {
        settings: false,
        alerts: false,
        expectedRouteFingerprint: scope.routeFingerprint,
        isolated: true,
        signal: controller.signal,
        force: options.force === true,
        reason: String(options.reason || "mtf_lens_drawings"),
      })
        .then(payload => {
          if (
            controller.signal.aborted
            || runtime.drawingRequest !== entry
            || runtime.drawingRequestEpoch !== requestEpoch
            || runtime.epoch !== scopeEpoch
            || mtfLensDrawingScopeKey(runtime.scope) !== scopeKey
          ) return false;
          const drawings = mtfLensDrawingsForScope(payload, scope);
          const previousHydration = runtime.drawingsHydrated;
          runtime.drawingsHydrated = true;
          try {
            mtfLensResolveDrawings(drawings, { completeRefresh: true });
          } catch (error) {
            runtime.drawingsHydrated = previousHydration;
            throw error;
          }
          runtime.drawings = drawings;
          if (!runtime.drawingReloadQueued) {
            runtime.drawingExactRefreshPending = false;
          }
          mtfLensScheduleInteractionRender();
          return true;
        })
        .catch(error => {
          const current = (
            !controller.signal.aborted
            && runtime.drawingRequest === entry
            && runtime.drawingRequestEpoch === requestEpoch
            && runtime.epoch === scopeEpoch
            && mtfLensDrawingScopeKey(runtime.scope) === scopeKey
          );
          if (error?.name === "AbortError") {
            if (current) runtime.drawingReloadQueued = true;
            return false;
          }
          if (current) {
            mtfLensScheduleDrawingProjectionRefresh();
            console.warn(
              "MTF Lens drawing load failed",
              requestErrorMessage(error, "drawing load failed"),
            );
          }
          return false;
        })
        .finally(() => {
          if (runtime.drawingRequest !== entry) return;
          runtime.drawingRequest = null;
          const reloadQueued = runtime.drawingReloadQueued;
          runtime.drawingReloadQueued = false;
          if (
            reloadQueued
            && runtime.open
            && !state.serverSleeping
            && runtime.epoch === scopeEpoch
            && mtfLensDrawingScopeKey(runtime.scope) === scopeKey
          ) {
            void mtfLensLoadDrawings(scope, scopeEpoch, {
              force: true,
              reason: "mtf_lens_drawings_coalesced",
            });
          }
        });
      entry.promise = request;
      runtime.drawingRequest = entry;
      return request;
    }

    function mtfLensHandleSharedObjectsStorageEvent(event) {
      const scope = mtfLensRuntime.scope;
      if (
        !event?.key
        || event.storageArea !== localStorage
        || !mtfLensRuntime.open
        || state.serverSleeping
        || !scope
        || event.key !== sharedObjectsBroadcastKey(
          scope.instrumentId,
          scope.timeframe,
          scope.routeFingerprint,
        )
      ) return false;
      mtfLensRuntime.drawingExactRefreshPending = true;
      void mtfLensLoadDrawings(scope, mtfLensRuntime.epoch, {
        force: true,
        reason: "mtf_lens_objects_broadcast",
      });
      return true;
    }

    function mtfLensNormalizeTailMembership(value) {
      if (!value || typeof value !== "object" || Array.isArray(value)) {
        return { ok: false, reasonCode: "tail_membership_shape_invalid" };
      }
      const keys = Object.keys(value).sort();
      if (keys.length !== 2 || keys[0] !== "limit" || keys[1] !== "timestamps") {
        return { ok: false, reasonCode: "tail_membership_shape_invalid" };
      }
      if (!Number.isSafeInteger(value.limit) || value.limit < 64 || value.limit > 500) {
        return { ok: false, reasonCode: "tail_membership_limit_invalid" };
      }
      if (!Array.isArray(value.timestamps) || value.timestamps.length > value.limit) {
        return { ok: false, reasonCode: "tail_membership_timestamps_invalid" };
      }
      const timestamps = [];
      let previousMs = -Infinity;
      const seen = new Set();
      for (const rawTimestamp of value.timestamps) {
        if (
          typeof rawTimestamp !== "string"
          || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)$/.test(
            rawTimestamp,
          )
        ) return { ok: false, reasonCode: "tail_membership_timestamp_invalid" };
        const timestampMs = Date.parse(rawTimestamp);
        const timestampKey = mtfLensTimestampKey(rawTimestamp);
        if (
          !Number.isFinite(timestampMs)
          || !timestampKey
          || timestampMs <= previousMs
          || seen.has(timestampKey)
        ) return { ok: false, reasonCode: "tail_membership_order_invalid" };
        seen.add(timestampKey);
        timestamps.push(timestampKey);
        previousMs = timestampMs;
      }
      return {
        ok: true,
        value: Object.freeze({
          limit: value.limit,
          timestamps: Object.freeze(timestamps),
        }),
      };
    }

    function mtfLensMessageAdmission(message, expectedScope, currentOrder = {}) {
      if (!message || typeof message !== "object" || Array.isArray(message) || !expectedScope) {
        return { ok: false, reasonCode: "payload_invalid" };
      }
      if (
        exactIdentityText(message.instrument_id) !== expectedScope.instrumentId
        || exactIdentityText(message.route_fingerprint) !== expectedScope.routeFingerprint
        || String(message.interval || "") !== expectedScope.timeframe
        || String(message.consumer_role || "") !== expectedScope.consumerRole
        || String(message.range || "") !== expectedScope.range
      ) return { ok: false, reasonCode: "scope_mismatch" };

      const messageType = String(message.type || "");
      let normalizedBars = null;
      let tailMembership = null;
      const hasTailMembership = Object.prototype.hasOwnProperty.call(
        message,
        "secondary_tail_membership",
      );
      if (messageType === "chart_status") {
        if (hasTailMembership) return { ok: false, reasonCode: "tail_membership_not_recovery" };
        if (
          typeof message.message !== "string"
          || !message.message.trim()
          || typeof message.retry_in_seconds !== "number"
          || !Number.isFinite(message.retry_in_seconds)
          || message.retry_in_seconds < 0
          || (
            message.requires_resubscribe !== undefined
            && typeof message.requires_resubscribe !== "boolean"
          )
        ) return { ok: false, reasonCode: "status_invalid" };
      } else if (messageType === "heartbeat") {
        if (hasTailMembership) return { ok: false, reasonCode: "tail_membership_not_recovery" };
        if (
          typeof message.source !== "string"
          || !message.source.trim()
          || !Number.isFinite(Date.parse(message.ts || ""))
        ) return { ok: false, reasonCode: "heartbeat_invalid" };
      } else if (messageType === "chart_bars") {
        const recoveryScope = String(message.recovery_scope || "");
        const recoveryFrame = message.recovery === true || message.recovery_complete === true;
        if (
          !Array.isArray(message.bars)
          || message.bars.length > MTF_LENS_MAX_BARS
          || typeof message.recovery !== "boolean"
          || typeof message.recovery_complete !== "boolean"
          || (recoveryFrame && !["initial", "gap_repair", "checkpoint"].includes(recoveryScope))
          || (!recoveryFrame && recoveryScope !== "")
          || !Number.isSafeInteger(message.canonical_revision ?? 0)
          || (message.canonical_revision ?? 0) < 0
        ) return { ok: false, reasonCode: "bars_invalid" };
        if (!recoveryFrame && hasTailMembership) {
          return { ok: false, reasonCode: "tail_membership_not_recovery", reconnect: true };
        }
        if (recoveryFrame && !hasTailMembership) {
          return { ok: false, reasonCode: "tail_membership_missing", reconnect: true };
        }
        if (recoveryFrame) {
          const membershipAdmission = mtfLensNormalizeTailMembership(
            message.secondary_tail_membership,
          );
          if (!membershipAdmission.ok) {
            return { ...membershipAdmission, reconnect: true };
          }
          tailMembership = membershipAdmission.value;
          if (tailMembership.limit !== expectedScope.tailBars) {
            return {
              ok: false,
              reasonCode: "tail_membership_profile_mismatch",
              reconnect: true,
            };
          }
        }
        const streamOrder = {
          streamEpoch: Math.max(Number(currentOrder.epoch || 0), 0),
          streamGeneration: message.stream_generation,
          streamSeq: message.stream_seq,
          canonicalRevision: message.canonical_revision ?? 0,
        };
        if (
          Number(message.canonical_revision ?? 0)
          < Math.max(Number(currentOrder.canonicalRevision || 0), 0)
        ) return { ok: false, reasonCode: "canonical_revision_regression" };
        normalizedBars = message.bars.map(bar => normalizeChartStreamBarForScope(
          bar,
          expectedScope,
          streamOrder,
        ));
        if (normalizedBars.some(bar => bar === null)) {
          return { ok: false, reasonCode: "bar_scope_or_geometry_invalid" };
        }
        if (recoveryFrame) {
          const membershipKeys = new Set(tailMembership.timestamps);
          const recoveryBarInvalid = normalizedBars.some((bar, index) => (
            message.bars[index]?.authoritative !== true
            || !mtfLensBarIsAuthoritativeConfirmed(bar)
            || !membershipKeys.has(mtfLensTimestampKey(bar.ts))
          ));
          if (recoveryBarInvalid) {
            return {
              ok: false,
              reasonCode: "tail_membership_recovery_bar_invalid",
              reconnect: true,
            };
          }
        }
      } else {
        return { ok: false, reasonCode: "message_type_invalid" };
      }

      const streamGeneration = message.stream_generation ?? 0;
      const streamSeq = message.stream_seq ?? 0;
      if (messageType === "chart_status" && streamGeneration === 0 && streamSeq === 0) {
        return {
          ok: true,
          messageType,
          streamGeneration,
          streamSeq,
          normalizedBars,
          tailMembership,
          unsequenced: true,
          drop: false,
        };
      }
      if (
        !Number.isSafeInteger(streamGeneration)
        || streamGeneration <= 0
        || !Number.isSafeInteger(streamSeq)
        || streamSeq <= 0
      ) return { ok: false, reasonCode: "sequence_invalid" };

      const currentGeneration = Math.max(Number(currentOrder.generation || 0), 0);
      const currentSequence = Math.max(Number(currentOrder.sequence || 0), 0);
      const stale = streamGeneration < currentGeneration
        || (streamGeneration === currentGeneration && streamSeq <= currentSequence);
      return {
        ok: true,
        messageType,
        streamGeneration,
        streamSeq,
        normalizedBars,
        tailMembership,
        unsequenced: false,
        drop: stale,
      };
    }

    function mtfLensTimestampKey(value) {
      const parsed = Date.parse(value || "");
      return Number.isFinite(parsed) ? new Date(parsed).toISOString() : "";
    }

    function mtfLensBarIsAuthoritativeConfirmed(bar) {
      return Boolean(
        bar
        && bar.closed === true
        && bar.state === "confirmed"
        && bar.authoritative !== false
      );
    }

    function mtfLensBarBelongsToTail(key, bar, membershipKeys, latestMembershipMs) {
      if (mtfLensBarIsAuthoritativeConfirmed(bar)) return membershipKeys.has(key);
      const timestampMs = Date.parse(key);
      return Number.isFinite(timestampMs)
        && (latestMembershipMs === null || timestampMs > latestMembershipMs);
    }

    function mtfLensCommitBarFrame(runtime, message, admission) {
      if (!admission?.ok || admission.drop || admission.messageType !== "chart_bars") {
        return { ok: false, reasonCode: "bar_frame_not_admitted", reconnect: false };
      }
      const membership = admission.tailMembership;
      const membershipKeys = new Set(membership?.timestamps || []);
      const latestMembershipMs = membership?.timestamps.length
        ? Date.parse(membership.timestamps[membership.timestamps.length - 1])
        : null;
      const nextBars = new Map(runtime.barsByTs);
      if (membership) {
        for (const [key, bar] of nextBars) {
          if (!mtfLensBarBelongsToTail(key, bar, membershipKeys, latestMembershipMs)) {
            nextBars.delete(key);
          }
        }
      }
      for (const bar of admission.normalizedBars || []) {
        const key = mtfLensTimestampKey(bar?.ts);
        if (!key) {
          return { ok: false, reasonCode: "bar_timestamp_invalid", reconnect: true };
        }
        const resolved = resolveStreamBar(nextBars.get(key), bar);
        if (resolved) nextBars.set(key, resolved);
      }
      if (membership) {
        for (const [key, bar] of nextBars) {
          if (!mtfLensBarBelongsToTail(key, bar, membershipKeys, latestMembershipMs)) {
            nextBars.delete(key);
          }
        }
        const missingTimestamps = membership.timestamps.filter(key => (
          !mtfLensBarIsAuthoritativeConfirmed(nextBars.get(key))
        ));
        if (missingTimestamps.length) {
          return {
            ok: false,
            reasonCode: "tail_membership_member_missing",
            reconnect: true,
            missingTimestamps,
          };
        }
      }
      const ordered = [...nextBars.entries()]
        .sort((left, right) => left[0].localeCompare(right[0]));
      let bounded = ordered.slice(-MTF_LENS_MAX_BARS);
      if (membership && ordered.length > MTF_LENS_MAX_BARS) {
        const confirmedTail = ordered.filter(([key, bar]) => (
          membershipKeys.has(key) && mtfLensBarIsAuthoritativeConfirmed(bar)
        ));
        const provisionalCapacity = Math.max(
          MTF_LENS_MAX_BARS - confirmedTail.length,
          0,
        );
        const provisionalTail = ordered.filter(([, bar]) => (
          !mtfLensBarIsAuthoritativeConfirmed(bar)
        ));
        bounded = [
          ...confirmedTail,
          ...(provisionalCapacity
            ? provisionalTail.slice(-provisionalCapacity)
            : []),
        ].sort((left, right) => left[0].localeCompare(right[0]));
      }
      const boundedByTs = new Map(bounded);
      if (
        membership
        && membership.timestamps.some(key => (
          !mtfLensBarIsAuthoritativeConfirmed(boundedByTs.get(key))
        ))
      ) {
        return {
          ok: false,
          reasonCode: "tail_membership_projection_overflow",
          reconnect: true,
        };
      }
      runtime.barsByTs = boundedByTs;
      runtime.orderedBars = bounded.map(([, bar]) => bar);
      runtime.generation = admission.streamGeneration;
      runtime.sequence = admission.streamSeq;
      runtime.canonicalRevision = Math.max(
        Number(runtime.canonicalRevision || 0),
        Number(message.canonical_revision || 0),
      );
      runtime.barsVersion = Math.max(Number(runtime.barsVersion || 0), 0) + 1;
      runtime.chartFrameReady = true;
      return { ok: true, reasonCode: "", reconnect: false };
    }

    function mtfLensNodes() {
      return {
        frame: document.getElementById("mtf-lens"),
        title: document.getElementById("mtf-lens-title"),
        status: document.getElementById("mtf-lens-status"),
        anchor: document.getElementById("mtf-lens-anchor"),
        close: document.getElementById("mtf-lens-close"),
        resize: document.getElementById("mtf-lens-resize"),
        chart: document.getElementById("mtf-lens-chart"),
        interaction: document.getElementById("mtf-lens-interaction"),
        parent: document.getElementById("price-frame"),
      };
    }

    function mtfLensSetStatus(message, kind = "") {
      const node = document.getElementById("mtf-lens-status");
      if (!node) return;
      node.textContent = String(message || "Waiting");
      node.title = node.textContent;
      node.classList.toggle("live", kind === "live");
      node.classList.toggle("warn", kind === "warn");
      node.classList.toggle("error", kind === "error");
    }

    function mtfLensUpdateTitle() {
      const title = document.getElementById("mtf-lens-title");
      if (!title) return;
      const timeframe = (TIMEFRAMES.find(item => item.interval === mtfLensRuntime.interval) || {})
        .label || mtfLensRuntime.interval.toUpperCase();
      const symbol = mtfLensRuntime.scope?.symbol || String(state.symbol || "").trim();
      title.textContent = symbol ? `${timeframe} · ${symbol}` : `${timeframe} · MTF Lens`;
      title.title = title.textContent;
    }

    function mtfLensAnchorValue() {
      const stored = String(serverSettingValue(MTF_LENS_ANCHOR_SETTING) || "");
      return MTF_LENS_ANCHORS.includes(stored) ? stored : "bottom-left";
    }

    function mtfLensApplyAnchor(anchor, persist = false) {
      const frame = document.getElementById("mtf-lens");
      const normalized = MTF_LENS_ANCHORS.includes(anchor) ? anchor : "bottom-left";
      if (frame) frame.dataset.anchor = normalized;
      if (persist) setServerSettingValue(MTF_LENS_ANCHOR_SETTING, normalized);
      mtfLensScheduleRender();
      return normalized;
    }

    function mtfLensSizeBounds(parentRect) {
      const availableWidth = Math.max(Math.floor(Number(parentRect?.width || 0) - 24), 1);
      const availableHeight = Math.max(Math.floor(Number(parentRect?.height || 0) - 24), 1);
      return {
        minWidth: Math.min(MTF_LENS_MIN_WIDTH, availableWidth),
        minHeight: Math.min(MTF_LENS_MIN_HEIGHT, availableHeight),
        maxWidth: Math.max(
          Math.min(Math.floor(availableWidth * MTF_LENS_MAX_PARENT_RATIO), 2400),
          Math.min(MTF_LENS_MIN_WIDTH, availableWidth),
        ),
        maxHeight: Math.max(
          Math.min(Math.floor(availableHeight * MTF_LENS_MAX_PARENT_RATIO), 1800),
          Math.min(MTF_LENS_MIN_HEIGHT, availableHeight),
        ),
      };
    }

    function mtfLensSetFrameSize(width, height) {
      const { frame, parent } = mtfLensNodes();
      if (!frame || !parent) return null;
      const bounds = mtfLensSizeBounds(parent.getBoundingClientRect());
      const nextWidth = Math.round(clamp(Number(width) || bounds.minWidth, bounds.minWidth, bounds.maxWidth));
      const nextHeight = Math.round(clamp(Number(height) || bounds.minHeight, bounds.minHeight, bounds.maxHeight));
      frame.style.width = `${nextWidth}px`;
      frame.style.height = `${nextHeight}px`;
      return { width: nextWidth, height: nextHeight };
    }

    function mtfLensRestoreFrameSize() {
      const { parent } = mtfLensNodes();
      if (!parent) return;
      const parentRect = parent.getBoundingClientRect();
      const storedWidth = Number(serverSettingValue(MTF_LENS_WIDTH_SETTING));
      const storedHeight = Number(serverSettingValue(MTF_LENS_HEIGHT_SETTING));
      mtfLensSetFrameSize(
        Number.isFinite(storedWidth) && storedWidth >= MTF_LENS_MIN_WIDTH
          ? storedWidth
          : Math.round(parentRect.width * 0.38),
        Number.isFinite(storedHeight) && storedHeight >= MTF_LENS_MIN_HEIGHT
          ? storedHeight
          : Math.round(parentRect.height * 0.34),
      );
    }

    function mtfLensApplyStoredPresentation() {
      if (!mtfLensRuntime.open) return false;
      mtfLensApplyAnchor(mtfLensAnchorValue(), false);
      mtfLensRestoreFrameSize();
      mtfLensScheduleRender();
      return true;
    }

    function mtfLensPersistFrameSize() {
      const frame = document.getElementById("mtf-lens");
      if (!frame) return;
      const width = Math.max(Math.round(frame.getBoundingClientRect().width), MTF_LENS_MIN_WIDTH);
      const height = Math.max(Math.round(frame.getBoundingClientRect().height), MTF_LENS_MIN_HEIGHT);
      setServerSettingValue(MTF_LENS_WIDTH_SETTING, String(width));
      setServerSettingValue(MTF_LENS_HEIGHT_SETTING, String(height));
    }

    function mtfLensConstrainFrame() {
      const frame = document.getElementById("mtf-lens");
      if (!frame || !mtfLensRuntime.open) return;
      const rect = frame.getBoundingClientRect();
      mtfLensSetFrameSize(rect.width, rect.height);
      mtfLensScheduleRender();
    }

    function mtfLensPrepareCanvas(canvas, opaque = false) {
      if (!canvas) return null;
      const rect = canvas.getBoundingClientRect();
      if (rect.width <= 0 || rect.height <= 0) return null;
      const ratio = mtfLensCanvasPixelRatio(rect.width, rect.height);
      const pixelWidth = Math.max(Math.floor(rect.width * ratio), 1);
      const pixelHeight = Math.max(Math.floor(rect.height * ratio), 1);
      if (canvas.width !== pixelWidth || canvas.height !== pixelHeight) {
        canvas.width = pixelWidth;
        canvas.height = pixelHeight;
      }
      const ctx = canvas.getContext("2d");
      if (!ctx) return null;
      ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
      ctx.clearRect(0, 0, rect.width, rect.height);
      if (opaque) {
        ctx.fillStyle = css("--chart-bg");
        ctx.fillRect(0, 0, rect.width, rect.height);
      }
      return { ctx, width: rect.width, height: rect.height, ratio };
    }

    function mtfLensCanvasPixelRatio(width, height) {
      const requestedRatio = Math.min(
        Math.max(Number(window.devicePixelRatio || 1), 1),
        2,
      );
      const aggregateCssPixels = Math.max(Number(width) * Number(height) * 2, 1);
      const budgetRatio = Math.max(
        Math.sqrt(MTF_LENS_CANVAS_PIXEL_BUDGET / aggregateCssPixels),
        1,
      );
      return Math.min(requestedRatio, budgetRatio);
    }

    function mtfLensFormatPrice(value) {
      const absolute = Math.abs(Number(value));
      const digits = absolute >= 100 ? 2 : absolute >= 1 ? 3 : 5;
      return Number(value).toFixed(digits);
    }

    function mtfLensTimeText(ts) {
      const date = new Date(ts);
      if (!Number.isFinite(date.getTime())) return "";
      return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    }

    function mtfLensRenderBase() {
      if (!mtfLensRuntime.open) return;
      const prepared = mtfLensPrepareCanvas(document.getElementById("mtf-lens-chart"), true);
      if (!prepared) return;
      const { ctx, width, height } = prepared;
      const pad = { left: 8, right: 62, top: 8, bottom: 23 };
      const plotWidth = Math.max(width - pad.left - pad.right, 1);
      const plotHeight = Math.max(height - pad.top - pad.bottom, 1);
      const capacity = Math.round(clamp(Math.floor(plotWidth / 5), 40, MTF_LENS_TAIL_BARS));
      const bars = mtfLensRuntime.orderedBars.slice(-capacity);

      ctx.lineWidth = 1;
      ctx.strokeStyle = css("--grid-line");
      ctx.fillStyle = css("--axis");
      ctx.font = `9px ${CANVAS_MONO_FONT}`;
      ctx.textBaseline = "middle";
      for (let row = 0; row <= 4; row += 1) {
        const y = pad.top + (plotHeight * row) / 4;
        ctx.beginPath();
        ctx.moveTo(pad.left, Math.round(y) + 0.5);
        ctx.lineTo(width - pad.right, Math.round(y) + 0.5);
        ctx.stroke();
      }
      ctx.beginPath();
      ctx.moveTo(width - pad.right + 0.5, pad.top);
      ctx.lineTo(width - pad.right + 0.5, height - pad.bottom);
      ctx.stroke();
      ctx.beginPath();
      ctx.moveTo(pad.left, height - pad.bottom + 0.5);
      ctx.lineTo(width - pad.right, height - pad.bottom + 0.5);
      ctx.stroke();

      if (!bars.length) {
        ctx.fillStyle = css("--muted");
        ctx.textAlign = "center";
        ctx.fillText("Waiting for confirmed/provider candles", width / 2, height / 2);
        mtfLensRuntime.geometry = null;
        mtfLensScheduleInteractionRender();
        return;
      }

      let priceLow = Math.min(...bars.map(bar => Number(bar.low)));
      let priceHigh = Math.max(...bars.map(bar => Number(bar.high)));
      const rawRange = priceHigh - priceLow;
      const pricePad = rawRange > 0 ? rawRange * 0.06 : Math.max(Math.abs(priceHigh) * 0.002, 0.01);
      priceLow -= pricePad;
      priceHigh += pricePad;
      const priceRange = Math.max(priceHigh - priceLow, Number.EPSILON);
      const priceY = price => pad.top + ((priceHigh - Number(price)) / priceRange) * plotHeight;
      const step = plotWidth / Math.max(bars.length, 1);
      const bodyWidth = clamp(step * 0.62, 1, 8);
      const upColor = css("--green");
      const downColor = css("--red");

      bars.forEach((bar, index) => {
        const x = pad.left + (index + 0.5) * step;
        const openY = priceY(bar.open);
        const closeY = priceY(bar.close);
        const highY = priceY(bar.high);
        const lowY = priceY(bar.low);
        const up = Number(bar.close) >= Number(bar.open);
        const color = up ? upColor : downColor;
        const confirmed = bar.closed === true && bar.authoritative !== false;
        ctx.save();
        ctx.globalAlpha = confirmed ? 0.96 : 0.72;
        ctx.strokeStyle = color;
        ctx.fillStyle = color;
        ctx.lineWidth = Math.max(1, Math.min(step * 0.12, 1.5));
        ctx.beginPath();
        ctx.moveTo(Math.round(x) + 0.5, highY);
        ctx.lineTo(Math.round(x) + 0.5, lowY);
        ctx.stroke();
        const bodyTop = Math.min(openY, closeY);
        const bodyHeight = Math.max(Math.abs(closeY - openY), 1);
        const bodyLeft = x - bodyWidth / 2;
        if (confirmed) ctx.fillRect(bodyLeft, bodyTop, bodyWidth, bodyHeight);
        else {
          ctx.setLineDash([2, 2]);
          ctx.strokeRect(bodyLeft, bodyTop, bodyWidth, bodyHeight);
        }
        ctx.restore();
      });

      ctx.fillStyle = css("--axis");
      ctx.textAlign = "left";
      for (let row = 0; row <= 4; row += 1) {
        const price = priceHigh - (priceRange * row) / 4;
        const y = pad.top + (plotHeight * row) / 4;
        ctx.fillText(mtfLensFormatPrice(price), width - pad.right + 6, y);
      }
      const timeIndexes = [...new Set([0, Math.floor((bars.length - 1) / 3), Math.floor(((bars.length - 1) * 2) / 3), bars.length - 1])];
      ctx.textAlign = "center";
      ctx.textBaseline = "alphabetic";
      for (const index of timeIndexes) {
        const x = pad.left + (index + 0.5) * step;
        ctx.fillText(mtfLensTimeText(bars[index]?.ts), x, height - 5);
      }
      const latest = bars[bars.length - 1];
      const latestY = priceY(latest.close);
      ctx.save();
      ctx.strokeStyle = latest.close >= latest.open ? upColor : downColor;
      ctx.setLineDash([3, 3]);
      ctx.globalAlpha = 0.55;
      ctx.beginPath();
      ctx.moveTo(pad.left, latestY);
      ctx.lineTo(width - pad.right, latestY);
      ctx.stroke();
      ctx.restore();

      mtfLensRuntime.geometry = {
        bars,
        pad,
        plotWidth,
        plotHeight,
        step,
        priceLow,
        priceHigh,
        priceY,
        width,
        height,
      };
      mtfLensScheduleInteractionRender();
    }

    function mtfLensRenderInteraction() {
      if (!mtfLensRuntime.open) return;
      const prepared = mtfLensPrepareCanvas(document.getElementById("mtf-lens-interaction"));
      if (!prepared) return;
      const { ctx, width, height } = prepared;
      const geometry = mtfLensRuntime.geometry;
      const scope = mtfLensRuntime.scope;
      const snapshot = mtfLensDrawingSnapshot();
      if (
        geometry
        && scope
        && snapshot
        && mtfLensRuntime.drawings.length
        && typeof drawDrawingObjects === "function"
      ) {
        const allBars = mtfLensRuntime.orderedBars;
        const visible = {
          bars: geometry.bars,
          allBars,
          start: Math.max(allBars.length - geometry.bars.length, 0),
        };
        const drawingX = index => geometry.pad.left + (Number(index) + 0.5) * geometry.step;
        drawDrawingObjects(
          ctx,
          mtfLensRuntime.drawings,
          visible,
          geometry.pad,
          geometry.plotHeight,
          geometry.step,
          drawingX,
          geometry.priceY,
          width,
          {
            snapshot,
            slotStep: mtfLensIntervalMinutes(scope.timeframe),
            rightGapBars: 0,
            barsVersion: mtfLensRuntime.barsVersion,
            selectedId: null,
            showHandles: false,
            decorators: false,
            timeframe: scope.timeframe,
            futureAxis: null,
            renderableOnly: true,
          },
        );
      }
      if (!mtfLensRuntime.crosshair.visible || !geometry) return;
      const plotRight = width - geometry.pad.right;
      const plotBottom = height - geometry.pad.bottom;
      const x = clamp(mtfLensRuntime.crosshair.x, geometry.pad.left, plotRight);
      const y = clamp(mtfLensRuntime.crosshair.y, geometry.pad.top, plotBottom);
      const index = Math.round((x - geometry.pad.left) / geometry.step - 0.5);
      const bar = geometry.bars[clamp(index, 0, geometry.bars.length - 1)];
      const price = geometry.priceHigh
        - ((y - geometry.pad.top) / geometry.plotHeight)
        * (geometry.priceHigh - geometry.priceLow);
      ctx.save();
      ctx.strokeStyle = css("--crosshair");
      ctx.lineWidth = 1;
      ctx.setLineDash([4, 4]);
      ctx.beginPath();
      ctx.moveTo(Math.round(x) + 0.5, geometry.pad.top);
      ctx.lineTo(Math.round(x) + 0.5, plotBottom);
      ctx.moveTo(geometry.pad.left, Math.round(y) + 0.5);
      ctx.lineTo(plotRight, Math.round(y) + 0.5);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.font = `9px ${CANVAS_MONO_FONT}`;
      ctx.textBaseline = "middle";
      ctx.fillStyle = css("--tooltip-bg");
      ctx.fillRect(plotRight + 2, y - 8, geometry.pad.right - 4, 16);
      ctx.fillStyle = css("--tooltip-text");
      ctx.textAlign = "left";
      ctx.fillText(mtfLensFormatPrice(price), plotRight + 5, y);
      if (bar) {
        const label = `${mtfLensTimeText(bar.ts)}  O ${mtfLensFormatPrice(bar.open)}  H ${mtfLensFormatPrice(bar.high)}  L ${mtfLensFormatPrice(bar.low)}  C ${mtfLensFormatPrice(bar.close)}`;
        const labelWidth = Math.min(ctx.measureText(label).width + 10, Math.max(plotRight - geometry.pad.left, 0));
        ctx.fillStyle = css("--tooltip-bg");
        ctx.fillRect(geometry.pad.left, geometry.pad.top, labelWidth, 17);
        ctx.fillStyle = css("--tooltip-text");
        ctx.textAlign = "left";
        ctx.fillText(label, geometry.pad.left + 5, geometry.pad.top + 8.5);
      }
      ctx.restore();
    }

    function mtfLensScheduleRender() {
      if (!mtfLensRuntime.open || mtfLensRuntime.renderFrame) return;
      mtfLensRuntime.renderFrame = requestAnimationFrame(() => {
        mtfLensRuntime.renderFrame = 0;
        mtfLensRenderBase();
      });
    }

    function mtfLensScheduleInteractionRender() {
      if (!mtfLensRuntime.open || mtfLensRuntime.interactionFrame) return;
      mtfLensRuntime.interactionFrame = requestAnimationFrame(() => {
        mtfLensRuntime.interactionFrame = 0;
        mtfLensRenderInteraction();
      });
    }

    function mtfLensCancelFrames() {
      if (mtfLensRuntime.renderFrame) cancelAnimationFrame(mtfLensRuntime.renderFrame);
      if (mtfLensRuntime.interactionFrame) cancelAnimationFrame(mtfLensRuntime.interactionFrame);
      if (mtfLensRuntime.resizeFrame) cancelAnimationFrame(mtfLensRuntime.resizeFrame);
      mtfLensRuntime.renderFrame = 0;
      mtfLensRuntime.interactionFrame = 0;
      mtfLensRuntime.resizeFrame = 0;
    }

    function mtfLensClearTransportTimers() {
      if (mtfLensRuntime.reconnectTimer) window.clearTimeout(mtfLensRuntime.reconnectTimer);
      if (mtfLensRuntime.staleTimer) window.clearTimeout(mtfLensRuntime.staleTimer);
      mtfLensRuntime.reconnectTimer = null;
      mtfLensRuntime.staleTimer = null;
    }

    function mtfLensResetConnectionOrder(runtime = mtfLensRuntime) {
      runtime.connectionEpoch = Math.max(Number(runtime.connectionEpoch || 0), 0) + 1;
      runtime.generation = 0;
      runtime.sequence = 0;
      runtime.canonicalRevision = 0;
      runtime.chartFrameReady = false;
      return runtime.connectionEpoch;
    }

    function mtfLensSocketIsCurrent(runtime, socket, scope, scopeEpoch, connectionEpoch) {
      return Boolean(runtime.open)
        && runtime.socket === socket
        && runtime.epoch === scopeEpoch
        && runtime.connectionEpoch === connectionEpoch
        && mtfLensScopeEquals(runtime.scope, scope);
    }

    function mtfLensStatusIsTerminal(message) {
      return message?.type === "chart_status"
        && message.retry_in_seconds === 0
        && message.requires_resubscribe !== true;
    }

    function mtfLensApplyChartStatus(message, scope, epoch) {
      const terminal = mtfLensStatusIsTerminal(message);
      const sleeping = String(message?.source || "") === "server:sleep" || state.serverSleeping;
      mtfLensSetStatus(message.message, terminal ? "warn" : message.requires_resubscribe ? "warn" : "");
      if (message.requires_resubscribe) {
        mtfLensRefreshRoute(scope, epoch);
        return;
      }
      if (!terminal) return;
      mtfLensRuntime.reconnectBlocked = true;
      mtfLensRuntime.reconnectBlockedForSleep = sleeping;
      mtfLensRuntime.blockedSleepObserved = sleeping && state.serverSleeping;
      mtfLensDisconnectTransport();
      if (sleeping) {
        mtfLensCancelDrawingRuntime({ clear: true });
        mtfLensScheduleInteractionRender();
      }
    }

    function mtfLensDisconnectTransport() {
      mtfLensClearTransportTimers();
      mtfLensResetConnectionOrder();
      mtfLensCompleteDrawingProjectionRefresh();
      mtfLensScheduleInteractionRender();
      const socket = mtfLensRuntime.socket;
      mtfLensRuntime.socket = null;
      if (!socket) return;
      socket.onopen = null;
      socket.onmessage = null;
      socket.onerror = null;
      socket.onclose = null;
      if (socket.readyState < 2) socket.close(1000, "MTF Lens scope closed");
    }

    function mtfLensScheduleStaleCheck(socket, scope, epoch, connectionEpoch) {
      if (mtfLensRuntime.staleTimer) window.clearTimeout(mtfLensRuntime.staleTimer);
      mtfLensRuntime.staleTimer = window.setTimeout(() => {
        mtfLensRuntime.staleTimer = null;
        if (
          mtfLensSocketIsCurrent(
            mtfLensRuntime,
            socket,
            scope,
            epoch,
            connectionEpoch,
          )
        ) {
          mtfLensSetStatus("Stream stale · reconnecting", "warn");
          socket.close(4000, "MTF Lens stale");
        }
      }, Math.max(CHART_STREAM_STALE_MS, 20000));
    }

    function mtfLensScheduleReconnect(scope, epoch) {
      if (mtfLensRuntime.reconnectTimer || mtfLensRuntime.reconnectBlocked || state.serverSleeping) return;
      const delay = Math.min(1500 * (2 ** Math.min(mtfLensRuntime.reconnectAttempt, 4)), 15000);
      mtfLensRuntime.reconnectAttempt += 1;
      mtfLensRuntime.reconnectTimer = window.setTimeout(() => {
        mtfLensRuntime.reconnectTimer = null;
        if (
          mtfLensRuntime.open
          && mtfLensRuntime.epoch === epoch
          && mtfLensScopeEquals(mtfLensRuntime.scope, scope)
        ) mtfLensConnect(scope, epoch);
      }, delay);
    }

    function mtfLensRequestFreshInitial(socket, scope, epoch, connectionEpoch, reasonCode) {
      if (!mtfLensSocketIsCurrent(
        mtfLensRuntime,
        socket,
        scope,
        epoch,
        connectionEpoch,
      )) return false;
      mtfLensSetStatus(`Tail resync · ${reasonCode}`, "error");
      socket.close(4002, "MTF Lens tail resync");
      return true;
    }

    function mtfLensHandleStreamMessage(message, scope, epoch, connectionEpoch, socket) {
      if (!mtfLensSocketIsCurrent(
        mtfLensRuntime,
        socket,
        scope,
        epoch,
        connectionEpoch,
      )) return;
      const admission = mtfLensMessageAdmission(message, scope, {
        epoch: connectionEpoch,
        generation: mtfLensRuntime.generation,
        sequence: mtfLensRuntime.sequence,
        canonicalRevision: mtfLensRuntime.canonicalRevision,
      });
      if (!admission.ok) {
        mtfLensSetStatus(`Invalid frame · ${admission.reasonCode}`, "error");
        if (admission.reconnect) {
          mtfLensRequestFreshInitial(
            socket,
            scope,
            epoch,
            connectionEpoch,
            admission.reasonCode,
          );
        }
        return;
      }
      if (admission.drop) return;
      mtfLensScheduleStaleCheck(socket, scope, epoch, connectionEpoch);
      if (admission.unsequenced) {
        mtfLensApplyChartStatus(message, scope, epoch);
        return;
      }

      if (admission.messageType === "chart_bars") {
        const firstAcceptedChartFrame = !mtfLensRuntime.chartFrameReady;
        const commit = mtfLensCommitBarFrame(mtfLensRuntime, message, admission);
        if (!commit.ok) {
          if (commit.reconnect) {
            mtfLensRequestFreshInitial(
              socket,
              scope,
              epoch,
              connectionEpoch,
              commit.reasonCode,
            );
          }
          return;
        }
        mtfLensResolveDrawings(mtfLensRuntime.drawings);
        if (
          firstAcceptedChartFrame
          && (
            !mtfLensRuntime.drawingsHydrated
            || mtfLensRuntime.drawingExactRefreshPending
          )
          && !mtfLensRuntime.drawingRequest
        ) {
          void mtfLensLoadDrawings(scope, epoch, {
            force: true,
            reason: "mtf_lens_first_chart_frame",
          });
        }
        mtfLensSetStatus(
          mtfLensRuntime.orderedBars.length ? `${mtfLensRuntime.orderedBars.length} bars · live` : "No bars",
          mtfLensRuntime.orderedBars.length ? "live" : "warn",
        );
        mtfLensScheduleRender();
        return;
      }

      mtfLensRuntime.generation = admission.streamGeneration;
      mtfLensRuntime.sequence = admission.streamSeq;
      if (admission.messageType === "chart_status") {
        mtfLensApplyChartStatus(message, scope, epoch);
      } else if (!mtfLensRuntime.orderedBars.length) {
        mtfLensSetStatus("Connected · waiting for bars", "live");
      }
    }

    function mtfLensRefreshRoute(scope, epoch) {
      if (!mtfLensRuntime.open || mtfLensRuntime.epoch !== epoch) return;
      mtfLensDisconnectTransport();
      const refresh = Promise.resolve().then(() => (
        typeof loadInstruments === "function" ? loadInstruments() : undefined
      ));
      refresh.catch(() => null).finally(() => {
        if (
          mtfLensRuntime.open
          && mtfLensRuntime.epoch === epoch
          && exactIdentityText(state.instrumentId) === scope.instrumentId
        ) mtfLensRebindCurrentScope({ force: true });
      });
    }

    function mtfLensConnect(scope, epoch) {
      if (
        !mtfLensRuntime.open
        || state.serverSleeping
        || mtfLensRuntime.epoch !== epoch
        || !mtfLensScopeEquals(mtfLensRuntime.scope, scope)
      ) {
        if (state.serverSleeping) {
          mtfLensRuntime.reconnectBlocked = true;
          mtfLensRuntime.reconnectBlockedForSleep = true;
          mtfLensRuntime.blockedSleepObserved = true;
          mtfLensSetStatus("Server sleeping", "warn");
        }
        return;
      }
      mtfLensDisconnectTransport();
      const connectionEpoch = mtfLensRuntime.connectionEpoch;
      const params = new URLSearchParams({
        instrument_id: scope.instrumentId,
        expected_route_fingerprint: scope.routeFingerprint,
        interval: scope.timeframe,
        range: scope.range,
        consumer_role: scope.consumerRole,
        tail_bars: String(scope.tailBars),
      });
      const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      let socket;
      try {
        socket = new WebSocket(`${protocol}//${window.location.host}/ws/chart?${params.toString()}`);
      } catch (_) {
        if (
          mtfLensRuntime.open
          && mtfLensRuntime.epoch === epoch
          && mtfLensRuntime.connectionEpoch === connectionEpoch
          && mtfLensScopeEquals(mtfLensRuntime.scope, scope)
        ) {
          mtfLensSetStatus("Stream unavailable · retrying", "error");
          mtfLensScheduleReconnect(scope, epoch);
        }
        return;
      }
      mtfLensRuntime.socket = socket;
      mtfLensSetStatus("Connecting", "warn");
      socket.onopen = () => {
        if (!mtfLensSocketIsCurrent(
          mtfLensRuntime,
          socket,
          scope,
          epoch,
          connectionEpoch,
        )) return;
        mtfLensRuntime.reconnectAttempt = 0;
        mtfLensSetStatus("Connected · syncing", "live");
        mtfLensScheduleStaleCheck(socket, scope, epoch, connectionEpoch);
      };
      socket.onmessage = event => {
        let message;
        try {
          message = JSON.parse(event.data);
        } catch (_) {
          mtfLensSetStatus("Malformed stream frame", "error");
          return;
        }
        mtfLensHandleStreamMessage(message, scope, epoch, connectionEpoch, socket);
      };
      socket.onerror = () => {
        if (mtfLensSocketIsCurrent(
          mtfLensRuntime,
          socket,
          scope,
          epoch,
          connectionEpoch,
        )) {
          mtfLensSetStatus("Stream error", "error");
        }
      };
      socket.onclose = () => {
        if (!mtfLensSocketIsCurrent(
          mtfLensRuntime,
          socket,
          scope,
          epoch,
          connectionEpoch,
        )) return;
        mtfLensRuntime.socket = null;
        if (mtfLensRuntime.staleTimer) window.clearTimeout(mtfLensRuntime.staleTimer);
        mtfLensRuntime.staleTimer = null;
        if (mtfLensRuntime.open && mtfLensScopeEquals(mtfLensRuntime.scope, scope)) {
          mtfLensSetStatus(state.serverSleeping ? "Server sleeping" : "Disconnected · retrying", "warn");
          mtfLensScheduleReconnect(scope, epoch);
        }
      };
    }

    function mtfLensBindScope(scope) {
      mtfLensDisconnectTransport();
      mtfLensCancelDrawingRuntime({ clear: true });
      mtfLensRuntime.scope = scope;
      mtfLensRuntime.epoch += 1;
      mtfLensRuntime.generation = 0;
      mtfLensRuntime.sequence = 0;
      mtfLensRuntime.canonicalRevision = 0;
      mtfLensRuntime.barsVersion = 0;
      mtfLensRuntime.chartFrameReady = false;
      mtfLensRuntime.reconnectBlocked = false;
      mtfLensRuntime.reconnectBlockedForSleep = false;
      mtfLensRuntime.blockedSleepObserved = false;
      mtfLensRuntime.barsByTs = new Map();
      mtfLensRuntime.orderedBars = [];
      mtfLensRuntime.crosshair = { visible: false, x: 0, y: 0 };
      mtfLensRuntime.geometry = null;
      mtfLensRuntime.reconnectAttempt = 0;
      mtfLensUpdateTitle();
      mtfLensScheduleRender();
      void mtfLensLoadDrawings(scope, mtfLensRuntime.epoch, {
        force: true,
        reason: "mtf_lens_bind",
      });
      mtfLensConnect(scope, mtfLensRuntime.epoch);
    }

    function mtfLensRebindCurrentScope(options = {}) {
      if (!mtfLensRuntime.open) return false;
      if (mtfLensRuntime.interval === state.timeframe) {
        mtfLensClose();
        return false;
      }
      const scope = mtfLensScope();
      if (!scope) {
        mtfLensDisconnectTransport();
        mtfLensCancelDrawingRuntime({ clear: true });
        mtfLensRuntime.scope = null;
        mtfLensRuntime.epoch += 1;
        mtfLensRuntime.barsByTs = new Map();
        mtfLensRuntime.orderedBars = [];
        mtfLensRuntime.barsVersion = 0;
        mtfLensRuntime.geometry = null;
        mtfLensSetStatus("Waiting for exact route", "warn");
        mtfLensScheduleRender();
        return false;
      }
      if (!options.force && mtfLensScopeEquals(scope, mtfLensRuntime.scope)) {
        if (mtfLensRuntime.reconnectBlockedForSleep && state.serverSleeping) {
          mtfLensRuntime.blockedSleepObserved = true;
        }
        const wokeAfterObservedSleep = mtfLensRuntime.reconnectBlockedForSleep
          && mtfLensRuntime.blockedSleepObserved
          && !state.serverSleeping;
        if (mtfLensRuntime.reconnectBlocked && !wokeAfterObservedSleep) {
          mtfLensUpdateTitle();
          mtfLensScheduleRender();
          return true;
        }
        if (wokeAfterObservedSleep) {
          mtfLensRuntime.reconnectBlocked = false;
          mtfLensRuntime.reconnectBlockedForSleep = false;
          mtfLensRuntime.blockedSleepObserved = false;
        }
        if (!mtfLensRuntime.socket && !mtfLensRuntime.reconnectTimer) {
          mtfLensConnect(mtfLensRuntime.scope, mtfLensRuntime.epoch);
        }
        mtfLensUpdateTitle();
        mtfLensScheduleRender();
        return true;
      }
      mtfLensBindScope(scope);
      return true;
    }

    function mtfLensPrepareForPrimaryScopeChange() {
      if (!mtfLensRuntime.open) return;
      mtfLensDisconnectTransport();
      mtfLensCancelDrawingRuntime({ clear: true });
      mtfLensRuntime.scope = null;
      mtfLensRuntime.epoch += 1;
      mtfLensRuntime.generation = 0;
      mtfLensRuntime.sequence = 0;
      mtfLensRuntime.canonicalRevision = 0;
      mtfLensRuntime.barsVersion = 0;
      mtfLensRuntime.chartFrameReady = false;
      mtfLensRuntime.reconnectBlocked = false;
      mtfLensRuntime.reconnectBlockedForSleep = false;
      mtfLensRuntime.blockedSleepObserved = false;
      mtfLensRuntime.barsByTs = new Map();
      mtfLensRuntime.orderedBars = [];
      mtfLensRuntime.geometry = null;
      mtfLensRuntime.crosshair = { visible: false, x: 0, y: 0 };
      mtfLensSetStatus("Switching instrument", "warn");
      mtfLensScheduleRender();
    }

    function mtfLensHandlePrimaryTimeframeChange(nextTimeframe) {
      if (mtfLensRuntime.open && mtfLensRuntime.interval === String(nextTimeframe || "")) {
        mtfLensClose();
        return true;
      }
      return false;
    }

    function mtfLensHandleServerSleepState() {
      if (!mtfLensRuntime.open) return;
      if (state.serverSleeping) {
        mtfLensRuntime.reconnectBlocked = true;
        mtfLensRuntime.reconnectBlockedForSleep = true;
        mtfLensRuntime.blockedSleepObserved = true;
        mtfLensDisconnectTransport();
        mtfLensCancelDrawingRuntime({ clear: true });
        mtfLensSetStatus("Server sleeping", "warn");
        mtfLensScheduleInteractionRender();
        return;
      }
      if (!mtfLensRuntime.reconnectBlockedForSleep) return;
      mtfLensRuntime.reconnectBlocked = false;
      mtfLensRuntime.reconnectBlockedForSleep = false;
      mtfLensRuntime.blockedSleepObserved = false;
      if (mtfLensRuntime.scope) {
        if (typeof mtfLensLoadDrawings === "function") {
          void mtfLensLoadDrawings(mtfLensRuntime.scope, mtfLensRuntime.epoch, {
            force: true,
            reason: "mtf_lens_wake",
          });
        }
        mtfLensConnect(mtfLensRuntime.scope, mtfLensRuntime.epoch);
      }
    }

    function mtfLensSyncTimeframeButtons() {
      const activeInterval = mtfLensRuntime.open ? mtfLensRuntime.interval : "";
      document.querySelectorAll?.("#timeframe-buttons [data-timeframe]").forEach(button => {
        const secondary = Boolean(activeInterval && button.dataset.timeframe === activeInterval);
        const primary = button.dataset.timeframe === state.timeframe;
        button.classList.toggle("mtf-secondary-timeframe", secondary);
        button.setAttribute("aria-label", primary
          ? `${button.textContent} primary timeframe; choose a different timeframe for MTF Lens`
          : secondary
            ? `${button.textContent} secondary timeframe in MTF Lens`
            : `${button.textContent} timeframe; right-click to open in MTF Lens`);
        button.title = primary
          ? "Primary timeframe · MTF Lens requires a different timeframe"
          : secondary
            ? "Right-click to close MTF Lens"
            : "Right-click to open this timeframe in MTF Lens";
      });
      mtfLensScheduleRender();
    }

    function mtfLensToggle(interval) {
      const requested = String(interval || "").trim();
      if (!TIMEFRAMES.some(item => item.interval === requested) || requested === state.timeframe) {
        return false;
      }
      if (mtfLensRuntime.open && mtfLensRuntime.interval === requested) {
        mtfLensClose();
        return false;
      }
      const frame = document.getElementById("mtf-lens");
      if (!frame) return false;
      mtfLensRuntime.open = true;
      mtfLensRuntime.interval = requested;
      frame.classList.remove("hidden");
      frame.setAttribute("aria-hidden", "false");
      mtfLensBindDom();
      mtfLensApplyStoredPresentation();
      mtfLensObserveFrame();
      mtfLensRebindCurrentScope({ force: true });
      mtfLensSyncTimeframeButtons();
      return true;
    }

    function mtfLensTimeframe() {
      return mtfLensRuntime.open ? mtfLensRuntime.interval : "";
    }

    function mtfLensAbortResize() {
      const pointer = mtfLensRuntime.resizePointer;
      mtfLensRuntime.resizePointer = null;
      if (!pointer) return;
      try {
        pointer.node.releasePointerCapture(pointer.pointerId);
      } catch (_) {
        // Capture may already be released by the browser.
      }
    }

    function mtfLensClose() {
      const frame = document.getElementById("mtf-lens");
      mtfLensDisconnectTransport();
      mtfLensCancelDrawingRuntime({ clear: true });
      mtfLensCancelFrames();
      mtfLensAbortResize();
      mtfLensRuntime.resizeObserver?.disconnect();
      mtfLensRuntime.resizeObserver = null;
      mtfLensRuntime.open = false;
      mtfLensRuntime.interval = "";
      mtfLensRuntime.scope = null;
      mtfLensRuntime.epoch += 1;
      mtfLensRuntime.generation = 0;
      mtfLensRuntime.sequence = 0;
      mtfLensRuntime.canonicalRevision = 0;
      mtfLensRuntime.barsVersion = 0;
      mtfLensRuntime.chartFrameReady = false;
      mtfLensRuntime.reconnectBlocked = false;
      mtfLensRuntime.reconnectBlockedForSleep = false;
      mtfLensRuntime.blockedSleepObserved = false;
      mtfLensRuntime.barsByTs = new Map();
      mtfLensRuntime.orderedBars = [];
      mtfLensRuntime.geometry = null;
      mtfLensRuntime.crosshair = { visible: false, x: 0, y: 0 };
      if (frame) {
        frame.classList.add("hidden");
        frame.setAttribute("aria-hidden", "true");
      }
      mtfLensSyncTimeframeButtons();
    }

    function mtfLensApplyPendingResize() {
      mtfLensRuntime.resizeFrame = 0;
      const pointer = mtfLensRuntime.resizePointer;
      if (!pointer) return;
      const dx = pointer.clientX - pointer.startX;
      const dy = pointer.clientY - pointer.startY;
      const anchorRight = pointer.anchor.endsWith("right");
      const anchorBottom = pointer.anchor.startsWith("bottom");
      mtfLensSetFrameSize(
        pointer.startWidth + (anchorRight ? -dx : dx),
        pointer.startHeight + (anchorBottom ? -dy : dy),
      );
      mtfLensScheduleRender();
    }

    function mtfLensResizePointerMove(event) {
      const pointer = mtfLensRuntime.resizePointer;
      if (!pointer || pointer.pointerId !== event.pointerId) return;
      pointer.clientX = event.clientX;
      pointer.clientY = event.clientY;
      if (!mtfLensRuntime.resizeFrame) {
        mtfLensRuntime.resizeFrame = requestAnimationFrame(mtfLensApplyPendingResize);
      }
    }

    function mtfLensResizePointerEnd(event) {
      const pointer = mtfLensRuntime.resizePointer;
      if (!pointer || pointer.pointerId !== event.pointerId) return;
      if (mtfLensRuntime.resizeFrame) {
        cancelAnimationFrame(mtfLensRuntime.resizeFrame);
        mtfLensRuntime.resizeFrame = 0;
        mtfLensApplyPendingResize();
      }
      mtfLensAbortResize();
      mtfLensPersistFrameSize();
      mtfLensScheduleRender();
    }

    function mtfLensObserveFrame() {
      mtfLensRuntime.resizeObserver?.disconnect();
      mtfLensRuntime.resizeObserver = null;
      if (typeof ResizeObserver !== "function") return;
      const { frame, parent } = mtfLensNodes();
      if (!frame || !parent) return;
      mtfLensRuntime.resizeObserver = new ResizeObserver(() => mtfLensConstrainFrame());
      mtfLensRuntime.resizeObserver.observe(frame);
      mtfLensRuntime.resizeObserver.observe(parent);
    }

    function mtfLensBindDom() {
      const nodes = mtfLensNodes();
      if (!nodes.frame || nodes.frame.dataset.bound === "1") return;
      nodes.frame.dataset.bound = "1";
      nodes.close?.addEventListener("click", mtfLensClose);
      nodes.anchor?.addEventListener("click", () => {
        const current = MTF_LENS_ANCHORS.indexOf(nodes.frame.dataset.anchor || "bottom-left");
        mtfLensApplyAnchor(MTF_LENS_ANCHORS[(current + 1) % MTF_LENS_ANCHORS.length], true);
        mtfLensConstrainFrame();
      });
      nodes.resize?.addEventListener("pointerdown", event => {
        if (!mtfLensRuntime.open || event.button !== 0) return;
        event.preventDefault();
        const rect = nodes.frame.getBoundingClientRect();
        mtfLensRuntime.resizePointer = {
          node: nodes.resize,
          pointerId: event.pointerId,
          startX: event.clientX,
          startY: event.clientY,
          clientX: event.clientX,
          clientY: event.clientY,
          startWidth: rect.width,
          startHeight: rect.height,
          anchor: nodes.frame.dataset.anchor || "bottom-left",
        };
        nodes.resize.setPointerCapture(event.pointerId);
      });
      nodes.resize?.addEventListener("pointermove", mtfLensResizePointerMove);
      nodes.resize?.addEventListener("pointerup", mtfLensResizePointerEnd);
      nodes.resize?.addEventListener("pointercancel", mtfLensResizePointerEnd);
      nodes.resize?.addEventListener("lostpointercapture", mtfLensResizePointerEnd);
      nodes.resize?.addEventListener("keydown", event => {
        const step = event.shiftKey ? 48 : 16;
        const rect = nodes.frame.getBoundingClientRect();
        let width = rect.width;
        let height = rect.height;
        if (event.key === "ArrowLeft") width -= step;
        else if (event.key === "ArrowRight") width += step;
        else if (event.key === "ArrowUp") height -= step;
        else if (event.key === "ArrowDown") height += step;
        else return;
        event.preventDefault();
        mtfLensSetFrameSize(width, height);
        mtfLensPersistFrameSize();
        mtfLensScheduleRender();
      });
      nodes.interaction?.addEventListener("pointermove", event => {
        if (!mtfLensRuntime.open) return;
        const rect = nodes.interaction.getBoundingClientRect();
        mtfLensRuntime.crosshair = {
          visible: true,
          x: event.clientX - rect.left,
          y: event.clientY - rect.top,
        };
        mtfLensScheduleInteractionRender();
      });
      nodes.interaction?.addEventListener("pointerleave", () => {
        mtfLensRuntime.crosshair.visible = false;
        mtfLensScheduleInteractionRender();
      });
      window.addEventListener("storage", mtfLensHandleSharedObjectsStorageEvent);
    }

    window.addEventListener("pagehide", () => mtfLensClose());
