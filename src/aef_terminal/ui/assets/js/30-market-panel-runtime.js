    function signed(value, suffix = "") {
      if (value === null || value === undefined) return "-";
      const sign = Number(value) > 0 ? "+" : "";
      return `${sign}${Number(value).toFixed(2)}${suffix}`;
    }

    function escapeHtml(value) {
      return String(value ?? "").replace(/[&<>"']/g, char => ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
      }[char]));
    }

    function candidateQualityMeta(candidate) {
      const details = candidate?.details && typeof candidate.details === "object" ? candidate.details : {};
      const rr = Number(details.rr);
      const minRr = Number(details.global_min_rr);
      const rrText = Number.isFinite(rr) ? `RR ${rr.toFixed(2)}` : "RR -";
      if (details.blocked_by_min_rr) {
        const threshold = Number.isFinite(minRr) ? ` < ${minRr.toFixed(2)}` : "";
        return {
          className: "candidate-gate blocked",
          text: `${rrText}${threshold} BLOCK`,
          title: details.blocked_reason || "Candidate blocked by global minimum R:R.",
        };
      }
      if (details.rr_gate_status === "pending_plan" || details.plan_complete === false) {
        return {
          className: "candidate-gate pending",
          text: "PLAN",
          title: details.blocked_reason || "Candidate has no complete executable trade plan yet.",
        };
      }
      if (details.rr_gate_status === "passed" || details.plan_coherent === true) {
        return {
          className: "candidate-gate passed",
          text: rrText,
          title: Number.isFinite(minRr) ? `Passed global minimum R:R ${minRr.toFixed(2)}.` : "Candidate plan is coherent.",
        };
      }
      return {
        className: "candidate-gate neutral",
        text: rrText,
        title: details.blocked_reason || "Candidate quality status.",
      };
    }

    function candidateStatusMeta(candidate) {
      const status = candidate?.status && typeof candidate.status === "object" ? candidate.status : {};
      const details = candidate?.details && typeof candidate.details === "object" ? candidate.details : {};
      const preview = status.stage === "preview" || status.execution_candidate === false || details.execution_candidate === false;
      if (preview) {
        return {
          className: "candidate-stage preview",
          text: "PREVIEW",
          title: status.reason ? `Preview signal: ${status.reason}` : "Preview signal; not eligible for execution decision.",
        };
      }
      if (status.decision_eligible === false) {
        return {
          className: "candidate-stage blocked",
          text: "HELD",
          title: status.reason ? `Held from decision: ${status.reason}` : "Candidate is not decision eligible.",
        };
      }
      return null;
    }

    function renderCandidateRow(candidate) {
      const gate = candidateQualityMeta(candidate);
      const status = candidateStatusMeta(candidate);
      const triggerEvent = candidate?.trigger_event;
      const reason = triggerEvent && typeof triggerEvent === "object" && !Array.isArray(triggerEvent) && typeof triggerEvent.code === "string" && triggerEvent.code
        ? domainFactText(triggerEvent)
        : "";
      const title = [reason, status?.title, candidate?.details?.blocked_reason, gate.title].filter(Boolean).join("\n");
      const rawScore = Number(candidate?.raw_score ?? candidate?.score);
      const reliability = Number(candidate?.reliability_weight);
      const finalRank = Number(candidate?.final_rank_score);
      const rankBits = [
        Number.isFinite(rawScore) ? `raw ${rawScore.toFixed(1)}` : "",
        Number.isFinite(reliability) ? `w ${reliability.toFixed(2)}` : "",
        Number.isFinite(finalRank) ? `rank ${finalRank.toFixed(1)}` : "",
      ].filter(Boolean).join(" · ");
      return `
        <li title="${escapeHtml(title)}">
          <div class="candidate-head">
            <b class="${escapeHtml(candidate.direction)}">${escapeHtml(candidate.name)} ${escapeHtml(candidate.direction)}</b>
            <span class="candidate-badges">${status ? `<span class="${status.className}">${escapeHtml(status.text)}</span>` : ""}<span class="${gate.className}">${escapeHtml(gate.text)}</span></span>
          </div>
          <span class="muted">Q ${escapeHtml(candidate.score)}${rankBits ? ` · ${escapeHtml(rankBits)}` : ""} | ${fmt(candidate.level)} | ${escapeHtml(reason)}</span>
        </li>`;
    }

    function sentimentSourceLabel(source) {
      if (source === "decision") return "Engine";
      const spec = typeof indicatorSpec === "function" ? indicatorSpec(source) : {};
      return spec?.label || String(source || "Signal").replaceAll("_", " ");
    }

    function urgentRiskMessageKey(item) {
      return JSON.stringify([
        String(item?.id || JSON.stringify([
          item?.category || "risk",
          item?.source || "system",
          item?.code || "notice",
          item?.entityId || "global",
        ])),
        exactIdentityText(item?.routeFingerprint || ""),
        String(item?.timeframe || ""),
      ]);
    }

    function urgentRiskTimeLabel(ts) {
      const date = new Date(ts || "");
      if (!Number.isFinite(date.getTime())) return "";
      return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    }

    const URGENT_RISK_MAX_ROWS = 8;
    const URGENT_RISK_TAG_MAX_CHARS = 24;
    const URGENT_RISK_PRESENTATION_KEY = "martincall:attention-presentation:v1";
    const URGENT_RISK_ACK_MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000;
    const URGENT_RISK_SNOOZE_MS = 15 * 60 * 1000;

    function urgentRiskSortMs(row) {
      const parsed = Date.parse(row?.ts || "");
      return Number.isFinite(parsed) ? parsed : 0;
    }

    function isObsoleteSignalNotice(latest = {}, signal = {}) {
      const lifecycle = latest?.lifecycle && typeof latest.lifecycle === "object" ? latest.lifecycle : {};
      return latest?.obsolete_signal === true
        || signal?.obsolete_signal === true
        || lifecycle.obsolete === true;
    }

    function urgentRiskPresentationState() {
      if (state.urgentRiskPresentationLoaded) {
        return {
          acknowledged: state.urgentRiskDismissed || {},
          snoozedUntil: state.urgentRiskSnoozedUntil || {},
        };
      }
      let stored = {};
      try {
        const parsed = JSON.parse(localStorage.getItem(URGENT_RISK_PRESENTATION_KEY) || "{}");
        if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) stored = parsed;
      } catch (_) {
        stored = {};
      }
      state.urgentRiskDismissed = stored.acknowledged && typeof stored.acknowledged === "object" ? stored.acknowledged : {};
      state.urgentRiskSnoozedUntil = stored.snoozedUntil && typeof stored.snoozedUntil === "object" ? stored.snoozedUntil : {};
      state.urgentRiskPresentationLoaded = true;
      return { acknowledged: state.urgentRiskDismissed, snoozedUntil: state.urgentRiskSnoozedUntil };
    }

    function persistUrgentRiskPresentationState() {
      const presentation = urgentRiskPresentationState();
      rawLocalStorageSetItem(URGENT_RISK_PRESENTATION_KEY, JSON.stringify(presentation));
    }

    function pushUrgentRiskMessage(rows, seen, item) {
      const text = String(item?.text || "").replace(/\s+/g, " ").trim();
      if (!text) return;
      const tag = String(item?.tag || "RISK").trim().toUpperCase().slice(0, URGENT_RISK_TAG_MAX_CHARS) || "RISK";
      const row = {
        id: String(item?.id || JSON.stringify([
          item?.category || "risk",
          item?.source || "system",
          item?.code || "notice",
          item?.entityId || "global",
        ])),
        tag,
        text,
        ts: item?.ts || "",
        category: String(item?.category || "risk"),
        severity: ["warning", "high", "critical"].includes(String(item?.severity || "").toLowerCase())
          ? String(item.severity).toLowerCase()
          : "warning",
        source: String(item?.source || "system"),
        code: String(item?.code || "notice"),
        entityId: String(item?.entityId || ""),
        routeFingerprint: exactIdentityText(item?.routeFingerprint || instrumentRouteFingerprint()),
        timeframe: String(item?.timeframe || state.timeframe || ""),
        targetTab: sidePanelTabs().includes(String(item?.targetTab || ""))
          ? String(item.targetTab)
          : "",
      };
      const key = urgentRiskMessageKey(row);
      if (seen.has(key)) return;
      seen.add(key);
      rows.push(row);
    }

    const indicatorAttentionProviderRegistry = new Map();

    function registerIndicatorAttentionProvider(indicatorId, provider) {
      const key = String(indicatorId || "").trim();
      if (!indicatorSpec(key)?.id || typeof provider !== "function") {
        throw new Error(`Invalid indicator attention provider: ${key || "missing"}`);
      }
      if (indicatorAttentionProviderRegistry.has(key)) {
        throw new Error(`Duplicate indicator attention provider: ${key}`);
      }
      indicatorAttentionProviderRegistry.set(key, provider);
    }

    function collectIndicatorAttentionMessages(snapshot) {
      const messages = [];
      indicatorAttentionProviderRegistry.forEach((provider, indicatorId) => {
        if (!indicatorCalcForId(indicatorId)) return;
        try {
          const contributed = provider(snapshot);
          if (Array.isArray(contributed)) messages.push(...contributed);
          else if (contributed && typeof contributed === "object") messages.push(contributed);
        } catch (error) {
          console.error(`Indicator attention provider failed: ${indicatorId}`, error);
        }
      });
      return messages;
    }

    function collectUrgentRiskMessages(snapshot) {
      const rows = [];
      const seen = new Set();
      const quality = effectiveDataQuality(snapshot?.meta || {});
      if (quality.signals_ok === false) {
        pushUrgentRiskMessage(rows, seen, {
          tag: "DATA",
          text: quality.warning || snapshot?.meta?.warning || "Signals blocked by data quality",
          ts: quality.latest_ts || snapshot?.meta?.analysis_ts,
          category: "data",
          severity: "critical",
          source: String(snapshot?.meta?.source || "provider"),
          code: `signals_blocked_${String(quality.status || "unknown")}`,
          entityId: exactIdentityText(state.instrumentId),
        });
      }
      const sentiment = snapshot?.direction_sentiment || {};
      const institutionalEdge = sentiment.institutional_edge && typeof sentiment.institutional_edge === "object"
        ? sentiment.institutional_edge
        : null;
      const edgeAction = institutionalEdge
        ? sentimentActionLabel({ ...institutionalEdge, role: "institutional_edge" })
        : "";
      const edgePhase = String(institutionalEdge?.phase || "").toLowerCase();
      if (institutionalEdge && edgeAction && ["go", "watch"].includes(edgePhase)) {
        const edgeDirection = String(institutionalEdge.direction || "flat").toUpperCase();
        const edgeSignalCode = String(institutionalEdge.signal_code || "").replaceAll("_", " ").toUpperCase();
        const edgeUrgentTag = edgePhase === "go" ? "EDGE" : "EDGE?";
        pushUrgentRiskMessage(rows, seen, {
          tag: edgeUrgentTag,
          text: `${edgeAction}: ${edgeDirection} institutional edge${edgeSignalCode ? ` · ${edgeSignalCode}` : ""} · DER absorption`,
          ts: snapshot?.meta?.analysis_ts,
          category: "signal",
          severity: edgePhase === "go" ? "high" : "warning",
          source: "institutional_edge",
          code: String(institutionalEdge.signal_code || edgePhase || "edge"),
          entityId: exactIdentityText(state.instrumentId),
          targetTab: "go",
        });
      }
      const indicators = snapshot?.indicators || {};
      for (const message of collectIndicatorAttentionMessages(snapshot)) {
        pushUrgentRiskMessage(rows, seen, message);
      }
      for (const [source, indicator] of Object.entries(indicators)) {
        if (!indicator || typeof indicator !== "object") continue;
        const latest = indicator.latest && typeof indicator.latest === "object" ? indicator.latest : {};
        const signal = latest.signal && typeof latest.signal === "object" ? latest.signal : {};
        const blockedReason = latest.blocked_reason || signal.blocked_reason;
        if ((blockedReason || signal.blocked === true || latest.blocked === true)
          && !isObsoleteSignalNotice(latest, signal)) {
          pushUrgentRiskMessage(rows, seen, {
            tag: sentimentSourceLabel(source),
            text: blockedReason || signal.reason || latest.reason || "Signal blocked",
            ts: latest.ts || snapshot?.meta?.analysis_ts,
            category: "signal",
            severity: "warning",
            source,
            code: String(latest.blocked_reason_code || signal.blocked_reason_code || signal.code || "blocked"),
            entityId: exactIdentityText(state.instrumentId),
            targetTab: "indicators",
          });
        }
        const derStatus = String(latest.der_status || "").toUpperCase();
        if (derStatus && derStatus !== "NORMAL") {
          pushUrgentRiskMessage(rows, seen, {
            tag: "DER",
            text: derStatus.replaceAll("_", " "),
            ts: latest.ts || snapshot?.meta?.analysis_ts,
            category: "risk",
            severity: "high",
            source,
            code: String(latest.der_code || latest.der_status || "der"),
            entityId: exactIdentityText(state.instrumentId),
            targetTab: "indicators",
          });
        }
        const events = Array.isArray(indicator.events) ? indicator.events.slice(-6) : [];
        for (const event of events) {
          const stateName = String(event?.state || "").toUpperCase();
          const eventDer = String(event?.der_status || "").toUpperCase();
          if (stateName === "BLOCKED_ANOMALY" || (eventDer && eventDer !== "NORMAL")) {
            let eventEntityId = exactIdentityText(state.instrumentId);
            if (event?.id) eventEntityId = String(event.id);
            pushUrgentRiskMessage(rows, seen, {
              tag: "DER",
              text: `${stateName || "ANOMALY"} ${eventDer}`.replaceAll("_", " "),
              ts: event?.ts || snapshot?.meta?.analysis_ts,
              category: "risk",
              severity: "high",
              source,
              code: String(event?.code || event?.state || event?.der_status || "der_anomaly"),
              entityId: eventEntityId,
              targetTab: "indicators",
            });
          }
        }
      }
      for (const event of typeof uiAttentionEvents === "function" ? uiAttentionEvents() : []) {
        if (event.instrument_id && event.instrument_id !== exactIdentityText(state.instrumentId)) continue;
        if (event.route_fingerprint && event.route_fingerprint !== instrumentRouteFingerprint()) continue;
        if (event.timeframe && String(event.timeframe) !== String(state.timeframe)) continue;
        pushUrgentRiskMessage(rows, seen, {
          id: uiFeedbackEventKey(event),
          tag: event.category || event.kind,
          text: [event.title, event.detail].filter(Boolean).join(": "),
          ts: event.ts,
          category: event.category,
          severity: event.severity,
          source: event.source,
          code: event.code,
          entityId: event.entity_id,
          routeFingerprint: event.route_fingerprint || instrumentRouteFingerprint(),
          timeframe: event.timeframe || state.timeframe,
          targetTab: event.target_tab,
        });
      }
      return rows
        .map((row, index) => ({ row, index }))
        .sort((left, right) => urgentRiskSortMs(right.row) - urgentRiskSortMs(left.row) || right.index - left.index)
        .map(item => item.row);
    }

    function renderUrgentRiskFeed(snapshot, options = {}) {
      const node = document.getElementById("urgent-risk-feed");
      if (!node) return;
      const rows = collectUrgentRiskMessages(snapshot);
      const presentation = urgentRiskPresentationState();
      const now = Date.now();
      const activeKeys = new Set(rows.map(urgentRiskMessageKey));
      let presentationPruned = false;
      Object.keys(presentation.acknowledged).forEach(key => {
        const acknowledgedAt = Number(presentation.acknowledged[key] || 0);
        if (!activeKeys.has(key) || now - acknowledgedAt > URGENT_RISK_ACK_MAX_AGE_MS) {
          delete presentation.acknowledged[key];
          presentationPruned = true;
        }
      });
      Object.keys(presentation.snoozedUntil).forEach(key => {
        if (!activeKeys.has(key) || Number(presentation.snoozedUntil[key] || 0) <= now) {
          delete presentation.snoozedUntil[key];
          presentationPruned = true;
        }
      });
      if (presentationPruned) persistUrgentRiskPresentationState();
      const nextSnoozeWakeAt = Math.min(
        ...Object.values(presentation.snoozedUntil)
          .map(Number)
          .filter(value => Number.isFinite(value) && value > now),
      );
      if (Number.isFinite(nextSnoozeWakeAt) && nextSnoozeWakeAt !== state.urgentRiskSnoozeWakeAt) {
        window.clearTimeout(state.urgentRiskSnoozeTimer);
        state.urgentRiskSnoozeWakeAt = nextSnoozeWakeAt;
        state.urgentRiskSnoozeTimer = window.setTimeout(() => {
          state.urgentRiskSnoozeWakeAt = 0;
          state.urgentRiskSnoozeTimer = null;
          if (state.snapshot) renderUrgentRiskFeed(state.snapshot, { force: true });
        }, Math.max(0, nextSnoozeWakeAt - now + 20));
      } else if (!Number.isFinite(nextSnoozeWakeAt) && state.urgentRiskSnoozeWakeAt) {
        window.clearTimeout(state.urgentRiskSnoozeTimer);
        state.urgentRiskSnoozeWakeAt = 0;
        state.urgentRiskSnoozeTimer = null;
      }
      const visibleRows = rows.filter(row => {
        const rowKey = urgentRiskMessageKey(row);
        return !presentation.acknowledged[rowKey] && Number(presentation.snoozedUntil[rowKey] || 0) <= now;
      }).slice(0, URGENT_RISK_MAX_ROWS);
      state.urgentRiskMessages = visibleRows;
      const key = visibleRows.map(row => [urgentRiskMessageKey(row), row.severity, row.text].join("|")).join("||");
      if (!visibleRows.length) {
        state.urgentRiskLastKey = "";
        node.classList.remove("open");
        node.replaceChildren();
        return;
      }
      if (!options.force && state.urgentRiskLastKey === key && node.classList.contains("open")) return;
      state.urgentRiskLastKey = key;
      node.classList.add("open");
      node.innerHTML = `
        <div class="risk-feed-title"><span class="risk-feed-dot"></span><span>Attention</span><small>${visibleRows.length}</small><button class="risk-feed-close" type="button" title="Acknowledge all visible notices">✓</button></div>
        ${visibleRows.map(row => `
          <div class="risk-feed-item severity-${escapeHtml(row.severity)}" data-risk-key="${escapeHtml(urgentRiskMessageKey(row))}">
            <span class="risk-feed-tag">${escapeHtml(row.tag)}${urgentRiskTimeLabel(row.ts) ? `<small>${escapeHtml(urgentRiskTimeLabel(row.ts))}</small>` : ""}</span>
            <span class="risk-feed-text">${escapeHtml(row.text)}</span>
            <span class="risk-feed-actions">
              ${row.targetTab ? `<button type="button" data-risk-action="open" title="Open ${escapeHtml(row.targetTab)}">Open</button>` : ""}
              <button type="button" data-risk-action="snooze" title="Snooze for 15 minutes">15m</button>
              <button type="button" data-risk-action="ack" title="Acknowledge">✓</button>
            </span>
          </div>`).join("")}`;
      node.querySelector(".risk-feed-close")?.addEventListener("click", event => {
        event.preventDefault();
        event.stopPropagation();
        visibleRows.forEach(row => {
          presentation.acknowledged[urgentRiskMessageKey(row)] = Date.now();
        });
        persistUrgentRiskPresentationState();
        state.urgentRiskLastKey = "";
        node.classList.remove("open");
        node.replaceChildren();
      });
      node.querySelectorAll("[data-risk-action]").forEach(button => {
        button.addEventListener("click", event => {
          event.preventDefault();
          event.stopPropagation();
          const itemNode = button.closest("[data-risk-key]");
          const rowKey = itemNode?.dataset?.riskKey || "";
          const row = visibleRows.find(candidate => urgentRiskMessageKey(candidate) === rowKey);
          if (!row) return;
          const action = button.dataset.riskAction;
          if (action === "open" && row.targetTab) {
            state.sideTab = row.targetTab;
            workspaceSet("sideTab", state.sideTab);
            syncWorkspacePageState();
            applySideTabs();
            return;
          }
          if (action === "snooze") presentation.snoozedUntil[rowKey] = Date.now() + URGENT_RISK_SNOOZE_MS;
          else presentation.acknowledged[rowKey] = Date.now();
          persistUrgentRiskPresentationState();
          state.urgentRiskLastKey = "";
          renderUrgentRiskFeed(snapshot, { force: true });
        });
      });
    }

    function sentimentActionLabel(item = {}) {
      if (item.blocked_by_context === true) return "REV WATCH";
      if (item.role === "market_context") {
        if (item.strategy_active !== true) return "CONTEXT";
        return {
          execute: "STRATEGY GO",
          armed: "STRATEGY ARM",
          impulse_confirmed: "IMPULSE CONTEXT",
          impulse_developing: "IMPULSE WATCH",
        }[item.strategy_phase] || "CONTEXT";
      }
      if (item.role === "institutional_edge") {
        const phase = item.phase === "go" ? "GO" : item.phase === "watch" ? "WATCH" : "";
        const direction = item.direction === "long" ? "LONG" : item.direction === "short" ? "SHORT" : "";
        return phase && direction ? `EDGE_${phase}_${direction}` : "";
      }
      if (item.role === "tick_flow") return "CONFIRM";
      return String(item.action || "");
    }

    function renderDirectionSentimentLight(snapshot) {
      const node = document.getElementById("direction-sentiment-light");
      const scoreNode = document.getElementById("direction-sentiment-score");
      if (!node || !scoreNode) return;
      const sentiment = snapshot.direction_sentiment || {};
      const score = typeof sentiment.score === "number" && Number.isFinite(sentiment.score)
        ? sentiment.score
        : Number.NaN;
      const direction = String(sentiment.direction || "flat").toLowerCase();
      const cleanDirection = direction === "long" ? "long" : direction === "short" ? "short" : "flat";
      const rounded = Number.isFinite(score) ? Math.round(score) : "-";
      const contributors = Array.isArray(sentiment.contributors) ? sentiment.contributors : [];
      const lines = contributors.slice(0, 8).map(item => {
        const dir = String(item.direction || "").toUpperCase();
        const itemScore = typeof item.score === "number" && Number.isFinite(item.score) ? item.score : Number.NaN;
        const weight = typeof item.weight === "number" && Number.isFinite(item.weight) ? item.weight : Number.NaN;
        const tier = String(item.weight_tier || "").toLowerCase();
        const weightLabel = Number.isFinite(weight) ? `w${weight.toFixed(2)}` : "";
        const tierLabel = tier ? `/${tier}` : "";
        return `${sentimentSourceLabel(item.source)}: ${dir || "-"} ${Number.isFinite(itemScore) ? Math.round(itemScore) : "-"} · ${weightLabel}${tierLabel} · ${sentimentActionLabel(item)}`;
      });
      node.className = `direction-sentiment-light ${cleanDirection}`;
      scoreNode.textContent = String(rounded);
      const longPower = typeof sentiment.long_power === "number" && Number.isFinite(sentiment.long_power)
        ? sentiment.long_power.toFixed(2)
        : "-";
      const shortPower = typeof sentiment.short_power === "number" && Number.isFinite(sentiment.short_power)
        ? sentiment.short_power.toFixed(2)
        : "-";
      const tooltip = [
        `Direction sentiment: ${cleanDirection.toUpperCase()} ${rounded}/100`,
        `Long power ${longPower} / Short power ${shortPower}`,
        ...lines,
      ].filter(Boolean).join(String.fromCharCode(10));
      const sentimentRows = contributors.slice(0, 10).map(item => {
        const dir = String(item.direction || "").toLowerCase();
        const itemScore = typeof item.score === "number" && Number.isFinite(item.score) ? item.score : Number.NaN;
        const scoreText = Number.isFinite(itemScore) ? String(Math.round(itemScore)) : "-";
        const weight = typeof item.weight === "number" && Number.isFinite(item.weight) ? item.weight : Number.NaN;
        const tier = String(item.weight_tier || "").toLowerCase();
        const rating = [Number.isFinite(weight) ? `w${weight.toFixed(2)}` : "", tier].filter(Boolean).join(" / ") || "-";
        return {
          cells: [
            sentimentSourceLabel(item.source),
            dir === "long" ? { text: scoreText, tone: "long" } : { text: "", tone: "muted" },
            dir === "short" ? { text: scoreText, tone: "short" } : { text: "", tone: "muted" },
            sentimentActionLabel(item) || "-",
            rating,
          ],
        };
      });
      node._sentimentTooltipContent = {
        lines: [
          `Direction sentiment: ${cleanDirection.toUpperCase()} ${rounded}/100`,
          `Long power ${Number(sentiment.long_power || 0).toFixed(2)} / Short power ${Number(sentiment.short_power || 0).toFixed(2)}`,
        ],
        tables: sentimentRows.length ? [{
          columns: ["Indicator", "Long", "Short", "Status", "Rating"],
          rows: sentimentRows,
          max_rows: 10,
        }] : [],
      };
      node.setAttribute("aria-label", tooltip);
      node.dataset.tooltip = tooltip;
    }

    function setupDirectionSentimentTooltip() {
      const node = document.getElementById("direction-sentiment-light");
      if (!node || node.dataset.sentimentTooltipReady === "1") return;
      node.dataset.sentimentTooltipReady = "1";
      const show = event => {
        const text = node.dataset.tooltip || "";
        if (!text) {
          hideCanvasTooltip();
          return;
        }
        const rect = node.getBoundingClientRect();
        const anchorX = Number.isFinite(Number(event?.clientX)) ? Number(event.clientX) : rect.right - 4;
        const anchorY = Number.isFinite(Number(event?.clientY)) ? Number(event.clientY) : rect.bottom;
        showCanvasTooltip(anchorX, anchorY, node._sentimentTooltipContent || text, document.getElementById("price-frame"));
      };
      node.addEventListener("mouseenter", show);
      node.addEventListener("mousemove", show);
      node.addEventListener("mouseleave", hideCanvasTooltip);
    }

    let lastPanelStableKey = "";
    const QUOTE_HEADER_REFRESH_MS = 400;

    function snapshotPanelStableKey(snapshot) {
      if (!snapshot) return "";
      const d = snapshot.decision || {};
      const meta = snapshot.meta || {};
      const quality = effectiveDataQuality(meta);
      const command = snapshot.command && typeof snapshot.command === "object" ? snapshot.command : null;
      const executionAuthority = tradeSetupExecutionAuthority(snapshot);
      const sentiment = snapshot.direction_sentiment || {};
      const candidateKey = (snapshot.candidates || []).slice(0, 8).map(candidate => [
        candidate.name,
        candidate.direction,
        candidate.action,
        candidate.score,
        candidate.status?.stage,
        candidate.status?.decision_eligible,
      ].join(":")).join(",");
      const levelKey = (snapshot.levels || []).slice(0, 6).map(level => [
        level.name,
        level.price,
        level.kind,
      ].join(":")).join(",");
      return [
        d.action,
        d.direction,
        d.kind,
        d.confidence,
        d.trigger,
        d.stop,
        d.target,
        command?.phase,
        actionCardCompactLabel(command),
        executionAuthority.ready ? "authority-ready" : `authority-${executionAuthority.state}`,
        executionAuthority.reasonCode,
        executionAuthority.providerId,
        sentiment.direction,
        sentiment.score,
        (sentiment.contributors || []).length,
        quality.status,
        quality.signals_ok,
        quality.gap_count,
        meta.analysis_ts,
        meta.warning,
        meta.freshness?.status,
        candidateKey,
        levelKey,
        (d.reasons || []).length,
        uiNoticeSignature(),
        state.requests.history.loading ? "1" : "0",
      ].join("|");
    }

    function livePreviewState(meta = {}) {
      const preview = meta.preview && typeof meta.preview === "object" ? meta.preview : {};
      const active = preview.active === true || meta.live_bar_closed === false;
      return {
        active,
        gapActive: preview.gap_active === true,
        latestKind: String(preview.latest_kind || ""),
        policy: preview.policy || (active ? "preview_only_until_exchange_confirmed" : ""),
        candidateCount: Number(preview.candidate_count || 0),
        executionCandidateCount: Number(preview.execution_candidate_count || 0),
        confirmedLatestTs: preview.confirmed_latest_ts || meta.analysis_ts || "",
        latestTs: preview.latest_ts || "",
        missingSlotTs: preview.missing_slot_ts || "",
        missingRangeTo: preview.missing_range_to || "",
        gapRepairPending: preview.gap_repair_pending === true,
        gapRepairFailed: preview.gap_repair_failed === true,
        gapRepairStatus: String(preview.gap_repair_status || ""),
        gapRepairCode: String(preview.gap_repair_code || ""),
      };
    }

    function livePreviewActive(meta = {}) {
      return livePreviewState(meta).active;
    }

    function livePreviewTooltip(meta = {}) {
      const preview = livePreviewState(meta);
      if (!preview.active) return "";
      if (preview.gapActive) {
        const status = preview.gapRepairStatus
          ? preview.gapRepairStatus.replace(/_/g, " ").toUpperCase()
          : preview.gapRepairPending
            ? "RUNNING"
            : "FAILED";
        return [
          `HISTORY SYNC ${status}: ${preview.gapRepairFailed ? "provider repair did not complete" : "requesting confirmed bars in the background"}.`,
          preview.missingSlotTs
            ? `Range: ${preview.missingSlotTs}${preview.missingRangeTo ? ` → ${preview.missingRangeTo}` : ""}`
            : "",
          preview.gapRepairCode ? `Reason: ${preview.gapRepairCode}` : "",
          preview.confirmedLatestTs ? `Indicators use confirmed provider bars through ${preview.confirmedLatestTs}.` : "Indicators continue on the available confirmed provider series.",
        ].filter(Boolean).join(String.fromCharCode(10));
      }
      return [
        "LIVE PREVIEW: current candle is provisional.",
        preview.confirmedLatestTs ? `Decision analysis: ${preview.confirmedLatestTs}` : "Decision analysis uses the latest confirmed bar.",
        preview.latestTs ? `Live candle: ${preview.latestTs}` : "",
        `Preview candidates: ${preview.candidateCount}`,
        `Execution candidates: ${preview.executionCandidateCount}`,
        "GO/ARM logic waits for the forming broker candle to be exchange-confirmed.",
      ].filter(Boolean).join(String.fromCharCode(10));
    }

    function panelSnapshotReady(snapshot) {
      const decision = snapshot?.decision;
      return Boolean(
        snapshot?.meta
        && typeof snapshot.meta === "object"
        && decision
        && typeof decision === "object"
        && typeof decision.action === "string"
        && typeof decision.direction === "string"
        && typeof decision.kind === "string"
      );
    }

    function renderQuoteHeader(snapshot) {
      const meta = snapshot?.meta;
      if (!meta) return false;
      const hasPrice = meta.price !== null && meta.price !== undefined;
      const hasBidAsk = (meta.bid !== null && meta.bid !== undefined)
        || (meta.ask !== null && meta.ask !== undefined);
      const priceSourceCode = meta.price_source === "last"
        ? "L"
        : meta.price_source === "bid_ask_mid"
          ? "M"
          : "";
      const priceSourceTitle = priceSourceCode === "L"
        ? "Last trade"
        : priceSourceCode === "M"
          ? "Bid/ask midpoint"
          : "";
      const quoteStatus = String(meta.quote_status || "");
      const quoteStatusCode = quoteStatus && quoteStatus !== "live"
        ? quoteStatus.toUpperCase()
        : "";
      const quoteDiagnosticStatus = quoteStatus === "live"
        ? "LIVE"
        : quoteStatusCode || "UNKNOWN";
      const arrow = meta.change >= 0 ? "▲" : "▼";
      const changeValue = Number(meta.change_pct);
      const changeClass = changeValue > 0 ? "positive" : changeValue < 0 ? "negative" : "neutral";
      const title = hasPrice ? `${meta.symbol} ${arrow} ${fmt(meta.price)} ${signed(meta.change_pct, "%")}` : `${meta.symbol} NO HISTORY`;
      const headerText = hasPrice
        ? `${meta.symbol} ${timeframeLabel(state.timeframe)} · ${fmt(meta.price)} · ${signed(meta.change_pct, "%")}${hasBidAsk ? ` · B ${fmt(meta.bid)} / A ${fmt(meta.ask)}` : ""} · ${state.range} · ${meta.source}`
        : `${meta.symbol} ${timeframeLabel(state.timeframe)} · NO HISTORY`;
      const quoteDiagnostic = hasPrice
        ? [priceSourceTitle || "Provider price", quoteDiagnosticStatus]
          .filter(Boolean).join(" · ")
        : "";
      const headerStableKey = [
        meta.symbol,
        state.timeframe,
        state.range,
        meta.source,
        hasPrice ? "price" : "empty",
      ].join("|");
      const now = Date.now();
      const titleNode = document.getElementById("chart-title");
      const diagnosticChanged = Boolean(
        titleNode && titleNode.title !== quoteDiagnostic
      );
      if (diagnosticChanged) titleNode.title = quoteDiagnostic;
      if (
        headerText === state.lastHeaderText
        && document.title === title
        && now - state.lastHeaderRenderAt < QUOTE_HEADER_REFRESH_MS
      ) {
        return diagnosticChanged;
      }
      if (
        headerText !== state.lastHeaderText
        && (
          headerStableKey !== state.lastHeaderStableKey
          || now - state.lastHeaderRenderAt >= QUOTE_HEADER_REFRESH_MS
          || !state.lastHeaderText
        )
      ) {
        document.title = title;
        if (titleNode) {
          titleNode.innerHTML = hasPrice ? `
            <span class="quote-symbol">${escapeHtml(meta.symbol)}</span>
            <span class="quote-timeframe">${escapeHtml(timeframeLabel(state.timeframe))}</span>
            <span class="quote-price">${escapeHtml(fmt(meta.price))}</span>
            <span class="quote-change ${changeClass}">${escapeHtml(signed(meta.change_pct, "%"))}</span>
            ${hasBidAsk ? `<span class="quote-bidask">B ${escapeHtml(fmt(meta.bid))} / A ${escapeHtml(fmt(meta.ask))}</span>` : ""}
            <span class="quote-context">${escapeHtml(state.range)} · ${escapeHtml(meta.source)}</span>` : `
            <span class="quote-symbol">${escapeHtml(meta.symbol)}</span>
            <span class="quote-timeframe">${escapeHtml(timeframeLabel(state.timeframe))}</span>
            <span class="quote-empty">NO HISTORY</span>`;
          titleNode.setAttribute("aria-label", headerText);
        }
        state.lastHeaderText = headerText;
        state.lastHeaderTitle = title;
        state.lastHeaderStableKey = headerStableKey;
        state.lastDocumentTitleKey = `${meta.symbol}|${meta.price}|${signed(meta.change_pct, "%")}`;
        state.lastHeaderRenderAt = now;
      }
      state.lastPrice = meta.price;
      if (typeof updateChartNavigationControls === "function") updateChartNavigationControls();
      return true;
    }

    let pendingLiveMarketChartSnapshot = null;
    let liveMarketChartRenderTimer = null;
    let lastLiveMarketChartRenderAt = 0;

    function renderLiveMarketChart(snapshot, options = {}) {
      const throttleMs = Math.max(Number(options.chartThrottleMs || 0), 0);
      if (!throttleMs) {
        if (liveMarketChartRenderTimer) {
          window.clearTimeout(liveMarketChartRenderTimer);
          liveMarketChartRenderTimer = null;
          pendingLiveMarketChartSnapshot = null;
        }
        renderCharts(snapshot);
        lastLiveMarketChartRenderAt = Date.now();
        return;
      }
      pendingLiveMarketChartSnapshot = snapshot;
      const now = Date.now();
      const elapsed = now - Number(lastLiveMarketChartRenderAt || 0);
      const run = () => {
        liveMarketChartRenderTimer = null;
        const target = pendingLiveMarketChartSnapshot;
        pendingLiveMarketChartSnapshot = null;
        if (!target || !snapshotMatchesCurrentRoute(target)) return;
        renderCharts(target);
        lastLiveMarketChartRenderAt = Date.now();
      };
      if (elapsed >= throttleMs) {
        run();
        return;
      }
      if (!liveMarketChartRenderTimer) {
        liveMarketChartRenderTimer = window.setTimeout(run, Math.max(throttleMs - elapsed, 16));
      }
    }

    function renderLiveMarketVisuals(snapshot, options = {}) {
      const stableKey = snapshotPanelStableKey(snapshot);
      const panelChanged = Boolean(options.forcePanel) || stableKey !== lastPanelStableKey;
      if (panelChanged) lastPanelStableKey = stableKey;
      if (!options.skipChart) renderLiveMarketChart(snapshot, options);
      if (panelChanged) renderPanel(snapshot);
      else renderQuoteHeader(snapshot);
      renderDirectionSentimentLight(snapshot);
      renderIndicatorExtensionPanels(snapshot);
      renderInstruments(options.instruments || {});
      if (options.updateTitle !== false) updateDocumentTitleFromScreenerQuote();
    }

    let pendingPanelSnapshot = null;
    let panelRenderTimer = null;
    let lastPanelRenderAt = 0;
    const PANEL_RENDER_MIN_MS = 120;

    function resetDeferredMarketRenders() {
      if (liveMarketChartRenderTimer) window.clearTimeout(liveMarketChartRenderTimer);
      if (panelRenderTimer) window.clearTimeout(panelRenderTimer);
      liveMarketChartRenderTimer = null;
      pendingLiveMarketChartSnapshot = null;
      lastLiveMarketChartRenderAt = 0;
      panelRenderTimer = null;
      pendingPanelSnapshot = null;
      lastPanelRenderAt = 0;
    }

    function marketAnalysisLifecyclePresentation(snapshot) {
      const meta = snapshot?.meta || {};
      const status = String(meta.analysis_status || "missing").trim().toLowerCase();
      const reason = meta.analysis_reason && typeof meta.analysis_reason === "object"
        ? meta.analysis_reason
        : {};
      const presentations = {
        ready: { label: "READY", tone: "ok" },
        queued: { label: "QUEUED", tone: "warn" },
        running: { label: "RUNNING", tone: "warn" },
        repairing: { label: "REPAIRING", tone: "warn" },
        missing: { label: "MISSING", tone: "bad" },
        error: { label: "ERROR", tone: "bad" },
        degraded: { label: "DEGRADED", tone: "bad" },
        superseded: { label: "DEGRADED", tone: "bad" },
      };
      const presentation = presentations[status] || { label: "DEGRADED", tone: "bad" };
      return {
        ...presentation,
        status,
        runtimeStatus: String(meta.analysis_runtime_status || status).trim().toLowerCase(),
        reasonCode: String(reason.code || "").trim(),
        reasonMessage: String(reason.message || "").trim(),
      };
    }

    function renderMarketHealthStatus(snapshot) {
      const meta = snapshot?.meta;
      if (!meta || !snapshotMatchesCurrentRoute(snapshot)) return false;
      const quality = effectiveDataQuality(meta);
      const quoteOnlySession = meta.quote_only === true
        && meta.market_state === "quote_only"
        && Boolean(currentLiveQuoteDisplay());
      const effectiveWarning = marketSnapshotNotice(snapshot);
      const qualityStatus = String(quality.status || (effectiveWarning ? "warn" : "ok")).toLowerCase();
      const qualityOk = quoteOnlySession || (quality.signals_ok !== false && qualityStatus === "ok");
      const qualityText = quoteOnlySession ? "QUOTE ONLY" : qualityStatus === "ok" ? "DATA OK" : `DATA ${qualityStatus.toUpperCase()}`;
      const qualityTip = [
        `Bar quality: ${qualityStatus.toUpperCase()}`,
        `Bar-derived inputs: ${quality.signals_ok === false ? "blocked" : "available"}`,
        `Gaps: ${Number(quality.gap_count || 0)}`,
        quality.provider_pending_slots ? `Provider-pending slots: ${Number(quality.provider_pending_slots || 0)}` : "",
        quality.provider_no_bar_slots ? `Provider-confirmed no-bar slots: ${Number(quality.provider_no_bar_slots || 0)}` : "",
        quality.max_missing_bars ? `Max missing bars: ${quality.max_missing_bars}` : "",
        quality.stale_minutes ? `Stale minutes: ${Number(quality.stale_minutes).toFixed(1)}` : "",
        quality.stale_threshold_minutes ? `Stale threshold: ${Number(quality.stale_threshold_minutes).toFixed(1)} min` : "",
        quality.latest_ts ? `Latest bar: ${quality.latest_ts}` : "",
        quality.warning || effectiveWarning || "",
      ].filter(Boolean).join(String.fromCharCode(10));
      state.lastPrice = meta.price;
      const previewActive = livePreviewActive(meta);
      const previewState = livePreviewState(meta);
      const lifecycle = marketAnalysisLifecyclePresentation(snapshot);
      const historyCoverage = meta.history_coverage && typeof meta.history_coverage === "object"
        ? meta.history_coverage
        : {};
      const coverageState = String(historyCoverage.state || "").trim().toUpperCase();
      const verification = typeof historyCoverageVerificationStatus === "function"
        ? historyCoverageVerificationStatus(historyCoverage, state.dataSource)
        : {
            available: false,
            active: false,
            concreteRepair: false,
            repairStatus: "not_supported",
            absenceSupported: false,
            absenceState: "unsupported",
          };
      renderQuoteHeader(snapshot);
      const notice = document.getElementById("history-notice");
      const sourceNotice = ibkrDataNotice();
      const qualityNotice = quality.signals_ok === false
        ? (quoteOnlySession ? "" : quality.warning || effectiveWarning || "Market data is inconsistent")
        : "";
      const noticeText = state.requests.history.loading ? "Loading older history..." : uiNoticeMessage() || qualityNotice || sourceNotice || "";
      if (notice) {
        notice.textContent = noticeText;
        notice.classList.toggle("show", Boolean(noticeText));
      }
      const copyText = dataQualityCopyText(snapshot, noticeText, sourceNotice);
      if (notice) {
        notice.dataset.copyText = copyText;
        notice.title = copyText || noticeText || "";
        notice.classList.toggle("copyable", Boolean(copyText));
      }
      const freshness = meta.freshness && typeof meta.freshness === "object" ? meta.freshness : {};
      const barAge = Number(freshness.bar_age_seconds);
      const threshold = Number(freshness.stale_threshold_seconds);
      const stale = String(freshness.status || "").toLowerCase() === "stale"
        || (Number.isFinite(barAge) && Number.isFinite(threshold) && barAge > threshold);
      const freshnessTip = [
        `Status: ${String(freshness.status || (stale ? "stale" : "fresh")).toUpperCase()}`,
        Number.isFinite(barAge) ? `Bar age: ${barAge.toFixed(1)}s` : "",
        Number.isFinite(threshold) ? `Threshold: ${threshold.toFixed(1)}s` : "",
        freshness.latest_bar_ts ? `Latest bar: ${freshness.latest_bar_ts}` : "",
        freshness.source ? `Source: ${freshness.source}` : "",
      ].filter(Boolean).join(String.fromCharCode(10));
      const dataHealthBadge = document.getElementById("data-health-badge");
      if (dataHealthBadge) {
        const combinedClass = quoteOnlySession
          ? "warn"
          : !qualityOk || lifecycle.tone === "bad"
            ? "bad"
            : previewActive || stale || lifecycle.tone === "warn"
              ? "warn"
              : "ok";
        const barMode = quoteOnlySession
          ? "QUOTE"
          : previewState.gapRepairFailed
            ? "REPAIR FAILED"
            : previewState.gapRepairPending
              ? "REPAIRING"
              : previewActive ? "PREVIEW" : stale ? "STALE" : "CONFIRMED";
        dataHealthBadge.textContent = `BAR ${barMode} · ANALYSIS ${lifecycle.label}`;
        dataHealthBadge.dataset.copyText = copyText;
        dataHealthBadge.dataset.barMode = barMode.toLowerCase();
        dataHealthBadge.dataset.analysisMode = lifecycle.status;
        dataHealthBadge.setAttribute("aria-label", `Market bars ${barMode.toLowerCase()}; analysis ${lifecycle.label.toLowerCase()}`);
        dataHealthBadge.className = `quality-badge ${combinedClass}`;
        bindStatusTooltip(dataHealthBadge, {
          title: `${qualityText} · ANALYSIS ${lifecycle.label}`,
          lines: [
            {
              text: quoteOnlySession
              ? "Mode: QUOTE ONLY, showing live Bid/Ask without creating a candle"
              : previewState.gapRepairFailed
                ? `Mode: HISTORY REPAIR FAILED${previewState.gapRepairCode ? ` (${previewState.gapRepairCode})` : ""}`
                : previewState.gapRepairPending
                  ? `Mode: HISTORY REPAIR ${previewState.gapRepairStatus.toUpperCase() || "RUNNING"}`
                : previewActive ? "Mode: LIVE PREVIEW, current candle is provisional" : "Mode: CONFIRMED, using closed exchange bars",
              tone: quoteOnlySession || previewState.gapRepairPending || previewActive
                ? "warn"
                : previewState.gapRepairFailed || stale ? "bad" : "ok",
            },
            { text: `Analysis lifecycle: ${lifecycle.label}`, tone: lifecycle.tone },
            lifecycle.runtimeStatus && lifecycle.runtimeStatus !== lifecycle.status
              ? `Server analysis status: ${lifecycle.runtimeStatus.toUpperCase()}`
              : "",
            lifecycle.reasonCode ? `Analysis reason: ${lifecycle.reasonCode}` : "",
            coverageState ? `History coverage (diagnostic): ${coverageState}` : "",
            verification.concreteRepair
              ? `Bar repair${verification.repairPhase ? ` (${verification.repairPhase})` : ""}: ${verification.repairStatus.toUpperCase()}`
              : "",
            verification.repairErrorCode
              ? `Bar repair reason: ${verification.repairErrorCode}`
              : "",
            verification.concreteRepair
              && verification.repairRequestedFrom
              && verification.repairRequestedTo
              ? `Bar repair range: ${verification.repairRequestedFrom} → ${verification.repairRequestedTo}`
              : "",
            coverageState
              ? `Absence verification (audit): ${verification.absenceState.toUpperCase()}`
              : "",
            verification.absenceErrorCode
              ? `Absence verification reason: ${verification.absenceErrorCode}`
              : "",
            Number.isFinite(barAge) ? `Bar age: ${barAge.toFixed(1)}s` : "",
            Number.isFinite(threshold) ? `Stale threshold: ${(threshold / 60).toFixed(1)} min` : "",
            qualityTip,
            freshnessTip,
            livePreviewTooltip(meta),
          ].filter(Boolean),
          error: lifecycle.reasonMessage || (quoteOnlySession ? "" : quality.warning || effectiveWarning || (stale ? freshnessTip : "")),
        });
      }
      return true;
    }

    function renderPanel(snapshot, options = {}) {
      if (!snapshotMatchesCurrentRoute(snapshot)) return;
      if (snapshot?.meta) renderMarketHealthStatus(snapshot);
      if (!panelSnapshotReady(snapshot)) {
        return;
      }
      pendingPanelSnapshot = snapshot;
      const run = () => {
        window.clearTimeout(panelRenderTimer);
        panelRenderTimer = null;
        const target = pendingPanelSnapshot;
        pendingPanelSnapshot = null;
        if (!target || !snapshotMatchesCurrentRoute(target)) return;
        renderPanelNow(target);
        lastPanelRenderAt = Date.now();
      };
      const now = Date.now();
      if (options.immediate || now - lastPanelRenderAt >= PANEL_RENDER_MIN_MS) {
        run();
        return;
      }
      if (!panelRenderTimer) {
        panelRenderTimer = window.setTimeout(run, Math.max(PANEL_RENDER_MIN_MS - (now - lastPanelRenderAt), 16));
      }
    }

    function renderPanelNow(snapshot) {
      if (!panelSnapshotReady(snapshot)) {
        if (snapshot?.meta) renderQuoteHeader(snapshot);
        return;
      }
      const d = snapshot.decision;
      const command = snapshot.command && typeof snapshot.command === "object" ? snapshot.command : null;
      const executionAuthority = tradeSetupExecutionAuthority(snapshot);
      const unavailableExecution = executionAuthority.ready && command
        ? null
        : executionAuthority.ready
          ? {
              phase: "UNAVAILABLE",
              actionClass: "bad",
              note: "Trade Setup execution command is unavailable.",
            }
          : tradeSetupAuthorityDisplay(executionAuthority);
      renderUrgentRiskFeed(snapshot);
      renderVitalityHealth(snapshot);
      const tzNode = document.getElementById("tz-label");
      if (tzNode) tzNode.textContent = "";
      if (typeof updateChartNavigationControls === "function") updateChartNavigationControls();
      const actionEl = document.getElementById("action");
      actionEl.textContent = unavailableExecution
        ? unavailableExecution.phase
        : command ? `${actionPhaseGlyph(command.phase) || ""} ${command.phase || "WAIT"}`.trim() : "WAIT";
      actionEl.title = unavailableExecution?.note || actionCardDoNow(command) || "";
      actionEl.className = unavailableExecution
        ? "wait"
        : d.direction === "long" ? "long" : d.direction === "short" ? "short" : "wait";
      document.getElementById("side").textContent = unavailableExecution ? "FLAT" : d.direction.toUpperCase();
      const kindEl = document.getElementById("kind");
      kindEl.textContent = unavailableExecution
        ? executionAuthority.ready
          ? "Unavailable"
          : domainCodeLabel(executionAuthority.state) || "Unavailable"
        : actionCardCompactLabel(command) || "WAIT";
      kindEl.title = unavailableExecution
        ? unavailableExecution.note
        : command ? `${actionSourceLabel(command) || ""} ${command.direction_mark || ""} ${command.setup || ""}` : "";
      document.getElementById("confidence").textContent = `Q ${d.confidence}`;
      document.getElementById("trigger").textContent = unavailableExecution ? "-" : fmt(d.trigger);
      document.getElementById("stop").textContent = unavailableExecution ? "-" : fmt(d.stop);
      document.getElementById("target").textContent = unavailableExecution ? "-" : fmt(d.target);
      document.getElementById("atr").textContent = fmt(snapshot.features?.atr);
      renderTradeSetupPanel(snapshot);
      renderTradeSetupRuntime(snapshot);
      if (typeof renderAiThirdOpinionDiscussion === "function") renderAiThirdOpinionDiscussion(snapshot);
      renderDirectionSentimentLight(snapshot);
      renderIndicatorRuntimeStatus(snapshot);
      renderAlertManager();
      const candidatesNode = document.getElementById("candidates");
      if (candidatesNode) candidatesNode.innerHTML = snapshot.candidates.length
        ? snapshot.candidates.map(renderCandidateRow).join("")
        : `<li class="muted">No candidate</li>`;
      const levelsNode = document.getElementById("levels");
      if (levelsNode) levelsNode.innerHTML = snapshot.levels
        .length
        ? snapshot.levels.map(l => `<li><b>${escapeHtml(l.name)}</b><br><span class="muted">${escapeHtml(fmt(l.price))} | ${escapeHtml(l.kind)}</span></li>`).join("")
        : `<li class="muted">No levels</li>`;
      const reasonsNode = document.getElementById("reasons");
      if (reasonsNode) reasonsNode.innerHTML = d.reasons
        .length
        ? d.reasons.map(r => `<li>${escapeHtml(r)}</li>`).join("")
        : `<li class="muted">No reason</li>`;
    }

    function bindStatusTooltip(node, payload) {
      if (!node || !payload) return;
      node.dataset.statusTooltipTitle = String(payload.title || "");
      const lines = (Array.isArray(payload.lines) ? payload.lines : [])
        .map(part => {
          if (part && typeof part === "object") {
            const text = String(part.text || "").trim();
            const tone = ["ok", "warn", "bad"].includes(String(part.tone || ""))
              ? String(part.tone)
              : "";
            const kind = part.kind === "separator" ? "separator" : "line";
            return { text, tone, kind };
          }
          return { text: String(part || "").trim(), tone: "", kind: "line" };
        })
        .filter(part => part.text || part.kind === "separator");
      node.dataset.statusTooltipLines = JSON.stringify(lines);
      node.dataset.statusTooltipError = String(payload.error || "").trim();
      node.removeAttribute("title");
      node.onmouseenter = event => showStatusTooltip(event.currentTarget, event);
      node.onmousemove = event => showStatusTooltip(event.currentTarget, event);
      node.onmouseleave = hideStatusTooltip;
      node.onblur = hideStatusTooltip;
    }

    function renderStatusTooltipLine(line) {
      if (!line || typeof line !== "object") return "";
      if (line.kind === "separator") return '<span class="status-tooltip-separator"></span>';
      const text = String(line.text || "").trim();
      if (!text) return "";
      const idx = text.indexOf(":");
      const lineTone = ["ok", "warn", "bad"].includes(String(line.tone || ""))
        ? String(line.tone)
        : "";
      const tone = lineTone ? ` ${lineTone}` : "";
      if (idx > 0 && idx <= 28) {
        return `<span class="status-tooltip-line${tone}"><span class="status-tooltip-label">${escapeHtml(text.slice(0, idx + 1))}</span> <span class="status-tooltip-value">${escapeHtml(text.slice(idx + 1).trim())}</span></span>`;
      }
      return `<span class="status-tooltip-line${tone}">${escapeHtml(text)}</span>`;
    }

    function renderStatusTooltipLines(lines) {
      return (Array.isArray(lines) ? lines : [])
        .map(renderStatusTooltipLine)
        .filter(Boolean)
        .join("");
    }

    function showStatusTooltip(node, event) {
      const tooltip = document.getElementById("status-tooltip");
      if (!tooltip || !node) return;
      const title = String(node.dataset.statusTooltipTitle || "").trim();
      let lines = [];
      try {
        const parsedLines = JSON.parse(node.dataset.statusTooltipLines || "[]");
        lines = Array.isArray(parsedLines) ? parsedLines : [];
      } catch (_error) {
        lines = [];
      }
      const error = String(node.dataset.statusTooltipError || "").trim();
      const html = [
        title ? `<span class="status-tooltip-title">${escapeHtml(title)}</span>` : "",
        lines.length ? renderStatusTooltipLines(lines) : "",
        error ? `<span class="status-tooltip-error">${escapeHtml(error)}</span>` : "",
      ].filter(Boolean).join("");
      if (!html) {
        hideStatusTooltip();
        return;
      }
      tooltip.innerHTML = html;
      tooltip.classList.add("open");
      const rect = tooltip.getBoundingClientRect();
      const x = Math.min((event?.clientX || 0) + 14, window.innerWidth - rect.width - 8);
      const y = Math.min((event?.clientY || 0) + 16, window.innerHeight - rect.height - 8);
      tooltip.style.left = `${Math.max(8, x)}px`;
      tooltip.style.top = `${Math.max(8, y)}px`;
    }

    function hideStatusTooltip() {
      const tooltip = document.getElementById("status-tooltip");
      if (!tooltip) return;
      tooltip.classList.remove("open");
      tooltip.innerHTML = "";
    }

    function uniqueNonEmptyLines(parts) {
      const seen = new Set();
      return parts
        .flatMap(part => String(part || "").split(";"))
        .map(part => part.trim())
        .filter(part => {
          if (!part || seen.has(part)) return false;
          seen.add(part);
          return true;
        });
    }

    function dataQualityCopyText(snapshot = state.snapshot, noticeText = "", sourceNotice = "") {
      const meta = snapshot?.meta || {};
      const quality = effectiveDataQuality(meta);
      const lifecycle = marketAnalysisLifecyclePresentation(snapshot);
      const effectiveWarning = marketSnapshotNotice(snapshot);
      const status = String(quality.status || (effectiveWarning ? "warn" : "ok")).toLowerCase();
      const parts = uniqueNonEmptyLines([
        quality.warning || effectiveWarning || "",
        lifecycle.reasonCode ? `Analysis reason: ${lifecycle.reasonCode}` : "",
        lifecycle.reasonMessage,
        noticeText,
        sourceNotice,
      ]);
      if (!parts.length && status === "ok" && lifecycle.status === "ready") return "";
      const barPrefix = status === "ok"
        ? "BAR DATA OK"
        : `BAR DATA ${status.toUpperCase()}: inputs ${quality.signals_ok === false ? "blocked" : "available"}`;
      const prefix = `${barPrefix}; ANALYSIS ${lifecycle.label}`;
      const detail = parts.length ? parts.join("; ") : "Market data status needs attention.";
      return `${prefix}; ${detail}`;
    }

    async function copyDataQualityStatus(event) {
      const text = event?.currentTarget?.dataset?.copyText || dataQualityCopyText();
      if (!text) return;
      try {
        if (navigator.clipboard?.writeText) {
          await navigator.clipboard.writeText(text);
        } else {
          const textarea = document.createElement("textarea");
          textarea.value = text;
          textarea.setAttribute("readonly", "readonly");
          textarea.style.position = "fixed";
          textarea.style.left = "-9999px";
          document.body.appendChild(textarea);
          textarea.select();
          document.execCommand("copy");
          textarea.remove();
        }
        showBrowserToast("Market data status copied");
      } catch (error) {
        showBrowserToast(`Copy failed: ${requestErrorMessage(error, "copy failed")}`);
      }
    }
