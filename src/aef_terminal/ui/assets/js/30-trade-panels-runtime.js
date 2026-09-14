    function tradeSetupExecutionAuthority(snapshot = state.snapshot) {
      const setup = snapshot?.trade_setup && typeof snapshot.trade_setup === "object"
        ? snapshot.trade_setup
        : null;
      const authority = setup?.execution_authority && typeof setup.execution_authority === "object"
        && !Array.isArray(setup.execution_authority)
        ? setup.execution_authority
        : null;
      const authorityState = String(authority?.state || "unavailable").trim().toLowerCase();
      const reasonCode = String(
        authority?.reason_code
        || (authority ? "trade_setup_authority_not_ready" : "trade_setup_authority_missing")
      ).trim();
      const providerId = String(authority?.provider_id || "").trim();
      const providerIds = Array.isArray(authority?.provider_ids)
        ? authority.provider_ids.map(value => String(value || "").trim()).filter(Boolean)
        : [];
      const ready = authority?.contract === "trade-setup-authority-v1"
        && authority?.ready === true
        && authorityState === "ready"
        && Boolean(providerId)
        && providerIds.length === 1
        && providerIds[0] === providerId
        && reasonCode === "trade_setup_authority_ready";
      return {
        ready,
        state: ready ? "ready" : authorityState === "ready" ? "error" : authorityState || "unavailable",
        reasonCode: !ready && authorityState === "ready"
          ? "trade_setup_authority_invalid"
          : reasonCode,
        providerId,
        providerIds,
      };
    }

    function tradeSetupAuthorityDisplay(authority) {
      const authorityState = String(authority?.state || "unavailable").toLowerCase();
      if (authorityState === "disabled") {
        return {
          phase: "OFF",
          actionClass: "bad",
          note: "Trade Setup execution authority is disabled.",
        };
      }
      if (authorityState === "blocked") {
        return {
          phase: "BLOCK",
          actionClass: "warn",
          note: domainCodeLabel(authority?.reasonCode) || "Trade Setup is blocked by unavailable confirmed data.",
        };
      }
      if (authorityState === "ambiguous") {
        return {
          phase: "UNAVAILABLE",
          actionClass: "bad",
          note: "Trade Setup execution authority is ambiguous.",
        };
      }
      if (authorityState === "error") {
        return {
          phase: "UNAVAILABLE",
          actionClass: "bad",
          note: domainCodeLabel(authority?.reasonCode) || "Trade Setup execution authority failed.",
        };
      }
      return {
        phase: "UNAVAILABLE",
        actionClass: "bad",
        note: domainCodeLabel(authority?.reasonCode) || "Trade Setup execution authority is unavailable.",
      };
    }

    function tradeSetupExecutionState(snapshot = state.snapshot) {
      const setup = snapshot?.trade_setup && typeof snapshot.trade_setup === "object"
        ? snapshot.trade_setup
        : {};
      const authority = tradeSetupExecutionAuthority(snapshot);
      if (!authority.ready) {
        const unavailable = tradeSetupAuthorityDisplay(authority);
        return {
          text: unavailable.phase,
          displayPhase: unavailable.phase,
          muted: unavailable.phase === "OFF",
          actionClass: unavailable.actionClass,
          currentPrice: Number(currentSnapshotPrice(snapshot)),
          triggerReady: null,
          noteSuffix: unavailable.note,
          authority,
          authorityReady: false,
          executable: false,
        };
      }
      const card = setup.action_card && typeof setup.action_card === "object"
        ? setup.action_card
        : {};
      const displayPhase = normalizeRuntimeAction(
        card.phase || setup.action || "WAIT"
      ) || "WAIT";
      const direction = String(
        setup.side || card.direction || "flat"
      ).toLowerCase();
      return {
        text: displayPhase,
        displayPhase,
        muted: ["WAIT", "WATCH"].includes(displayPhase),
        actionClass: runtimeActionClass(displayPhase, direction),
        currentPrice: Number(currentSnapshotPrice(snapshot)),
        triggerReady: card.trigger_ready ?? setup.trigger_ready ?? null,
        noteSuffix: "",
        authority,
        authorityReady: true,
        executable: setup.ok === true && displayPhase === "GO" && card.blocked !== true,
      };
    }

    function runtimeDirectionGlyph(side) {
      if (side === "long") return "↑";
      if (side === "short") return "↓";
      return "·";
    }

    function tradeSetupRuntimeModel(snapshot) {
      const setup = snapshot?.trade_setup && typeof snapshot.trade_setup === "object"
        ? snapshot.trade_setup
        : {};
      const executionState = tradeSetupExecutionState(snapshot);
      if (!executionState.authorityReady) {
        const unavailableTitle = {
          disabled: "Execution authority disabled",
          blocked: "Confirmed data unavailable",
          error: "Execution authority error",
          ambiguous: "Execution authority ambiguous",
          unavailable: "Execution authority unavailable",
        }[executionState.authority?.state] || "Execution authority unavailable";
        return {
          phase: executionState.displayPhase,
          displayPhase: executionState.displayPhase,
          advisorCandidate: false,
          advisorActive: typeof isAdvisorChartMode === "function" && isAdvisorChartMode(),
          side: "flat",
          kind: "unavailable",
          quality: 0,
          entry: null,
          stop: null,
          target: null,
          rr: null,
          levelPrice: null,
          levelKind: "cluster",
          note: executionState.noteSuffix,
          compact: unavailableTitle,
          blocked: true,
          directionGlyph: "·",
          metaLine: executionState.noteSuffix,
          authority: executionState.authority,
          authorityReady: false,
          executable: false,
          actionClass: executionState.actionClass,
        };
      }
      const card = setup.action_card && typeof setup.action_card === "object" ? setup.action_card : {};
      const plan = setup.plan && typeof setup.plan === "object" ? setup.plan : {};
      const phase = normalizeRuntimeAction(card.phase || setup.action || "WAIT") || "WAIT";
      const blocked = card.blocked === true || phase === "BLOCK";
      const side = String(setup.side || card.direction || "flat").toLowerCase();
      const kind = String(setup.kind || card.setup || "wait").toUpperCase();
      const entry = Number(card.entry ?? plan.entry ?? plan.trigger);
      const stop = Number(card.stop ?? plan.stop);
      const target = Number(card.target ?? plan.target);
      const rr = Number(plan.rr);
      const level = setup.level && typeof setup.level === "object" ? setup.level : null;
      const noteBase = actionCardDoNow(card) || "Waiting for setup confirmation.";
      const advisorActive = typeof isAdvisorChartMode === "function" && isAdvisorChartMode();
      const displayPhase = executionState.displayPhase;
      const advisorCandidate = advisorActive && !blocked && displayPhase !== "GO" && side !== "flat"
        && ["WATCH", "ARM", "CANDIDATE"].includes(displayPhase);
      const advisorNote = advisorCandidate
        ? `Candidate setup (${displayPhase}) — plan only, not an entry command. ${noteBase}`
        : (advisorActive && displayPhase === "GO" ? `Entry command (GO). ${noteBase}` : noteBase);
      return {
        phase,
        displayPhase,
        advisorCandidate,
        advisorActive,
        side,
        kind,
        quality: Number(setup.quality || 0),
        entry: Number.isFinite(entry) ? entry : null,
        stop: Number.isFinite(stop) ? stop : null,
        target: Number.isFinite(target) ? target : null,
        rr: Number.isFinite(rr) ? rr : null,
        levelPrice: level && Number.isFinite(Number(level.price)) ? Number(level.price) : null,
        levelKind: String(level?.kind || "cluster"),
        note: executionState.noteSuffix ? `${executionState.noteSuffix}. ${advisorNote}` : advisorNote,
        compact: String(actionCardCompactLabel(card) || domainCodeLabel(setup?.kind || setup?.state || "setup") || `${side.toUpperCase()} / ${kind}`),
        blocked,
        directionGlyph: runtimeDirectionGlyph(side),
        metaLine: `${side.toUpperCase()} · ${kind}${Number.isFinite(Number(setup.quality)) ? ` · Q ${Number(setup.quality).toFixed(1)}` : ""}${Number.isFinite(rr) ? ` · RR ${rr.toFixed(1)}` : ""}`,
        authority: executionState.authority,
        authorityReady: true,
        executable: executionState.executable,
        actionClass: executionState.actionClass,
      };
    }

    function renderTradeSetupPanel(snapshot) {
      const panel = document.getElementById("trade-setup-panel");
      if (!panel) return;
      const model = tradeSetupRuntimeModel(snapshot);
      if (!model.authorityReady) {
        const authorityState = domainCodeLabel(model.authority?.state) || "Unavailable";
        panel.className = "trade-setup-card muted";
        panel.innerHTML = `
          <div class="trade-setup-head">
            <span class="trade-setup-phase indicator-runtime-state ${escapeHtml(model.actionClass || "bad")}">${escapeHtml(model.displayPhase)}</span>
            <span class="trade-setup-title wait">${escapeHtml(model.compact)}</span>
            <span class="trade-setup-quality">Q -</span>
          </div>
          <div class="trade-setup-row">
            <span>Authority</span>
            <b>${escapeHtml(authorityState)}</b>
          </div>
          <div class="trade-setup-note">${escapeHtml(model.note)}</div>
        `;
        return;
      }
      if (snapshotAnalysisPreservedStale(snapshot)) {
        panel.className = "trade-setup-card muted";
        panel.innerHTML = `
          <div class="trade-setup-head">
            <span class="trade-setup-phase indicator-runtime-state WATCH">STALE</span>
            <span class="trade-setup-title wait">Analysis updating</span>
            <span class="trade-setup-quality">Q -</span>
          </div>
          <div class="trade-setup-note">Waiting for analysis on the current chart window.</div>
        `;
        return;
      }
      const setup = snapshot.trade_setup || {};
      const plan = setup.plan || {};
      const level = setup.level || null;
      const sideClass = model.side === "long" ? "long" : model.side === "short" ? "short" : "wait";
      const phaseClass = runtimeActionClass(model.blocked ? "BLOCK" : model.displayPhase || model.phase);
      const actionCard = setup.action_card && typeof setup.action_card === "object" ? setup.action_card : null;
      const cardPresentation = actionCardPresentation(actionCard) || {};
      const cardFactLines = [...(cardPresentation.pro || []), ...(cardPresentation.con || [])];
      const confluence = Array.isArray(setup.confluence) ? setup.confluence.filter(Boolean) : [];
      const levelSources = level && Array.isArray(level.sources) ? level.sources : [];
      const sourceText = levelSources
        .slice(0, 4)
        .map(item => `${item.name || item.source || "level"} ${fmt(item.price)}`)
        .join(" · ");
      const tags = confluence.slice(0, 4).map(item => {
        const title = `${item.source || "signal"}${item.score ? ` · Q ${item.score}` : ""}`;
        return `<span class="trade-setup-tag" title="${escapeHtml(title)}">${escapeHtml(item.source || "signal")}</span>`;
      }).join("");
      const primaryNote = model.note || actionCardDoNow(actionCard) || "Wait for confirmation at the level.";
      const planText = `E ${fmt(model.entry)} · S ${fmt(model.stop)} · T ${fmt(model.target)} · RR ${model.rr !== null ? Number(model.rr).toFixed(1) : "-"}`;
      panel.innerHTML = `
        <div class="trade-setup-head">
          <span class="trade-setup-phase indicator-runtime-state ${phaseClass}">${escapeHtml(model.displayPhase || model.phase)}</span>
          <span class="trade-setup-title ${sideClass}">${escapeHtml(actionCardCompactLabel(actionCard) || domainCodeLabel(setup?.kind || setup?.state || "setup") || model.compact)}</span>
          <span class="trade-setup-quality">Q ${Number(model.quality || 0).toFixed(1)}</span>
        </div>
        <div class="trade-setup-row">
          <span>Phase</span>
          <b>${escapeHtml(`${model.displayPhase || model.phase}${model.advisorCandidate ? ` (${model.phase})` : ""} · ${model.side.toUpperCase()} · ${model.kind}`)}</b>
        </div>
        <div class="trade-setup-row" title="${escapeHtml(sourceText || "No clustered level yet")}">
          <span>Level</span>
          <b>${model.levelPrice !== null ? `${fmt(model.levelPrice)} · ${escapeHtml(model.levelKind)}` : "-"}</b>
        </div>
        <div class="trade-setup-row">
          <span>Plan</span>
          <b title="${escapeHtml(model.note || "")}">${model.advisorCandidate ? "Candidate · " : model.phase === "GO" && model.advisorActive ? "Entry · " : ""}${planText}</b>
        </div>
        <div class="trade-setup-tags">${tags || `<span class="trade-setup-tag">no confluence</span>`}</div>
        <div class="trade-setup-note" title="${escapeHtml(cardFactLines.join(String.fromCharCode(10)))}">${escapeHtml(primaryNote)}</div>
      `;
    }

    function syncTradeSetupRuntimeLayoutButtons() {
      const layout = state.settings?.tradeSetupRuntimeLayout === "compact" ? "compact" : "full";
      const fullBtn = document.getElementById("trade-setup-runtime-layout-full");
      const compactBtn = document.getElementById("trade-setup-runtime-layout-compact");
      if (fullBtn) fullBtn.classList.toggle("active", layout === "full");
      if (compactBtn) compactBtn.classList.toggle("active", layout === "compact");
    }

    function setTradeSetupRuntimeLayout(layout) {
      const next = layout === "compact" ? "compact" : "full";
      state.settings.tradeSetupRuntimeLayout = next;
      setServerSettingValue("aef:tradeSetupRuntimeLayout", next);
      syncTradeSetupRuntimeLayoutButtons();
      if (state.snapshot) renderTradeSetupRuntime(state.snapshot);
    }

    function renderTradeSetupRuntime(snapshot) {
      const node = document.getElementById("trade-setup-runtime");
      if (!node) return;
      const model = tradeSetupRuntimeModel(snapshot);
      const layout = state.settings?.tradeSetupRuntimeLayout === "compact" ? "compact" : "full";
      if (!model.authorityReady) {
        node.className = `trade-setup-runtime trade-setup-runtime-${layout} empty`;
        node.title = model.note || "";
        node.innerHTML = `
          <div class="trade-setup-runtime-head">
            <span class="trade-setup-phase indicator-runtime-state ${escapeHtml(model.actionClass || "bad")}">${escapeHtml(model.displayPhase)}</span>
            <span class="trade-setup-runtime-glyphs"><span class="trade-setup-runtime-dir wait">·</span></span>
            <div class="trade-setup-runtime-copy">
              <div class="trade-setup-runtime-title wait">${escapeHtml(model.compact)}</div>
            </div>
          </div>
          ${layout === "compact" ? "" : `<div class="trade-setup-runtime-note">${escapeHtml(model.note)}</div>`}
        `;
        return;
      }
      if (snapshotAnalysisPreservedStale(snapshot)) {
        node.className = `trade-setup-runtime trade-setup-runtime-${layout} empty`;
        node.title = "Waiting for analysis on the current chart window.";
        node.innerHTML = `
          <div class="trade-setup-runtime-head">
            <span class="trade-setup-phase indicator-runtime-state WATCH">STALE</span>
            <span class="trade-setup-runtime-glyphs"><span class="trade-setup-runtime-dir wait">·</span></span>
            <div class="trade-setup-runtime-copy">
              <div class="trade-setup-runtime-title wait">Analysis updating</div>
            </div>
          </div>
        `;
        return;
      }
      const phaseClass = runtimeActionClass(model.blocked ? "BLOCK" : model.displayPhase || model.phase);
      const sideClass = model.side === "long" ? "long" : model.side === "short" ? "short" : "wait";
      const phaseLabel = escapeHtml(model.displayPhase || model.phase || "WAIT");
      const glyph = model.phase === "GO"
        ? directionalGoMarkHtml(model.side)
        : model.displayPhase === "CANDIDATE" ? "👀" : "";
      const setup = snapshot.trade_setup || {};
      const actionCard = setup.action_card && typeof setup.action_card === "object" ? setup.action_card : null;
      const cardTitle = actionCardCompactLabel(actionCard) || model.compact;
      const meta = snapshot.meta || {};
      const freshness = meta.freshness && typeof meta.freshness === "object" ? meta.freshness : {};
      const dataAge = Number(freshness.bar_age_seconds);
      const dataThreshold = Number(freshness.stale_threshold_seconds);
      const dataStale = String(freshness.status || "").toLowerCase() === "stale"
        || (Number.isFinite(dataAge) && Number.isFinite(dataThreshold) && dataAge > dataThreshold);
      const dataText = livePreviewActive(meta)
        ? "PREVIEW"
        : dataStale ? "STALE" : Number.isFinite(dataAge) ? `${Math.round(dataAge)}s` : "OK";
      const rrText = model.rr !== null ? Number(model.rr).toFixed(1) : "-";
      node.className = `trade-setup-runtime trade-setup-runtime-${layout}${model.phase === "WAIT" && model.side === "flat" ? " empty" : ""}`;
      node.title = model.note || "";
      if (layout === "compact") {
        node.innerHTML = `
          <div class="trade-setup-runtime-head">
            <span class="trade-setup-phase indicator-runtime-state ${phaseClass}">${phaseLabel}</span>
            <span class="trade-setup-runtime-glyphs">${glyph ? `${glyph} ` : ""}<span class="trade-setup-runtime-dir ${sideClass}">${escapeHtml(model.directionGlyph || "·")}</span></span>
            <div class="trade-setup-runtime-copy">
              <div class="trade-setup-runtime-title ${sideClass}">${escapeHtml(cardTitle)}</div>
            </div>
          </div>
        `;
        return;
      }
      node.innerHTML = `
        <div class="trade-setup-runtime-head">
          <span class="trade-setup-phase indicator-runtime-state ${phaseClass}">${phaseLabel}</span>
          <span class="trade-setup-runtime-glyphs">${glyph ? `${glyph} ` : ""}<span class="trade-setup-runtime-dir ${sideClass}">${escapeHtml(model.directionGlyph || "·")}</span></span>
          <div class="trade-setup-runtime-copy">
            <div class="trade-setup-runtime-title ${sideClass}">${escapeHtml(model.compact)}</div>
          </div>
        </div>
        <div class="trade-setup-runtime-sub">${escapeHtml(model.metaLine || `${model.side.toUpperCase()} · ${model.kind}`)}</div>
        <div class="trade-setup-runtime-grid">
          <span>ENTRY<b>${model.entry !== null ? fmt(model.entry) : "-"}</b></span>
          <span>STOP<b>${model.stop !== null ? fmt(model.stop) : "-"}</b></span>
          <span>TARGET<b>${model.target !== null ? fmt(model.target) : "-"}</b></span>
          <span>RR<b>${rrText}</b></span>
          <span>DATA<b>${escapeHtml(dataText)}</b></span>
        </div>
        <div class="trade-setup-runtime-note">${escapeHtml(model.note)}</div>
      `;
    }

    function indicatorStatusMessage(status = {}, indicator = {}) {
      const triggerEvent = status?.trigger_event && typeof status.trigger_event === "object"
        ? status.trigger_event
        : {};
      const eventCode = String(triggerEvent.code || "").toLowerCase();
      const reasonCode = String(triggerEvent.reason_code || status.reason_code || "").toLowerCase();
      const count = Math.max(0, Number(triggerEvent.count) || 0);
      if (eventCode === "signal_obsolete") {
        const exitReason = String(
          triggerEvent.exit_reason
          || indicator?.latest?.obsolete_reason
          || indicator?.latest?.lifecycle?.exit_reason
          || "closed"
        ).toUpperCase();
        return `Signal obsolete after ${exitReason}`;
      }
      if (eventCode === "indicator_error") return String(status.last_error || "Indicator calculation failed");
      if (eventCode === "indicator_context_blocked") return domainCodeLabel(reasonCode) || "Required context unavailable";
      if (eventCode === "indicator_context_degraded") return domainCodeLabel(reasonCode) || "Optional context unavailable";
      if (eventCode === "indicator_input_missing") return "No input bars";
      if (eventCode === "indicator_signal_blocked") return `${count} blocked raw signal(s) on analysis bar`;
      if (eventCode === "indicator_live_preview") return `${count} live preview signal(s)`;
      if (eventCode === "indicator_signal") return `${count} signal(s) on analysis bar`;
      if (eventCode === "indicator_no_signal") return "Calculated, no current signal";
      if (eventCode === "ai_advisory_ready") return `${String(triggerEvent.provider || "AI").toUpperCase()} advisory ready`;
      if (eventCode === "ai_advisory_cache") return `${String(triggerEvent.provider || "AI").toUpperCase()} advisory cache`;
      if (eventCode === "ai_advisory_unavailable") return String(status.last_error || domainCodeLabel(reasonCode) || "AI advisory unavailable");
      return domainCodeLabel(reasonCode || eventCode) || "-";
    }

    function renderIndicatorRuntimeStatus(snapshot) {
      const listNode = document.getElementById("indicator-runtime-list");
      const summaryNode = document.getElementById("indicator-runtime-summary");
      if (!listNode) return;
      listNode.style.marginBottom = "3px";
      const indicators = snapshot.indicators || {};
      let signalCount = 0;
      let errorCount = 0;
      let offCount = 0;
      const html = RUNTIME_INDICATOR_ROWS
        .filter(([, , , calcGetter]) => Boolean(calcGetter ? calcGetter() : true))
        .map(([id, controlId, visibleGetter, calcGetter]) => {
        const spec = indicatorSpec(id);
        const ui = spec?.ui || {};
        const tableSettingId = ui.table_setting_id || "";

        const visible = Boolean(visibleGetter());
        const calcEnabled = Boolean(calcGetter ? calcGetter() : true);
        const indicator = indicators[id] || {};
        const status = indicator.status || {};
        const summary = indicatorSignalSummary(indicator);
        const label = runtimeIndicatorLabel(id, status);
        const runtimeState = resolveIndicatorRuntimeState(
          calcEnabled,
          visible,
          status,
          summary,
          snapshot,
          ui.runtime_state_ref,
        );
        const stateText = runtimeState.text;
        const stateClass = runtimeState.actionClass;
        const isMuted = runtimeState.muted;
        const runtimeDetails = String(runtimeState.details || "").trim();

        const tableSelect = tableSettingId ? document.getElementById(tableSettingId) : null;
        const tableEnabled = tableSelect && tableSelect.value !== "off";

        const separator = tableSettingId
          ? `<span style="width: 1px; height: 8px; background: var(--line); opacity: 0.4; margin-right: 1px; flex-shrink: 0;"></span>`
          : "";

        const statusHealth = String(status.health || "").toLowerCase();
        const statusCode = String(status.state_code || "").toLowerCase();
        const isBad = statusHealth === "error" || statusHealth === "stale" || statusCode === "error" || statusCode === "stale";
        const isSignal = runtimeState.countsAsSignal !== false
          && ["GO", "IN", "TRAIL", "SIGNAL", "LIVE"].includes(stateText);
        if (!calcEnabled || status.mode === "disabled") offCount += 1;
        else if (isBad) errorCount += 1;
        else if (isSignal || stateText === "ARM") signalCount += 1;
        const planLine = runtimeState.compactPlan ? runtimeCompactPlanLine(summary) : runtimePlanLine(summary);
        const promotion = indicator.candidate_promotion || indicator.status?.candidate_promotion;
        const tip = [
          `${label}: ${stateText}`,
          summary?.code ? `Code: ${summary.code}` : "",
          summary?.reason ? `Reason: ${summary.reason}` : "",
          planLine ? `Plan: ${planLine.replace(/<[^>]+>/g, "")}` : "",
          tableSettingId ? `Table: ${tableEnabled ? "VISIBLE" : "HIDDEN"}` : "",
          `Calc: ${calcEnabled ? "ON" : "OFF"}`,
          `Visibility: ${visible ? "SHOW" : "HIDE"}`,
          ...(runtimeDetails ? runtimeDetails.split(String.fromCharCode(10)) : []),
          ...(Object.keys(status).length
            ? [
                `Message: ${indicatorStatusMessage(status, indicator)}`,
                `Mode: ${status.mode || "-"}`,
                `Params: ${status.params_hash || "-"}`,
              ]
            : []),
          promotion
            ? `Promotion: ${promotion.promoted ? "yes" : "no"}${promotion.reject_reason ? ` (${promotion.reject_reason})` : ""}${promotion.score_band ? ` band=${promotion.score_band}` : ""}${promotion.rvol_source ? ` rvol=${promotion.rvol_source}` : ""}`
            : "",
        ].filter(Boolean).join(String.fromCharCode(10));

        const tableBtn = tableSettingId
          ? `<button class="indicator-runtime-state ${tableEnabled ? 'ok' : 'bad'}" data-runtime-table-toggle="${tableSettingId}" title="Toggle ${escapeHtml(label)} table" style="width: 24px; flex-shrink: 0; border-radius: 2px; font-size: 7px; font-weight: 900; height: 18px; line-height: 16px; border: 1px solid currentColor; background: rgba(0,0,0,0.06); text-transform: uppercase; padding: 0; cursor: pointer; margin-right: 0; opacity: ${tableEnabled ? '1' : '0.3'};">TBL</button>`
          : "";

        return `
          <div class="indicator-runtime-row indicator-runtime-row-rich" title="${escapeHtml(tip)}">
            <div class="indicator-runtime-main">
              <button class="indicator-runtime-state ${stateClass}" data-runtime-toggle="${escapeHtml(controlId)}" title="${visible ? "Hide on chart" : "Show on chart"} · ${escapeHtml(label)}" style="min-width: 58px; flex-shrink: 0; border-radius: 2px; font-size: 8px; font-weight: 900; height: 18px; line-height: 16px; border: 1px solid currentColor; background: rgba(0,0,0,0.06); text-transform: uppercase; padding: 0 6px; cursor: pointer; margin-right: 1px; opacity: ${isMuted ? '0.45' : '1'}; filter: saturate(0.8) brightness(0.92);">${escapeHtml(stateText)}</button>
              ${separator}
              ${tableBtn}
              <div class="indicator-runtime-copy">
                <div class="indicator-runtime-title">${escapeHtml(label)}</div>
                ${planLine ? `<div class="indicator-runtime-plan ${summary?.direction === "long" || summary?.direction === "short" ? summary.direction : ""}">${planLine}</div>` : ""}
              </div>
            </div>
          </div>
        `;
      }).join("");
      listNode.innerHTML = html || `<div class="indicator-runtime-note">No indicators</div>`;
      if (summaryNode) {
        const container = summaryNode.parentElement;
        if (container) {
          container.style.display = "flex";
          container.style.justifyContent = "center";
          container.style.gap = "0";
        }
        summaryNode.textContent = signalCount > 0 ? `/${signalCount}` : "";
      }
      renderIndicatorExtensionPanels(snapshot);
      if (typeof renderIndicatorSettingsLinkBadges === "function") renderIndicatorSettingsLinkBadges();
      if (typeof refreshIndicatorSettingsStatusBadges === "function") {
        refreshIndicatorSettingsStatusBadges(snapshot);
      }
    }
