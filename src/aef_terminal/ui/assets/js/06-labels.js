    const WOLFE_MARK = "🐺";
    const LIVE_MARK = "●";
    const WATCH_MARK = "👁";
    const CANDIDATE_MARK = "🔎";
    const READY_MARK = "💣";
    const GO_MARK = "🚀";
    const TRAIL_MARK = "🧭";
    const TAKE_MARK = "🎯";
    const WAIT_MARK = "⏳";
    const STOP_MARK = "☠";
    const UI_PRESENTATION_STATE_MATRIX = Object.freeze({
      waiting: Object.freeze({ label: "WAITING", kind: "loading", tone: "warn" }),
      connecting: Object.freeze({ label: "CONNECTING", kind: "loading", tone: "warn" }),
      syncing: Object.freeze({ label: "SYNCING", kind: "loading", tone: "warn" }),
      loading: Object.freeze({ label: "LOADING", kind: "loading", tone: "warn" }),
      warming: Object.freeze({ label: "WARMING", kind: "loading", tone: "warn" }),
      submitting: Object.freeze({ label: "SUBMITTING", kind: "loading", tone: "warn" }),
      pending: Object.freeze({ label: "PENDING", kind: "loading", tone: "warn" }),
      live: Object.freeze({ label: "LIVE", kind: "ready", tone: "ok" }),
      open: Object.freeze({ label: "OPEN", kind: "ready", tone: "ok" }),
      ready: Object.freeze({ label: "READY", kind: "ready", tone: "ok" }),
      filled: Object.freeze({ label: "FILLED", kind: "ready", tone: "ok" }),
      armed: Object.freeze({ label: "ARMED", kind: "attention", tone: "warn" }),
      stale: Object.freeze({ label: "STALE", kind: "stale", tone: "warn" }),
      delayed: Object.freeze({ label: "DELAYED", kind: "stale", tone: "warn" }),
      delayed_frozen: Object.freeze({ label: "DELAYED/FROZEN", kind: "stale", tone: "warn" }),
      frozen: Object.freeze({ label: "FROZEN", kind: "stale", tone: "warn" }),
      degraded: Object.freeze({ label: "DEGRADED", kind: "degraded", tone: "warn" }),
      partial: Object.freeze({ label: "PARTIAL", kind: "degraded", tone: "warn" }),
      retry: Object.freeze({ label: "RETRY", kind: "degraded", tone: "warn" }),
      empty: Object.freeze({ label: "EMPTY", kind: "empty", tone: "muted" }),
      unavailable: Object.freeze({ label: "NO DATA", kind: "empty", tone: "muted" }),
      closed: Object.freeze({ label: "CLOSED", kind: "disabled", tone: "muted" }),
      sleeping: Object.freeze({ label: "SLEEPING", kind: "disabled", tone: "muted" }),
      sleep: Object.freeze({ label: "SLEEP", kind: "disabled", tone: "muted" }),
      idle: Object.freeze({ label: "IDLE", kind: "disabled", tone: "muted" }),
      disabled: Object.freeze({ label: "DISABLED", kind: "disabled", tone: "muted" }),
      unsupported: Object.freeze({ label: "UNSUPPORTED", kind: "disabled", tone: "muted" }),
      cancelled: Object.freeze({ label: "CANCELLED", kind: "disabled", tone: "muted" }),
      blocked: Object.freeze({ label: "BLOCKED", kind: "error", tone: "bad" }),
      storage: Object.freeze({ label: "STORAGE ERROR", kind: "error", tone: "bad" }),
      error: Object.freeze({ label: "ERROR", kind: "error", tone: "bad" }),
      unknown: Object.freeze({ label: "UNKNOWN", kind: "degraded", tone: "warn" }),
    });

    function uiPresentationStateDescriptor(value) {
      const requestedState = String(value || "unknown").trim().toLowerCase();
      const stateKey = Object.hasOwn(UI_PRESENTATION_STATE_MATRIX, requestedState)
        ? requestedState
        : "unknown";
      return {
        state: stateKey,
        requestedState,
        ...UI_PRESENTATION_STATE_MATRIX[stateKey],
      };
    }

    function applyUiPresentationState(node, value, options = {}) {
      if (!node) return uiPresentationStateDescriptor(value);
      const descriptor = uiPresentationStateDescriptor(value);
      const label = String(options.label || descriptor.label);
      if (node.textContent !== label) node.textContent = label;
      node.dataset.state = descriptor.state;
      node.dataset.stateKind = descriptor.kind;
      node.setAttribute("aria-busy", descriptor.kind === "loading" ? "true" : "false");
      if (options.title !== undefined) node.title = String(options.title || "");
      return descriptor;
    }

    function snapshotSessionPresentation(snapshot = state.snapshot) {
      if (!snapshot?.meta || typeof effectiveDataQuality !== "function") {
        return { state: "unknown", title: "Session facts are not loaded for the current route." };
      }
      const quality = effectiveDataQuality(snapshot.meta || {});
      if (quality.session_unknown === true) {
        return { state: "unknown", title: quality.warning || "Provider session facts are unknown." };
      }
      if (quality.market_closed === true) {
        return { state: "closed", title: quality.warning || "Provider schedule reports the market closed." };
      }
      if (quality.session_warmup === true) {
        return { state: "warming", title: quality.warning || "The open session is awaiting its first confirmed bar." };
      }
      if (quality.empty_history === true || String(quality.status || "").split("+").includes("no_data")) {
        return { state: "unknown", title: quality.warning || "Session cannot be shown without current provider data." };
      }
      return { state: "open", title: "Provider session facts do not report a closed or unknown session." };
    }

    function renderExecutionEligibility(node, overallState, facts = [], options = {}) {
      if (!node) return false;
      const overall = uiPresentationStateDescriptor(overallState);
      const normalizedFacts = facts.map(fact => {
        const descriptor = uiPresentationStateDescriptor(fact?.state);
        return {
          label: String(fact?.label || "STATE").trim().toUpperCase(),
          descriptor,
          title: String(fact?.title || ""),
        };
      });
      const signature = JSON.stringify([
        overall.state,
        options.label || "ACTION",
        options.title || "",
        normalizedFacts.map(fact => [fact.label, fact.descriptor.state, fact.title]),
      ]);
      if (node.dataset.signature === signature) return false;
      node.dataset.signature = signature;
      node.dataset.state = overall.state;
      node.dataset.stateKind = overall.kind;
      node.setAttribute("aria-busy", overall.kind === "loading" ? "true" : "false");
      node.replaceChildren();
      const action = document.createElement("strong");
      action.className = `execution-eligibility-action ${overall.tone}`.trim();
      action.dataset.state = overall.state;
      action.dataset.stateKind = overall.kind;
      action.textContent = `${String(options.label || "ACTION").toUpperCase()} ${overall.label}`;
      node.appendChild(action);
      for (const fact of normalizedFacts) {
        const badge = document.createElement("span");
        badge.className = `execution-eligibility-fact ${fact.descriptor.tone}`.trim();
        badge.dataset.state = fact.descriptor.state;
        badge.dataset.stateKind = fact.descriptor.kind;
        badge.textContent = `${fact.label} ${fact.descriptor.label}`;
        if (fact.title) badge.title = fact.title;
        node.appendChild(badge);
      }
      node.title = String(options.title || normalizedFacts.map(fact => fact.title).filter(Boolean).join(" · "));
      return true;
    }
    const COMPACT_LABELS = new Map([
      ["WAIT", "·"],
      ["WATCH", "WT"],
      ["CANDIDATE", "CAND"],
      ["ARM", "ARM"],
      ["READY", "RDY"],
      ["ARMED", "ARM"],
      ["GO", "GO"],
      ["BLOCK", "×"],
      ["BLOCKED", "×"],
      ["FOLLOW", "TR"],
      ["RIDE", "TR"],
      ["WAIT ENTRY", "E?"],
      ["WAIT_ENTRY", "E?"],
      ["DATA GAP", "GAP"],
      ["LIVE", LIVE_MARK],
      ["CONFIRMED", "OK"],
      ["FORMING RANGE", "⏳?"],
      ["WIDE RANGE", "⏳"],
      ["NO FOLLOW", "NF"],
      ["MIDLINE FAIL", "MID×"],
      ["BREAK FAIL", "BRK×"],
      ["BREAKOUT FAIL", "BRK×"],
      ["CALL", "C"],
      ["PUT", "P"],
      ["READY CALL", "C RDY"],
      ["READY PUT", "P RDY"],
      ["ARM CALL", "C ARM"],
      ["ARM PUT", "P ARM"],
      ["GO CALL", "C GO"],
      ["GO PUT", "P GO"],
      ["CALL WATCH", "CW"],
      ["PUT WATCH", "PW"],
      ["LONG", "↑"],
      ["SHORT", "↓"],
      ["LINDA", "LV"],
      ["IMPULSE", "⚡"],
      ["BREAKOUT/ACCUMULATION", "BRK/ACC"],
      ["BAR", "BAR"],
      ["TRAIL", "TR"],
      ["TARGET", "T"],
      ["TGT", "T"],
      ["TAKE", "TP"],
      ["TAKE PROFIT", "TP"],
      ["TP", "TP"],
      ["STOP", `${STOP_MARK} SL`],
      ["ENTRY", "E"],
      ["RANGE", "RNG"],
      ["MARKET", "MKT"],
      ["EXHAUST", "EXH"],
      ["ABSORB", "ABSORB_WALL"],
      ["ABSORB WALL", "ABSORB_WALL"],
      ["ABS HIGH", "ABSORB_WALL_DOWN"],
      ["ABS LOW", "ABSORB_WALL_UP"],
      ["ABS TRAP", "ABSORB_WALL"],
      ["ABS TRAP BLOCKED", "ABS×"],
      ["PULLBACK", "PB"],
      ["LIQUIDITY", "LQ"],
      ["FAILED BREAKOUT", "FB"],
      ["COMPRESSION", "CMP"],
      ["COIL", "C"],
      ["BARCODE", "⏳"],
      ["CHURN", "CH"],
      ["SWEEP", "SWP"],
      ["SWEEP HIGH", "SWP↑"],
      ["SWEEP LOW", "SWP↓"],
      ["BOS UP", "BOS↑"],
      ["BOS DOWN", "BOS↓"],
      ["CHOCH UP", "CH↑"],
      ["CHOCH DOWN", "CH↓"],
      ["BULL FVG", "FVG↑"],
      ["BEAR FVG", "FVG↓"],
      ["P5 SELL", "P5▼"],
      ["P5 BUY", "P5▲"],
      ["P5 SELL ZONE", "P5▼"],
      ["P5 BUY ZONE", "P5▲"],
      ["P5 SELL PATH", "P5?▼"],
      ["P5 BUY PATH", "P5?▲"],
      ["WOLFE CANDIDATE", "🐺?"],
      ["WOLFE CONFIRMED", "🐺✓"],
    ]);
