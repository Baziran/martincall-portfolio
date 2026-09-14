    function formatBytes(value) {
      const bytes = Number(value || 0);
      if (!Number.isFinite(bytes) || bytes <= 0) return "-";
      const units = ["B", "KB", "MB", "GB", "TB"];
      let current = bytes;
      let unitIndex = 0;
      while (current >= 1024 && unitIndex < units.length - 1) {
        current /= 1024;
        unitIndex += 1;
      }
      const decimals = unitIndex >= 2 ? 1 : 0;
      return `${current.toFixed(decimals)} ${units[unitIndex]}`;
    }

    function formatDuration(seconds) {
      const total = Math.max(Math.floor(Number(seconds || 0)), 0);
      const hours = Math.floor(total / 3600);
      const minutes = Math.floor((total % 3600) / 60);
      return hours ? `${hours}h ${minutes}m` : `${minutes}m`;
    }

    function gexRuntimeTooltip(gex = {}) {
      const instrumentId = exactIdentityText(state.instrumentId);
      const routeFingerprint = instrumentRouteFingerprint();
      const identityKey = instrumentId && routeFingerprint
        ? JSON.stringify([instrumentId, routeFingerprint])
        : "";
      const runtime = gex.routes?.[identityKey] && typeof gex.routes[identityKey] === "object"
        ? gex.routes[identityKey]
        : {};
      const diag = runtime.last_diagnostics || {};
      const backoff = runtime.failure_backoff || {};
      const reliability = gex.reliability_by_identity?.[identityKey] || {};
      const freshness = reliability.freshness || {};
      const latestError = Object.entries(runtime.errors || {})
        .map(([source, error]) => ({ source, ...(error && typeof error === "object" ? error : {}) }))
        .sort((left, right) => (Date.parse(right.at || "") || 0) - (Date.parse(left.at || "") || 0))[0] || null;
      const num = (value) => {
        const number = Number(value);
        return Number.isFinite(number) ? String(Math.round(number)) : "";
      };
      const pct = (value) => {
        const number = Number(value);
        return Number.isFinite(number) ? `${(number * 100).toFixed(1)}%` : "-";
      };
      const requestedTicks = String(gex.generic_ticks || "").trim();
      const effectiveTicks = requestedTicks || "-";
      const rows = [
        "--------------------",
        "GEX client:",
        `Connected: ${gex.connected ? "yes" : "no"} · request ${gex.request_connected ? "yes" : "no"} · stream ${gex.live_connected ? "yes" : "no"}`,
        `Stream sessions: ${Array.isArray(gex.sessions) ? gex.sessions.length : 0} · subscriptions ${Number(gex.subscriptions || 0)}/${Number(gex.subscription_budget || 0)} · over ${Number(gex.subscription_over_budget || 0)} · busy ${gex.lock_busy ? "yes" : "no"}`,
        `Provider: ${gex.provider || "-"} · endpoint ${gex.endpoint || "-"} · session ${gex.request_session || "-"}`,
        `Request preference: ${gex.requested_market_data_entitlement || "unknown"} · ticks ${effectiveTicks} · not actual entitlement`,
        `Batch: ${gex.batch_size || "-"} · max contracts ${gex.max_contracts || "-"} · expiry ${gex.expiry_mode || "-"}`,
        `Queue: pending ${Number(gex.queue_pending || 0)} · running ${gex.queue_running ? (gex.queue_running_label || "yes") : "no"}`,
        `Instrument: ${state.symbol || "-"}`,
        runtime.last_sync_at ? `Last sync: ${runtime.last_sync_at}` : "Last sync: never",
        runtime.last_request ? `Last request: ${runtime.last_request}` : "",
        `Requests: ${Number(runtime.request_count || 0)}`,
        diag.diag_contracts_requested || diag.diag_option_rows ? `Last option rows: ${num(diag.diag_option_rows) || "0"}/${num(diag.diag_contracts_requested) || "0"}` : "",
        diag.diag_strikes_requested || diag.diag_strike_rows ? `Last strikes: ${num(diag.diag_strike_rows) || "0"}/${num(diag.diag_strikes_requested) || "0"}` : "",
        diag.diag_usable_gex_rows || diag.diag_open_interest_rows ? `Last usable GEX rows: ${num(diag.diag_usable_gex_rows) || "0"} · OI rows ${num(diag.diag_open_interest_rows) || "0"}` : "",
        Number.isFinite(Number(reliability.open_interest_available_share)) ? `OI available share: ${pct(reliability.open_interest_available_share)}` : "",
        Number.isFinite(Number(reliability.open_interest_unavailable_share)) ? `OI unavailable share: ${pct(reliability.open_interest_unavailable_share)} (${Number(reliability.open_interest_unavailable_rows || 0)} rows)` : "",
        freshness.status ? `Freshness: ${String(freshness.status).toUpperCase()}${Number.isFinite(Number(freshness.age_seconds)) ? ` · age ${Number(freshness.age_seconds).toFixed(1)}s` : ""}` : "",
        backoff.active ? `Backoff: ${Number(backoff.retry_in_seconds || 0).toFixed(1)}s · ${backoff.source || "provider"} · failures ${Number(backoff.consecutive_failures || 0)}` : "",
        diag.diag_collection_timeout ? `Last timeout: ${diag.diag_collection_timeout_phase || "option market data"}` : "",
        latestError?.message ? `Last error: ${latestError.message} · ${latestError.source}` : "",
      ].filter(Boolean);
      return rows.join("\n");
    }

    let systemHealthRenderFrame = 0;
    let systemHealthRenderTimer = 0;
    let systemHealthRenderPending = false;
    let systemHealthLastRenderAt = 0;

    function renderSystemHealth(options = {}) {
      systemHealthRenderPending = true;
      if (options.immediate || options.force) {
        if (systemHealthRenderFrame) {
          cancelAnimationFrame(systemHealthRenderFrame);
          systemHealthRenderFrame = 0;
        }
        if (systemHealthRenderTimer) {
          clearTimeout(systemHealthRenderTimer);
          systemHealthRenderTimer = 0;
        }
        systemHealthRenderPending = false;
        systemHealthLastRenderAt = Date.now();
        renderSystemHealthNow();
        return;
      }
      if (systemHealthRenderFrame || systemHealthRenderTimer) return;
      const now = Date.now();
      const remainingMs = 1000 - (now - systemHealthLastRenderAt);
      if (remainingMs > 0) {
        systemHealthRenderTimer = setTimeout(() => {
          systemHealthRenderTimer = 0;
          renderSystemHealth({ force: true });
        }, remainingMs);
        return;
      }
      systemHealthRenderFrame = requestAnimationFrame(() => {
        systemHealthRenderFrame = 0;
        if (!systemHealthRenderPending) return;
        systemHealthRenderPending = false;
        systemHealthLastRenderAt = Date.now();
        renderSystemHealthNow();
      });
    }

    function renderSystemHealthNow() {
      const node = document.getElementById("system-health");
      const timeNode = document.getElementById("system-health-time");
      if (!node) return;
      const health = state.systemHealth;
      if (!health) {
        node.innerHTML = `
          <div class="health-card"><span>Backend</span><b>loading</b></div>
          <div class="health-card"><span>Database</span><b>loading</b></div>
          <div class="health-card"><span>Rows</span><b>-</b></div>
          <div class="health-card"><span>Memory</span><b>-</b></div>
        `;
        if (timeNode) timeNode.textContent = "Not checked yet";
        return;
      }
      const liveStorage = health.storage || {};
      const hasStorageDiagnostics = Boolean(
        systemStorageDiagnostics && typeof systemStorageDiagnostics === "object"
      );
      const storage = {
        ...(hasStorageDiagnostics ? systemStorageDiagnostics : {}),
        ...liveStorage,
      };
      const app = health.app || {};
      const ibkr = health.ibkr || {};
      const gatewayLoginControl = ibkr.gateway_login_control || {};
      const chartStream = health.chart_stream || {};
      const ibkrSelfHeal = health.ibkr_self_heal || {};
      const runtimeObservability = health.runtime_observability || {};
      const ibkrLaneHealth = runtimeObservability.ibkr_lanes || {};
      const tickLiveHealth = runtimeObservability.tick_live || {};
      const gexRuntime = health.gex || {};
      const gexInstrumentId = exactIdentityText(state.instrumentId);
      const gexRouteFingerprint = instrumentRouteFingerprint();
      const gexIdentityKey = gexInstrumentId && gexRouteFingerprint
        ? JSON.stringify([gexInstrumentId, gexRouteFingerprint])
        : "";
      const gexReliability = gexRuntime.reliability_by_identity?.[gexIdentityKey] || {};
      const sleep = health.sleep || {};
      const sleeping = Boolean(sleep.sleeping);
      state.serverSleeping = sleeping;
      const gexScheduler = health.gex?.scheduler || {};
      const quoteSnapshots = health.quote_snapshots || {};
      const optionCaps = health.options?.premium_caps || {};
      const telegram = health.telegram || {};
      const interactiveTelegram = telegram.interactive || {};
      const dbOk = Boolean(storage.ok);
      const ibkrLive = Boolean(ibkr.live_ok);
      const ibkrConnected = Boolean(ibkr.quote_connected || ibkr.chart_connected || ibkr.history_connected);
      const ibkrIssue = String(ibkr.live_issue || ibkr.ibkr_flood_warning || "");
      const ibkrCompeting = Boolean(ibkr.competing_session);
      const ibkrSelfHealBlocked = Boolean(ibkrSelfHeal.blocked);
      const ibkrNoisy = Boolean(ibkrIssue);
      const appOk = health.status === "ok" || dbOk || ibkrConnected;
      const dbSettings = storage.settings || {};
      const latest = storage.latest_bar_ts ? axisTimeFormatter.format(new Date(storage.latest_bar_ts)) : "-";
      const snapshotMeta = state.snapshot?.meta || {};
      const freshness = snapshotMeta.freshness && typeof snapshotMeta.freshness === "object" ? snapshotMeta.freshness : {};
      const quality = effectiveDataQuality(snapshotMeta);
      const freshnessStatus = String(freshness.status || "unknown").toUpperCase();
      const freshnessAge = Number(freshness.bar_age_seconds);
      const freshnessText = Number.isFinite(freshnessAge)
        ? `${freshnessStatus} ${Math.round(freshnessAge)}s`
        : freshnessStatus;
      const sourceText = String(snapshotMeta.source || "-");
      const qualityText = String(quality.status || "unknown").toUpperCase();
      const preview = snapshotMeta.preview && typeof snapshotMeta.preview === "object" ? snapshotMeta.preview : {};
      const provisionalData = preview.active === true
        || snapshotMeta.live_bar_closed === false;
      const dataLaneText = provisionalData ? "PREVIEW" : "CONFIRMED";
      const dataLaneClass = provisionalData
        ? "warn"
        : freshnessStatus === "STALE" ? "warn" : freshnessStatus === "FRESH" ? "ok" : "";
      const dataLaneTip = [
        `Lane: ${dataLaneText}`,
        `Source: ${sourceText}`,
        `Quality: ${qualityText}`,
        `Freshness: ${freshnessText}`,
        provisionalData ? `Policy: ${preview.policy || "preview_only_until_exchange_confirmed"}` : "",
        preview.confirmed_latest_ts || snapshotMeta.analysis_ts ? `Confirmed latest: ${preview.confirmed_latest_ts || snapshotMeta.analysis_ts}` : "",
        preview.latest_ts ? `Latest candle: ${preview.latest_ts}` : "",
        provisionalData ? "Execution uses confirmed candles only." : "",
      ].filter(Boolean).join("\n");
      const memoryText = app.mem_total_bytes
        ? `${formatBytes(app.mem_available_bytes)} free / ${formatBytes(app.mem_total_bytes)}`
        : formatBytes(app.rss_bytes);
      const buildVersion = String(app.version || "dev");
      const buildGit = String(app.git_sha || "");
      const buildAtRaw = String(app.built_at || "");
      const buildAtText = buildAtRaw ? localTimeFormatter.format(new Date(buildAtRaw)) : "local / unknown";
      const buildTip = [
        `Version: ${buildVersion}`,
        buildGit ? `Git: ${buildGit}` : "",
        buildAtRaw ? `Image built: ${buildAtRaw}` : "Image built: not recorded (local dev)",
      ].filter(Boolean).join("\n");
      const overlayDiagnostics = window.mcTelemetryState?.["indicator_overlays.diagnostics"] || {};
      const overlayInvalid = Number(overlayDiagnostics.invalid || 0);
      const overlayRenderable = Number(overlayDiagnostics.renderable || 0);
      const overlayPolicyHidden = Number(overlayDiagnostics.policy_hidden || 0);
      const overlayAxisCulled = Number(overlayDiagnostics.axis_culled || 0);
      const overlayCompaction = state.snapshot?.meta?.indicator_overlay_compaction || {};
      const overlayCompactionLines = Object.entries(overlayCompaction).map(
        ([source, values]) => `${source}: compacted ${Number(values?.dropped || 0)} · retained ${Number(values?.retained || 0)}`,
      );
      const overlayCompacted = Object.values(overlayCompaction).reduce(
        (total, values) => total + Number(values?.dropped || 0),
        0,
      );
      const overlayInvalidLines = Object.entries(overlayDiagnostics.invalid_by_source || {}).flatMap(
        ([source, reasons]) => Object.entries(reasons || {}).map(
          ([reason, count]) => `${source}: ${reason} × ${Number(count || 0)}`,
        ),
      );
      const overlayDiagnosticsTip = [
        `Renderable: ${overlayRenderable}`,
        `Policy-hidden: ${overlayPolicyHidden}`,
        `Axis-culled: ${overlayAxisCulled}`,
        `History compacted: ${overlayCompacted}`,
        `Invalid drops: ${overlayInvalid}`,
        ...overlayCompactionLines,
        ...overlayInvalidLines,
      ].join("\n");
      const gexAvailableShare = Number(gexReliability.open_interest_available_share);
      const gexUnavailableShare = Number(gexReliability.open_interest_unavailable_share);
      const gexFreshness = gexReliability.freshness || {};
      const gexReliabilityText = [
        Number.isFinite(gexAvailableShare) ? `OI available ${(gexAvailableShare * 100).toFixed(1)}%` : "OI available -",
        Number.isFinite(gexUnavailableShare) ? `OI unknown ${(gexUnavailableShare * 100).toFixed(1)}%` : "OI unknown -",
        gexFreshness.status
          ? `${String(gexFreshness.status).toUpperCase()}${Number.isFinite(Number(gexFreshness.age_seconds)) ? ` ${Number(gexFreshness.age_seconds).toFixed(0)}s` : ""}`
          : "freshness -",
      ].join(" · ");
      const gexReliabilityWarn = Number.isFinite(gexUnavailableShare) && gexUnavailableShare >= 0.5;
      const quoteSnapshotPersisted = Number(quoteSnapshots.persisted || 0);
      const quoteSnapshotSkipped = Number(quoteSnapshots.skipped_unchanged || 0);
      const quoteSnapshotTotal = quoteSnapshotPersisted + quoteSnapshotSkipped;
      const quoteSnapshotSkipShare = quoteSnapshotTotal > 0 ? quoteSnapshotSkipped / quoteSnapshotTotal : 0;
      const quoteSnapshotTip = [
        `Persisted: ${quoteSnapshotPersisted}`,
        `Skipped unchanged: ${quoteSnapshotSkipped}`,
        `First: ${Number(quoteSnapshots.first || 0)}`,
        `Changed: ${Number(quoteSnapshots.changed || 0)}`,
        `Keepalive: ${Number(quoteSnapshots.keepalive || 0)}`,
        `Skip share: ${(quoteSnapshotSkipShare * 100).toFixed(1)}%`,
      ].join("\n");
      const dataHygiene = storage.data_hygiene || {};
      const dataHygieneSummary = dataHygiene.summary || {};
      const dataHygieneSeverity = String(dataHygiene.severity || "ok").toLowerCase();
      const provisionalGroups = Number(dataHygieneSummary.provisional_groups || 0);
      const maxDeadRatio = Number(dataHygieneSummary.max_dead_ratio || 0);
      const dataHygieneTip = [
        `Window: ${dataHygiene.window || "-"}`,
        `Provisional row groups: ${provisionalGroups}`,
        `Max dead tuple ratio: ${(maxDeadRatio * 100).toFixed(1)}%`,
      ].join("\n");
      const dataHygieneText = `${dataHygieneSeverity.toUpperCase()} · P${provisionalGroups} · D${(maxDeadRatio * 100).toFixed(0)}%`;
      const futuresContinuous = Array.isArray(storage.futures_continuous) ? storage.futures_continuous : [];
      const futuresContinuousText = futuresContinuous.length
        ? futuresContinuous.slice(0, 3).map(item => `${item.instrument_key || "-"} ${item.timeframe || "-"} ${item.last_bar_ts || "-"}`).join(" · ")
        : "none";
      const futuresContinuousLines = futuresContinuous.slice(0, 8).map(item => (
        `${item.provider || "-"} ${item.instrument_key || "-"} ${item.timeframe || "-"} ${item.series_type || "-"} ${item.roll_policy || "-"} ${item.last_bar_ts || "-"}`
      )).join("\n") || "No canonical futures rows";
      const dataHygieneLines = [
        ...(Array.isArray(dataHygiene.provisional_rows) ? dataHygiene.provisional_rows.slice(0, 5).map(item => (
          `${item.provider || "-"} ${item.symbol || "-"} ${item.timeframe || "-"} ${item.source || "-"} closed=${item.closed === false ? "N" : "Y"} rows=${Number(item.rows || 0)}`
        )) : []),
        ...(Array.isArray(dataHygiene.table_pressure) ? dataHygiene.table_pressure.slice(0, 5).map(item => (
          `${item.table || "-"} dead=${Number(item.dead_rows_est || 0).toLocaleString()} ratio=${(Number(item.dead_ratio || 0) * 100).toFixed(1)}%`
        )) : []),
      ].slice(0, 10).join("\n") || "No hygiene warnings";
      const runtimeSeverity = String(runtimeObservability.severity || "ok").toLowerCase();
      const laneSeverity = String(ibkrLaneHealth.severity || "ok").toLowerCase();
      const lanePendingSeconds = Number(ibkrLaneHealth.max_pending_seconds || 0);
      const laneRunningSeconds = Number(ibkrLaneHealth.max_running_seconds || 0);
      const pendingLanes = Array.isArray(ibkrLaneHealth.pending_lanes) ? ibkrLaneHealth.pending_lanes.join(", ") : "";
      const runningLanes = Array.isArray(ibkrLaneHealth.running_lanes) ? ibkrLaneHealth.running_lanes.join(", ") : "";
      const laneTip = [
        `Runtime: ${runtimeSeverity.toUpperCase()}`,
        `IBKR lane reason: ${ibkrLaneHealth.reason || "ok"}`,
        pendingLanes ? `Pending lanes: ${pendingLanes}` : "Pending lanes: none",
        runningLanes ? `Running lanes: ${runningLanes}` : "Running lanes: none",
        `Max pending: ${lanePendingSeconds.toFixed(1)}s`,
        `Max running: ${laneRunningSeconds.toFixed(1)}s`,
      ].join("\n");
      const tickSeverity = String(tickLiveHealth.severity || "ok").toLowerCase();
      const tickDropped = Number(tickLiveHealth.dropped || 0);
      const tickBuffered = Number(tickLiveHealth.buffered || 0);
      const tickBufferRatio = Number(tickLiveHealth.buffer_ratio || 0);
      const tickHealthTip = [
        `Severity: ${tickSeverity.toUpperCase()}`,
        `Reason: ${tickLiveHealth.reason || "ok"}`,
        tickLiveHealth.message || "",
        `Buffered: ${tickBuffered}`,
        `Dropped: ${tickDropped}`,
        `Buffer: ${(tickBufferRatio * 100).toFixed(1)}%`,
        `DB errors: ${Number(tickLiveHealth.db_consecutive_errors || 0)}`,
        `Purge errors: ${Number(tickLiveHealth.purge_consecutive_errors || 0)}`,
      ].filter(Boolean).join("\n");
      const chartStreamClients = Number(chartStream.clients || 0);
      const chartStreamCount = Number(chartStream.streams || 0);
      const chartActive = chartStream.active_chart || {};
      const chartActiveAge = Number(chartActive.age_seconds || 0);
      const chartStreamTip = [
        `Browser clients: ${chartStreamClients}`,
        `Browser streams: ${chartStreamCount}`,
        `Broker streams: ${Number(ibkr.chart_streams || 0)}`,
        chartActive.symbol ? `Active: ${chartActive.symbol} ${chartActive.interval || "-"} ${chartActive.range || "-"}` : "Active: -",
        chartActive.symbol ? `Active age: ${chartActiveAge.toFixed(1)}s` : "",
      ].filter(Boolean).join("\n");
      const ibkrStatusText = sleeping
        ? "SLEEP"
        : ibkrSelfHealBlocked
        ? "SELF-HEAL STOP"
        : ibkrCompeting
        ? "DUP SESSION"
        : ibkrNoisy
          ? "IBKR WARN"
          : ibkrLive
        ? `LIVE ${Number(ibkr.quote_values || 0)}/${Number(ibkr.quote_subscriptions || 0)}`
        : ibkrConnected
          ? String(ibkr.status || "connected").replaceAll("_", " ").toUpperCase()
          : "DISCONNECTED";
      const chartError = String(ibkr.last_chart_error || "");
      const quietChartBackoff = ibkr.chart_connected && chartError.toLowerCase().includes("connect backoff active");
      const ibkrTip = [
        `Status: ${ibkrStatusText}`,
        sleeping ? `Sleep reason: ${sleep.reason || "manual"}` : "",
        sleeping ? `Sleeping for: ${formatDuration(sleep.duration_seconds || 0)}` : "",
        `Gateway: ${ibkr.host || "127.0.0.1"}:${ibkr.port || "-"}`,
        `History session: ${ibkr.history_connected ? "connected" : "off"}`,
        `Chart stream: ${ibkr.chart_connected ? "connected" : "off"} (${Number(ibkr.chart_streams || 0)} streams, client ${ibkr.chart_client_id || "-"})`,
        `Quote session: ${ibkr.quote_connected ? "connected" : "off"}`,
        `Quote client: ${ibkr.quote_client_id || "-"}`,
        `Self-heal: ${ibkrSelfHealBlocked ? "BLOCKED" : "active"} (${Number(ibkrSelfHeal.attempts_in_window || 0)}/${Number(ibkrSelfHeal.max_attempts_in_window || 0)} in ${Number(ibkrSelfHeal.window_seconds || 0).toFixed(0)}s)`,
        ibkrSelfHeal.blocked_reason ? `Self-heal error: ${ibkrSelfHeal.blocked_reason}` : "",
        ibkrSelfHeal.last_issue?.reason ? `Self-heal last issue: ${ibkrSelfHeal.last_issue.reason}` : "",
        ibkrSelfHeal.last_error ? `Self-heal last error: ${ibkrSelfHeal.last_error}` : "",
        `Subscriptions: ${Number(ibkr.quote_subscriptions || 0)}`,
        `Live values: ${Number(ibkr.quote_values || 0)}`,
        ibkr.history_request ? `History request: ${ibkr.history_request}` : "",
        ibkr.history_busy_seconds ? `History busy: ${Number(ibkr.history_busy_seconds).toFixed(1)}s` : "",
        ibkrIssue,
        ibkr.last_api_error || "",
        ibkr.last_history_error || "",
        quietChartBackoff ? "" : chartError,
        ibkr.last_quote_error || "",
        laneSeverity !== "ok" ? laneTip : "",
        gexRuntimeTooltip(gexRuntime),
      ].filter(Boolean).join("\n");
      const storageDiagnosticsCards = hasStorageDiagnostics ? `
        <div class="health-card"><span>DB size</span><b>${formatBytes(storage.database_bytes)}</b></div>
        <div class="health-card ${dataHygieneSeverity === "ok" ? "ok" : "warn"}" title="${escapeHtml(dataHygieneTip)}"><span>Data hygiene</span><b>${escapeHtml(dataHygieneText)}</b></div>
        <div class="health-card ${futuresContinuous.length ? "ok" : "warn"}" title="${escapeHtml(futuresContinuousLines)}"><span>Futures continuous</span><b>${escapeHtml(futuresContinuousText)}</b></div>
        <div class="health-card"><span>Postgres</span><b>work ${escapeHtml(dbSettings.work_mem || "-")} · buf ${escapeHtml(dbSettings.shared_buffers || "-")}</b></div>
        <div class="health-card wide"><span>Futures Continuous</span><div class="health-lines">${escapeHtml(futuresContinuousLines)}</div></div>
        <div class="health-card wide"><span>Data Hygiene</span><div class="health-lines">${escapeHtml(dataHygieneLines)}</div></div>
      ` : "";
      node.innerHTML = `
        <div class="health-card wide ${sleeping ? "warn" : appOk ? "ok" : "bad"}">
          <div class="health-inline">
            <span>Backend</span>
            <b>${sleeping ? "SLEEP" : appOk ? "OK" : "DEGRADED"} · ${formatDuration(app.uptime_seconds)}</b>
            <div class="health-actions">
              <button id="server-sleep-toggle" class="tool-button" type="button" title="Pause or resume IBKR polling">${sleeping ? "Wake" : "Sleep"}</button>
              <button id="backend-restart" class="tool-button" type="button" title="Restart backend container">Restart</button>
            </div>
          </div>
          <div id="server-sleep-status" class="health-status health-status-line">${sleeping ? `Sleeping ${formatDuration(sleep.duration_seconds || 0)}` : "IBKR polling active"}</div>
          <div id="backend-restart-status" class="health-status hidden"></div>
        </div>
        <div class="health-card ${sleeping ? "warn" : ibkrNoisy || laneSeverity === "warn" ? "warn" : laneSeverity === "error" ? "bad" : ibkrLive ? "ok" : ibkrConnected ? "warn" : "bad"}">
          <span>IBKR</span>
          <b>${escapeHtml(ibkrStatusText)}</b>
          <div class="health-actions">
            <input id="ibkr-port-input" class="settings-control health-port-input" type="number" min="1" max="65535" step="1" value="${clamp(Number(state.settings.ibkrPort) || Number(ibkr.port) || 7497, 1, 65535)}" title="TWS paper/live usually 7497/7496; IB Gateway paper/live usually 4002/4001">
            <button id="tws-reconnect" class="tool-button" type="button" title="Drop MartinCall IBKR API sockets and reconnect them">API RST</button>
            ${gatewayLoginControl.configured ? '<button id="ibkr-gateway-login" class="tool-button" type="button" title="Cold-restart IB Gateway and request one new IB Key authorization">New login</button>' : ""}
          </div>
          <div id="tws-reconnect-status" class="health-status">IBKR API connection</div>
          ${gatewayLoginControl.configured ? '<div id="ibkr-gateway-login-status" class="health-status">IB Gateway login control ready</div>' : ""}
        </div>
        <div class="health-card ${tickSeverity === "error" || tickDropped > 0 ? "bad" : tickSeverity === "warn" ? "warn" : "ok"}" title="${escapeHtml(tickHealthTip)}"><span>Tick writer</span><b>${escapeHtml(tickSeverity.toUpperCase())} · ${tickBuffered}B · ${tickDropped}D</b></div>
        <div class="health-card ${chartStreamClients > 0 ? "ok" : ibkr.chart_connected ? "warn" : ""}" title="${escapeHtml(chartStreamTip)}"><span>Chart stream</span><b>${chartStreamClients}C · ${chartStreamCount}S · ${Number(ibkr.chart_streams || 0)}B</b></div>
        <div class="health-card"><span>Rows / latest</span><b>${Number(storage.bars || 0).toLocaleString()} · ${escapeHtml(latest)}</b></div>
        <div class="health-card ${dataLaneClass}" title="${escapeHtml(dataLaneTip)}"><span>Data lane</span><b>${escapeHtml(dataLaneText)} · ${escapeHtml(freshnessText)} · ${escapeHtml(qualityText)}</b></div>
        <div class="health-card"><span>Memory</span><b>${escapeHtml(memoryText)}</b></div>
        <div class="health-card" title="${escapeHtml(buildTip)}"><span>Build</span><b>${escapeHtml(buildVersion)} · ${escapeHtml(buildAtText)}</b></div>
        <div class="health-card ${overlayInvalid > 0 ? "bad" : "ok"}" title="${escapeHtml(overlayDiagnosticsTip)}"><span>Indicator visuals</span><b>${overlayInvalid > 0 ? `INVALID ${overlayInvalid}` : `OK · ${overlayRenderable}${overlayCompacted > 0 ? ` · C${overlayCompacted}` : ""}`}</b></div>
        <div class="health-card ${gexScheduler.enabled ? "ok" : "warn"}" title="${escapeHtml(gexRuntimeTooltip(gexRuntime))}"><span>GEX auto</span><b>${gexScheduler.enabled ? "ON" : "OFF"} ${escapeHtml((gexScheduler.instrument_ids || []).join(", "))}</b></div>
        <div class="health-card ${gexReliabilityWarn ? "warn" : "ok"}" title="${escapeHtml(gexRuntimeTooltip(gexRuntime))}"><span>GEX OI quality</span><b>${escapeHtml(gexReliabilityText)}</b></div>
        <div class="health-card ${quoteSnapshotSkipped > 0 ? "ok" : quoteSnapshotPersisted > 20 ? "warn" : ""}" title="${escapeHtml(quoteSnapshotTip)}"><span>Quote DB</span><b>${quoteSnapshotPersisted}W · ${quoteSnapshotSkipped}S</b></div>
        <div class="health-card"><span>Current option cap</span><b>${fmt(optionCaps[exactIdentityText(state.instrumentId)] ?? null)}</b></div>
        <div class="health-card ${dbOk ? "ok" : "bad"}"><span>Database</span><b>${dbOk ? "OK" : escapeHtml(storage.message || "error")}</b></div>
        ${storageDiagnosticsCards}
      `;
      const diagnosticsStatusNode = document.getElementById("system-diagnostics-status");
      const diagnosticsCheckedAt = systemStorageDiagnosticsCheckedAt
        ? localTimeFormatter.format(new Date(systemStorageDiagnosticsCheckedAt))
        : "";
      if (diagnosticsStatusNode) {
        diagnosticsStatusNode.textContent = hasStorageDiagnostics
          ? `Detailed PostgreSQL diagnostics loaded ${diagnosticsCheckedAt || "now"}; refreshes only on request.`
          : "Detailed PostgreSQL statistics load only on request.";
      }
      if (timeNode) {
        const checked = health.checked_at ? localTimeFormatter.format(new Date(health.checked_at)) : "-";
        timeNode.textContent = `Health checked ${checked} · DB diagnostics ${diagnosticsCheckedAt || "not requested"}`;
      }
      const sleepStatusNode = document.getElementById("server-sleep-status");
      const sleepButton = document.getElementById("server-sleep-toggle");
      if (sleepStatusNode) {
        sleepStatusNode.textContent = sleeping
          ? `Sleeping ${formatDuration(sleep.duration_seconds || 0)}`
          : "IBKR polling active";
      }
      if (sleepButton) {
        sleepButton.textContent = sleeping ? "Wake" : "Sleep";
        sleepButton.classList.toggle("active", sleeping);
        sleepButton.title = sleeping ? "Resume IBKR polling" : "Pause IBKR polling";
      }
      const telegramInteractiveStatus = document.getElementById("telegram-interactive-status");
      const telegramInteractiveToggle = document.getElementById("telegram-interactive-toggle");
      if (telegramInteractiveStatus) {
        telegramInteractiveStatus.textContent = interactiveTelegram.running
          ? "Telegram interactive bot running"
          : interactiveTelegram.configured
            ? interactiveTelegram.enabled
              ? "Telegram bot enabled, waiting"
              : "Telegram bot disabled"
            : "Telegram token/chat not configured";
      }
      if (telegramInteractiveToggle) {
        telegramInteractiveToggle.checked = Boolean(interactiveTelegram.enabled);
        telegramInteractiveToggle.disabled = !interactiveTelegram.configured;
        telegramInteractiveToggle.title = interactiveTelegram.configured
          ? "Enable Telegram command bot"
          : "Set Telegram bot token and chat id first";
      }
      const ibkrBadge = document.getElementById("ibkr-status-badge");
      if (ibkrBadge) {
        ibkrBadge.textContent = sleeping ? "SERVER SLEEP" : ibkrLive ? "IBKR LIVE" : ibkrConnected ? "IBKR WAIT" : "IBKR OFF";
        if (sleeping) ibkrBadge.textContent = "SERVER SLEEP";
        else if (ibkrSelfHealBlocked) ibkrBadge.textContent = "IBKR HEAL STOP";
        else if (ibkrCompeting) ibkrBadge.textContent = "IBKR DUP";
        else if (ibkrNoisy) ibkrBadge.textContent = "IBKR WARN";
        ibkrBadge.className = `quality-badge ${sleeping ? "warn" : ibkrSelfHealBlocked ? "bad" : ibkrNoisy ? "warn" : ibkrLive ? "ok" : ibkrConnected ? "warn" : "bad"}`;
        if (typeof bindStatusTooltip === "function") {
          const ibkrTone = sleeping || ibkrNoisy || (ibkrConnected && !ibkrLive)
            ? "warn"
            : ibkrSelfHealBlocked || !ibkrConnected ? "bad" : "ok";
          bindStatusTooltip(ibkrBadge, {
            title: ibkrBadge.textContent || "IBKR Gateway status",
            lines: String(ibkrTip || "IBKR Gateway status")
              .split("\n")
              .map(text => ({ text, tone: ibkrTone })),
            error: ibkrSelfHealBlocked ? String(ibkrSelfHeal.blocked_reason || "IBKR self-heal reconnect limit reached.") : ibkrNoisy ? ibkrIssue : (!ibkrConnected ? "IBKR backend session is disconnected." : ""),
          });
        } else {
          ibkrBadge.title = ibkrTip || "IBKR Gateway status";
        }
      }
      renderTelegramAlertStatus();
    }

    function ibkrConfiguredEndpoint() {
      const ibkr = state.systemHealth?.ibkr || {};
      const runtime = state.systemHealth?.runtime_settings || {};
      const host = String(ibkr.host || runtime.ibkr_host || "127.0.0.1");
      const port = clamp(
        Number(
          ibkr.port
          || runtime.ibkr_port
          || state.settings.ibkrPort
          || 7497
        ),
        1,
        65535,
      );
      return { host, port };
    }

    function ibkrDataNotice() {
      if (!providerCapability(state.dataSource, "runtime_settings")) return "";
      if (state.serverSleeping) return "Server sleep: IBKR polling paused. Wake the server to reconnect live quotes.";
      const ibkr = state.systemHealth?.ibkr;
      if (!ibkr || typeof ibkr !== "object") return "";
      const connected = Boolean(ibkr.quote_connected || ibkr.chart_connected || ibkr.history_connected);
      const quoteConnected = ibkr.quote_connected === true;
      const connectionInProgress = ibkr.connection_in_progress && typeof ibkr.connection_in_progress === "object"
        ? ibkr.connection_in_progress
        : {};
      const quoteConnecting = connectionInProgress.quote === true;
      const quoteSubscriptions = Math.max(Number(ibkr.quote_subscriptions) || 0, 0);
      const quoteValues = Math.max(Number(ibkr.quote_values) || 0, 0);
      const { host, port } = ibkrConfiguredEndpoint();
      const currentRouteFingerprint = instrumentRouteFingerprint();
      const missingRoutes = Array.isArray(ibkr.quote_missing_routes) ? ibkr.quote_missing_routes : [];
      const currentMissing = Boolean(
        currentRouteFingerprint
        && missingRoutes.includes(currentRouteFingerprint)
      );
      if (quoteConnecting) {
        return "IBKR live quote session is connecting. Live bid/ask/last is warming up.";
      }
      if (!connected) {
        const quality = effectiveDataQuality(state.snapshot?.meta || {});
        const chartFromDb = quality.status === "ok" && Boolean(state.snapshot?.bars?.length);
        if (chartFromDb) {
          return `Chart is using cached DB bars. Backend IBKR session is offline at ${host}:${port}; use TWS RST in System Health or fix the API port.`;
        }
        return `Backend IBKR session is offline at ${host}:${port}. Gateway may still be running on another port — set the API port in System Health, then TWS RST.`;
      }
      if (!quoteConnected) {
        return "IBKR Gateway is connected, but the live quote session is reconnecting. Live bid/ask/last is warming up.";
      }
      if (quoteSubscriptions > 0 && currentMissing) {
        return `IBKR live quote is not arriving for ${state.symbol}. TWS API may be serving delayed history only; check API market-data permissions for this contract.`;
      }
      if (ibkr.live_issue) {
        return ibkr.live_issue;
      }
      if (quoteSubscriptions > 0 && quoteValues === 0) {
        return "IBKR Gateway is connected, but live bid/ask/last is not arriving yet. Check market-data permissions, API acknowledgement, and competing sessions.";
      }
      return "";
    }
