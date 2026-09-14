function marketSpotlightArrow(direction) {
  const side = String(direction || "flat").toLowerCase();
  return side === "long" ? "↑" : side === "short" ? "↓" : "↔";
}

function marketSpotlightCodeText(value) {
  const code = String(value || "").trim().toLowerCase();
  const labels = {
    trap: "TRAP",
    micro_break: "MICRO WARN",
    vix_risk: "VIX WARN",
    reversal_attempt: "REV ATTEMPT",
    absorption: "ABSORB",
    trend_initiation: "TREND INIT",
    balance: "BALANCE",
    directional_flow: "FLOW WATCH",
    continuation_pattern: "PATTERN",
    context_wait: "CTX WAIT",
    context_conflict: "CTX CONFLICT",
    continuation_watch: "PATTERN WATCH",
    continuation_triggered: "PATTERN GO",
    bull_flag: "BULL FLAG",
    bear_flag: "BEAR FLAG",
    bull_falling_wedge: "BULL WEDGE",
    bear_rising_wedge: "BEAR WEDGE",
    bull_pennant: "BULL PENNANT",
    bear_pennant: "BEAR PENNANT",
    bull_runaway_channel: "BULL RUN",
    bear_runaway_channel: "BEAR RUN",
    pullback_reentry_arm: "PB ARM",
    countertrend_watch: "WATCH CT",
    micro_break_watch: "WATCH MICRO BREAK",
    micro_watch: "WATCH MICRO",
    poi_wait: "CTX POI WAIT",
    poi_hot: "CTX POI",
    pullback_wait: "CTX WAIT PB",
    reversal_trigger_watch: "WATCH TRIG",
    no_trade: "CTX NO TRADE",
    no_short: "RISK: NO SHORT",
    no_long: "RISK: NO LONG",
    void: "RISK: VOID",
    vix: "RISK: VIX WARN",
    countertrend_trap: "RISK: CT TRAP",
    exhaustion: "RISK: EXH",
    micro: "RISK: MICRO WARN",
    chop: "RISK: CHOP",
    clear: "RISK: CLEAR",
    smart_money_buy: "SM BUY",
    smart_money_sell: "SM SELL",
    none: "OPP -",
    wait_for_context: "wait for context",
    enter_from_level_retest_if_stretched: "enter from level; wait retest if stretched",
    prepare_plan_wait_confirmation: "prepare plan; wait confirmation",
    wait_pullback: "wait pullback",
    wait_closed_bar_confirmation: "wait closed-bar confirmation",
    wait_break_confirmation: "wait break confirmation",
    execute_break: "execute confirmed break",
    wait_reclaim: "wait reclaim",
    prepare_reclaim_entry: "prepare reclaim entry",
    await_context: "wait context",
    no_force: "no force",
    break_level: "break level",
    reclaim_trigger: "reclaim",
    need_reclaim: "need reclaim",
    need_reject: "need reject",
    poi_not_hot: "POI not hot",
    no_chase: "no chase",
    reclaim_or_reject: "reclaim/reject",
    mixed_flow: "mixed flow",
    within_limits: "within limits",
  };
  return labels[code] || code.replace(/_/g, " ").toUpperCase();
}

function marketSpotlightPriceCell(value, prefix = "") {
  return { kind: "price", prefix, value, digits: 2, empty: "-" };
}

function marketSpotlightStateText(context) {
  if (!context || typeof context !== "object") return "";
  return `${marketSpotlightArrow(context.state_direction || context.direction)} ${marketSpotlightCodeText(context.state)}`.trim();
}

function marketSpotlightFlowText(context) {
  if (context?.flow_conflict) return "FLOW CONFLICT";
  const bias = Number(context?.flow_bias || 0);
  return bias > 0 ? `FLOW L +${bias}` : bias < 0 ? `FLOW S ${bias}` : "FLOW 0";
}

function marketSpotlightEntryText(entry) {
  const direction = String(entry?.direction || "flat").toLowerCase();
  const suffix = direction === "long" ? " L" : direction === "short" ? " S" : "";
  return `${marketSpotlightCodeText(entry?.state)}${suffix}`.trim();
}

function marketSpotlightEntryDetail(entry) {
  const trigger = Number(entry?.trigger);
  if (!Number.isFinite(trigger)) return marketSpotlightCodeText(entry?.detail_code);
  const relation = String(entry?.relation || "").toLowerCase();
  const prefix = relation === "below"
    ? "break <"
    : relation === "above"
      ? "break >"
      : relation === "reclaim"
        ? "reclaim "
        : "";
  return marketSpotlightPriceCell(trigger, prefix);
}

function marketSpotlightRiskText(risk) {
  return marketSpotlightCodeText(risk?.state || "clear");
}

function marketSpotlightOpponentText(opponent) {
  return marketSpotlightCodeText(opponent?.state || "none");
}

function marketSpotlightInvalidationText(invalidation) {
  const source = marketSpotlightCodeText(invalidation?.source || "wait");
  const direction = String(invalidation?.direction || "flat").toLowerCase();
  const side = direction === "long" ? " L" : direction === "short" ? " S" : "";
  return invalidation?.blocks_side
    ? `NO ${String(invalidation.blocks_side).toUpperCase()}`
    : `INV: ${source}${side}`;
}

function marketSpotlightInvalidationDetail(invalidation) {
  const level = Number(invalidation?.level);
  if (!Number.isFinite(level)) {
    return marketSpotlightCodeText(invalidation?.relation || "wait_reclaim");
  }
  const relation = String(invalidation?.relation || "").toLowerCase();
  const prefix = relation === "below"
    ? "<"
    : relation === "above"
      ? ">"
      : relation === "reclaim"
        ? "VWAP "
        : "";
  return marketSpotlightPriceCell(level, prefix);
}

function marketSpotlightStrategyText(strategy) {
  const direction = String(strategy?.direction || "flat").toLowerCase();
  const side = direction === "long" ? "L" : direction === "short" ? "S" : "-";
  const phase = String(strategy?.phase || "off").toLowerCase();
  if (strategy?.mode === "continuation_pattern") {
    const pattern = marketSpotlightCodeText(strategy?.pattern_type || "continuation_pattern");
    return `${pattern} ${side} ${marketSpotlightCodeText(phase)}`;
  }
  if (strategy?.mode === "pullback_reentry") {
    return `PB ${side} ${marketSpotlightCodeText(phase)}`;
  }
  if (phase === "execute") return `GO ${side}`;
  if (phase === "armed") return `ARM ${side}`;
  if (phase === "impulse_confirmed") return `IMP OK ${side}`;
  if (phase === "impulse_developing") return `IMP DEV ${side}`;
  return "SETUP -";
}

function marketSpotlightVoteFactGroups(context) {
  const votes = context?.pressure_votes && typeof context.pressure_votes === "object"
    ? context.pressure_votes
    : {};
  return ["long", "short"].map(side => ({
    title: `${side.toUpperCase()} pressure votes`,
    columns: ["Side", "Condition", "Active"],
    rows: (Array.isArray(votes[side]) ? votes[side] : []).map(fact => ({
      cells: [
        { text: side.toUpperCase(), tone: side },
        marketSpotlightCodeText(fact?.code),
        {
          text: fact?.active ? "yes" : "no",
          tone: fact?.active ? "ok" : "muted",
        },
      ],
    })),
    max_rows: 8,
  }));
}

function marketSpotlightEntryGateFactGroups(entry) {
  const gate = entry?.gate && typeof entry.gate === "object" ? entry.gate : {};
  return [{
    columns: ["Side", "Allowed", "Hot", "Bypass", "Confirm", "Warn", "Block"],
    rows: ["long", "short"].map(side => {
      const facts = gate[side] && typeof gate[side] === "object" ? gate[side] : {};
      const boolCell = (value, activeTone = "ok") => ({
        text: value ? "yes" : "no",
        tone: value ? activeTone : "muted",
      });
      return {
        cells: [
          { text: side.toUpperCase(), tone: side },
          boolCell(facts.allowed),
          boolCell(facts.hot, "warn"),
          boolCell(facts.bypass),
          boolCell(facts.confirm),
          boolCell(facts.warn, "warn"),
          {
            text: marketSpotlightCodeText(facts.blocked_reason) || "-",
            tone: facts.blocked_reason ? "bad" : "muted",
          },
        ],
      };
    }),
  }];
}

function marketSpotlightColumn(head, line1, line2, options = {}) {
  return {
    cells: [head, line1, line2],
    direction: options.direction || "flat",
    tone: options.tone || options.direction || "neutral",
    scenario: `Market Spotlight ${head}`,
    fact_groups: options.factGroups || [],
    metrics: options.metrics || {},
  };
}

function marketSpotlightTableColumns(table) {
  const context = table?.market_context && typeof table.market_context === "object"
    ? table.market_context
    : {};
  const entry = context.entry && typeof context.entry === "object" ? context.entry : {};
  const risk = context.risk && typeof context.risk === "object" ? context.risk : {};
  const opponent = context.opponent && typeof context.opponent === "object"
    ? context.opponent
    : {};
  const invalidation = context.invalidation && typeof context.invalidation === "object"
    ? context.invalidation
    : {};
  const strategy = context.strategy && typeof context.strategy === "object"
    ? context.strategy
    : {};
  const votes = marketSpotlightVoteFactGroups(context);
  const metrics = { ...(context.metrics || {}), ...(context.vote_metrics || {}) };
  const flowDirection = Number(context.flow_bias || 0) > 0
    ? "long"
    : Number(context.flow_bias || 0) < 0
      ? "short"
      : "flat";
  const lockDirection = context.side_lock === "LONG"
    ? "long"
    : context.side_lock === "SHORT"
      ? "short"
      : "flat";
  const lockText = context.side_lock === "LONG"
    ? "ONLY LONG"
    : context.side_lock === "SHORT"
      ? "ONLY SHORT"
      : "SIDE CHECK";
  const stateColumn = marketSpotlightColumn(
    "STATE",
    marketSpotlightStateText(context),
    context.closed ? "Closed" : "Live",
    {
      direction: context.state_direction || context.direction,
      tone: context.attention ? "warning" : context.state_direction || context.direction,
      factGroups: votes,
      metrics,
    },
  );
  const flow = marketSpotlightColumn(
    "FLOW",
    marketSpotlightFlowText(context),
    {
      parts: [
        { kind: "integer", prefix: "L ", value: context.metrics?.long_pressure },
        { kind: "integer", prefix: "S ", value: context.metrics?.short_pressure },
      ],
      separator: " / ",
    },
    { direction: flowDirection, factGroups: votes, metrics },
  );
  const lock = marketSpotlightColumn("LOCK", lockText, marketSpotlightCodeText(context.trap?.direction || "flat"), {
    direction: lockDirection,
    metrics,
  });
  const setup = marketSpotlightColumn(
    "SETUP",
    marketSpotlightStrategyText(strategy),
    marketSpotlightCodeText(strategy.directive),
    { direction: strategy.direction, metrics },
  );
  if (String(table?.style || "compact") !== "full") {
    return [stateColumn, flow, lock, setup];
  }
  return [
    stateColumn,
    flow,
    marketSpotlightColumn(
      "ENTRY",
      marketSpotlightEntryText(entry),
      marketSpotlightEntryDetail(entry),
      {
        direction: entry.direction,
        tone: entry.attention ? "warning" : entry.direction,
        factGroups: marketSpotlightEntryGateFactGroups(entry),
        metrics,
      },
    ),
    marketSpotlightColumn(
      "RISK",
      marketSpotlightRiskText(risk),
      Number.isFinite(Number(risk.rvol))
        ? { kind: "fixed", prefix: "RVOL ", value: risk.rvol, digits: 2 }
        : marketSpotlightCodeText(risk.detail_code),
      { tone: risk.tone, metrics },
    ),
    marketSpotlightColumn(
      "OPP",
      marketSpotlightOpponentText(opponent),
      Number.isFinite(Number(opponent.trigger))
        ? marketSpotlightPriceCell(opponent.trigger, opponent.direction === "long" ? ">" : "<")
        : marketSpotlightCodeText(opponent.poi_hot ? "poi_hot" : opponent.poi_active ? "poi_wait" : "none"),
      {
        direction: opponent.direction,
        tone: opponent.poi_active && !opponent.exhaustion_long && !opponent.exhaustion_short
          ? "warning"
          : opponent.direction,
        metrics,
      },
    ),
    marketSpotlightColumn(
      "INV",
      marketSpotlightInvalidationText(invalidation),
      marketSpotlightInvalidationDetail(invalidation),
      {
        direction: invalidation.direction,
        tone: invalidation.blocks_side ? "danger" : invalidation.direction,
        metrics,
      },
    ),
    lock,
  ];
}

let marketSpotlightAnimationFrame = 0;
let marketSpotlightAnimationLast = 0;
let marketSpotlightLastStateKey = "";
let marketSpotlightStateBeamUntil = 0;
const MARKET_SPOTLIGHT_STATE_BEAM_MS = 15000;

function marketSpotlightAnimationEnabled() {
  const config = state.indicators?.marketSpotlight || {};
  return (
    uiMotionEnabled()
    && config.animate !== false
    && config.visible !== false
    && config.table !== false
    && config.tablePosition !== "off"
  );
}

function ensureMarketSpotlightAnimationLoop() {
  if (
    marketSpotlightAnimationFrame
    || !marketSpotlightAnimationEnabled()
    || !state.snapshot?.bars?.length
    || Date.now() >= marketSpotlightStateBeamUntil
  ) return;
  const tick = timestamp => {
    if (
      !marketSpotlightAnimationEnabled()
      || !state.snapshot?.bars?.length
      || Date.now() >= marketSpotlightStateBeamUntil
    ) {
      marketSpotlightAnimationFrame = 0;
      return;
    }
    marketSpotlightAnimationFrame = requestAnimationFrame(tick);
    if (!marketSpotlightAnimationLast || timestamp - marketSpotlightAnimationLast >= 90) {
      marketSpotlightAnimationLast = timestamp;
      if (typeof renderPriceOverlay === "function") renderPriceOverlay(state.snapshot);
    }
  };
  marketSpotlightAnimationFrame = requestAnimationFrame(tick);
}

function marketSpotlightStateKey(table) {
  const context = table?.market_context || {};
  const entry = context.entry && typeof context.entry === "object" ? context.entry : {};
  const risk = context.risk && typeof context.risk === "object" ? context.risk : {};
  return [
    context.state || "",
    context.state_direction || context.direction || "",
    context.flow_bias || "",
    context.side_lock || "",
    entry.state || entry.phase || "",
    risk.state || "",
  ].map(value => String(value || "").replace(/\s+/g, " ").trim()).join("|");
}

function updateMarketSpotlightStateBeam(table) {
  const key = marketSpotlightStateKey(table);
  if (key && !marketSpotlightLastStateKey) {
    marketSpotlightLastStateKey = key;
    return false;
  }
  if (key && key !== marketSpotlightLastStateKey) {
    marketSpotlightLastStateKey = key;
    marketSpotlightStateBeamUntil = Date.now() + MARKET_SPOTLIGHT_STATE_BEAM_MS;
  }
  return Date.now() < marketSpotlightStateBeamUntil;
}

function drawBeaconSpotlightBeam(ctx, centerX, centerY, targetX, targetY, options = {}) {
  const active = options.mode === "state";
  const elapsed = Date.now() / 1000;
  const angle = active ? Math.atan2(targetY - centerY, targetX - centerX) : -Math.PI / 2;
  const length = active
    ? Math.max(34, Math.hypot(targetX - centerX, targetY - centerY) + 18)
    : 22;
  const spread = active ? 0.30 : 0.34;
  const alpha = active ? 0.28 + 0.08 * Math.sin(elapsed * 6.0) : 0.07;
  const x1 = centerX + Math.cos(angle - spread) * length;
  const y1 = centerY + Math.sin(angle - spread) * length;
  const x2 = centerX + Math.cos(angle + spread) * length;
  const y2 = centerY + Math.sin(angle + spread) * length;
  ctx.save();
  ctx.globalCompositeOperation = "lighter";
  const gradient = ctx.createRadialGradient(centerX, centerY, 2, centerX, centerY, length);
  gradient.addColorStop(0, `rgba(255, 236, 159, ${Math.min(alpha + 0.14, 0.42)})`);
  gradient.addColorStop(0.55, `rgba(255, 224, 122, ${alpha})`);
  gradient.addColorStop(1, "rgba(255, 224, 122, 0)");
  ctx.fillStyle = gradient;
  ctx.beginPath();
  ctx.moveTo(centerX, centerY);
  ctx.lineTo(x1, y1);
  ctx.lineTo(x2, y2);
  ctx.closePath();
  ctx.fill();
  ctx.restore();
}

let beaconTableIconPathCache = null;

function beaconTableIconPaths() {
  if (beaconTableIconPathCache) return beaconTableIconPathCache;
  beaconTableIconPathCache = Object.freeze({
    towerBody: new Path2D("M8.5 21.5l1.5-12h4l1.5 12z"),
    towerCap: new Path2D("M8 9.5h8v2H8z"),
    roof: new Path2D("M8 5.5l4-3 4 3z"),
    ground: new Path2D("M4.5 21.5c1-2 4-2 6-1s3 2 4 1h-10z"),
    leftRays: new Path2D("M4.2 3.5l1.2 1.2 M2.5 7.5h2.5 M4.2 11.5l1.2-1.2"),
    rightRays: new Path2D("M19.8 3.5l-1.2 1.2 M19 7.5h2.5 M19.8 11.5l-1.2-1.2"),
    roofFrame: new Path2D("M11.2 2.5a.8.8 0 0 1 1.6 0M8 5.5l4-3 4 3z M8 5.5h8"),
    towerFrame: new Path2D("M9.5 9.5V5.5 M14.5 5.5v4 M11 9.5V7.5h2v2"),
    towerCapFrame: new Path2D("M7.5 9.5h9v2h-9z"),
    towerStripes: new Path2D("M8 17.6l8-4.8 M8.3 21l8-4.8"),
    groundLine: new Path2D("M2 21.5h20"),
  });
  return beaconTableIconPathCache;
}

function drawBeaconTableIcon({ ctx, centerX, centerY, size }) {
  const scale = size / 24;
  const paths = beaconTableIconPaths();
  ctx.save();
  ctx.translate(centerX - 12 * scale, centerY - 12 * scale);
  ctx.scale(scale, scale);
  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  ctx.fillStyle = "#e3f2fd";
  ctx.fill(paths.towerBody);
  ctx.save();
  ctx.clip(paths.towerBody);
  ctx.fillStyle = "#ff4747";
  ctx.beginPath();
  ctx.moveTo(7, 18);
  ctx.lineTo(17, 12);
  ctx.lineTo(17, 17);
  ctx.lineTo(7, 23);
  ctx.closePath();
  ctx.fill();
  ctx.restore();
  ctx.fillStyle = "#8d6e63";
  ctx.fill(paths.towerCap);
  ctx.fillStyle = "#ffb74d";
  ctx.fillRect(11, 7.5, 2, 2);
  ctx.fillStyle = "#ff4747";
  ctx.fill(paths.roof);
  ctx.fillStyle = "#ffd54f";
  ctx.beginPath();
  ctx.arc(12, 2.5, 0.8, Math.PI, 0);
  ctx.fill();
  ctx.fillStyle = "#f5f5f5";
  ctx.fill(paths.ground);
  ctx.fillStyle = "#424242";
  ctx.fillRect(14.5, 20.5, 2, 1);
  ctx.strokeStyle = "#1a1a1a";
  ctx.lineWidth = 1.25;
  ctx.stroke(paths.leftRays);
  ctx.stroke(paths.rightRays);
  ctx.stroke(paths.roofFrame);
  ctx.stroke(paths.towerFrame);
  ctx.stroke(paths.towerCapFrame);
  ctx.stroke(paths.towerBody);
  ctx.save();
  ctx.clip(paths.towerBody);
  ctx.stroke(paths.towerStripes);
  ctx.restore();
  ctx.stroke(paths.ground);
  ctx.stroke(paths.groundLine);
  ctx.restore();
}

function marketSpotlightContext(snapshot = state.snapshot) {
  const indicator = snapshot?.indicators?.market_spotlight || {};
  if (indicator.market_context && typeof indicator.market_context === "object") {
    return indicator.market_context;
  }
  return indicator.latest?.market_context && typeof indicator.latest.market_context === "object"
    ? indicator.latest.market_context
    : null;
}

const marketSpotlightBriefState = {
  open: false,
  showScenario: false,
  scopeKey: "",
  focusPending: false,
};

function marketSpotlightBriefScopeKey(snapshot = state.snapshot) {
  const meta = snapshot?.meta || {};
  return JSON.stringify([
    exactIdentityText(meta.instrument_id || state.instrumentId),
    exactIdentityText(meta.route_fingerprint || instrumentRouteFingerprint()),
    String(meta.timeframe || state.timeframe || ""),
  ]);
}

function marketSpotlightSyncBriefScope(snapshot = state.snapshot) {
  const scopeKey = marketSpotlightBriefScopeKey(snapshot);
  if (scopeKey === marketSpotlightBriefState.scopeKey) return false;
  marketSpotlightBriefState.scopeKey = scopeKey;
  marketSpotlightBriefState.open = false;
  marketSpotlightSetScenarioVisible(false);
  const layer = document.getElementById("market-spotlight-brief-layer");
  if (layer) marketSpotlightRenderBriefModal(snapshot);
  return true;
}

function marketSpotlightBriefPrice(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  const absolute = Math.abs(number);
  const digits = absolute >= 100 ? 2 : absolute >= 1 ? 3 : 6;
  return number.toLocaleString(undefined, {
    minimumFractionDigits: 0,
    maximumFractionDigits: digits,
  });
}

function marketSpotlightBriefRelation(value) {
  return ({
    above: "выше",
    below: "ниже",
    reclaim: "после возврата",
    at: "у",
  })[String(value || "").toLowerCase()] || "у";
}

function marketSpotlightBriefDirection(value) {
  return ({
    long: "вверх",
    short: "вниз",
    flat: "в балансе",
  })[String(value || "flat").toLowerCase()] || "в балансе";
}

function marketSpotlightBriefStateText(value) {
  return ({
    trap: "ловушка",
    micro_break: "микропробой",
    vix_risk: "риск VIX",
    reversal_attempt: "попытка разворота",
    absorption: "поглощение",
    trend_initiation: "зарождение тренда",
    balance: "баланс",
    directional_flow: "направленный поток",
    continuation_pattern: "паттерн продолжения",
  })[String(value || "").toLowerCase()] || "контекст ожидания";
}

function marketSpotlightBriefDirective(value) {
  return ({
    avoid_short: "не открывать шорт против подтверждённого потока",
    avoid_long: "не открывать лонг против подтверждённого потока",
    pullback_watch: "ждать откат и повторное подтверждение",
    follow_flow: "работать только по направлению подтверждённого потока",
    wait_balance: "ждать выхода из баланса",
    wait_for_context: "ждать контекст",
    enter_from_level_retest_if_stretched: "ждать ретест уровня, если цена растянута",
    prepare_plan_wait_confirmation: "подготовить план и ждать подтверждение",
    wait_pullback: "ждать откат",
    wait_closed_bar_confirmation: "ждать подтверждение закрытым баром",
    wait_break_confirmation: "ждать подтверждённый пробой",
    execute_break: "пробой подтверждён; следовать Trade Setup",
    wait_reclaim: "ждать возврат уровня",
    prepare_reclaim_entry: "подготовить вход после подтверждённого возврата",
  })[String(value || "").toLowerCase()] || "ждать подтверждение";
}

function marketSpotlightBriefToneColor(tone) {
  return ({
    entry: "#f4c95d",
    invalidation: "#f97316",
    stop: "#ef4444",
    target: "#22c55e",
    call: "#ef4444",
    put: "#22c55e",
    gamma: "#a855f7",
    vwap: "#38bdf8",
    range: "#94a3b8",
  })[tone] || "#f4c95d";
}

function marketSpotlightBriefAddLevel(levels, item) {
  const price = Number(item?.price);
  if (!Number.isFinite(price)) return;
  const duplicate = levels.find(level => (
    Math.abs(level.price - price) <= Math.max(1e-8, Math.abs(price) * 1e-9)
  ));
  if (duplicate) {
    if (!duplicate.aliases.includes(item.shortLabel)) duplicate.aliases.push(item.shortLabel);
    if (!duplicate.sources.includes(item.source)) duplicate.sources.push(item.source);
    if (!duplicate.details.some(detail => detail.label === item.label)) {
      duplicate.details.push({
        label: item.label,
        read: item.read,
        source: item.source,
      });
      duplicate.label = duplicate.details.map(detail => detail.label).join(" / ");
      duplicate.read = duplicate.details.map(detail => detail.read).join(" ");
    }
    duplicate.overlay = duplicate.overlay || item.overlay === true;
    return;
  }
  levels.push({
    ...item,
    price,
    aliases: [item.shortLabel],
    sources: [item.source],
    details: [{ label: item.label, read: item.read, source: item.source }],
    overlay: item.overlay === true,
  });
}

function marketSpotlightBriefTradeSetup(snapshot) {
  return typeof tradeSetupRuntimeModel === "function"
    ? tradeSetupRuntimeModel(snapshot)
    : null;
}

function marketSpotlightBriefGex(snapshot) {
  return typeof activeGexContext === "function" ? activeGexContext(snapshot) : null;
}

function marketSpotlightBriefLevels(snapshot = state.snapshot) {
  const context = marketSpotlightContext(snapshot) || {};
  const levels = [];
  const setup = marketSpotlightBriefTradeSetup(snapshot);
  if (setup?.authorityReady) {
    marketSpotlightBriefAddLevel(levels, {
      key: "trade_entry",
      role: "trade_entry",
      label: "Вход Trade Setup",
      shortLabel: "TS ENTRY",
      price: setup.entry,
      meaning: "Текущий вход канонического Trade Setup",
      read: setup.blocked
        ? "План заблокирован; уровень показан только для контекста."
        : "Действует только при authority READY и выполнении typed trigger.",
      source: "trade_setup",
      tone: "entry",
      style: "dash",
      direction: setup.side,
      overlay: true,
    });
    marketSpotlightBriefAddLevel(levels, {
      key: "trade_stop",
      role: "trade_stop",
      label: "Стоп Trade Setup",
      shortLabel: "TS STOP",
      price: setup.stop,
      meaning: "Риск текущего Trade Setup",
      read: "Инвалидация плана; дайджест не переносит и не пересчитывает этот уровень.",
      source: "trade_setup",
      tone: "stop",
      style: "dash",
      direction: setup.side,
      overlay: true,
    });
    marketSpotlightBriefAddLevel(levels, {
      key: "trade_target",
      role: "trade_target",
      label: "Цель Trade Setup",
      shortLabel: "TS TARGET",
      price: setup.target,
      meaning: "Цель текущего Trade Setup",
      read: "Ориентир активного плана; не является обещанием исполнения.",
      source: "trade_setup",
      tone: "target",
      style: "dash",
      direction: setup.side,
      overlay: true,
    });
  }

  const entry = context.entry && typeof context.entry === "object" ? context.entry : {};
  marketSpotlightBriefAddLevel(levels, {
    key: "spotlight_entry",
    role: "spotlight_entry",
    label: "Триггер Spotlight",
    shortLabel: "MS TRIGGER",
    price: entry.trigger,
    meaning: "Условие входного контекста Spotlight",
    read: `Ждать закрытие ${marketSpotlightBriefRelation(entry.relation)} уровня; затем ${marketSpotlightBriefDirective(entry.directive)}.`,
    source: "market_spotlight",
    tone: "entry",
    style: "solid",
    direction: entry.direction,
    overlay: true,
  });

  const invalidation = context.invalidation && typeof context.invalidation === "object"
    ? context.invalidation
    : {};
  marketSpotlightBriefAddLevel(levels, {
    key: "spotlight_invalidation",
    role: "spotlight_invalidation",
    label: "Инвалидация Spotlight",
    shortLabel: "MS INV",
    price: invalidation.level,
    meaning: "Граница отмены текущего контекста",
    read: `Подтверждённое закрытие ${marketSpotlightBriefRelation(invalidation.relation)} уровня отменяет текущий сценарий.`,
    source: "market_spotlight",
    tone: "invalidation",
    style: "dash",
    direction: invalidation.direction,
    overlay: true,
  });

  const gex = marketSpotlightBriefGex(snapshot);
  if (gex) {
    const gexLevels = Array.isArray(gex.levels) ? gex.levels : [];
    const callWall = gexLevels.find(level => level?.kind === "CALL_WALL");
    const putWall = gexLevels.find(level => level?.kind === "PUT_WALL");
    const gexAuthority = gex.decision_authoritative === true
      ? "Текущий exact-route GEX контекст допущен; сам уровень остаётся ориентиром, а не сигналом."
      : "GEX доступен только для отображения и не авторизует решение.";
    marketSpotlightBriefAddLevel(levels, {
      key: "gex_call_wall",
      role: "gex_call_wall",
      label: "Call Wall",
      shortLabel: "CALL WALL",
      price: callWall?.strike,
      meaning: "Опубликованная call-концентрация",
      read: `${gexAuthority} Принятый пробой может изменить режим; касание само по себе ничего не подтверждает.`,
      source: "gex",
      tone: "call",
      style: "dot",
      direction: "short",
      overlay: true,
    });
    marketSpotlightBriefAddLevel(levels, {
      key: "gex_put_wall",
      role: "gex_put_wall",
      label: "Put Wall",
      shortLabel: "PUT WALL",
      price: putWall?.strike,
      meaning: "Опубликованная put-концентрация",
      read: `${gexAuthority} Это не автоматическая поддержка.`,
      source: "gex",
      tone: "put",
      style: "dot",
      direction: "long",
      overlay: true,
    });
    marketSpotlightBriefAddLevel(levels, {
      key: "gex_zero_gamma",
      role: "gex_zero_gamma",
      label: "Zero Gamma",
      shortLabel: "ZERO GAMMA",
      price: gex.gamma_flip,
      meaning: "Модельная граница gamma-режима",
      read: `${gexAuthority} Это оценочная граница, не автоматическая поддержка или сопротивление.`,
      source: "gex",
      tone: "gamma",
      style: "dash",
      direction: "flat",
      overlay: true,
    });
  }

  const vwap = context.vwap && typeof context.vwap === "object" ? context.vwap : {};
  marketSpotlightBriefAddLevel(levels, {
    key: "vwap",
    role: "vwap",
    label: "VWAP",
    shortLabel: "VWAP",
    price: vwap.base,
    meaning: "Сессионная средняя провайдера",
    read: "Динамический контекст возврата/удержания; не торговый сигнал сам по себе.",
    source: "market_spotlight",
    tone: "vwap",
    style: "dot",
    direction: context.direction,
    overlay: true,
  });
  for (const [key, label, shortLabel] of [
    ["upper_2", "VWAP +2σ", "VWAP +2σ"],
    ["lower_2", "VWAP −2σ", "VWAP −2σ"],
  ]) {
    marketSpotlightBriefAddLevel(levels, {
      key: `vwap_${key}`,
      role: `vwap_${key}`,
      label,
      shortLabel,
      price: vwap[key],
      meaning: "Внешняя граница текущего VWAP-контекста",
      read: "Зона растяжения; продолжение или разворот требует отдельного typed подтверждения.",
      source: "market_spotlight",
      tone: "vwap",
      style: "dot",
      direction: key === "upper_2" ? "short" : "long",
      overlay: false,
    });
  }

  const openingRange = context.opening_range && typeof context.opening_range === "object"
    ? context.opening_range
    : {};
  if (openingRange.ready === true) {
    marketSpotlightBriefAddLevel(levels, {
      key: "opening_range_high",
      role: "opening_range_high",
      label: "Opening Range High",
      shortLabel: "OR HIGH",
      price: openingRange.high,
      meaning: "Верх подтверждённого provider opening range",
      read: "Удержание выше меняет session context; касание без принятия не является пробоем.",
      source: "market_spotlight",
      tone: "range",
      style: "dash",
      direction: "long",
      overlay: true,
    });
    marketSpotlightBriefAddLevel(levels, {
      key: "opening_range_low",
      role: "opening_range_low",
      label: "Opening Range Low",
      shortLabel: "OR LOW",
      price: openingRange.low,
      meaning: "Низ подтверждённого provider opening range",
      read: "Удержание ниже меняет session context; касание без принятия не является пробоем.",
      source: "market_spotlight",
      tone: "range",
      style: "dash",
      direction: "short",
      overlay: true,
    });
  }
  return levels;
}

function marketSpotlightBriefSummary(context) {
  const flow = context?.flow_state && typeof context.flow_state === "object"
    ? context.flow_state
    : {};
  const risk = context?.risk && typeof context.risk === "object" ? context.risk : {};
  const finality = context?.closed === false
    ? "Текущий бар формируется: это provisional preview, а не подтверждённое состояние."
    : "Контекст рассчитан по подтверждённому закрытому бару.";
  const riskText = String(risk.state || "clear").toLowerCase() === "clear"
    ? "Явного risk veto сейчас нет."
    : `Активен риск: ${marketSpotlightCodeText(risk.state)}.`;
  return [
    `Рынок ${marketSpotlightBriefDirection(context?.direction)}; состояние — ${marketSpotlightBriefStateText(context?.state)}.`,
    marketSpotlightBriefDirective(flow.directive),
    riskText,
    finality,
  ].join(" ");
}

function marketSpotlightBriefPlaybook(context, setup) {
  const entry = context?.entry && typeof context.entry === "object" ? context.entry : {};
  const invalidation = context?.invalidation && typeof context.invalidation === "object"
    ? context.invalidation
    : {};
  const rows = [];
  if (Number.isFinite(Number(entry.trigger))) {
    rows.push({
      label: `Подтверждение ${marketSpotlightBriefRelation(entry.relation)} ${marketSpotlightBriefPrice(entry.trigger)}`,
      text: marketSpotlightBriefDirective(entry.directive),
      tone: "primary",
    });
  }
  if (Number.isFinite(Number(invalidation.level))) {
    rows.push({
      label: `Инвалидация ${marketSpotlightBriefRelation(invalidation.relation)} ${marketSpotlightBriefPrice(invalidation.level)}`,
      text: invalidation.blocks_side
        ? `Блокируется сторона ${String(invalidation.blocks_side).toUpperCase()}; ждать нового typed demand.`
        : "Текущий контекст отменяется; ждать нового подтверждённого состояния Spotlight.",
      tone: "danger",
    });
  }
  if (setup?.authorityReady && Number.isFinite(Number(setup.entry))) {
    rows.push({
      label: `Trade Setup ${setup.displayPhase} ${String(setup.side || "flat").toUpperCase()}`,
      text: setup.blocked
        ? "План заблокирован и показан только для контекста."
        : `Entry ${marketSpotlightBriefPrice(setup.entry)} · Stop ${marketSpotlightBriefPrice(setup.stop)} · Target ${marketSpotlightBriefPrice(setup.target)}.`,
      tone: setup.blocked ? "danger" : "success",
    });
  } else {
    rows.push({
      label: "Trade Setup",
      text: "Execution authority не READY; дайджест не создаёт собственный план.",
      tone: "muted",
    });
  }
  return rows;
}

function marketSpotlightScenarioOverlays(snapshot = state.snapshot) {
  if (!marketSpotlightBriefState.showScenario || !marketSpotlightContext(snapshot)) return [];
  const bars = Array.isArray(snapshot?.bars) ? snapshot.bars : [];
  if (!bars.length) return [];
  const startTs = bars[0]?.ts;
  const endAnchorTs = bars[bars.length - 1]?.ts;
  if (!startTs || !endAnchorTs) return [];
  return marketSpotlightBriefLevels(snapshot)
    .filter(level => level.overlay)
    .map(level => ({
      type: "line",
      source: "market_spotlight",
      layer: "levels",
      contract: "overlay-contract-v1",
      id: `market_spotlight_brief:${level.key}`,
      start_ts: startTs,
      end_anchor_ts: endAnchorTs,
      end_bar_offset: 32,
      price: level.price,
      y1: level.price,
      y2: level.price,
      label: level.aliases.join(" · "),
      label_position: "right",
      label_anchor: "price_axis",
      label_font_size: 9,
      color: marketSpotlightBriefToneColor(level.tone),
      opacity: 0.86,
      width: ["trade_stop", "spotlight_invalidation"].includes(level.role) ? 1.5 : 1,
      style: level.style,
      role: "scenario_reference",
      kind: level.role,
      scenario_role: level.role,
      scenario: "market_spotlight_brief_reference",
      setup: "advisory_market_brief",
      direction: level.direction || "flat",
      advisory_only: true,
      signal_overlay: false,
      context_lines: [level.meaning, level.read],
      metrics: {
        sources: level.sources,
        role: level.role,
        price: level.price,
      },
    }));
}

function marketSpotlightBriefOverlayStateKey() {
  if (!marketSpotlightBriefState.showScenario) return "off";
  if (!marketSpotlightContext(state.snapshot)) return "unavailable";
  const levels = marketSpotlightBriefLevels(state.snapshot)
    .filter(level => level.overlay)
    .map(level => [level.key, level.price, level.aliases]);
  return JSON.stringify([
    "on",
    marketSpotlightBriefScopeKey(state.snapshot),
    levels,
  ]);
}

function marketSpotlightRefreshScenarioOverlay() {
  if (typeof bumpChartRenderVersion === "function") bumpChartRenderVersion("indicators");
  if (state.snapshot && typeof renderPriceOverlay === "function") {
    renderPriceOverlay(state.snapshot);
  }
}

function marketSpotlightSetScenarioVisible(visible) {
  const next = visible === true;
  if (marketSpotlightBriefState.showScenario === next) return;
  marketSpotlightBriefState.showScenario = next;
  marketSpotlightRefreshScenarioOverlay();
}

function marketSpotlightBriefLayer() {
  let layer = document.getElementById("market-spotlight-brief-layer");
  if (layer) return layer;
  layer = document.createElement("div");
  layer.id = "market-spotlight-brief-layer";
  layer.className = "market-spotlight-brief-layer";
  layer.hidden = true;
  layer.addEventListener("click", event => {
    const actionNode = event.target.closest?.("[data-market-spotlight-brief-action]");
    const action = actionNode?.dataset.marketSpotlightBriefAction || "";
    if (action === "close" || event.target === layer) {
      marketSpotlightBriefState.open = false;
      marketSpotlightRenderBriefModal(state.snapshot);
      return;
    }
    if (action === "scenario") {
      marketSpotlightSetScenarioVisible(!marketSpotlightBriefState.showScenario);
      marketSpotlightRenderBriefModal(state.snapshot);
    }
  });
  document.addEventListener("keydown", event => {
    if (event.key !== "Escape" || !marketSpotlightBriefState.open) return;
    marketSpotlightBriefState.open = false;
    marketSpotlightRenderBriefModal(state.snapshot);
  });
  document.body.appendChild(layer);
  return layer;
}

function marketSpotlightBriefLevelRows(levels) {
  if (!levels.length) {
    return '<tr><td colspan="3" class="market-spotlight-brief-empty">Нет доступных typed уровней.</td></tr>';
  }
  return levels.map(level => `
    <tr>
      <td class="market-spotlight-brief-level-price">
        <span class="market-spotlight-brief-level-swatch" style="--brief-level-color:${escapeHtml(marketSpotlightBriefToneColor(level.tone))}"></span>
        ${escapeHtml(marketSpotlightBriefPrice(level.price))}
      </td>
      <td>
        <b>${escapeHtml(level.label)}</b>
        <span>${escapeHtml(level.sources.map(source => (
          source === "gex" ? "GEX" : source === "trade_setup" ? "Trade Setup" : "Spotlight"
        )).join(" · "))}</span>
      </td>
      <td>${escapeHtml(level.read)}</td>
    </tr>
  `).join("");
}

function marketSpotlightBriefPlaybookRows(rows) {
  return rows.map(row => `
    <div class="market-spotlight-brief-playbook-row ${escapeHtml(row.tone)}">
      <b>${escapeHtml(row.label)}:</b>
      <span>${escapeHtml(row.text)}</span>
    </div>
  `).join("");
}

function marketSpotlightRenderBriefModal(snapshot = state.snapshot) {
  const layer = marketSpotlightBriefLayer();
  layer.hidden = !marketSpotlightBriefState.open;
  if (!marketSpotlightBriefState.open) {
    layer.innerHTML = "";
    return;
  }
  const context = marketSpotlightContext(snapshot);
  const meta = snapshot?.meta || {};
  const levels = context ? marketSpotlightBriefLevels(snapshot) : [];
  const setup = context ? marketSpotlightBriefTradeSetup(snapshot) : null;
  const playbook = context ? marketSpotlightBriefPlaybook(context, setup) : [];
  const titleSymbol = String(meta.provider_symbol || state.symbol || "Market");
  const timeframe = String(meta.timeframe || state.timeframe || "");
  const latestTs = String(snapshot?.indicators?.market_spotlight?.latest?.ts || meta.analysis_ts || "");
  const timestampLabel = latestTs
    ? new Date(latestTs).toLocaleString()
    : "время расчёта недоступно";
  const scenarioLabel = marketSpotlightBriefState.showScenario
    ? "Скрыть сценарий"
    : "Показать сценарий";
  layer.innerHTML = `
    <section class="market-spotlight-brief" role="dialog" aria-modal="true" aria-labelledby="market-spotlight-brief-title">
      <header class="market-spotlight-brief-head">
        <div>
          <div class="market-spotlight-brief-kicker">MARKET SPOTLIGHT</div>
          <h2 id="market-spotlight-brief-title">Market Brief · ${escapeHtml(titleSymbol)}</h2>
          <p>${escapeHtml([timeframe, timestampLabel].filter(Boolean).join(" · "))}</p>
        </div>
        <button type="button" class="market-spotlight-brief-close" data-market-spotlight-brief-action="close" aria-label="Закрыть Market Brief">×</button>
      </header>
      ${context ? `
        <div class="market-spotlight-brief-summary">
          <b>Общая картина:</b>
          <span>${escapeHtml(marketSpotlightBriefSummary(context))}</span>
        </div>
        <div class="market-spotlight-brief-section-head">
          <h3>Ключевые уровни</h3>
          <button type="button" class="market-spotlight-brief-scenario ${marketSpotlightBriefState.showScenario ? "active" : ""}" data-market-spotlight-brief-action="scenario" aria-pressed="${marketSpotlightBriefState.showScenario ? "true" : "false"}">
            ${escapeHtml(scenarioLabel)}
          </button>
        </div>
        <div class="market-spotlight-brief-table-wrap">
          <table class="market-spotlight-brief-table">
            <thead><tr><th>Уровень</th><th>Что означает</th><th>Простое чтение</th></tr></thead>
            <tbody>${marketSpotlightBriefLevelRows(levels)}</tbody>
          </table>
        </div>
        <section class="market-spotlight-brief-playbook">
          <h3>Playbook</h3>
          ${marketSpotlightBriefPlaybookRows(playbook)}
        </section>
        <div class="market-spotlight-brief-rule">
          <b>Главное правило:</b>
          касание уровня не равно подтверждению. Для действия нужен typed trigger, закрытый бар и готовая execution authority.
        </div>
        <footer class="market-spotlight-brief-foot">
          <span>Advisory presentation</span>
          <span>${escapeHtml(context.closed === false ? "PROVISIONAL" : "CONFIRMED BAR")}</span>
          <span>${escapeHtml(levels.some(level => level.sources.includes("gex")) ? "EXACT-ROUTE GEX" : "GEX UNAVAILABLE")}</span>
        </footer>
      ` : `
        <div class="market-spotlight-brief-unavailable">
          Spotlight не опубликовал typed market context. Дайджест и сценарные уровни недоступны.
        </div>
      `}
    </section>
  `;
  if (marketSpotlightBriefState.focusPending) {
    marketSpotlightBriefState.focusPending = false;
    window.requestAnimationFrame(() => {
      layer.querySelector(".market-spotlight-brief-close")?.focus();
    });
  }
}

function marketSpotlightToggleBrief(snapshot = state.snapshot) {
  if (!marketSpotlightContext(snapshot)) return false;
  marketSpotlightSyncBriefScope(snapshot);
  marketSpotlightBriefState.open = !marketSpotlightBriefState.open;
  marketSpotlightBriefState.focusPending = marketSpotlightBriefState.open;
  marketSpotlightRenderBriefModal(snapshot);
  return true;
}

function renderMarketSpotlightPanel(snapshot, context = {}) {
  renderMarketSpotlightRiskFeedback(snapshot, context);
  marketSpotlightSyncBriefScope(snapshot);
  const available = context.calcEnabled !== false
    && context.visible !== false
    && Boolean(marketSpotlightContext(snapshot));
  if (!available) {
    marketSpotlightBriefState.open = false;
    marketSpotlightSetScenarioVisible(false);
    const layer = document.getElementById("market-spotlight-brief-layer");
    if (layer) marketSpotlightRenderBriefModal(snapshot);
    return;
  }
  if (marketSpotlightBriefState.open) marketSpotlightRenderBriefModal(snapshot);
}

let lastMarketRiskTone = "";
let marketRiskShiftTimer = null;

function renderMarketSpotlightRiskFeedback(snapshot, context = {}) {
  const frame = document.getElementById("price-frame");
  if (!frame) return;
  const risk = context.calcEnabled === false ? null : marketSpotlightContext(snapshot)?.risk;
  const tone = ["warn", "danger"].includes(String(risk?.tone || "").toLowerCase())
    ? String(risk.tone).toLowerCase()
    : "clear";
  frame.dataset.marketRisk = tone;
  frame.classList.toggle("market-risk-warn", tone === "warn");
  frame.classList.toggle("market-risk-danger", tone === "danger");
  const changed = Boolean(lastMarketRiskTone) && tone !== lastMarketRiskTone;
  lastMarketRiskTone = tone;
  if (!changed || tone === "clear" || !uiMotionEnabled()) return;
  frame.classList.remove("market-risk-shift");
  window.clearTimeout(marketRiskShiftTimer);
  window.requestAnimationFrame(() => frame.classList.add("market-risk-shift"));
  marketRiskShiftTimer = window.setTimeout(
    () => frame.classList.remove("market-risk-shift"),
    520,
  );
}

registerIndicatorTableModel("market_spotlight", marketSpotlightTableColumns);
registerIndicatorTableAction("market-spotlight-open-brief", {
  click: ({ snapshot } = {}) => {
    const activeSnapshot = snapshot || state.snapshot;
    if (!indicatorCalcForId("market_spotlight")) return;
    if (indicatorVisibleForId("market_spotlight") === false) return;
    marketSpotlightToggleBrief(activeSnapshot);
  },
});
registerIndicatorTableHeaderIcon("beacon", drawBeaconTableIcon, {
  action: "market-spotlight-open-brief",
});
registerIndicatorTableAnimation("market_spotlight_state_beam", context => {
  if (!marketSpotlightAnimationEnabled()) return;
  const beamX = context.left - context.headerW / 2;
  const beamY = context.top + context.cellH / 2;
  const stateBeamActive = updateMarketSpotlightStateBeam(context.table);
  drawBeaconSpotlightBeam(
    context.ctx,
    beamX,
    beamY - 4,
    context.left + context.cellW / 2,
    context.top + context.cellH / 2,
    { mode: stateBeamActive ? "state" : "idle" },
  );
  ensureMarketSpotlightAnimationLoop();
});
registerIndicatorControlEffect("market_spotlight_animation", () => {
  if (state.snapshot) renderPriceOverlay(state.snapshot);
  ensureMarketSpotlightAnimationLoop();
  return true;
});
registerIndicatorOverlayContribution("market_spotlight_brief", {
  layer: "levels",
  enabled: () => (
    marketSpotlightBriefState.showScenario
    && indicatorCalcForId("market_spotlight")
    && indicatorVisibleForId("market_spotlight") !== false
  ),
  collect: marketSpotlightScenarioOverlays,
  stateKey: marketSpotlightBriefOverlayStateKey,
});
registerIndicatorPanelRenderer(
  "market_spotlight",
  renderMarketSpotlightPanel,
);
