    const discordSignalsFeedState = {
      requestKey: "",
      activeRequestKey: "",
      payload: null,
      loading: false,
      error: "",
      loadedAtMs: 0,
      connectionStatus: null,
      connectionLoading: false,
      connectionError: "",
      connectionLoadedAtMs: 0,
      generation: 0,
      controller: null,
    };
    const DISCORD_SIGNALS_TEXT_SIZE_SCALE = Object.freeze({
      small: 0.5,
      medium: 0.6,
      large: 0.7,
    });

    function refreshDiscordSignalsSettingsStatus() {
      if (typeof refreshIndicatorSettingsStatusBadges === "function") {
        refreshIndicatorSettingsStatusBadges(state.snapshot);
      }
    }

    function cancelDiscordSignalsRead() {
      discordSignalsFeedState.generation += 1;
      discordSignalsFeedState.controller?.abort();
      discordSignalsFeedState.controller = null;
      discordSignalsFeedState.activeRequestKey = "";
      discordSignalsFeedState.loading = false;
      discordSignalsFeedState.connectionLoading = false;
    }

    async function syncDiscordSignalsConnectionStatus(options = {}) {
      const recentlyLoaded = (
        discordSignalsFeedState.connectionStatus
        && Date.now() - discordSignalsFeedState.connectionLoadedAtMs < 4000
      );
      if (!options.force && recentlyLoaded) {
        return discordSignalsFeedState.connectionStatus;
      }
      const generation = options.generation;
      const controller = options.controller;
      if (
        discordSignalsFeedState.generation !== generation
        || discordSignalsFeedState.controller !== controller
        || controller?.signal?.aborted
      ) return null;
      discordSignalsFeedState.connectionLoading = true;
      discordSignalsFeedState.connectionError = "";
      refreshDiscordSignalsSettingsStatus();
      try {
        const payload = await fetchJson(
          "/api/indicators/discord-signals/status",
          {
            timeoutMs: 7000,
            dedupe: false,
            cache: "no-store",
            signal: controller.signal,
          },
        );
        if (
          discordSignalsFeedState.generation !== generation
          || discordSignalsFeedState.controller !== controller
          || controller.signal.aborted
          || !indicatorCalcForId("discord_signals")
        ) return null;
        discordSignalsFeedState.connectionStatus = payload;
        discordSignalsFeedState.connectionLoadedAtMs = Date.now();
        return payload;
      } catch (error) {
        if (
          discordSignalsFeedState.generation === generation
          && discordSignalsFeedState.controller === controller
          && !controller.signal.aborted
        ) {
          discordSignalsFeedState.connectionError = requestErrorMessage(
            error,
            "Discord connection status unavailable",
          );
        }
        return null;
      } finally {
        if (
          discordSignalsFeedState.generation === generation
          && discordSignalsFeedState.controller === controller
        ) {
          discordSignalsFeedState.connectionLoading = false;
          refreshDiscordSignalsSettingsStatus();
        }
      }
    }

    function discordSignalsFeedScope(snapshot = state.snapshot) {
      const meta = snapshot?.meta || {};
      const bars = Array.isArray(snapshot?.bars) ? snapshot.bars : [];
      return {
        instrumentId: exactIdentityText(meta.instrument_id || state.instrumentId),
        routeFingerprint: exactIdentityText(meta.route_fingerprint || instrumentRouteFingerprint()),
        timeframe: String(meta.timeframe || state.timeframe || "").trim(),
        firstBarTs: String(bars[0]?.ts || ""),
        lastBarTs: String(bars[bars.length - 1]?.ts || ""),
      };
    }

    function discordSignalsFeedScopeKey(scope) {
      return JSON.stringify([
        scope.instrumentId,
        scope.routeFingerprint,
        scope.timeframe,
        scope.firstBarTs,
        scope.lastBarTs,
      ]);
    }

    function discordSignalsPayloadMatchesScope(payload, scope) {
      return Boolean(
        payload?.ok
        && exactIdentityText(payload.instrument_id) === scope.instrumentId
        && exactIdentityText(payload.route_fingerprint) === scope.routeFingerprint
        && String(payload.timeframe || "") === scope.timeframe
      );
    }

    function discordSignalsReadCurrent(generation, controller, requestKey) {
      return Boolean(
        discordSignalsFeedState.generation === generation
        && discordSignalsFeedState.controller === controller
        && !controller.signal.aborted
        && indicatorCalcForId("discord_signals")
        && discordSignalsFeedScopeKey(discordSignalsFeedScope(state.snapshot)) === requestKey
      );
    }

    async function syncDiscordSignalsFeed(options = {}) {
      if (options.reset) {
        cancelDiscordSignalsRead();
        discordSignalsFeedState.requestKey = "";
        discordSignalsFeedState.payload = null;
        discordSignalsFeedState.error = "";
        discordSignalsFeedState.loadedAtMs = 0;
        refreshDiscordSignalsSettingsStatus();
        return null;
      }
      if (!indicatorCalcForId("discord_signals")) {
        cancelDiscordSignalsRead();
        discordSignalsFeedState.requestKey = "";
        discordSignalsFeedState.payload = null;
        discordSignalsFeedState.error = "";
        refreshDiscordSignalsSettingsStatus();
        return null;
      }
      const scope = discordSignalsFeedScope(options.snapshot || state.snapshot);
      if (!scope.instrumentId || !scope.routeFingerprint || !scope.timeframe) {
        cancelDiscordSignalsRead();
        return null;
      }
      const requestKey = discordSignalsFeedScopeKey(scope);
      if (
        !options.force
        && discordSignalsFeedState.activeRequestKey === requestKey
      ) return discordSignalsFeedState.payload;
      cancelDiscordSignalsRead();
      const generation = discordSignalsFeedState.generation;
      const controller = new AbortController();
      discordSignalsFeedState.controller = controller;
      discordSignalsFeedState.activeRequestKey = requestKey;
      try {
        const connectionStatus = await syncDiscordSignalsConnectionStatus({
          force: options.force === true,
          generation,
          controller,
        });
        if (!discordSignalsReadCurrent(generation, controller, requestKey)) return null;
        if (
          !connectionStatus
          || connectionStatus.ok === false
          || connectionStatus.configured === false
          || connectionStatus.gate?.active !== true
        ) {
          discordSignalsFeedState.payload = null;
          discordSignalsFeedState.error = "";
          return null;
        }
        const recentlyLoaded = (
          discordSignalsFeedState.requestKey === requestKey
          && Date.now() - discordSignalsFeedState.loadedAtMs < 4000
        );
        if (!options.force && recentlyLoaded) return discordSignalsFeedState.payload;
        discordSignalsFeedState.loading = true;
        discordSignalsFeedState.error = "";
        const query = new URLSearchParams({
          instrument_id: scope.instrumentId,
          route_fingerprint: scope.routeFingerprint,
          timeframe: scope.timeframe,
        });
        const payload = await fetchJson(
          `/api/indicators/discord-signals/feed?${query.toString()}`,
          {
            timeoutMs: 7000,
            dedupe: false,
            cache: "no-store",
            signal: controller.signal,
          },
        );
        const currentScope = discordSignalsFeedScope(state.snapshot);
        if (
          !discordSignalsReadCurrent(generation, controller, requestKey)
          || !discordSignalsPayloadMatchesScope(payload, currentScope)
        ) return null;
        discordSignalsFeedState.payload = payload;
        discordSignalsFeedState.requestKey = requestKey;
        discordSignalsFeedState.loadedAtMs = Date.now();
        if (typeof bumpChartRenderVersion === "function") bumpChartRenderVersion("indicators");
        if (state.snapshot) renderCharts(state.snapshot, { overlayImmediate: true, reason: "discord-signals" });
        return payload;
      } catch (error) {
        if (
          discordSignalsFeedState.generation === generation
          && discordSignalsFeedState.controller === controller
          && !controller.signal.aborted
        ) {
          discordSignalsFeedState.error = requestErrorMessage(error, "Discord signal feed unavailable");
        }
        return null;
      } finally {
        if (
          discordSignalsFeedState.generation === generation
          && discordSignalsFeedState.controller === controller
        ) {
          discordSignalsFeedState.controller = null;
          discordSignalsFeedState.activeRequestKey = "";
          discordSignalsFeedState.loading = false;
        }
      }
    }

    function discordSignalsEventControlKey(event) {
      const action = String(event?.effective_action || event?.action || "");
      if (event?.action === "directional_level") return "directionalLevels";
      if (action === "entry" || action === "add") return "entries";
      if (action === "exit") return "exits";
      if (action === "reduce") return "trims";
      return "management";
    }

    function discordSignalsAnchorBar(eventTs, bars) {
      const timestamp = Date.parse(eventTs || "");
      if (!Number.isFinite(timestamp) || !Array.isArray(bars) || !bars.length) return null;
      const index = chartBarIndexForEventTimestamp(bars, timestamp);
      return index >= 0 ? bars[index] : null;
    }

    function discordSignalsContractText(event) {
      if (event?.action === "directional_level") {
        const right = String(event?.option_right || "").toLowerCase() === "put" ? "P" : "C";
        const condition = event?.directional_condition === "below" ? "<" : ">";
        const rawLevel = event?.directional_level;
        const level = rawLevel === null || rawLevel === undefined || rawLevel === ""
          ? Number.NaN
          : Number(rawLevel);
        const reference = Number.isFinite(level)
          ? (Number.isInteger(level) ? level.toFixed(0) : String(level))
          : String(event?.directional_reference || "LEVEL").replaceAll("_", " ").toUpperCase();
        const confirmation = event?.directional_confirmation === "session_close"
          ? " EOD"
          : event?.directional_confirmation === "bar_close"
            ? " CLOSE"
            : "";
        return `${right}${condition}${reference}${confirmation}`;
      }
      const strike = Number(event?.strike);
      if (!Number.isFinite(strike)) return "CTX";
      const right = String(event?.option_right || "").toLowerCase() === "put" ? "P" : "C";
      return `${Number.isInteger(strike) ? strike.toFixed(0) : String(strike)}${right}`;
    }

    function discordSignalsEventMark(event) {
      const right = String(event?.option_right || "").toLowerCase();
      const directionalMark = right === "put" ? "▼" : right === "call" ? "▲" : "•";
      if (event?.action === "directional_level") {
        return right === "put" ? "P<" : right === "call" ? "C>" : "L";
      }
      const action = String(event?.effective_action || event?.action || "");
      if (action === "entry") return directionalMark;
      if (action === "add") return `${directionalMark}+`;
      if (action === "reduce") return `${directionalMark}T`;
      if (action === "exit") return `${directionalMark}✓`;
      if (event?.stop_mode === "breakeven") return "BE";
      return "M";
    }

    function discordSignalsEventTone(event) {
      if (event?.parse_status === "ambiguous" || event?.parse_status === "rejected") return "warning";
      const right = String(event?.option_right || "").toLowerCase();
      if (right === "call") return "positive";
      if (right === "put") return "negative";
      const action = String(event?.effective_action || event?.action || "");
      if (action === "reduce") return "warning";
      if (action === "exit") return "positive";
      return "neutral";
    }

    function discordSignalsFeedOverlays(snapshot) {
      const payload = discordSignalsFeedState.payload;
      const scope = discordSignalsFeedScope(snapshot);
      if (
        !indicatorCalcForId("discord_signals")
        || indicatorVisibleForId("discord_signals") === false
        || !discordSignalsPayloadMatchesScope(payload, scope)
      ) return [];
      const bars = Array.isArray(snapshot?.bars) ? snapshot.bars : [];
      const positionById = new Map(
        (Array.isArray(payload.positions) ? payload.positions : []).map(position => [position.position_id, position]),
      );
      const config = state.indicators?.discordSignals || {};
      const textSize = String(config.textSize || "medium");
      const markerFontScale = DISCORD_SIGNALS_TEXT_SIZE_SCALE[textSize]
        || DISCORD_SIGNALS_TEXT_SIZE_SCALE.medium;
      const tradePathOverlays = config.tradePaths === false
        ? []
        : (Array.isArray(payload.positions) ? payload.positions : []).flatMap(position => {
          if (position?.state !== "closed" || !position?.entry_at || !position?.exit_at) return [];
          const entryBar = discordSignalsAnchorBar(position.entry_at, bars);
          const exitBar = discordSignalsAnchorBar(position.exit_at, bars);
          const entryPrice = Number(entryBar?.close);
          const exitPrice = Number(exitBar?.close);
          if (
            !entryBar
            || !exitBar
            || entryBar.ts === exitBar.ts
            || !Number.isFinite(entryPrice)
            || !Number.isFinite(exitPrice)
          ) return [];
          const right = String(position.option_right || "").toLowerCase();
          const direction = String(position.underlying_bias || "flat");
          return [{
            type: "line",
            start_ts: entryBar.ts,
            end_ts: exitBar.ts,
            y1: entryPrice,
            y2: exitPrice,
            role: "external_option_trade_path",
            code: "DISCORD_TRADE_PATH",
            source: "discord_signals",
            tone: right === "put" ? "negative" : right === "call" ? "positive" : "neutral",
            direction,
            style: "dashed",
            width: 1.25,
            opacity: 0.62,
            arrow_head: true,
            arrow_size: 6,
            interactive: true,
            presentation_policy: "persistent",
            event_code: position.position_id,
            scenario: "external_option_signal",
            setup: "discord_advisory_trade_path",
          }];
        });
      const eventOverlays = (Array.isArray(payload.events) ? payload.events : []).flatMap(event => {
        const controlKey = discordSignalsEventControlKey(event);
        if (config[controlKey] === false) return [];
        const anchorBar = discordSignalsAnchorBar(event.published_at, bars);
        if (!anchorBar) return [];
        const linkedPosition = positionById.get(event.position_id);
        const strike = Number(event.strike ?? linkedPosition?.strike);
        const price = Number(anchorBar.close);
        if (!Number.isFinite(price)) return [];
        const right = event.option_right || linkedPosition?.option_right || "";
        const direction = String(
          event.underlying_bias || linkedPosition?.underlying_bias || "flat"
        );
        const premium = event.reported_premium === null || event.reported_premium === undefined
          ? Number.NaN
          : Number(event.reported_premium);
        const contractText = discordSignalsContractText({
          ...event,
          strike,
          option_right: right,
        });
        const commentary = String(event.raw_content || "").trim();
        if (event.action === "directional_level") {
          const anchorIndex = bars.indexOf(anchorBar);
          const startBar = bars[Math.max(anchorIndex - 1, 0)] || anchorBar;
          const commonLevelFacts = {
            ...(event.facts && typeof event.facts === "object" ? event.facts : {}),
            action: "DIRECTIONAL_LEVEL",
            source: "discord_signals",
            tone: discordSignalsEventTone(event),
            direction,
            interactive: true,
            presentation_policy: "persistent",
            control_key: controlKey,
            event_code: event.event_id,
            ...(commentary ? { commentary } : {}),
          };
          return [
            {
              type: "line",
              start_ts: startBar.ts,
              end_ts: anchorBar.ts,
              y1: price,
              y2: price,
              role: "external_directional_level",
              code: "DISCORD_DIRECTIONAL_LEVEL",
              style: "solid",
              width: 2,
              opacity: 0.78,
              ...commonLevelFacts,
            },
            {
              type: "marker",
              ts: anchorBar.ts,
              price,
              lines: [right === "put" ? "▼" : "▲", contractText],
              role: "external_directional_level_label",
              code: "DISCORD_DIRECTIONAL_LEVEL_LABEL",
              marker_color_policy: "theme",
              marker_glyph_color_policy: "tone",
              marker_font_scale: markerFontScale,
              marker_text_anchor: right === "put" ? "lower_left" : "upper_left",
              marker_anchor_dot: false,
              no_tick: true,
              ...commonLevelFacts,
            },
          ];
        }
        const effectiveAction = String(event.effective_action || event.action || "event");
        return [{
          ...(event.facts && typeof event.facts === "object" ? event.facts : {}),
          type: "marker",
          ts: anchorBar.ts,
          price,
          lines: [
            discordSignalsEventMark({ ...event, option_right: right }),
            `${contractText}${Number.isFinite(premium) ? ` @ ${premium}` : ""}`,
          ],
          role: `external_option_${effectiveAction}`,
          action: effectiveAction.toUpperCase(),
          code: `DISCORD_${effectiveAction.toUpperCase()}`,
          source: "discord_signals",
          tone: discordSignalsEventTone({ ...event, option_right: right }),
          direction,
          presentation_policy: "persistent",
          marker_color_policy: "theme",
          marker_glyph_color_policy: "tone",
          marker_font_scale: markerFontScale,
          marker_text_anchor: right === "call" ? "upper_left" : "lower_left",
          marker_anchor_dot: true,
          interactive: true,
          no_tick: true,
          control_key: controlKey,
          event_code: event.event_id,
          ...(commentary ? { commentary } : {}),
        }];
      });
      return [...tradePathOverlays, ...eventOverlays];
    }

    function discordSignalsFeedStateKey() {
      const payload = discordSignalsFeedState.payload;
      const config = state.indicators?.discordSignals || {};
      return JSON.stringify([
        discordSignalsFeedState.requestKey,
        payload?.revision || "",
        discordSignalsFeedState.loading ? "loading" : "idle",
        discordSignalsFeedState.error,
        config.entries !== false,
        config.exits !== false,
        config.trims !== false,
        config.management === true,
        config.tradePaths !== false,
        config.directionalLevels !== false,
        config.textSize || "medium",
      ]);
    }

    function renderDiscordSignalsFeedPanel(_snapshot, context) {
      const node = context.mount("feed-status", "indicator-note");
      if (!node) return;
      if (!context.calcEnabled) {
        node.textContent = "Feed is off for this exact instrument.";
        return;
      }
      const stats = discordSignalsFeedState.payload?.stats;
      const feedStatus = discordSignalsFeedState.connectionStatus
        || discordSignalsFeedState.payload?.feed_status
        || {};
      const gate = feedStatus.gate || {};
      const gateText = gate.active ? "Gate ON" : "Gate OFF";
      if (feedStatus.state === "configuration_required" || feedStatus.configured === false) {
        node.textContent = `${gateText} · CONFIG · Discord Desktop RPC, channel IDs, author ID, and bridge secret are required.`;
        return;
      }
      if (discordSignalsFeedState.connectionError || discordSignalsFeedState.error) {
        node.textContent = `${gateText} · ${discordSignalsFeedState.connectionError || discordSignalsFeedState.error}`;
        return;
      }
      node.textContent = [
        gateText,
        `Discord ${String(feedStatus.state || (discordSignalsFeedState.connectionLoading ? "loading" : "waiting"))}`,
        stats ? `${stats.message_count || 0} messages` : "waiting for feed",
        stats ? `${stats.parsed_count || 0} parsed` : "",
        stats ? `${stats.unmatched_count || 0} review` : "",
        stats ? `${stats.open_positions || 0} open` : "",
      ].filter(Boolean).join(" · ");
    }

    function discordSignalsConnectionRuntimeState() {
      if (!indicatorCalcForId("discord_signals")) {
        return {
          text: "OFF",
          muted: true,
          actionClass: "bad",
          countsAsSignal: false,
          details: "Calc is off; the Discord Gate is closed for this exact instrument.",
        };
      }
      const feedStatus = discordSignalsFeedState.connectionStatus
        || discordSignalsFeedState.payload?.feed_status
        || {};
      const gate = feedStatus.gate || {};
      const stateCode = String(feedStatus.state || "waiting").toLowerCase();
      const stats = discordSignalsFeedState.payload?.stats || {};
      const details = [
        `Gate: ${gate.active ? "ON" : "OFF"} (forced by Calc)`,
        `Connection: ${stateCode || "waiting"}`,
        `Targets: ${Number(gate.target_instrument_ids?.length || 0)}`,
        feedStatus.last_bridge_age_seconds !== null
          && feedStatus.last_bridge_age_seconds !== undefined
          && Number.isFinite(Number(feedStatus.last_bridge_age_seconds))
          ? `Last heartbeat: ${Number(feedStatus.last_bridge_age_seconds).toFixed(1)}s ago`
          : "",
        `${Number(stats.message_count || 0)} messages`,
        `${Number(stats.parsed_count || 0)} parsed events`,
        `${Number(stats.unmatched_count || 0)} events for review`,
      ].join(" · ");
      if (discordSignalsFeedState.connectionError) {
        return {
          text: "ERROR",
          muted: false,
          actionClass: "bad",
          countsAsSignal: false,
          details: discordSignalsFeedState.connectionError,
        };
      }
      if (feedStatus.ok === false || stateCode === "service_not_initialized") {
        return {
          text: "ERROR",
          muted: false,
          actionClass: "bad",
          countsAsSignal: false,
          details: feedStatus.last_error || "Discord Signals service is not initialized.",
        };
      }
      if (feedStatus.configured === false || stateCode === "configuration_required") {
        return {
          text: "CONFIG",
          muted: false,
          actionClass: "bad",
          countsAsSignal: false,
          details: feedStatus.last_error || "Discord Signals feed configuration is incomplete.",
        };
      }
      if (gate.server_sleeping) {
        return {
          text: "SLEEP",
          muted: false,
          actionClass: "warn",
          countsAsSignal: false,
          details,
        };
      }
      if (!gate.active) {
        return {
          text: "GATE OFF",
          muted: false,
          actionClass: "warn",
          countsAsSignal: false,
          details,
        };
      }
      if (stateCode === "companion_live") {
        return {
          text: "LIVE",
          muted: false,
          actionClass: "ok",
          countsAsSignal: false,
          details,
        };
      }
      if (stateCode === "companion_stale") {
        return {
          text: "STALE",
          muted: false,
          actionClass: "bad",
          countsAsSignal: false,
          details: "Discord companion heartbeat is stale. New messages may be missing.\n" + details,
        };
      }
      if (stateCode === "companion_error" || stateCode === "error") {
        return {
          text: "ERROR",
          muted: false,
          actionClass: "bad",
          countsAsSignal: false,
          details: feedStatus.last_error || details,
        };
      }
      if (discordSignalsFeedState.connectionLoading && !discordSignalsFeedState.connectionStatus) {
        return {
          text: "LOAD",
          muted: false,
          actionClass: "warn",
          countsAsSignal: false,
          details: "Reading Discord Gate and companion status.",
        };
      }
      return {
        text: "WAIT",
        muted: false,
        actionClass: "warn",
        countsAsSignal: false,
        details: feedStatus.last_error || details,
      };
    }

    registerIndicatorRuntimeStateRef(
      "discord_signals_connection",
      discordSignalsConnectionRuntimeState,
    );

    registerIndicatorRuntimeStateRef("discord_signals_feed", () => {
      if (indicatorVisibleForId("discord_signals") === false) {
        return {
          text: "HIDE",
          muted: true,
          actionClass: "bad",
          countsAsSignal: false,
          details: "Discord Signals markers are hidden on this chart.",
        };
      }
      const connectionState = discordSignalsConnectionRuntimeState();
      if (connectionState.text !== "LIVE") return connectionState;
      if (discordSignalsFeedState.loading) {
        return {
          text: "LOAD",
          muted: false,
          actionClass: "warn",
          countsAsSignal: false,
          details: "Connection is live; loading the exact-route signal projection.",
        };
      }
      if (discordSignalsFeedState.error) {
        return {
          text: "ERROR",
          muted: false,
          actionClass: "bad",
          countsAsSignal: false,
          details: discordSignalsFeedState.error,
        };
      }
      return connectionState;
    });

    registerIndicatorLifecycleSync(
      "discord_signals",
      syncDiscordSignalsFeed,
      { poll: true },
    );
    registerIndicatorProcessEffect("discord_signals_reload", () => {
      void syncDiscordSignalsFeed({ force: true });
      return false;
    });
    registerIndicatorPanelRenderer("discord_signals", renderDiscordSignalsFeedPanel);
    registerIndicatorOverlayContribution("discord_signals", {
      layer: "signals",
      enabled: () => (
        indicatorCalcForId("discord_signals")
        && indicatorVisibleForId("discord_signals") !== false
      ),
      collect: discordSignalsFeedOverlays,
      stateKey: discordSignalsFeedStateKey,
    });
