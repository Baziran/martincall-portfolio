    const TICK_FLOW_INDICATOR_ID = "tick_flow";

    ensureManagedIndicatorStateGroups();
    state.tickContext ??= null;
    state.tickContextKey ??= "";
    state.tickContextLoadedAt ??= 0;
    state.loadingTickContext ??= false;
    state.tickContextDesiredRequest ??= null;
    state.tickContextRequestPromise ??= null;
    state.tickLiveStatus ??= null;
    state.loadingTickLive ??= false;
    state.tickLiveDesiredRequest ??= null;
    state.tickLiveRequestPromise ??= null;
    state.tickLiveDesiredKey ??= "";
    state.tickLiveSettledKey ??= "";
    state.tickLiveScopeKey ??= "";
    state.tickLiveTerminalErrorKey ??= "";
    state.tickLiveRetryState ??= null;

    const TICK_LIVE_RETRY_DELAYS_MS = Object.freeze([
      5000,
      15000,
      30000,
    ]);

    function tickFlowGroup() {
      return state.indicators?.tickFlow || null;
    }

    function tickFlowBadgeNode() {
      let badge = document.getElementById("tick-flow-badge");
      if (badge) return badge;
      const strip = document.querySelector(".chart-status-strip");
      if (!strip) return null;
      badge = document.createElement("span");
      badge.id = "tick-flow-badge";
      badge.className = "quality-badge hidden";
      badge.textContent = "TICK OFF";
      const ibkrBadge = document.getElementById("ibkr-status-badge");
      if (ibkrBadge?.parentElement === strip) ibkrBadge.insertAdjacentElement("afterend", badge);
      else strip.appendChild(badge);
      return badge;
    }

    function tickLiveStatusForCurrentRoute(live) {
      if (!live || typeof live !== "object") return {};
      const status = String(live.status || "").toLowerCase();
      const routeOwned = Boolean(
        live.enabled
        || live.running
        || status === "live"
        || status === "starting"
      );
      if (!routeOwned) return live;
      const instrumentId = exactIdentityText(state.instrumentId);
      const routeFingerprint = instrumentRouteFingerprint();
      const routeIdentities = Array.isArray(live.route_identities)
        ? live.route_identities
        : [];
      const matchesCurrentRoute = Boolean(
        instrumentId
        && routeFingerprint
        && routeIdentities.some(identity => (
          exactIdentityText(identity?.instrument_id) === instrumentId
          && exactIdentityText(identity?.route_fingerprint) === routeFingerprint
        ))
      );
      if (matchesCurrentRoute) return live;
      return {
        ...live,
        enabled: false,
        running: false,
        status: "error",
        last_error: "Tick live collector is not active for the current instrument route.",
      };
    }

    function tickFlowStatusFact() {
      const cfg = tickFlowGroup() || {};
      const context = state.tickContext || {};
      const stats = context.stats || {};
      const live = tickLiveStatusForCurrentRoute(
        state.tickLiveStatus || stats.live || {},
      );
      const bufferHealth = live.health && typeof live.health === "object"
        ? live.health
        : live.buffer?.health && typeof live.buffer.health === "object" ? live.buffer.health : {};
      const bufferSeverity = String(bufferHealth.severity || "").toLowerCase();
      const source = String(stats.source || "").toLowerCase();
      const liveStatus = String(live.status || "").toLowerCase();
      const liveSymbols = Array.isArray(live.symbols) ? live.symbols.join(",") : "";
      const lastTick = stats.last_delta_ts ? `Last tick bucket ${stats.last_delta_ts}` : "No tick bucket in current window";
      if (!cfg.enabled) {
        return {
          code: "disabled",
          reason_code: "tick_flow_disabled",
          lines: ["Tick Flow is disabled."],
          error: "",
        };
      }
      if (cfg.live) {
        if (bufferSeverity === "error") {
          const error = bufferHealth.message || live.last_error || "IBKR tick buffer is dropping data.";
          return {
            code: "error",
            reason_code: "tick_buffer_error",
            lines: [error],
            error,
          };
        }
        if (bufferSeverity === "warn") {
          return {
            code: "degraded",
            reason_code: "tick_buffer_pressure",
            lines: [bufferHealth.message || "IBKR tick buffer is under pressure."],
            error: "",
          };
        }
        if (liveStatus === "live") {
          return {
            code: "live",
            reason_code: "tick_collector_live",
            lines: [
              "IBKR tick-by-tick collection is running.",
              liveSymbols ? `Symbols: ${liveSymbols}` : "",
              `Client ${live.client_id || "-"}`,
              lastTick,
            ].filter(Boolean),
            error: "",
          };
        }
        if (liveStatus === "starting" || state.loadingTickLive) {
          return {
            code: "connecting",
            reason_code: "tick_collector_starting",
            lines: ["Starting IBKR tick-by-tick collection for the current instrument."],
            error: "",
          };
        }
        if (liveStatus === "sleeping") {
          return {
            code: "sleeping",
            reason_code: "tick_collector_sleeping",
            lines: ["Server is sleeping; tick-by-tick collection is paused."],
            error: "",
          };
        }
        if (liveStatus === "error") {
          const error = live.last_error || "IBKR tick-by-tick collection failed.";
          return {
            code: "error",
            reason_code: String(live.reason_code || "tick_collector_error"),
            lines: [error],
            error,
          };
        }
        return {
          code: "pending",
          reason_code: "tick_collector_unconfirmed",
          lines: ["Live tick mode is enabled, but the collector is not confirmed yet."],
          error: "",
        };
      }
      if (source === "tick") {
        return {
          code: "ready",
          reason_code: "stored_tick_data_available",
          lines: [
            "Using stored provider-confirmed broker ticks for this window.",
            lastTick,
            `Volume ${Number(stats.total_volume || stats.profile_total_volume || 0).toLocaleString("en-US")}`,
          ],
          error: "",
        };
      }
      if (state.loadingTickContext) {
        return {
          code: "loading",
          reason_code: "tick_context_loading",
          lines: ["Loading Tick Flow context."],
          error: "",
        };
      }
      return {
        code: "unavailable",
        reason_code: "tick_context_unavailable",
        lines: ["No raw broker ticks are available in the current window. Tick Flow remains unavailable."],
        error: "",
      };
    }

    function tickFlowStatusPresentation(status) {
      const descriptor = uiPresentationStateDescriptor(status?.code);
      const lines = Array.isArray(status?.lines)
        ? status.lines.filter(line => typeof line === "string" && line.trim())
        : [];
      return {
        code: descriptor.state,
        tone: descriptor.tone,
        badgeText: `TICK ${descriptor.label}`,
        runtimeText: descriptor.label,
        className: ["ok", "warn", "bad"].includes(descriptor.tone) ? descriptor.tone : "",
        lines,
        error: typeof status?.error === "string" ? status.error : "",
        details: lines.join("\n") || "Tick Flow status is unavailable.",
      };
    }

    registerIndicatorRuntimeStateRef("tick_flow_service", () => {
      const group = tickFlowGroup() || {};
      if (!group.enabled) {
        return {
          text: "OFF",
          muted: true,
          actionClass: "bad",
          details: "Tick Flow calculation is off.",
        };
      }
      if (group.visible === false) {
        return {
          text: "HIDE",
          muted: true,
          actionClass: "bad",
          details: "Chart overlays are hidden; Tick Flow collection and calculation remain active.",
        };
      }
      const status = tickFlowStatusFact();
      const presentation = tickFlowStatusPresentation(status);
      return {
        text: presentation.runtimeText,
        muted: presentation.tone === "muted",
        actionClass: presentation.tone === "bad" ? "bad" : presentation.tone === "ok" ? "ok" : "warn",
        countsAsSignal: false,
        details: presentation.details,
      };
    });

    function updateTickFlowBadge() {
      const badge = tickFlowBadgeNode();
      const status = tickFlowStatusFact();
      const presentation = tickFlowStatusPresentation(status);
      const visible = Boolean(tickFlowGroup()?.enabled);
      if (badge) {
        badge.textContent = presentation.badgeText;
        badge.className = `quality-badge ${presentation.className}${visible ? "" : " hidden"}`.trim();
        if (typeof bindStatusTooltip === "function") {
          bindStatusTooltip(badge, {
            title: presentation.badgeText,
            lines: presentation.lines,
            error: presentation.error,
          });
        } else {
          badge.title = presentation.details;
        }
      }
      const statusNode = document.getElementById("tick-flow-live-status");
      if (statusNode) {
        statusNode.textContent = presentation.runtimeText;
        statusNode.title = presentation.details;
        statusNode.className = `indicator-note ${presentation.className}`.trim();
      }
    }

    function clearTickContext() {
      const changed = Boolean(
        state.tickContext
        || state.tickContextKey
        || state.tickContextLoadedAt
        || state.loadingTickContext
      );
      state.tickContext = null;
      state.tickContextKey = "";
      state.tickContextLoadedAt = 0;
      state.loadingTickContext = false;
      if (changed && state.snapshot) renderCharts(state.snapshot);
    }

    function applyTickLiveStatus(live) {
      state.tickLiveStatus = live && typeof live === "object" ? live : null;
      updateTickFlowBadge(state.snapshot);
    }

    function tickLiveRequestKey(request) {
      return JSON.stringify([
        request.instrumentId,
        request.routeFingerprint,
        request.enabled ? "live" : "off",
      ]);
    }

    function clearTickLiveSettlement() {
      state.tickLiveSettledKey = "";
      state.tickLiveTerminalErrorKey = "";
      state.tickLiveRetryState = null;
    }

    function settleTickLiveFailure(requestKey, retryable) {
      state.tickLiveSettledKey = requestKey;
      if (!retryable) {
        state.tickLiveTerminalErrorKey = requestKey;
        state.tickLiveRetryState = null;
        return;
      }
      const previousFailures = state.tickLiveRetryState?.key === requestKey
        ? Number(state.tickLiveRetryState.failures || 0)
        : 0;
      const failures = previousFailures + 1;
      const delayMs = TICK_LIVE_RETRY_DELAYS_MS[failures - 1];
      state.tickLiveRetryState = {
        key: requestKey,
        failures,
        nextAt: Number.isFinite(delayMs) ? Date.now() + delayMs : null,
        exhausted: !Number.isFinite(delayMs),
      };
    }

    async function syncTickLiveMode(enabled, options = {}) {
      const requestSymbol = options.symbol || state.symbol;
      const requestInstrumentId = exactIdentityText(options.instrumentId ?? state.instrumentId);
      const requestRouteFingerprint = exactIdentityText(
        options.routeFingerprint ?? instrumentRouteFingerprint()
      );
      if (enabled && (!requestInstrumentId || !requestRouteFingerprint)) return state.tickLiveStatus;
      const request = {
        enabled: Boolean(enabled),
        instrumentId: requestInstrumentId,
        routeFingerprint: requestRouteFingerprint,
        symbol: requestSymbol,
      };
      const requestKey = tickLiveRequestKey(request);
      const scopeKey = JSON.stringify([
        request.instrumentId,
        request.routeFingerprint,
      ]);
      if (
        options.manual === true
        || (state.tickLiveScopeKey && state.tickLiveScopeKey !== scopeKey)
      ) {
        clearTickLiveSettlement();
      }
      state.tickLiveScopeKey = scopeKey;
      state.tickLiveDesiredKey = requestKey;
      state.tickLiveDesiredRequest = request;
      if (state.tickLiveRequestPromise) return state.tickLiveRequestPromise;
      const requestPromise = (async () => {
        await Promise.resolve();
        try {
          while (state.tickLiveDesiredRequest) {
            const request = state.tickLiveDesiredRequest;
            state.tickLiveDesiredRequest = null;
            const requestKey = tickLiveRequestKey(request);
            const retryState = state.tickLiveRetryState?.key === requestKey
              ? state.tickLiveRetryState
              : null;
            const retryDue = Boolean(
              retryState
              && !retryState.exhausted
              && Number.isFinite(retryState.nextAt)
              && Date.now() >= retryState.nextAt
            );
            if (state.tickLiveTerminalErrorKey === requestKey) {
              continue;
            }
            if (state.tickLiveSettledKey === requestKey && !retryDue) {
              continue;
            }
            state.loadingTickLive = true;
            applyTickLiveStatus({
              ...(state.tickLiveStatus || {}),
              enabled: request.enabled,
              status: request.enabled ? "starting" : "off",
              symbols: request.symbol ? [request.symbol] : [],
              route_identities: request.enabled ? [{
                instrument_id: request.instrumentId,
                route_fingerprint: request.routeFingerprint,
              }] : [],
              route_fingerprints: request.enabled
                ? [request.routeFingerprint]
                : [],
            });
            try {
              const result = await fetchJson("/api/ticks/live", {
                method: "POST",
                cache: "no-store",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                  instrument_id: request.instrumentId,
                  expected_route_fingerprint: request.routeFingerprint,
                  enabled: request.enabled,
                }),
              });
              const typedError = result?.error && typeof result.error === "object"
                ? result.error
                : null;
              const responseScopeMatches = (
                exactIdentityText(result?.instrument_id) === request.instrumentId
                && exactIdentityText(result?.route_fingerprint) === request.routeFingerprint
              );
              const requestStillDesired = state.tickLiveDesiredKey === requestKey;
              const routeStillCurrent = (
                (
                  !request.instrumentId
                  && !request.routeFingerprint
                )
                || (
                  exactIdentityText(state.instrumentId) === request.instrumentId
                  && instrumentRouteFingerprint() === request.routeFingerprint
                )
              );
              if (requestStillDesired && routeStillCurrent) {
                if (result?.ok !== true || responseScopeMatches) {
                  applyTickLiveStatus(result?.live || null);
                } else {
                  applyTickLiveStatus({
                    enabled: false,
                    running: false,
                    status: "error",
                    reason_code: "tick_live_response_scope_mismatch",
                    last_error: "Tick live response scope does not match the requested instrument route.",
                  });
                }
              }
              if (requestStillDesired) {
                if (result?.ok === true && responseScopeMatches) {
                  state.tickLiveSettledKey = requestKey;
                  state.tickLiveTerminalErrorKey = "";
                  state.tickLiveRetryState = null;
                  if (routeStillCurrent) clearUiNotice("tick_flow");
                } else if (result?.ok === true) {
                  settleTickLiveFailure(requestKey, false);
                } else {
                  settleTickLiveFailure(
                    requestKey,
                    typedError?.retryable === true,
                  );
                }
              }
              if (
                requestStillDesired
                && routeStillCurrent
                && (result?.ok !== true || !responseScopeMatches)
                && request.enabled
              ) {
                setUiNotice(
                  "tick_flow",
                  "TICK_LIVE_UNAVAILABLE",
                  `Tick live: ${apiErrorMessage(result, result?.live?.last_error || "not available")}`,
                  { state: "degraded" },
                );
              }
            } catch (error) {
              console.warn("tick live failed", requestErrorMessage(error, "tick live failed"));
              const errorPayload = error?.payload && typeof error.payload === "object"
                ? error.payload
                : null;
              const typedError = errorPayload?.error && typeof errorPayload.error === "object"
                ? errorPayload.error
                : null;
              if (state.tickLiveDesiredKey === requestKey) {
                settleTickLiveFailure(
                  requestKey,
                  typedError ? typedError.retryable === true : true,
                );
              }
              if (
                state.tickLiveDesiredKey === requestKey
                && (
                  (!request.instrumentId && !request.routeFingerprint)
                  || (
                    exactIdentityText(state.instrumentId) === request.instrumentId
                    && instrumentRouteFingerprint() === request.routeFingerprint
                  )
                )
              ) {
                applyTickLiveStatus({
                  enabled: false,
                  status: "error",
                  symbols: request.symbol ? [request.symbol] : [],
                  last_error: requestErrorMessage(error, "tick live failed"),
                });
              }
            }
          }
          return state.tickLiveStatus;
        } finally {
          state.tickLiveRequestPromise = null;
          state.loadingTickLive = false;
          updateTickFlowBadge();
          syncTickFlowSettings();
        }
      })();
      state.tickLiveRequestPromise = requestPromise;
      return requestPromise;
    }

    function tickContextWindow(snapshot) {
      const bars = snapshot?.bars || [];
      if (!bars.length) return null;
      const group = tickFlowGroup() || {};
      const lastBarMs = Date.parse(bars[bars.length - 1].ts);
      if (!Number.isFinite(lastBarMs)) return null;
      const stepMs = Math.max(intervalMsFromState(), 60_000);
      const liveEndMs = Date.now();
      const endMs = group.live
        ? Math.max(lastBarMs + stepMs, liveEndMs)
        : lastBarMs + stepMs;
      const lookbackMs = clamp(Number(group.lookbackMinutes) || 120, 60, 360) * 60000;
      const firstMs = Date.parse(bars[0].ts);
      const startMs = Math.max(Number.isFinite(firstMs) ? firstMs : endMs - lookbackMs, endMs - lookbackMs);
      return {
        start: new Date(startMs).toISOString(),
        end: new Date(endMs).toISOString(),
      };
    }

    function tickPriceStep(snapshot) {
      const priceIncrement = Number(snapshot?.meta?.price_increment);
      if (Number.isFinite(priceIncrement) && priceIncrement > 0) return priceIncrement;
      return null;
    }

    function tickContextLevels(snapshot) {
      const levels = [];
      for (const level of snapshot?.levels || []) {
        const price = Number(level?.price);
        if (Number.isFinite(price)) levels.push({ name: level.name || level.kind || "level", price });
      }
      const setupLevel = tradeSetupExecutionAuthority(snapshot).ready
        ? Number(snapshot?.trade_setup?.level?.price)
        : NaN;
      if (Number.isFinite(setupLevel)) levels.push({ name: "Trade Setup", price: setupLevel });
      const seen = new Set();
      return levels.filter(level => {
        const step = tickPriceStep(snapshot);
        const key = Number.isFinite(step) && step > 0
          ? `${Math.round(Number(level.price) / step)}`
          : String(Number(level.price));
        if (seen.has(key)) return false;
        seen.add(key);
        return true;
      }).slice(0, 8);
    }

    function tickContextPollingActive(options = {}) {
      if (options.force) return true;
      return backgroundPollingActive();
    }

    async function loadTickContextOnce(snapshot = state.snapshot, options = {}) {
      const perfToken = window.mcPerfStart ? window.mcPerfStart("loadTickContext") : null;
      const group = tickFlowGroup();
      if (!group?.enabled || !snapshot?.bars?.length) {
        clearTickContext();
        if (window.mcPerfEnd) window.mcPerfEnd(perfToken, "disabled", 120);
        return null;
      }
      if (!tickContextPollingActive(options)) {
        if (window.mcPerfEnd) window.mcPerfEnd(perfToken, "inactive", 120);
        return state.tickContext;
      }
      const windowRange = tickContextWindow(snapshot);
      if (!windowRange) return null;
      const requestSymbol = state.symbol;
      const requestInstrumentId = exactIdentityText(state.instrumentId);
      const requestRouteFingerprint = instrumentRouteFingerprint();
      if (!requestInstrumentId || !requestRouteFingerprint) return null;
      const requestTimeframe = state.timeframe;
      const tickBucket = group.live ? "1 second" : "1 minute";
      const levelsPayload = tickContextLevels(snapshot);
      const levelsKey = JSON.stringify(levelsPayload);
      const requestKey = JSON.stringify([
        requestInstrumentId,
        requestRouteFingerprint,
        requestTimeframe,
        windowRange.start,
        windowRange.end,
        group.lookbackMinutes,
        tickBucket,
        levelsKey,
      ]);
      const cacheAgeMs = Date.now() - Number(state.tickContextLoadedAt || 0);
      const cacheTtlMs = group.live ? 850 : 45000;
      if (state.tickContext && state.tickContextKey === requestKey && cacheAgeMs >= 0 && cacheAgeMs < cacheTtlMs) {
        updateTickFlowBadge();
        return state.tickContext;
      }
      if (state.loadingTickContext && state.tickContextKey === requestKey) return state.tickContext;
      state.tickContextKey = requestKey;
      state.loadingTickContext = true;
      const params = new URLSearchParams({
        instrument_id: requestInstrumentId,
        expected_route_fingerprint: requestRouteFingerprint,
        start: windowRange.start,
        end: windowRange.end,
        bucket: tickBucket,
        limit: "180",
      });
      const priceStep = tickPriceStep(snapshot);
      if (Number.isFinite(priceStep) && priceStep > 0) params.set("price_step", String(priceStep));
      if (levelsPayload.length) params.set("levels", levelsKey);
      if (window.mcPerfEnabled && window.mcPerfEnabled()) params.set("debug", "true");
      try {
        const payload = await fetchJson(`/api/ticks/context?${params.toString()}`, { cache: "no-store", sharedTtlMs: group.live ? 0 : 1200 });
        if (window.mcRecordBackendTiming) {
          window.mcRecordBackendTiming("tick_aggregates", payload?.stats?.aggregates_compute_ms, `${requestSymbol} ${requestTimeframe}`);
        }
        if (
          state.instrumentId !== requestInstrumentId
          || instrumentRouteFingerprint() !== requestRouteFingerprint
          || state.timeframe !== requestTimeframe
          || state.tickContextKey !== requestKey
          || exactIdentityText(payload?.route_fingerprint) !== requestRouteFingerprint
        ) return payload;
        state.tickContext = payload?.ok ? payload : null;
        state.tickContextLoadedAt = payload?.ok ? Date.now() : 0;
        applyTickLiveStatus(payload?.stats?.live || state.tickLiveStatus);
        if (state.snapshot) renderCharts(state.snapshot);
        return payload;
      } catch (error) {
        console.warn("tick context failed", requestErrorMessage(error, "tick context failed"));
        if (state.instrumentId === requestInstrumentId && instrumentRouteFingerprint() === requestRouteFingerprint && state.timeframe === requestTimeframe && state.tickContextKey === requestKey) state.tickContext = null;
        state.tickContextLoadedAt = 0;
        return null;
      } finally {
        if (window.mcPerfEnd) window.mcPerfEnd(perfToken, `${requestSymbol} ${tickBucket}`, 180);
        if (state.tickContextKey === requestKey) state.loadingTickContext = false;
      }
    }

    async function loadTickContext(snapshot = state.snapshot, options = {}) {
      state.tickContextDesiredRequest = {
        snapshot,
        options: { ...options },
      };
      if (state.tickContextRequestPromise) {
        return state.tickContextRequestPromise;
      }
      const requestPromise = (async () => {
        let result = state.tickContext;
        try {
          while (state.tickContextDesiredRequest) {
            const request = state.tickContextDesiredRequest;
            state.tickContextDesiredRequest = null;
            result = await loadTickContextOnce(
              request.snapshot,
              request.options,
            );
          }
          return result;
        } finally {
          state.tickContextRequestPromise = null;
        }
      })();
      state.tickContextRequestPromise = requestPromise;
      return requestPromise;
    }

    function syncTickFlowSettings() {
      const group = tickFlowGroup();
      const liveControl = document.getElementById("tick-flow-live");
      if (liveControl) {
        liveControl.checked = Boolean(group?.enabled && group?.live);
        liveControl.disabled = !group?.enabled || state.loadingTickLive;
        liveControl.closest("label.toggle")?.classList.toggle(
          "indicator-show-disabled",
          liveControl.disabled,
        );
      }
      updateTickFlowBadge();
    }

    function renderTickFlowStatusPanel(_snapshot, host) {
      const node = host.mount("live-status", "indicator-note");
      if (!node) return;
      node.id = "tick-flow-live-status";
      updateTickFlowBadge();
    }

    async function syncTickFlowLifecycle(options = {}) {
      const group = tickFlowGroup();
      if (!group) return;
      if (options.reset) clearTickContext();
      if (!group.enabled) {
        if (group.live) {
          group.live = false;
          saveInstrumentIndicatorSetting("tickFlowLive", "false");
        }
        clearTickContext();
        await syncTickLiveMode(false);
        syncTickFlowSettings();
        return;
      }
      await syncTickLiveMode(Boolean(group.live));
      const snapshot = options.snapshot || state.snapshot;
      if (
        snapshot?.bars?.length
        && (!options.poll || group.live)
      ) {
        await loadTickContext(snapshot, {
          force: Boolean(options.force || options.reset),
        });
      }
      syncTickFlowSettings();
    }

    registerIndicatorLifecycleSync(
      TICK_FLOW_INDICATOR_ID,
      syncTickFlowLifecycle,
      { poll: true },
    );
    registerIndicatorSettingsSync(
      TICK_FLOW_INDICATOR_ID,
      syncTickFlowSettings,
    );
    registerIndicatorPanelRenderer(
      TICK_FLOW_INDICATOR_ID,
      renderTickFlowStatusPanel,
    );
    registerIndicatorProcessEffect("tick_flow_context", ({ group }) => {
      if (!group?.enabled && group?.live) {
        group.live = false;
        saveInstrumentIndicatorSetting("tickFlowLive", "false");
      }
      void syncTickFlowLifecycle({
        force: true,
        reason: "calc",
      });
      return true;
    });
    registerIndicatorControlEffect(
      "tick_flow_live",
      async ({ control, value, group, node, saveControlValue }) => {
        const requested = Boolean(value && group?.enabled);
        if (group) group.live = requested;
        if (!requested && value) {
          node.checked = false;
          saveControlValue(control, false);
        }
        await syncTickLiveMode(requested, { manual: true });
        if (group?.enabled) {
          await loadTickContext(state.snapshot, { force: true });
        } else {
          clearTickContext();
        }
        syncTickFlowSettings();
        applySettings();
        return true;
      },
    );
    for (const effectRef of [
      "tick_flow_recent_delta",
      "tick_flow_recent_delta_seconds",
      "tick_flow_context_reload",
    ]) {
      registerIndicatorControlEffect(effectRef, ({ group }) => {
        if (group?.enabled) {
          void loadTickContext(state.snapshot, { force: true });
        }
        return false;
      });
    }
    updateTickFlowBadge();
