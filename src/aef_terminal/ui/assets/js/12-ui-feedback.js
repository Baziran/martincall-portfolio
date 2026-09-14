    const uiFeedbackRuntime = {
      events: [],
      keys: new Set(),
      toastTimer: null,
      deliveryQueued: false,
      deliveries: [],
      targetTimers: new WeakMap(),
    };

    const UI_NOTICE_PRIORITY = Object.freeze({
      error: 90,
      blocked: 90,
      attention: 80,
      stale: 70,
      degraded: 60,
      unknown: 50,
      loading: 40,
      empty: 30,
      disabled: 20,
      ready: 10,
    });

    function setUiNotice(owner, code, message, options = {}) {
      const exactOwner = String(owner || "").trim();
      const exactCode = String(code || "").trim();
      if (!exactOwner || !exactCode) throw new Error("UI_NOTICE_IDENTITY_REQUIRED");
      const text = String(message || "").trim();
      if (!text) {
        clearUiNotice(exactOwner);
        return null;
      }
      const descriptor = uiPresentationStateDescriptor(options.state || "unknown");
      const routeScoped = options.routeScoped !== false;
      state.noticeSequence = Math.max(Number(state.noticeSequence) || 0, 0) + 1;
      const notice = Object.freeze({
        owner: exactOwner,
        code: exactCode,
        state: descriptor.state,
        message: text,
        priority: Number.isFinite(Number(options.priority))
          ? Number(options.priority)
          : UI_NOTICE_PRIORITY[descriptor.kind] || UI_NOTICE_PRIORITY.unknown,
        sequence: state.noticeSequence,
        route_scoped: routeScoped,
        instrument_id: routeScoped ? exactIdentityText(options.instrumentId || state.instrumentId) : "",
        route_fingerprint: routeScoped
          ? exactIdentityText(options.routeFingerprint || instrumentRouteFingerprint())
          : "",
        timeframe: routeScoped ? String(options.timeframe || state.timeframe || "") : "",
      });
      state.notices[exactOwner] = notice;
      return notice;
    }

    function clearUiNotice(owner, code = "") {
      const exactOwner = String(owner || "").trim();
      const current = state.notices?.[exactOwner];
      if (!current || (code && current.code !== String(code))) return false;
      delete state.notices[exactOwner];
      return true;
    }

    function clearUiNotices(options = {}) {
      const routeScopedOnly = options.routeScopedOnly === true;
      let changed = false;
      for (const [owner, notice] of Object.entries(state.notices || {})) {
        if (routeScopedOnly && notice?.route_scoped !== true) continue;
        delete state.notices[owner];
        changed = true;
      }
      return changed;
    }

    function activeUiNotice() {
      const instrumentId = exactIdentityText(state.instrumentId);
      const routeFingerprint = instrumentRouteFingerprint();
      const timeframe = String(state.timeframe || "");
      return Object.values(state.notices || {})
        .filter(notice => (
          notice
          && (!notice.route_scoped || (
            notice.instrument_id === instrumentId
            && notice.route_fingerprint === routeFingerprint
            && notice.timeframe === timeframe
          ))
        ))
        .sort((left, right) => (
          Number(right.priority || 0) - Number(left.priority || 0)
          || Number(right.sequence || 0) - Number(left.sequence || 0)
          || String(left.owner).localeCompare(String(right.owner))
        ))[0] || null;
    }

    function uiNoticeMessage() {
      return String(activeUiNotice()?.message || "");
    }

    function uiNoticeSignature() {
      const notice = activeUiNotice();
      return notice
        ? [notice.owner, notice.code, notice.state, notice.sequence, notice.message].join(":")
        : "";
    }

    function uiFeedbackEventKey(event) {
      return JSON.stringify([
        exactIdentityText(event?.instrument_id || ""),
        exactIdentityText(event?.route_fingerprint || ""),
        String(event?.timeframe || ""),
        String(event?.entity_id || event?.id || event?.kind || "ui"),
        String(event?.status || "notice"),
        String(event?.ts || ""),
      ]);
    }

    function normalizeUiFeedback(payload = {}) {
      const severity = ["info", "warning", "high", "critical"].includes(String(payload.severity || "").toLowerCase())
        ? String(payload.severity).toLowerCase()
        : "info";
      const tone = ["neutral", "success", "warning", "error"].includes(String(payload.tone || "").toLowerCase())
        ? String(payload.tone).toLowerCase()
        : severity === "critical" || severity === "high" ? "error" : severity === "warning" ? "warning" : "neutral";
      const ts = payload.ts || new Date().toISOString();
      return {
        id: String(payload.id || ""),
        kind: String(payload.kind || "ui"),
        category: String(payload.category || payload.kind || "system"),
        source: String(payload.source || payload.kind || "ui"),
        code: String(payload.code || payload.status || "notice"),
        entity_id: String(payload.entity_id || payload.id || ""),
        instrument_id: exactIdentityText(payload.instrument_id || ""),
        route_fingerprint: exactIdentityText(payload.route_fingerprint || ""),
        timeframe: String(payload.timeframe || ""),
        status: String(payload.status || "notice"),
        title: String(payload.title || "Notice"),
        detail: String(payload.detail || ""),
        ts: String(ts),
        severity,
        tone,
        target_id: String(payload.target_id || ""),
        target_tab: String(payload.target_tab || ""),
        toast: payload.toast === true,
        attention: payload.attention === true
          || (payload.attention !== false && ["warning", "high", "critical"].includes(severity)),
      };
    }

    function showBrowserToast(message, options = {}) {
      const node = document.getElementById("browser-toast");
      if (!node || !message) return;
      const tone = ["success", "warning", "error"].includes(String(options.tone || ""))
        ? String(options.tone)
        : "neutral";
      node.textContent = String(message);
      node.dataset.tone = tone;
      node.setAttribute("aria-live", tone === "error" ? "assertive" : "polite");
      node.classList.add("open");
      window.clearTimeout(uiFeedbackRuntime.toastTimer);
      const duration = Math.max(1200, Number(options.durationMs) || (tone === "error" ? 5200 : 3600));
      uiFeedbackRuntime.toastTimer = window.setTimeout(() => {
        node.classList.remove("open");
        uiFeedbackRuntime.toastTimer = null;
      }, duration);
    }

    function flashUiFeedbackTarget(target, tone = "neutral") {
      const node = typeof target === "string" ? document.getElementById(target) : target;
      if (!node || uiMotionReduced()) return;
      const cleanTone = ["success", "warning", "error"].includes(tone) ? tone : "neutral";
      const previousTimer = uiFeedbackRuntime.targetTimers.get(node);
      if (previousTimer) window.clearTimeout(previousTimer);
      node.classList.remove("ui-feedback-success", "ui-feedback-warning", "ui-feedback-error", "ui-feedback-neutral");
      node.classList.add("ui-feedback-active", `ui-feedback-${cleanTone}`);
      const timer = window.setTimeout(() => {
        node.classList.remove("ui-feedback-active", `ui-feedback-${cleanTone}`);
        uiFeedbackRuntime.targetTimers.delete(node);
      }, 420);
      uiFeedbackRuntime.targetTimers.set(node, timer);
    }

    function deliverUiFeedbackQueue() {
      uiFeedbackRuntime.deliveryQueued = false;
      const deliveries = uiFeedbackRuntime.deliveries.splice(0);
      for (const event of deliveries) {
        if (event.target_id) flashUiFeedbackTarget(event.target_id, event.tone);
        if (event.toast) showBrowserToast([event.title, event.detail].filter(Boolean).join(": "), { tone: event.tone });
        window.dispatchEvent(new CustomEvent("martincall:ui-feedback", { detail: event }));
      }
      if (
        deliveries.some(event => event.attention)
        && typeof renderUrgentRiskFeed === "function"
        && state?.snapshot
      ) renderUrgentRiskFeed(state.snapshot, { force: true });
    }

    function publishUiFeedback(payload) {
      const event = normalizeUiFeedback(payload);
      const key = uiFeedbackEventKey(event);
      if (uiFeedbackRuntime.keys.has(key)) return event;
      uiFeedbackRuntime.keys.add(key);
      uiFeedbackRuntime.events.unshift(event);
      while (uiFeedbackRuntime.events.length > 50) {
        const removed = uiFeedbackRuntime.events.pop();
        uiFeedbackRuntime.keys.delete(uiFeedbackEventKey(removed));
      }
      uiFeedbackRuntime.deliveries.push(event);
      if (!uiFeedbackRuntime.deliveryQueued) {
        uiFeedbackRuntime.deliveryQueued = true;
        window.setTimeout(deliverUiFeedbackQueue, 0);
      }
      return event;
    }

    function uiAttentionEvents() {
      return uiFeedbackRuntime.events.filter(event => event.attention);
    }

    function renderSettingsSaveState(detail = serverSettingsSaveState) {
      const node = document.getElementById("settings-save-state");
      if (!node) return;
      const phase = ["saving", "saved", "error"].includes(detail?.phase) ? detail.phase : "saved";
      node.className = `settings-save-state ${phase}`;
      node.textContent = phase === "saving" ? "Saving…" : phase === "error" ? "Save error" : "Saved";
      node.title = phase === "error"
        ? `${detail?.error || "Settings could not be saved"}. Click to retry.`
        : phase === "saving" ? "Saving settings" : "Settings are saved";
      node.disabled = phase === "saved";
    }

    window.addEventListener("martincall:settings-save-state", event => renderSettingsSaveState(event.detail));
    document.getElementById("settings-save-state")?.addEventListener("click", () => flushServerSettings());
    renderSettingsSaveState();
