    function quoteStreamOpen() {
      return Boolean(
        state.transport.quote.socket
        && state.transport.quote.socket.readyState === WebSocket.OPEN
      );
    }

    function quoteStreamStale(maxAgeMs = QUOTE_STREAM_STALE_MS) {
      if (!quoteStreamOpen()) return false;
      const lastSeen = Number(state.transport.quote.lastStateAt || state.transport.quote.openedAt || 0);
      return lastSeen > 0 && Date.now() - lastSeen > maxAgeMs;
    }

    function quoteStreamStatusLabel() {
      if (state.serverSleeping) return "sleep";
      if (state.transport.quote.errorCode) return "error";
      const socket = state.transport.quote.socket;
      if (!socket) return state.requests?.screener?.loading ? "syncing" : "waiting";
      if (socket.readyState === WebSocket.CONNECTING) return "connecting";
      if (socket.readyState === WebSocket.OPEN) {
        if (!state.transport.quote.stateReady) return "syncing";
        if (quoteStreamStale()) return "stale";
        if (state.transport.quote.degradedWarning) return "degraded";
        return "live";
      }
      return "retry";
    }

    function setScreenerStateLabel(label = "") {
      const stateNode = document.getElementById("screener-state");
      if (!stateNode) return;
      const screenerError = state.screenerStatus && state.screenerStatus.ok === false;
      const storageError = state.storageStatus && state.storageStatus.ok === false;
      const quoteTransportError = String(state.transport.quote.errorMessage || "");
      const quoteWarning = String(state.transport.quote.degradedWarning || "");
      const resolvedLabel = label || (
        screenerError
          ? "error"
          : storageError ? "storage" : quoteTransportError ? "error" : quoteWarning ? "degraded" : quoteStreamStatusLabel()
      );
      const title = screenerError
        ? state.screenerStatus.message || "Screener refresh failed."
        : storageError
          ? state.storageStatus.message || "Storage/settings refresh failed."
          : quoteTransportError || quoteWarning;
      applyUiPresentationState(stateNode, resolvedLabel, { title });
    }

    const QUOTE_SHARED_WORKER_NAME = "aef-quote-stream-v1";
    const QUOTE_SHARED_WORKER_LEASE_MS = 15000;
    const QUOTE_SHARED_WORKER_CLOSE_ACK_MS = 2500;
    const QUOTE_SHARED_WORKER_STARTUP_TIMEOUT_MS = 5000;

    function failQuoteTransport(code, reason) {
      const exactCode = String(code || "QUOTE_SHARED_WORKER_UNAVAILABLE");
      const exactReason = String(reason || "shared quote transport unavailable").slice(0, 240);
      state.transport.quote.mode = "unavailable";
      if (window.mcTelemetrySet) {
        window.mcTelemetrySet("quote_transport.mode", "unavailable");
        window.mcTelemetrySet("quote_transport.shared_ports", 0);
        window.mcTelemetrySet("quote_transport.shared_groups", 0);
        window.mcTelemetrySet("quote_transport.last_error_code", exactCode);
        window.mcTelemetrySet("quote_transport.last_error_reason", exactReason);
        window.mcTelemetrySet("quote_transport.last_error_fatal", true);
      }
      const error = new Error(exactReason);
      error.code = exactCode;
      error.fatal = true;
      throw error;
    }

    function createQuoteStreamTransport(key, wsUrl, subscription) {
      const workerUrl = String(window.MARTINCALL_QUOTE_WORKER_URL || "");
      if (!workerUrl) failQuoteTransport(
        "QUOTE_SHARED_WORKER_ASSET_MISSING",
        "shared quote worker asset is not configured",
      );
      if (!("SharedWorker" in window)) failQuoteTransport(
        "QUOTE_SHARED_WORKER_UNSUPPORTED",
        "browser does not support the required shared quote transport",
      );
      try {
        const workerName = `${QUOTE_SHARED_WORKER_NAME}:${workerUrl}`;
        const worker = new window.SharedWorker(workerUrl, workerName);
        const port = worker.port;
        let leaseTimer = 0;
        let closeAckTimer = 0;
        let startupTimer = 0;
        let closing = false;
        let closed = false;
        let transport = null;
        const finalizeClose = (code, reason, options = {}) => {
          if (closed) return;
          const closeCode = Number(code);
          const exactCloseCode = Number.isFinite(closeCode) ? closeCode : 1000;
          const closeReason = typeof reason === "string"
            ? reason.slice(0, 240)
            : String(reason ?? "").slice(0, 240);
          closing = false;
          closed = true;
          window.clearInterval(leaseTimer);
          window.clearTimeout(closeAckTimer);
          window.clearTimeout(startupTimer);
          leaseTimer = 0;
          closeAckTimer = 0;
          startupTimer = 0;
          transport.readyState = WebSocket.CLOSED;
          worker.onerror = null;
          try {
            port.close();
          } catch (_) {
            // noop
          }
          if (options.closeAckTimeout && window.mcTelemetryInc) {
            window.mcTelemetryInc("quote_transport.close_ack_timeout");
          }
          if (window.mcTelemetrySet) {
            window.mcTelemetrySet("quote_transport.last_close_code", exactCloseCode);
            window.mcTelemetrySet("quote_transport.last_close_reason", closeReason);
            window.mcTelemetrySet("quote_transport.last_close_restart", options.restart === true);
            window.mcTelemetrySet("quote_transport.last_close_lease_expired", options.leaseExpired === true);
          }
          if (options.restart && window.mcTelemetryInc) {
            window.mcTelemetryInc("quote_transport.restart_close");
          }
          if (options.leaseExpired && window.mcTelemetryInc) {
            window.mcTelemetryInc("quote_transport.lease_expired");
          }
          if (typeof debugStep === "function") {
            const flags = [
              options.restart === true ? "restart" : "",
              options.leaseExpired === true ? "lease-expired" : "",
              options.closeAckTimeout === true ? "close-ack-timeout" : "",
            ].filter(Boolean).join(",");
            debugStep(
              "quote transport close",
              `${exactCloseCode}${flags ? ` ${flags}` : ""}${closeReason ? ` · ${closeReason}` : ""}`,
            );
          }
          const handler = transport.onclose;
          if (typeof handler === "function") {
            handler({
              code: exactCloseCode,
              reason: closeReason,
              wasClean: exactCloseCode === 1000,
              sharedTransport: true,
              restart: options.restart === true,
              leaseExpired: options.leaseExpired === true,
              closeAck: options.closeAck === true,
              closeAckTimeout: options.closeAckTimeout === true,
            });
          }
        };
        transport = {
          readyState: WebSocket.CONNECTING,
          sharedQuoteTransport: true,
          onopen: null,
          onmessage: null,
          onerror: null,
          onclose: null,
          close(code = 1000, reason = "") {
            if (closed || closing || this.readyState === WebSocket.CLOSED) return;
            closing = true;
            this.readyState = WebSocket.CLOSING;
            window.clearInterval(leaseTimer);
            window.clearTimeout(startupTimer);
            leaseTimer = 0;
            startupTimer = 0;
            try {
              port.postMessage({
                type: Number(code) === 4002 ? "restart" : "close",
                key,
                code: Number(code) || 1000,
                reason: String(reason || ""),
              });
              closeAckTimer = window.setTimeout(() => {
                finalizeClose(code, reason, {
                  restart: Number(code) === 4002,
                  closeAckTimeout: true,
                });
              }, QUOTE_SHARED_WORKER_CLOSE_ACK_MS);
            } catch (_) {
              finalizeClose(code, reason, {
                restart: Number(code) === 4002,
                closeAckTimeout: true,
              });
            }
          },
        };
        const recordTransportError = (code, reason, options = {}) => {
          const errorCode = String(code || "QUOTE_SHARED_WORKER_ERROR").slice(0, 120);
          const errorReason = String(reason || "shared quote transport error").slice(0, 240);
          const fatal = options.fatal === true;
          if (window.mcTelemetrySet) {
            window.mcTelemetrySet("quote_transport.last_error_code", errorCode);
            window.mcTelemetrySet("quote_transport.last_error_reason", errorReason);
            window.mcTelemetrySet("quote_transport.last_error_fatal", fatal);
          }
          if (window.mcTelemetryInc) window.mcTelemetryInc("quote_transport.error");
          if (typeof debugStep === "function") {
            debugStep(
              "quote transport error",
              `${errorCode}${fatal ? " fatal" : ""} · ${errorReason}`,
            );
          }
          if (typeof transport.onerror === "function") {
            transport.onerror({
              code: errorCode,
              message: errorReason,
              fatal,
              sharedTransport: true,
            });
          }
        };
        const updateDiagnostics = message => {
          const sharedPorts = Math.max(Number(message?.shared_ports || 0), 0);
          const sharedGroups = Math.max(Number(message?.shared_groups || 0), 0);
          state.transport.quote.mode = "shared_worker";
          if (window.mcTelemetrySet) {
            window.mcTelemetrySet("quote_transport.mode", "shared_worker");
            window.mcTelemetrySet("quote_transport.shared_ports", sharedPorts);
            window.mcTelemetrySet("quote_transport.shared_groups", sharedGroups);
          }
          const workerSentAt = Number(message?.worker_sent_at || 0);
          if (workerSentAt > 0 && window.mcTelemetryMax) {
            window.mcTelemetryMax(
              "quote_transport.worker_to_page_max_ms",
              Math.max(Date.now() - workerSentAt, 0),
            );
          }
        };
        port.onmessage = event => {
          const message = event?.data;
          if (!message || message.key !== key || closed) return;
          if (closing && message.type !== "close" && message.type !== "error") return;
          updateDiagnostics(message);
          if (message.type === "open") {
            window.clearTimeout(startupTimer);
            startupTimer = 0;
            transport.readyState = WebSocket.OPEN;
            if (typeof transport.onopen === "function") {
              transport.onopen({ sharedTransport: true });
            }
            return;
          }
          if (message.type === "message") {
            if (transport.readyState === WebSocket.CONNECTING) {
              window.clearTimeout(startupTimer);
              startupTimer = 0;
              transport.readyState = WebSocket.OPEN;
              if (typeof transport.onopen === "function") {
                transport.onopen({ sharedTransport: true });
              }
            }
            if (typeof transport.onmessage === "function") {
              transport.onmessage({
                data: String(message.data || ""),
                sharedTransport: true,
              });
            }
            return;
          }
          if (message.type === "error") {
            recordTransportError(
              message.code,
              message.message,
              { fatal: message.fatal === true },
            );
            return;
          }
          if (message.type === "close") {
            const workerCloseCode = Number(message.code);
            const closeCode = Number.isFinite(workerCloseCode)
              ? Math.max(workerCloseCode, 0)
              : 1006;
            const closeReason = typeof message.reason === "string"
              ? message.reason
              : "shared quote transport closed";
            finalizeClose(closeCode, closeReason, {
              restart: message.restart === true,
              leaseExpired: message.lease_expired === true,
              closeAck: message.close_ack === true,
            });
          }
        };
        port.onmessageerror = () => {
          if (window.mcTelemetryInc) window.mcTelemetryInc("quote_transport.message_error");
          recordTransportError(
            "QUOTE_SHARED_WORKER_MESSAGE_ERROR",
            "shared quote transport message error",
            { fatal: true },
          );
          transport.close(4002, "shared quote transport message error");
        };
        port.onclose = () => {
          if (closed) return;
          const duringStartup = transport.readyState === WebSocket.CONNECTING;
          const reason = duringStartup
            ? "shared worker port closed during startup"
            : "shared worker port closed";
          recordTransportError("QUOTE_SHARED_WORKER_PORT_CLOSED", reason, { fatal: true });
          finalizeClose(1006, reason);
        };
        worker.onerror = event => {
          event?.preventDefault?.();
          const reason = String(event?.message || "shared worker failed to load").slice(0, 240);
          recordTransportError("QUOTE_SHARED_WORKER_LOAD_ERROR", reason, { fatal: true });
          finalizeClose(1006, reason);
        };
        port.start();
        port.postMessage({ type: "subscribe", key, ws_url: wsUrl, subscription });
        startupTimer = window.setTimeout(() => {
          if (closed || closing || transport.readyState !== WebSocket.CONNECTING) return;
          const reason = "shared worker startup timed out";
          recordTransportError("QUOTE_SHARED_WORKER_STARTUP_TIMEOUT", reason, { fatal: true });
          finalizeClose(1006, reason);
        }, QUOTE_SHARED_WORKER_STARTUP_TIMEOUT_MS);
        leaseTimer = window.setInterval(() => {
          if (closed) return;
          try {
            port.postMessage({ type: "lease", key });
          } catch (_) {
            transport.close(1006, "shared quote lease failed");
          }
        }, QUOTE_SHARED_WORKER_LEASE_MS);
        state.transport.quote.mode = "shared_worker";
        if (window.mcTelemetrySet) {
          window.mcTelemetrySet("quote_transport.mode", "shared_worker");
        }
        return transport;
      } catch (error) {
        if (window.mcTelemetryInc) window.mcTelemetryInc("quote_transport.worker_create_error");
        const reason = typeof requestErrorMessage === "function"
          ? requestErrorMessage(error, "shared worker create failed")
          : "shared_worker_create_failed";
        if (window.mcTelemetrySet) {
          window.mcTelemetrySet("quote_transport.last_error_code", "QUOTE_SHARED_WORKER_CREATE_ERROR");
          window.mcTelemetrySet("quote_transport.last_error_reason", String(reason).slice(0, 240));
          window.mcTelemetrySet("quote_transport.last_error_fatal", true);
        }
        if (typeof debugStep === "function") {
          debugStep("quote transport error", `QUOTE_SHARED_WORKER_CREATE_ERROR fatal · ${String(reason).slice(0, 240)}`);
        }
        failQuoteTransport(
          "QUOTE_SHARED_WORKER_CREATE_ERROR",
          reason,
        );
      }
    }

    function sharedStreamStorageKey(kind, key) {
      return `aef:sharedStream:${kind}:${key}`;
    }

    function readSharedStreamOwner(kind, key) {
      try {
        const payload = JSON.parse(localStorage.getItem(sharedStreamStorageKey(kind, key)) || "{}");
        return payload && typeof payload === "object" ? payload : {};
      } catch {
        return {};
      }
    }

    function pruneSharedStreamOwners() {
      try {
        for (let index = localStorage.length - 1; index >= 0; index -= 1) {
          const itemKey = localStorage.key(index);
          if (itemKey && itemKey.startsWith("aef:sharedStream:")) localStorage.removeItem(itemKey);
        }
      } catch (_) {
        // Storage may be unavailable; background-work coalescing degrades to per-tab work.
      }
    }

    function writeSharedStreamOwner(kind, key, extra = {}) {
      const payload = {
        ...(extra && typeof extra === "object" ? extra : {}),
        owner: state.clientId,
        expires: Date.now() + SHARED_STREAM_OWNER_TTL_MS,
      };
      try {
        rawLocalStorageSetItem(sharedStreamStorageKey(kind, key), JSON.stringify(payload));
        return true;
      } catch (error) {
        pruneSharedStreamOwners();
        try {
          rawLocalStorageSetItem(sharedStreamStorageKey(kind, key), JSON.stringify(payload));
          return true;
        } catch (_) {
          console.warn("shared stream owner storage disabled", error);
          return false;
        }
      }
    }

    function acquireSharedStreamOwner(kind, key, extra = {}) {
      if (!("BroadcastChannel" in window) || !key) return true;
      const owner = readSharedStreamOwner(kind, key);
      const now = Date.now();
      if (!owner.owner || owner.owner === state.clientId || Number(owner.expires || 0) < now) {
        return writeSharedStreamOwner(kind, key, extra) || true;
      }
      return false;
    }

    function releaseSharedStreamOwner(kind, key) {
      if (!key) return;
      const owner = readSharedStreamOwner(kind, key);
      if (owner.owner === state.clientId) {
        try {
          localStorage.removeItem(sharedStreamStorageKey(kind, key));
        } catch (_) {
          // noop
        }
      }
    }

    function backgroundPollKey() {
      return JSON.stringify([
        activeWorkspaceSlot,
        state.instrumentId,
        instrumentRouteFingerprint(),
        String(state.timeframe ?? ""),
      ]);
    }

    function ownsBackgroundPolling() {
      if (document.visibilityState !== "visible" || state.serverSleeping) return false;
      return acquireSharedStreamOwner("poll", backgroundPollKey());
    }

    function backgroundPollingActive() {
      if (state.requests.market.loading || state.requests.history.loading || state.requests.market.pending) return false;
      const chartRequired = providerCapability(state.dataSource, "chart_stream")
        && (typeof chartStreamAllowedForRange !== "function" || chartStreamAllowedForRange())
        && (typeof currentInstrumentSessionClosed !== "function" || !currentInstrumentSessionClosed());
      if (chartRequired && !state.transport.chart.hasLiveBar) return false;
      return ownsBackgroundPolling();
    }

    const WINDOW_LINK_CHANNEL_NAME = "aef:window-link:v1";
    const WINDOW_LINK_MESSAGE_MAX_AGE_MS = 1500;
    const WINDOW_LINK_READ_MODEL_MESSAGE_MAX_AGE_MS = 10000;
    const WINDOW_LINK_READ_MODEL_RESPONSE_MS = 80;
    const WINDOW_LINK_LOCAL_PRIORITY_MS = 250;
    let windowLinkChannel = null;
    let linkedCrosshairSequence = 0;
    let linkedCrosshairOutboundFrame = 0;
    let linkedCrosshairInboundFrame = 0;
    let linkedCrosshairPendingOutbound = null;
    let linkedCrosshairInboundOrder = 0;
    const linkedCrosshairPendingInbound = new Map();
    let linkedCrosshairLocalPriorityUntil = 0;
    let linkedCrosshairActiveRemote = "";
    const linkedCrosshairRemoteSequences = new Map();

    function publishWatchlistPresentation(instrumentId, presentation) {
      const identity = exactIdentityText(instrumentId);
      const normalized = normalizeWatchlistPresentation(presentation);
      if (!windowLinkChannel || !identity || !normalized) return;
      try {
        windowLinkChannel.postMessage({
          version: 1,
          type: "watchlist_presentation_changed",
          source_client_id: state.clientId,
          sent_at: Date.now(),
          instrument_id: identity,
          presentation: normalized,
        });
        if (window.mcTelemetryInc) window.mcTelemetryInc("watchlist_presentation.sent");
      } catch (error) {
        if (window.mcTelemetryInc) window.mcTelemetryInc("watchlist_presentation.send_error");
        debugStep("watchlist presentation", requestErrorMessage(error, "send failed"));
      }
    }

    function shareReadModel(model, scope, payload = undefined) {
      const readModel = ["gex_context", "watchlist_trends"].includes(model) ? model : "";
      const isSnapshot = payload !== undefined;
      if (
        !windowLinkChannel
        || !readModel
        || !scope
        || typeof scope !== "object"
        || Array.isArray(scope)
        || (isSnapshot && (!payload || typeof payload !== "object" || Array.isArray(payload)))
      ) return;
      try {
        windowLinkChannel.postMessage({
          version: 1,
          type: isSnapshot ? "read_model_snapshot" : "read_model_requested",
          source_client_id: state.clientId,
          sent_at: Date.now(),
          model: readModel,
          scope,
          ...(isSnapshot ? { payload } : {}),
        });
        if (window.mcTelemetryInc) {
          window.mcTelemetryInc(`${readModel}.${isSnapshot ? "sent" : "requested"}`);
        }
      } catch (error) {
        if (window.mcTelemetryInc) {
          window.mcTelemetryInc(`${readModel}.${isSnapshot ? "send_error" : "request_error"}`);
        }
        debugStep(readModel.replaceAll("_", " "), requestErrorMessage(error, isSnapshot ? "send failed" : "request failed"));
      }
    }

    function queueSharedReadModelMessage(message) {
      const sourceClientId = exactIdentityText(message?.source_client_id);
      const sentAt = Number(message?.sent_at);
      if (
        message?.version !== 1
        || !["read_model_requested", "read_model_snapshot"].includes(message?.type)
        || !["gex_context", "watchlist_trends"].includes(message?.model)
        || !sourceClientId
        || sourceClientId === state.clientId
        || !Number.isFinite(sentAt)
        || Math.abs(Date.now() - sentAt) > WINDOW_LINK_READ_MODEL_MESSAGE_MAX_AGE_MS
        || !message.scope
        || typeof message.scope !== "object"
        || Array.isArray(message.scope)
        || (
          message.type === "read_model_snapshot"
          && (!message.payload || typeof message.payload !== "object" || Array.isArray(message.payload))
        )
      ) return;
      if (message.model === "gex_context") queueSharedGexContextMessage(message);
      else queueSharedWatchlistTrendMessage(message);
    }

    function queueWatchlistPresentationMessage(message) {
      const sourceClientId = exactIdentityText(message?.source_client_id);
      const instrumentId = exactIdentityText(message?.instrument_id);
      const presentation = normalizeWatchlistPresentation(message?.presentation);
      if (
        message?.version !== 1
        || message?.type !== "watchlist_presentation_changed"
        || !sourceClientId
        || sourceClientId === state.clientId
        || !instrumentId
        || !presentation
        || !Number.isFinite(Number(message.sent_at))
      ) return;
      try {
        const changed = applyWatchlistInstrumentPresentation(instrumentId, presentation, { refreshTrend: false });
        if (changed) {
          if (window.mcTelemetryInc) window.mcTelemetryInc("watchlist_presentation.applied");
        }
        if (presentation.display_mode === "trend") {
          loadWatchlistTrends({ instrumentIds: new Set([instrumentId]) });
        }
      } catch (error) {
        if (window.mcTelemetryInc) window.mcTelemetryInc("watchlist_presentation.apply_error");
        debugStep("watchlist presentation", requestErrorMessage(error, "apply failed"));
      }
    }

    function dispatchWindowLinkMessage(message) {
      if (message?.type === "crosshair") {
        queueLinkedCrosshairMessage(message);
      } else if (message?.type === "watchlist_presentation_changed") {
        queueWatchlistPresentationMessage(message);
      } else if (["read_model_requested", "read_model_snapshot"].includes(message?.type)) {
        queueSharedReadModelMessage(message);
      }
    }

    function linkedCrosshairScope() {
      const instrumentId = exactIdentityText(state.instrumentId);
      const routeFingerprint = exactIdentityText(instrumentRouteFingerprint());
      const timeframe = String(state.timeframe || "");
      const workspaceSlot = String(state.workspaceSlot || "");
      if (!instrumentId || !routeFingerprint || !timeframe || !workspaceSlot) return null;
      return {
        workspace_slot: workspaceSlot,
        instrument_id: instrumentId,
        route_fingerprint: routeFingerprint,
        timeframe,
      };
    }

    function linkedCrosshairInputBlocked() {
      return Boolean(
        Date.now() < linkedCrosshairLocalPriorityUntil
        || view.dragActive
        || state.drawing?.drag
        || state.drawing?.draft
        || state.alerts?.drag
        || state.optionTargets?.draggingId
        || state.paperTrading?.dragOrder
        || state.paperTrading?.dragEntryLabel
        || (typeof contextMenuCrosshairLocked === "function" && contextMenuCrosshairLocked())
      );
    }

    function flushLinkedCrosshairOutbound() {
      linkedCrosshairOutboundFrame = 0;
      const pending = linkedCrosshairPendingOutbound;
      linkedCrosshairPendingOutbound = null;
      const scope = linkedCrosshairScope();
      if (!windowLinkChannel || !pending || !scope) return;
      linkedCrosshairSequence += 1;
      const crosshair = pending.crosshair || {};
      try {
        windowLinkChannel.postMessage({
          version: 1,
          type: "crosshair",
          source_client_id: state.clientId,
          sequence: linkedCrosshairSequence,
          sent_at: Date.now(),
          ...scope,
          visible: pending.visible,
          source: crosshair.source === "volume" ? "volume" : "price",
          ts: typeof crosshair.ts === "string" ? crosshair.ts : null,
          price: crosshair.price !== null
            && crosshair.price !== undefined
            && Number.isFinite(Number(crosshair.price))
            ? Number(crosshair.price)
            : null,
          snap_kind: typeof crosshair.snapKind === "string" ? crosshair.snapKind : null,
        });
        if (window.mcTelemetryInc) window.mcTelemetryInc("crosshair_link.sent");
      } catch (error) {
        if (window.mcTelemetryInc) window.mcTelemetryInc("crosshair_link.send_error");
        debugStep("crosshair link", requestErrorMessage(error, "send failed"));
      }
    }

    function publishLinkedCrosshair(crosshair, options = {}) {
      if (!windowLinkChannel) return;
      const visible = options.visible !== false;
      linkedCrosshairLocalPriorityUntil = visible
        ? Date.now() + WINDOW_LINK_LOCAL_PRIORITY_MS
        : 0;
      linkedCrosshairActiveRemote = "";
      linkedCrosshairPendingOutbound = {
        crosshair: crosshair && typeof crosshair === "object" ? { ...crosshair } : {},
        visible,
      };
      if (options.immediate) {
        if (linkedCrosshairOutboundFrame) cancelAnimationFrame(linkedCrosshairOutboundFrame);
        flushLinkedCrosshairOutbound();
        return;
      }
      if (!linkedCrosshairOutboundFrame) {
        linkedCrosshairOutboundFrame = requestAnimationFrame(flushLinkedCrosshairOutbound);
      }
    }

    function clearLinkedRemoteCrosshair(sourceClientId) {
      if (linkedCrosshairActiveRemote !== sourceClientId) return false;
      linkedCrosshairActiveRemote = "";
      state.crosshair.visible = false;
      if (typeof refreshCrosshairOverlays === "function") {
        refreshCrosshairOverlays({ immediate: true });
      }
      return true;
    }

    function applyLinkedCrosshairMessage(message, paintPerf = null) {
      linkedCrosshairInboundFrame = 0;
      if (
        !Number.isFinite(Number(message?.sent_at))
        || Math.abs(Date.now() - Number(message.sent_at)) > WINDOW_LINK_MESSAGE_MAX_AGE_MS
      ) {
        clearLinkedRemoteCrosshair(exactIdentityText(message?.source_client_id));
        return;
      }
      const scope = linkedCrosshairScope();
      if (!scope || linkedCrosshairInputBlocked()) return;
      if (
        message.workspace_slot !== scope.workspace_slot
        || message.instrument_id !== scope.instrument_id
        || message.route_fingerprint !== scope.route_fingerprint
        || message.timeframe !== scope.timeframe
      ) return;
      if (message.visible === false) {
        clearLinkedRemoteCrosshair(message.source_client_id);
        return;
      }
      if (message.source !== "price" && message.source !== "volume") return;
      const geo = typeof priceChartGeometry === "function" ? priceChartGeometry() : null;
      if (!geo) {
        clearLinkedRemoteCrosshair(message.source_client_id);
        return;
      }
      const messageTsKey = drawingTimestampIdentity(message.ts);
      const allBars = Array.isArray(geo.allBars) ? geo.allBars : [];
      const absoluteIndex = allBars.findIndex(bar => (
        drawingTimestampIdentity(bar?.ts) === messageTsKey
        && barIsConfirmedClosed(bar)
        && bar?.authoritative !== false
      ));
      const screenIndex = absoluteIndex >= 0 ? absoluteIndex - geo.start : null;
      const x = Number.isFinite(screenIndex) ? geo.x(screenIndex) : null;
      if (
        !messageTsKey
        || !Number.isFinite(screenIndex)
        || !isChartPlotXVisible(x, geo.pad, geo.rect.width)
      ) {
        clearLinkedRemoteCrosshair(message.source_client_id);
        return;
      }
      const source = message.source;
      const price = message.price === null || message.price === undefined
        ? Number.NaN
        : Number(message.price);
      const mappedY = source === "price" && Number.isFinite(price) ? geo.y(price) : Number.NaN;
      const priceIsVisible = source === "price"
        && Number.isFinite(mappedY)
        && mappedY >= geo.pad.top
        && mappedY <= geo.pad.top + geo.priceH;
      const resolvedBar = allBars[absoluteIndex];
      state.crosshair = {
        visible: true,
        source: source === "price" && !priceIsVisible ? "time" : source,
        x,
        y: priceIsVisible ? mappedY : geo.pad.top,
        index: Math.round(screenIndex),
        ts: resolvedBar.ts,
        price: priceIsVisible ? price : null,
        snapKind: priceIsVisible && typeof message.snap_kind === "string"
          ? message.snap_kind
          : null,
      };
      linkedCrosshairActiveRemote = message.source_client_id;
      if (typeof refreshCrosshairOverlays === "function") {
        refreshCrosshairOverlays({ immediate: true });
      }
      if (window.mcPerfEnd) window.mcPerfEnd(paintPerf, state.crosshair.source || "none", 20);
      if (window.mcTelemetryInc) window.mcTelemetryInc("crosshair_link.applied");
    }

    function queueLinkedCrosshairMessage(message) {
      if (!message || typeof message !== "object") return;
      const sourceClientId = exactIdentityText(message.source_client_id);
      const sequence = message.sequence;
      const sentAt = message.sent_at;
      if (
        message.version !== 1
        || message.type !== "crosshair"
        || typeof message.visible !== "boolean"
        || !sourceClientId
        || sourceClientId === state.clientId
        || !Number.isSafeInteger(sequence)
        || sequence <= 0
        || sequence <= (linkedCrosshairRemoteSequences.get(sourceClientId) ?? 0)
        || typeof sentAt !== "number"
        || !Number.isFinite(sentAt)
        || Math.abs(Date.now() - sentAt) > WINDOW_LINK_MESSAGE_MAX_AGE_MS
      ) return;
      const scope = linkedCrosshairScope();
      if (
        !scope
        || message.workspace_slot !== scope.workspace_slot
        || exactIdentityText(message.instrument_id) !== scope.instrument_id
        || exactIdentityText(message.route_fingerprint) !== scope.route_fingerprint
        || String(message.timeframe || "") !== scope.timeframe
      ) return;
      linkedCrosshairRemoteSequences.set(sourceClientId, sequence);
      linkedCrosshairInboundOrder += 1;
      linkedCrosshairPendingInbound.set(sourceClientId, {
        message,
        order: linkedCrosshairInboundOrder,
        paintPerf: window.mcPerfStart ? window.mcPerfStart("crosshairRemoteToCommit") : null,
      });
      if (!linkedCrosshairInboundFrame) {
        linkedCrosshairInboundFrame = requestAnimationFrame(() => {
          const pendingRows = Array.from(linkedCrosshairPendingInbound.values());
          const visibleRows = pendingRows
            .filter(row => row.message.visible === true)
            .sort((left, right) => left.order - right.order);
          const pending = visibleRows[visibleRows.length - 1]
            || linkedCrosshairPendingInbound.get(linkedCrosshairActiveRemote)
            || null;
          linkedCrosshairPendingInbound.clear();
          if (!pending) {
            linkedCrosshairInboundFrame = 0;
            return;
          }
          try {
            applyLinkedCrosshairMessage(pending.message, pending.paintPerf);
          } catch (error) {
            linkedCrosshairInboundFrame = 0;
            if (window.mcTelemetryInc) window.mcTelemetryInc("crosshair_link.apply_error");
            debugStep("crosshair link", requestErrorMessage(error, "apply failed"));
          }
        });
      }
    }

    function setupWindowLinkChannel() {
      if (windowLinkChannel || !("BroadcastChannel" in window)) return;
      try {
        windowLinkChannel = new BroadcastChannel(WINDOW_LINK_CHANNEL_NAME);
        windowLinkChannel.onmessage = event => dispatchWindowLinkMessage(event.data);
        windowLinkChannel.onmessageerror = () => {
          if (window.mcTelemetryInc) window.mcTelemetryInc("crosshair_link.message_error");
        };
      } catch (error) {
        windowLinkChannel = null;
        debugStep("crosshair link", requestErrorMessage(error, "channel unavailable"));
      }
    }

    function closeWindowLinkChannel() {
      if (!windowLinkChannel) return;
      publishLinkedCrosshair(state.crosshair, { visible: false, immediate: true });
      try {
        windowLinkChannel.close();
      } catch (_) {
        // noop
      }
      windowLinkChannel = null;
      if (linkedCrosshairOutboundFrame) cancelAnimationFrame(linkedCrosshairOutboundFrame);
      if (linkedCrosshairInboundFrame) cancelAnimationFrame(linkedCrosshairInboundFrame);
      linkedCrosshairOutboundFrame = 0;
      linkedCrosshairInboundFrame = 0;
      linkedCrosshairPendingOutbound = null;
      linkedCrosshairPendingInbound.clear();
      linkedCrosshairRemoteSequences.clear();
      linkedCrosshairActiveRemote = "";
      state.crosshair.visible = false;
    }
