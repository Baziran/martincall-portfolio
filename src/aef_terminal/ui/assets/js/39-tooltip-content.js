    function tooltipTextValue(value) {
      const raw = Array.isArray(value) ? value.map(item => String(item ?? "").trim()).filter(Boolean).join("\n") : String(value ?? "");
      return raw.replace(/\\n/g, "\n").trim();
    }

    const CANVAS_TOOLTIP_DEFAULT_MAX_LINES = 32;

    function normalizedSignalSource(source, fallback = "SIGNAL") {
      const key = String(source || "").trim();
      if (!key) return fallback;
      const spec = typeof indicatorSpec === "function" ? indicatorSpec(key) : {};
      if (spec?.label) return String(spec.label).toUpperCase();
      return key.replace(/_/g, " ").trim().toUpperCase() || fallback;
    }

    const PLAN_ROLE_LABELS = new Set(["E", "S", "T", "ENTRY", "STOP", "TARGET", "TRAIL", "TRAIL STOP", "TRACK"]);

    function isPlanRoleLabel(value) {
      const text = String(value || "").replace(/\s+/g, " ").trim().toUpperCase();
      return PLAN_ROLE_LABELS.has(text);
    }

    function planRoleSuffix(role) {
      const normalized = String(role || "").toLowerCase();
      if (normalized === "entry") return "ENTRY";
      if (normalized === "target") return "TARGET";
      if (normalized === "stop") return "STOP";
      if (normalized === "trail") return "TRAIL STOP";
      if (normalized === "track") return "TRACK";
      return "";
    }

    function overlaySignalSource(item, sourceOverride = "") {
      const plan = structuredTradePlan(item);
      return sourceOverride || item?.source || item?.indicator || plan?.source || "";
    }

    function isPlanRoleItem(item) {
      const role = String(normalizedOverlaySignal(item).role || "").toLowerCase();
      return ["entry", "stop", "target", "trail", "track"].includes(role);
    }

    function planLineTitle(item, sourceOverride = "") {
      const role = String(normalizedOverlaySignal(item).role || "").toLowerCase();
      const suffix = planRoleSuffix(role);
      const source = normalizedSignalSource(overlaySignalSource(item, sourceOverride), "SIGNAL");
      if (suffix) return `${source} ${suffix}`.toUpperCase();
      return signalNoteTitle(item, sourceOverride);
    }


const ACTION_PHASE_GLYPHS = Object.freeze({
  WAIT: "[W]",
  WATCH: "[W]",
  CANDIDATE: "[C]",
  ARM: "[A]",
  GO: "[G]",
  IN: "[IN]",
  TRAIL: "[T]",
  TARGET_HIT: "[✓]",
  STOP_HIT: "[X]",
  BLOCK: "[X]",
});

const ACTION_SOURCE_LABELS = Object.freeze({
  option_reversal: "Option Reversal",
  smc: "Smart Money Concepts",
  structure: "Market Structure",
  data_quality: "Data Quality",
});

const ACTION_SOURCE_CODES = Object.freeze({
  option_reversal: "OR",
  smc: "SMC",
  structure: "STR",
  data_quality: "DQ",
});

function domainCodeLabel(value) {
  if (value === null || value === undefined || value === "") return "";
  if (typeof value === "number") return Number.isFinite(value) ? String(value) : "";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value !== "string") return "";
  return value
    .replace(/[_-]+/g, " ")
    .replace(/\s+/g, " ")
    .replace(/^\w/, (letter) => letter.toUpperCase());
}

const DOMAIN_FACT_TABLE_PRESENTATIONS = Object.freeze({
  bars_unavailable: Object.freeze(["CLOSED 1M", "UNAVAILABLE"]),
  option_target_required: Object.freeze(["OPTION TARGET", "REQUIRED"]),
  option_target_price_required: Object.freeze(["TARGET PRICE", "REQUIRED"]),
  option_market_data_pending: Object.freeze(["OPTION DATA", "PENDING"]),
  option_market_data_stale: Object.freeze(["OPTION DATA", "STALE"]),
  atr_unavailable: Object.freeze(["ATR", "UNAVAILABLE"]),
  premium_lag_entry_candidate: Object.freeze(["PREMIUM LAG", "ENTRY CANDIDATE"]),
  premium_compression_entry_candidate: Object.freeze(["PREMIUM COMP.", "ENTRY CANDIDATE"]),
  option_edge_confirmation_incomplete: Object.freeze(["OPTION EDGE", "UNCONFIRMED"]),
  premium_expanded_reduce: Object.freeze(["PREMIUM EXPANDED", "REDUCE"]),
  option_edge_absent: Object.freeze(["OPTION EDGE", "ABSENT"]),
});

function domainFactTableCellText(fact, part) {
  if (
    !fact
    || typeof fact !== "object"
    || Array.isArray(fact)
    || typeof fact.code !== "string"
    || !fact.code
  ) return null;
  const normalizedPart = String(part || "").trim().toLowerCase();
  if (!["primary", "secondary"].includes(normalizedPart)) return null;
  const presentation = DOMAIN_FACT_TABLE_PRESENTATIONS[fact.code.toLowerCase()];
  if (presentation) return presentation[normalizedPart === "primary" ? 0 : 1];
  return normalizedPart === "primary" ? domainCodeLabel(fact.code).toUpperCase() : "";
}

function domainFactText(fact) {
  if (!fact || typeof fact !== "object" || Array.isArray(fact) || typeof fact.code !== "string" || !fact.code) return "";
  const factCode = fact.code.toLowerCase();
  if (factCode === "option_market_data_pending") return "Waiting for broker option data";
  if (factCode === "option_market_data_stale") return "Broker option data stale";
  if (factCode.startsWith("smc_")) {
    const objectType = String(fact.object_type || "object").toLowerCase();
    const direction = String(fact.direction || "").toLowerCase();
    const kind = String(fact.kind || "").toLowerCase();
    const role = String(fact.role || "").toLowerCase();
    const eventCode = String(fact.event_code || "").toLowerCase();
    const highSide = direction === "short" || kind === "resistance";
    const lowSide = direction === "long" || kind === "support";
    const previousLevel = role === "previous_swing_high" || role === "previous_swing_low";
    const structureBreak = eventCode === "break_of_structure" || eventCode === "change_of_character";
    const liquidityEvent = ["liquidity_sweep", "equal_high", "equal_low", "reclaim", "reject"].includes(eventCode);
    if (factCode === "smc_bias") {
      if (lowSide && !highSide) return "Bias: bullish/low-side context.";
      if (highSide && !lowSide) return "Bias: bearish/high-side context.";
      return "Bias: two-sided structure context.";
    }
    if (factCode === "smc_importance") {
      if (objectType === "level") return previousLevel
        ? "Why: previous swing level; resting liquidity and failed-break reference."
        : "Why: active swing level; current market-structure boundary and liquidity pool.";
      if (objectType === "zone" && highSide) return "Why: supply marks high-side liquidity and likely trapped late longs.";
      if (objectType === "zone" && lowSide) return "Why: demand marks low-side liquidity and likely trapped late shorts.";
      if (objectType === "zone") return "Why: merged SMC zone concentrates prior imbalance and reaction flow.";
      if (objectType === "box") return "Why: imbalance/order-block memory marks a likely institutional reaction zone until mitigation.";
      if (objectType === "event" && structureBreak) return "Why: BOS/CHOCH confirms a market-structure shift and changes control side.";
      if (objectType === "event" && liquidityEvent) return "Why: sweep/equal-high-low marks stop liquidity and possible engineered reversal.";
      if (objectType === "event") return "Why: SMC event is a structure clue, not an execution command by itself.";
      return "Why: SMC object marks liquidity, imbalance or market-structure memory.";
    }
    if (factCode === "smc_confirmation") {
      if (objectType === "level" && highSide) return "Bounce: reject/CHOCH below the high-side level before fading long pressure.";
      if (objectType === "level" && lowSide) return "Bounce: reclaim/spring above the low-side level before fading short pressure.";
      if (objectType === "level") return "Bounce: wait for confirmed rejection before fading the move.";
      if (objectType === "zone" && highSide) return "Reaction: supply/high-side zone favors bearish rejection only after sweep, fail or CHOCH.";
      if (objectType === "zone" && lowSide) return "Reaction: demand/low-side zone favors bullish reclaim only after spring, hold or BOS.";
      if (objectType === "zone") return "Reaction: trade only after confirmed rejection or reclaim at the zone.";
      if (objectType === "box" && lowSide) return "Reaction: bullish FVG/OB should hold as demand; look for reclaim, spring or continuation BOS.";
      if (objectType === "box" && highSide) return "Reaction: bearish FVG/OB should cap as supply; look for rejection, sweep or continuation BOS.";
      if (objectType === "box") return "Reaction: wait for price to respect or reject the SMC box before acting.";
      if (objectType === "event" && structureBreak) return "Action: prefer continuation after acceptance and retest, not the first structure-break spike.";
      if (objectType === "event" && liquidityEvent) return "Action: fade only after rejection/reclaim confirms the liquidity trap.";
      if (objectType === "event") return "Action: wait for BOS, CHOCH, reclaim or rejection to confirm intent.";
      return "Action: wait for SMC confirmation before acting.";
    }
    if (factCode === "smc_invalidation") {
      if (objectType === "level" && highSide) return "Breakout: bullish BOS plus acceptance above; wait for a support retest.";
      if (objectType === "level" && lowSide) return "Breakout: bearish BOS plus acceptance below; wait for a resistance retest.";
      if (objectType === "level") return "Breakout: require BOS, acceptance and retest before continuation.";
      if (objectType === "zone" && highSide) return "Invalidation: bullish acceptance above cancels the fade and can turn the zone into support.";
      if (objectType === "zone" && lowSide) return "Invalidation: bearish acceptance below cancels the bounce and can turn the zone into resistance.";
      if (objectType === "zone") return "Invalidation: acceptance through the zone changes the active structure side.";
      if (objectType === "box" && lowSide) return "Failure: bearish acceptance through the box invalidates the bullish reaction.";
      if (objectType === "box" && highSide) return "Failure: bullish acceptance through the box invalidates the bearish reaction.";
      if (objectType === "box") return "Failure: acceptance through the box removes its immediate reaction value.";
      if (objectType === "event" && structureBreak) return "Invalidation: a failed retest through the broken level converts the move into a trap.";
      if (objectType === "event" && liquidityEvent) return "Invalidation: acceptance beyond swept liquidity means continuation, not reversal.";
      if (objectType === "event") return "Invalidation: no confirmation means no standalone trade.";
      return "Invalidation: acceptance through the object changes the structure read.";
    }
    if (factCode === "smc_retained_filled_zone") return `Filled zone retained for audit · age ${Number(fact.age_bars) || 0} bars.`;
    if (factCode === "smc_trend_day_warning") return "Trend-day context opposes this fade.";
    if (factCode === "smc_zone_sources") return `Sources: ${(Array.isArray(fact.sources) ? fact.sources : []).map(domainCodeLabel).join(", ") || "-"}`;
    if (factCode === "smc_confluence") return `Confluence: ${(Array.isArray(fact.levels) ? fact.levels : []).map(domainCodeLabel).join(", ") || "-"}`;
    if (factCode === "smc_guide_state") return `Guide: ${domainCodeLabel(fact.state) || "-"}`;
    if (factCode === "smc_vix_state") return `VIX: ${domainCodeLabel(fact.state) || "-"}`;
    if (factCode === "smc_zone_state") return `Zone: ${domainCodeLabel(fact.state) || "unknown"}`;
  }
  if (factCode.startsWith("linda_")) {
    const side = String(fact.side || "").toLowerCase();
    const direction = String(fact.direction || "").toLowerCase();
    const level = Number(fact.level);
    if (factCode === "linda_turtle_sweep_rejected") {
      const boundary = side === "low" ? "low" : "high";
      const returnSide = boundary === "low" ? "above" : "below";
      return `Swept prior ${boundary}${Number.isFinite(level) ? ` ${level.toFixed(2)}` : ""}; closed back ${returnSide}.`;
    }
    if (factCode === "linda_turtle_break_failed_next_bar") {
      return `Prior ${side === "low" ? "low" : "high"} break${Number.isFinite(level) ? ` at ${level.toFixed(2)}` : ""} failed on the next bar.`;
    }
    if (factCode === "linda_eighty_twenty_range_rotation") {
      return `Prior bar rotated from ${domainCodeLabel(fact.open_zone).toLowerCase()} to ${domainCodeLabel(fact.close_zone).toLowerCase()}.`;
    }
    if (factCode === "linda_anti_oscillator_cross") {
      return `3-10 oscillator crossed ${direction || "with"} with ${domainCodeLabel(fact.trend).toLowerCase()} trend.`;
    }
    if (factCode === "linda_momentum_pinball_break") {
      const rsi = Number(fact.rsi_roc);
      return `RSI(3) of ROC ${Number.isFinite(rsi) ? rsi.toFixed(1) : "-"}; broke first-hour ${side === "low" ? "low" : "high"}${Number.isFinite(level) ? ` ${level.toFixed(2)}` : ""}.`;
    }
    if (factCode === "linda_hv_squeeze_break") {
      const ratio = Number(fact.stdev_ratio);
      return `${Number(fact.short_length) || "-"}/${Number(fact.long_length) || "-"} stdev ratio ${Number.isFinite(ratio) ? ratio.toFixed(2) : "-"}; range break ${direction || "-"}.`;
    }
    if (factCode === "linda_adx_gap_reversion") {
      const adx = Number(fact.adx);
      const gap = Number(fact.gap);
      return `ADX ${Number.isFinite(adx) ? adx.toFixed(1) : "-"}; ${String(fact.gap_direction || "").toLowerCase()} gap ${Number.isFinite(gap) ? gap.toFixed(2) : "-"}.`;
    }
  }
  const code = domainCodeLabel(fact.code);
  const typedDetails = [fact.content, fact.state, fact.detail_code, fact.direction]
    .map(domainCodeLabel)
    .filter((item, index, items) => item && item !== code && items.indexOf(item) === index);
  const detail = typedDetails.join(" · ");
  const level = Number(fact.level ?? fact.price);
  const parts = [code, detail && detail !== code ? detail : ""];
  if (Number.isFinite(level)) parts.push("@ " + level);
  return parts.filter(Boolean).join(": ");
}

function actionPhaseGlyph(phase) {
  return ACTION_PHASE_GLYPHS[String(phase || "").trim().toUpperCase()] || "[ ]";
}

function actionSourceLabel(cardOrSource) {
  const source = typeof cardOrSource === "object" && cardOrSource
    ? cardOrSource.source
    : cardOrSource;
  return ACTION_SOURCE_LABELS[source]
    || indicatorSpec(source)?.label
    || domainCodeLabel(source)
    || "Trade setup";
}

function actionSourceCode(cardOrSource) {
  const source = typeof cardOrSource === "object" && cardOrSource
    ? cardOrSource.source
    : cardOrSource;
  const manifestLabel = indicatorSpec(source)?.label;
  return ACTION_SOURCE_CODES[source]
    || (
      manifestLabel
        ? manifestLabel
          .split(/\s+/)
          .map(part => part.slice(0, 1))
          .join("")
          .slice(0, 4)
          .toUpperCase()
        : ""
    )
    || domainCodeLabel(source).replace(/[^A-Za-z0-9]/g, "").slice(0, 4).toUpperCase();
}

function actionDirectionMark(cardOrDirection) {
  const direction = typeof cardOrDirection === "object" && cardOrDirection
    ? cardOrDirection.direction
    : cardOrDirection;
  if (direction === "long") return "LONG";
  if (direction === "short") return "SHORT";
  return "FLAT";
}

function actionSetupCode(card) {
  return domainCodeLabel(card?.setup || card?.phase || "setup");
}

function actionCardCompactLabel(card) {
  if (!card || typeof card !== "object") return "";
  return [actionSourceCode(card), actionDirectionMark(card), actionSetupCode(card)]
    .filter(Boolean)
    .join(" ");
}

function actionCardDoNow(card) {
  if (!card || typeof card !== "object") return "";
  const phase = String(card.phase || "").trim().toUpperCase();
  const firstBlockFact = Array.isArray(card.blocking_facts)
    ? card.blocking_facts.find(fact => fact && typeof fact === "object" && !Array.isArray(fact) && typeof fact.code === "string" && fact.code)
    : null;
  const firstBlock = domainFactText(firstBlockFact);
  if (card.blocked || phase === "BLOCK") return firstBlock ? "Blocked: " + firstBlock : "Execution blocked";
  const direction = actionDirectionMark(card);
  const setup = actionSetupCode(card);
  const trigger = card.trigger_event && typeof card.trigger_event === "object" && !Array.isArray(card.trigger_event) && typeof card.trigger_event.code === "string" && card.trigger_event.code
    ? domainFactText(card.trigger_event)
    : "";
  if (["GO", "IN", "TRAIL", "TARGET_HIT", "STOP_HIT"].includes(phase)) {
    return [direction, setup, trigger].filter(Boolean).join(" | ");
  }
  if (["ARM", "CANDIDATE"].includes(phase)) return ["Prepare", direction, setup, trigger].filter(Boolean).join(" | ");
  return ["Watch", direction, setup, trigger].filter(Boolean).join(" | ");
}

function actionCardPresentation(value) {
  const card = value?.action_card && typeof value.action_card === "object" ? value.action_card : value;
  if (!card || typeof card !== "object") return card;
  const supportingFacts = Array.isArray(card.supporting_facts)
    ? card.supporting_facts.filter(fact => fact && typeof fact === "object" && !Array.isArray(fact) && typeof fact.code === "string" && fact.code)
    : [];
  const blockingFacts = Array.isArray(card.blocking_facts)
    ? card.blocking_facts.filter(fact => fact && typeof fact === "object" && !Array.isArray(fact) && typeof fact.code === "string" && fact.code)
    : [];
  const triggerEvent = card.trigger_event && typeof card.trigger_event === "object" && !Array.isArray(card.trigger_event) && typeof card.trigger_event.code === "string" && card.trigger_event.code
    ? card.trigger_event
    : null;
  const supporting = supportingFacts.map(domainFactText).filter(Boolean);
  const blocking = blockingFacts.map(domainFactText).filter(Boolean);
  const numbers = Object.entries(card.metrics || {})
    .filter(([, metric]) => metric !== null && metric !== undefined && typeof metric !== "object")
    .map(([key, metric]) => domainCodeLabel(key) + ": " + domainCodeLabel(metric));
  return {
    ...card,
    trigger_event: triggerEvent,
    supporting_facts: supportingFacts,
    blocking_facts: blockingFacts,
    phase_glyph: actionPhaseGlyph(card.phase),
    source_label: actionSourceLabel(card),
    source_code: actionSourceCode(card),
    direction_glyph: actionDirectionMark(card),
    compact_label: actionCardCompactLabel(card),
    do_now: actionCardDoNow(card),
    trigger: domainFactText(triggerEvent),
    pro: supporting,
    con: blocking,
    numbers,
  };
}

function indicatorFactsForDisplay(value) {
  if (!value || typeof value !== "object") return value;
  const evidence = value.evidence && typeof value.evidence === "object" && !Array.isArray(value.evidence)
    ? value.evidence
    : {};
  const risk = value.risk && typeof value.risk === "object" && !Array.isArray(value.risk) && typeof value.risk.code === "string" && value.risk.code ? value.risk : {};
  const quality = value.quality && typeof value.quality === "object" && !Array.isArray(value.quality) && typeof value.quality.code === "string" && value.quality.code ? value.quality : {};
  const triggerEvent = value.trigger_event && typeof value.trigger_event === "object" && !Array.isArray(value.trigger_event) && typeof value.trigger_event.code === "string" && value.trigger_event.code
    ? value.trigger_event
    : null;
  const supportingFacts = Array.isArray(evidence.supporting)
    ? evidence.supporting.filter(fact => fact && typeof fact === "object" && !Array.isArray(fact) && typeof fact.code === "string" && fact.code)
    : [];
  const opposingFacts = Array.isArray(evidence.opposing)
    ? evidence.opposing.filter(fact => fact && typeof fact === "object" && !Array.isArray(fact) && typeof fact.code === "string" && fact.code)
    : [];
  const contextFacts = Array.isArray(evidence.context)
    ? evidence.context.filter(fact => fact && typeof fact === "object" && !Array.isArray(fact) && typeof fact.code === "string" && fact.code)
    : [];
  const riskBlockFacts = Array.isArray(risk.blocks)
    ? risk.blocks.filter(fact => fact && typeof fact === "object" && !Array.isArray(fact) && typeof fact.code === "string" && fact.code)
    : [];
  const factGroups = (Array.isArray(value.fact_groups) ? value.fact_groups : [])
    .filter(group => group && typeof group === "object" && !Array.isArray(group) && typeof group.kind === "string" && group.kind && Array.isArray(group.items))
    .map(group => ({
      kind: group.kind,
      items: group.items.filter(fact => fact && typeof fact === "object" && !Array.isArray(fact) && typeof fact.code === "string" && fact.code),
    }));
  const metrics = value.metrics && typeof value.metrics === "object" && !Array.isArray(value.metrics) ? value.metrics : {};
  const supporting = supportingFacts.map(domainFactText).filter(Boolean);
  const opposing = opposingFacts.map(domainFactText).filter(Boolean);
  const context = contextFacts.map(domainFactText).filter(Boolean);
  const riskText = domainFactText(risk);
  const directionValue = String(value.direction || value?.signal?.direction || value?.trade_plan?.direction || value?.plan?.direction || "").trim().toUpperCase();
  const direction = ["LONG", "SHORT"].includes(directionValue) ? directionValue : "";
  const blockedSides = new Set(riskBlockFacts.map(fact => String(fact.code || "").trim().toUpperCase()));
  const riskSupportsDirection = (direction === "LONG" && blockedSides.has("SHORT"))
    || (direction === "SHORT" && blockedSides.has("LONG"));
  const riskBlocksDirection = !direction
    ? blockedSides.size > 0
    : blockedSides.has(direction);
  const metricLines = Object.entries(metrics)
    .filter(([, metric]) => metric !== null && metric !== undefined && typeof metric !== "object")
    .map(([key, metric]) => domainCodeLabel(key) + ": " + domainCodeLabel(metric));
  const tables = factGroups.map((group) => ({
      title: domainCodeLabel(group.kind),
      headers: [],
      rows: group.items.map(fact => [domainFactText(fact)]),
    }));
  return {
    ...value,
    trigger_event: triggerEvent,
    evidence: {
      supporting: supportingFacts,
      opposing: opposingFacts,
      context: contextFacts,
    },
    risk: risk.code ? { ...risk, blocks: riskBlockFacts } : null,
    quality: quality.code ? quality : null,
    fact_groups: factGroups,
    metrics,
    scenario: domainCodeLabel(value.scenario),
    setup: domainCodeLabel(value.setup),
    trigger_label: domainFactText(triggerEvent),
    pro: riskSupportsDirection && riskText ? [...supporting, riskText] : supporting,
    con: riskBlocksDirection && riskText ? [...opposing, riskText] : opposing,
    risk_text: riskText,
    quality_text: domainFactText(quality),
    context_lines: context,
    notes: [],
    numbers: metricLines,
    tables,
  };
}

    function actionCardFromItem(item) {
  item = actionCardPresentation(item);
      if (!item || typeof item !== "object") return null;
      if (item.action_card && typeof item.action_card === "object") return item.action_card;
      if (item.command && typeof item.command === "object") return item.command;
      if (item.phase && item.phase_glyph && item.do_now) return item;
      return null;
    }

    function actionCardTooltip(card) {
  card = actionCardPresentation(card);
      if (!card || typeof card !== "object") return "";
      const titleSetup = card.setup_code && card.setup_code !== "-" ? ` · ${card.setup_code}` : "";
      const phase = String(card.phase || "").trim().toUpperCase();
      const direction = String(card.direction || "flat").trim().toUpperCase();
      const setup = String(card.setup || "-").trim();
      const doNow = String(card.do_now || "-").trim();
      const title = `${String(card.source_label || card.source || "SIGNAL").toUpperCase()}${titleSetup} — ${card.phase_glyph || ""} ${phase} ${direction}`.trim();
      const pro = usableServiceLines(card.pro);
      const con = usableServiceLines(card.con);
      const metricLines = tooltipMetricLines(card.metrics);
      const numberLines = metricLines.length
        ? metricLines
        : usableServiceLines(card.numbers).map(value => tooltipLine(value, "meta"));
      const lines = [
        tooltipLine(title, "title", tooltipTokenRoles([
          [phase, tooltipActionTokenRole(phase, phase)],
          [direction, tooltipDirectionTokenRole(direction)],
          [setup, "type"],
        ])),
        tooltipLabeledLine("DO", doNow, "meta", tooltipActionTokenRole(doNow, phase)),
        tooltipLine(`DIR: ${direction} / ${setup}`, "meta", tooltipTokenRoles([
          ["DIR", "type"],
          [direction, tooltipDirectionTokenRole(direction)],
          [setup, "type"],
        ])),
      ];
      if (card.trigger) lines.push(tooltipLabeledLine("TRIGGER", card.trigger, "entry", "type"));
      if (Number.isFinite(Number(card.entry))) lines.push(tooltipPriceLine("ENTRY", card.entry, "entry"));
      if (Number.isFinite(Number(card.stop))) lines.push(tooltipPriceLine(`${STOP_MARK} STOP`, card.stop, "stop"));
      if (Number.isFinite(Number(card.target))) lines.push(tooltipPriceLine("TARGET", card.target, "target"));
      lines.push(tooltipLine("--------------------", "separator"));
      pro.forEach(value => lines.push(tooltipLabeledLine("FOR", value, "pro")));
      con.forEach(value => lines.push(tooltipLabeledLine("AGAINST", value, "con")));
      if (numberLines.length) lines.push(tooltipLine("NUMBERS", "plan"), ...numberLines);
      return { lines: lines.filter(line => tooltipLineText(line).trim()) };
    }

    function finiteField(item, keys) {
      for (const key of keys) {
        const value = Number(item?.[key]);
        if (Number.isFinite(value)) return value;
      }
      return null;
    }

    function structuredTradePlan(item) {
      const candidates = [
        item?.plan,
        item?.trade_plan,
        item?.signal?.plan,
        item?.signal?.trade_plan,
        item?.latest?.signal?.trade_plan,
        item?.lifecycle?.plan,
        item?.lifecycle?.trade_plan,
        item?.latest?.lifecycle?.plan,
        item?.latest?.lifecycle?.trade_plan,
        item?.table?.trade_plan,
      ];
      for (const plan of candidates) {
        if (plan && typeof plan === "object") return plan;
      }
      return null;
    }

    function finitePlanField(item, keys) {
      const plan = structuredTradePlan(item);
      if (!plan) return null;
      for (const key of keys) {
        const value = Number(plan?.[key]);
        if (Number.isFinite(value)) return value;
      }
      return null;
    }

    function typedTextValue(value) {
      if (Array.isArray(value)) return value.map(typedTextValue).filter(Boolean).join(" | ");
      if (value && typeof value === "object") return domainFactText(value).replace(/\s+/g, " ").trim();
      return String(value ?? "").replace(/\s+/g, " ").trim();
    }

    function firstTypedField(sources, keys) {
      for (const source of sources) {
        if (!source || typeof source !== "object") continue;
        for (const key of keys) {
          const value = typedTextValue(source?.[key]);
          if (value && !/^[-+]?\d*\.?\d+$/.test(value)) return value;
        }
      }
      return "";
    }

    function tooltipLine(text, role = "", tokenRoles = null) {
      const line = { text: String(text ?? ""), role: String(role || "") };
      if (tokenRoles && typeof tokenRoles === "object") line.token_roles = { ...tokenRoles };
      return line;
    }

    function tooltipTokenRoles(entries = []) {
      const roles = {};
      for (const entry of entries) {
        if (!Array.isArray(entry) || entry.length < 2) continue;
        const token = String(entry[0] ?? "").trim();
        const role = String(entry[1] || "").trim().toLowerCase();
        if (token && role) roles[token.toUpperCase()] = role;
      }
      return roles;
    }

    function tooltipTypedTokenRole(value) {
      const role = String(value || "").trim().toLowerCase();
      const aliases = {
        call: "call",
        put: "put",
        type: "type",
        price: "price",
        level: "price",
        strength: "strength",
        score: "strength",
        confidence: "strength",
        percent: "strength",
        percentage: "strength",
        "percentile-low": "percentile-low",
        "percentile-mid": "percentile-mid",
        "percentile-high": "percentile-high",
        positive: "positive",
        success: "positive",
        long: "positive",
        up: "positive",
        clear: "positive",
        negative: "negative",
        danger: "negative",
        error: "negative",
        short: "negative",
        down: "negative",
        blocked: "negative",
        warning: "warning",
        warn: "warning",
        pending: "warning",
        provisional: "warning",
        neutral: "muted",
        info: "muted",
      };
      return aliases[role] || "";
    }

    function tooltipDirectionTokenRole(value) {
      const direction = normalizedTypedDirection(value);
      if (direction === "LONG") return "positive";
      if (direction === "SHORT") return "negative";
      return "type";
    }

    function tooltipActionTokenRole(action, phase = "") {
      const phaseCode = String(phase || "").trim().toUpperCase().replace(/\s+/g, "_");
      if (["STOP_HIT", "TRAIL_STOP_HIT", "BLOCK"].includes(phaseCode)) return "negative";
      if (["GO", "IN", "ENTRY_FILL", "TRAIL", "TARGET_HIT"].includes(phaseCode)) return "positive";
      const actionCode = String(action || "").trim().toUpperCase().replace(/[_-]+/g, " ").replace(/\s+/g, " ");
      if (["STOP HIT", "TRAIL STOP HIT", "BLOCK", "BLOCKED", "REJECTED", "ERROR", "RISK WAIT", "DATA GAP"].includes(actionCode)) return "negative";
      if (["GO", "IN", "ENTRY", "ENTER", "ENTRY FILL", "ENTRY FILLED", "TRAIL", "FOLLOW", "TARGET", "TARGET HIT", "TAKE", "TP"].includes(actionCode)) return "positive";
      return "type";
    }

    function tooltipLabeledLine(label, value, role = "meta", valueTokenRole = "", glyphDirection = "") {
      const labelText = String(label || "").trim();
      const valueText = String(value ?? "").trim();
      const tokenEntries = [[labelText, "type"]];
      const normalizedGlyphDirection = normalizedTypedDirection(glyphDirection);
      if (normalizedGlyphDirection && valueText.includes(GO_MARK)) {
        const valueWithoutGoMark = valueText.replaceAll(GO_MARK, "").trim();
        tokenEntries.push([
          GO_MARK,
          normalizedGlyphDirection === "SHORT" ? "go-short" : "go-long",
        ]);
        if (valueWithoutGoMark) tokenEntries.push([valueWithoutGoMark, valueTokenRole]);
      } else {
        tokenEntries.push([valueText, valueTokenRole]);
      }
      return tooltipLine(
        labelText ? `${labelText}: ${valueText}` : valueText,
        role,
        tooltipTokenRoles(tokenEntries),
      );
    }

    function tooltipPriceLine(label, value, role = "entry") {
      const hasPrice = value !== null && value !== undefined && value !== "" && Number.isFinite(Number(value));
      const price = hasPrice ? formatPlanNumber(value) : "-";
      return tooltipLine(
        `${String(label || "").trim()}: ${price}`,
        role,
        tooltipTokenRoles([[price, hasPrice ? "price" : ""]]),
      );
    }

    function tooltipLineText(line) {
      if (line && typeof line === "object") return String(line.text ?? line.label ?? line.value ?? "");
      return String(line || "");
    }

    function tooltipLineRole(line) {
      if (!line || typeof line !== "object") return "";
      return String(line.role || line.kind || "").trim().toLowerCase();
    }

    function tooltipLineTokenRole(line, token) {
      if (!line || typeof line !== "object" || !line.token_roles || typeof line.token_roles !== "object") return "";
      const key = String(token || "").trim().toUpperCase();
      return String(line.token_roles[key] || line.token_roles[String(token || "").trim()] || "").trim().toLowerCase();
    }

    function tooltipRegexEscape(value) {
      return String(value || "").replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    }

    function tooltipLineKey(value) {
      return tooltipLineText(value)
        .replace(/[🚀💣🔎🧭☠🎯⚡🛡⚠]/g, "")
        .replace(/\s+/g, " ")
        .trim()
        .toLowerCase();
    }

    function tooltipContextValueLines(value) {
      if (value === null || value === undefined) return [];
      if (Array.isArray(value)) return value.flatMap(item => tooltipContextValueLines(item));
      if (typeof value === "object") {
        return [
          value.commentary,
          value.comment,
          value.context,
          value.context_text,
          value.notes,
          value.description,
        ].flatMap(item => tooltipContextValueLines(item));
      }
      return tooltipTextValue(value).split(/\n+/).map(part => part.trim()).filter(Boolean);
    }

    function tooltipMetricLabel(key) {
      const text = String(key || "").replace(/_/g, " ").replace(/\s+/g, " ").trim();
      if (!text) return "";
      return text.length <= 5 ? text.toUpperCase() : text.replace(/\b\w/g, char => char.toUpperCase());
    }

    function tooltipMetricValueText(value) {
      if (value === null || value === undefined || value === "") return "-";
      if (typeof value === "object") {
        const rawValue = value.value ?? value.text ?? value.label;
        const numeric = Number(rawValue);
        const rawDigits = Number(value.digits ?? value.precision);
        const digits = Number.isFinite(rawDigits) ? Math.max(0, Math.min(8, Math.trunc(rawDigits))) : 2;
        const body = Number.isFinite(numeric) ? numeric.toFixed(digits) : tooltipTextValue(rawValue);
        return `${value.prefix ?? ""}${body || "-"}${value.suffix ?? value.unit ?? ""}`;
      }
      const numeric = Number(value);
      if (Number.isFinite(numeric) && typeof value !== "boolean") return Math.abs(numeric) >= 100 ? numeric.toFixed(0) : numeric.toFixed(2);
      return tooltipTextValue(value);
    }

    function tooltipMetricTokenRole(value) {
      if (value && typeof value === "object" && !Array.isArray(value)) {
        const explicit = tooltipTypedTokenRole(value.token_role || value.kind || value.role || value.tone);
        if (explicit) return explicit;
        return "";
      }
      return "";
    }

    function tooltipMetricLines(value) {
      if (!value || typeof value !== "object" || Array.isArray(value)) return [];
      return Object.entries(value)
        .filter(([, metric]) => {
          if (metric && typeof metric === "object" && !Array.isArray(metric)) {
            const rawValue = metric.value ?? metric.text ?? metric.label;
            return rawValue !== null && rawValue !== undefined && rawValue !== "";
          }
          return metric !== null && metric !== undefined && metric !== "";
        })
        .map(([key, metric]) => {
          const label = tooltipMetricLabel(key);
          if (!label) return null;
          const metricText = tooltipMetricValueText(metric);
          return tooltipLabeledLine(label, metricText, "meta", tooltipMetricTokenRole(metric));
        })
        .filter(Boolean);
    }

    function typedTooltipContextLines(item, excludeLines = []) {
  item = indicatorFactsForDisplay(item);
      const signal = item?.signal && typeof item.signal === "object" ? item.signal : {};
      const plan = structuredTradePlan(item) || {};
      const table = item?.table && typeof item.table === "object" ? item.table : {};
      const rawValues = [
        item?.commentary,
        item?.comment,
        item?.comments,
        item?.context,
        item?.context_text,
        item?.context_lines,
        item?.notes,
        item?.detail,
        item?.details,
        item?.description,
        signal?.commentary,
        signal?.context,
        signal?.notes,
        plan?.commentary,
        plan?.context,
        plan?.notes,
        table?.commentary,
        table?.context,
        table?.notes,
      ];
      const metricValues = [
        item?.metrics,
        signal?.metrics,
        plan?.metrics,
        table?.metrics,
      ];
      const lines = [];
      const seen = new Set(excludeLines.map(tooltipLineKey).filter(Boolean));
      for (const raw of rawValues) {
        for (const line of tooltipContextValueLines(raw)) {
          const key = tooltipLineKey(line);
          if (key && !seen.has(key)) {
            lines.push(tooltipLine(line, "meta"));
            seen.add(key);
          }
        }
      }
      for (const raw of metricValues) {
        for (const line of tooltipMetricLines(raw)) {
          const key = tooltipLineKey(line);
          if (key && !seen.has(key)) {
            lines.push(line);
            seen.add(key);
          }
        }
      }
      return lines.slice(0, 8);
    }

    function tooltipTableValues(value) {
      if (!value) return [];
      if (Array.isArray(value)) return value.flatMap(item => tooltipTableValues(item));
      if (typeof value === "object" && Array.isArray(value.columns) && Array.isArray(value.rows)) return [value];
      return [];
    }

    function typedTooltipTables(item) {
  item = indicatorFactsForDisplay(item);
      const signal = item?.signal && typeof item.signal === "object" ? item.signal : {};
      const table = item?.table && typeof item.table === "object" ? item.table : {};
      return [
        item?.tooltip_tables,
        signal?.tooltip_tables,
        table?.tooltip_tables,
      ].flatMap(value => tooltipTableValues(value));
    }

    function tooltipSectionLine(value) {
      if (value === null || value === undefined) return null;
      if (typeof value !== "object" || Array.isArray(value)) {
        const text = tooltipTextValue(value).trim();
        return text ? tooltipLine(text, "meta") : null;
      }
      const text = tooltipLineText(value).replace(/\s+/g, " ").trim();
      if (!text) return null;
      const allowedRoles = new Set([
        "title", "plan", "entry", "stop", "target", "meta", "positive", "negative",
        "role", "flow", "pro", "con", "more", "separator", "strength", "risk",
      ]);
      const requestedRole = String(value.role || value.kind || "meta").trim().toLowerCase();
      const role = allowedRoles.has(requestedRole) ? requestedRole : "meta";
      const rawTokenRoles = value.token_roles && typeof value.token_roles === "object"
        ? value.token_roles
        : {};
      const tokenRoles = {};
      Object.entries(rawTokenRoles).forEach(([token, tokenRole]) => {
        const key = String(token || "").trim();
        const normalizedRole = tooltipTypedTokenRole(tokenRole);
        if (key && normalizedRole) tokenRoles[key] = normalizedRole;
      });
      return tooltipLine(text, role, Object.keys(tokenRoles).length ? tokenRoles : null);
    }

    function tooltipSectionValues(value) {
      if (!value) return [];
      if (Array.isArray(value)) return value.flatMap(item => tooltipSectionValues(item));
      if (typeof value !== "object") return [];
      const title = tooltipTextValue(value.title || value.label || "INFO").replace(/\s+/g, " ").trim();
      const rawLines = Array.isArray(value.lines) ? value.lines : [];
      const lines = rawLines.map(tooltipSectionLine).filter(Boolean).slice(0, 12);
      return lines.length ? [{ title: title || "INFO", lines }] : [];
    }

    function typedTooltipSections(item) {
  item = indicatorFactsForDisplay(item);
      const direct = tooltipSectionValues(item?.tooltip_sections);
      if (direct.length) return direct.slice(0, 4);
      const signal = item?.signal && typeof item.signal === "object" ? item.signal : {};
      const table = item?.table && typeof item.table === "object" ? item.table : {};
      return [signal?.tooltip_sections, table?.tooltip_sections]
        .flatMap(value => tooltipSectionValues(value))
        .slice(0, 4);
    }

    function normalizedTypedDirection(value) {
      const text = String(value || "").trim().toLowerCase();
      if (text === "long") return "LONG";
      if (text === "short") return "SHORT";
      return "";
    }

    function directionalGoMarkClass(direction) {
      return normalizedTypedDirection(direction) === "SHORT"
        ? "directional-go-mark short"
        : "directional-go-mark";
    }

    function directionalGoMarkHtml(direction) {
      return `<span class="${directionalGoMarkClass(direction)}" aria-hidden="true">${GO_MARK}</span>`;
    }

    function normalizedOverlaySignal(item) {
      if (!item || typeof item !== "object") {
        return { direction: "", role: "", glyph: "", glyph_kind: "", trade_plan: null };
      }
      const signal = item?.signal && typeof item.signal === "object" ? item.signal : {};
      const lifecycle = item?.lifecycle && typeof item.lifecycle === "object" ? item.lifecycle : {};
      const latestLifecycle = item?.latest?.lifecycle && typeof item.latest.lifecycle === "object" ? item.latest.lifecycle : {};
      const plan = structuredTradePlan(item);
      const direction = normalizedTypedDirection(item?.direction)
        || normalizedTypedDirection(signal?.direction)
        || normalizedTypedDirection(plan?.direction)
        || normalizedTypedDirection(signal?.plan?.direction)
        || normalizedTypedDirection(signal?.trade_plan?.direction)
        || normalizedTypedDirection(lifecycle?.direction)
        || normalizedTypedDirection(latestLifecycle?.direction);
      const role = String(item?.role || item?.kind || signal?.role || signal?.kind || lifecycle?.role || "").trim().toLowerCase();
      const glyph = String(item?.glyph || signal?.glyph || plan?.glyph || "").trim();
      const glyphKind = String(item?.glyph_kind || signal?.glyph_kind || plan?.glyph_kind || "").trim().toLowerCase();
      return {
        direction,
        role,
        glyph,
        glyph_kind: glyphKind,
        trade_plan: plan,
      };
    }

    function planRolePrice(item, roleName) {
      const role = String(normalizedOverlaySignal(item).role || "").toLowerCase();
      if (role !== roleName) return null;
      return finiteField(item, ["price", "y1", "level"]);
    }

    function standardPlanEntry(item) {
      return finitePlanField(item, ["entry", "trigger"]) ?? finiteField(item, ["entry", "entry_price", "trigger", "price"]) ?? planRolePrice(item, "entry");
    }

    function standardPlanStop(item) {
      return finitePlanField(item, ["stop", "invalidation", "invalid"]) ?? finiteField(item, ["stop", "stop_price", "invalidation", "invalid"]) ?? planRolePrice(item, "stop");
    }

    function standardPlanTarget(item) {
      return finitePlanField(item, ["target"]) ?? finiteField(item, ["target", "target_price"]) ?? planRolePrice(item, "target");
    }

    function standardPlanDisplayPrice(item) {
      const role = String(normalizedOverlaySignal(item).role || "").toLowerCase();
      if (role === "target") return planRolePrice(item, "target") ?? standardPlanTarget(item);
      if (role === "stop") return planRolePrice(item, "stop") ?? standardPlanStop(item);
      if (role === "trail") return planRolePrice(item, "trail") ?? finitePlanField(item, ["trail"]) ?? standardPlanStop(item);
      return standardPlanEntry(item);
    }

    function signalDirectionLabel(item) {
      const normalized = normalizedOverlaySignal(item);
      return normalized.direction || "";
    }

    function signalTradeDirectionText(item) {
      const normalized = normalizedOverlaySignal(item);
      if (normalized.direction) return normalized.direction;
      return "-";
    }

    function standardSignalType(item, sourceOverride = "") {
      const source = normalizedSignalSource(sourceOverride || item?.source || item?.indicator || item?.table_id || "", "SIGNAL");
      const action = String(item?.action || "").trim().toUpperCase();
      const direction = signalDirectionLabel(item);
      if (action && ["GO", "WAIT", "WATCH", "CANDIDATE", "ARM", "BLOCK"].includes(action) && direction) return `${source} ${direction}`;
      return direction ? `${source} ${direction}` : source;
    }

    function signalNoteTitle(item, sourceOverride = "") {
      const setup = signalScenarioText(item);
      const direction = signalDirectionLabel(item);
      const source = normalizedSignalSource(sourceOverride || item?.source || item?.indicator || "", "SIGNAL");
      if (setup && setup !== "-") {
        const cleanSetup = setup.replace(/^SCENARIO[_\s-]*/i, "").replace(/_/g, " ").trim();
        if (cleanSetup && !/^(E|S|T|TRAIL)$/i.test(cleanSetup)) return `${source} ${cleanSetup}`.toUpperCase();
      }
      return standardSignalType(item, sourceOverride).replace(/[\[\]]/g, "").toUpperCase();
    }

    function standardTriggerText(item) {
      const directKeys = ["trigger_label", "trigger_event", "trigger_name", "event", "state"];
      for (const key of directKeys) {
        const value = typedTextValue(item?.[key]);
        if (value && !/^[-+]?\d*\.?\d+$/.test(value)) return value;
      }
      const plan = structuredTradePlan(item);
      const planReason = typedTextValue(plan?.reason || plan?.code);
      return planReason || "-";
    }

    function formatPlanNumber(value) {
      const number = Number(value);
      return Number.isFinite(number) ? fmt(number) : "-";
    }

    function positivePlanNumber(value) {
      const number = Number(value);
      return Number.isFinite(number) && Math.abs(number) > 0.0000001;
    }

    function actionablePlanGeometry(item) {
      const entry = standardPlanEntry(item);
      const stop = standardPlanStop(item);
      const target = standardPlanTarget(item);
      const direction = signalDirectionLabel(item);
      const action = signalActionText(item).toUpperCase();
      const actionable = Boolean(direction) && !["", "-", "WAIT", "OFF", "DISABLED", "NO SIGNAL"].includes(action);
      if (!actionable || (!positivePlanNumber(stop) && !positivePlanNumber(target))) return null;
      if (positivePlanNumber(entry) && positivePlanNumber(stop) && positivePlanNumber(target)) {
        const coherentLong = direction === "LONG" && Number(stop) < Number(entry) && Number(entry) < Number(target);
        const coherentShort = direction === "SHORT" && Number(target) < Number(entry) && Number(entry) < Number(stop);
        const role = String(item?.role || item?.kind || "").toLowerCase();
        const managedPlan = role === "trail" || positivePlanNumber(structuredTradePlan(item)?.trail);
        const managedLong = managedPlan && direction === "LONG" && Number(entry) < Number(target) && Number(stop) < Number(target);
        const managedShort = managedPlan && direction === "SHORT" && Number(target) < Number(entry) && Number(target) < Number(stop);
        if (!coherentLong && !coherentShort && !managedLong && !managedShort) return null;
      }
      return { entry, stop, target };
    }

    function coherentDecisionPlan(decision) {
      const direction = String(decision?.direction || "").trim().toUpperCase();
      const entry = Number(decision?.trigger ?? decision?.entry);
      const stop = Number(decision?.stop ?? decision?.invalidation);
      const target = Number(decision?.target);
      if (![entry, stop, target].every(Number.isFinite)) return null;
      if (direction === "LONG" && stop < entry && entry < target) return { trigger: entry, stop, target };
      if (direction === "SHORT" && target < entry && entry < stop) return { trigger: entry, stop, target };
      return null;
    }

    function tooltipPlanLines(item, includePrice = true) {
      const plan = actionablePlanGeometry(item);
      if (!plan) return [];
      const role = String(normalizedOverlaySignal(item).role || "").toLowerCase();
      const lines = [];
      const displayPrice = standardPlanDisplayPrice(item);
      const showPrice = includePrice
        && positivePlanNumber(displayPrice)
        && !["target", "stop"].includes(role);
      if (showPrice) lines.push(tooltipPriceLine("PRICE", displayPrice, "entry"));
      if (positivePlanNumber(plan.stop)) lines.push(tooltipPriceLine(`${STOP_MARK} INVALID/SL`, plan.stop, "stop"));
      if (positivePlanNumber(plan.target)) lines.push(tooltipPriceLine("TARGET", plan.target, "target"));
      return lines;
    }

    function isSignalLikeItem(item) {
      if (!item || typeof item !== "object") return false;
      if (structuredTradePlan(item)) return true;
      if (["entry", "stop", "target", "trigger", "entry_price", "stop_price", "target_price"].some(key => item[key] !== undefined && item[key] !== null)) return true;
      if (["entry", "stop", "target", "track"].includes(String(item.role || "").toLowerCase())) return true;
      return Boolean(item?.action || item?.scenario || item?.setup || item?.event || item?.trigger_label || item?.trigger_event || item?.signal);
    }

    function signalScenarioText(item) {
      const signal = item?.signal && typeof item.signal === "object" ? item.signal : {};
      const plan = structuredTradePlan(item) || {};
      const direct = firstTypedField([item, signal, plan], ["scenario", "setup", "setup_code", "event"]);
      if (direct && !/^(E|S|T|TRAIL)$/i.test(direct) && !/^[-+]?\d*\.?\d+$/.test(direct)) return direct;
      return "-";
    }

    function signalActionText(item) {
      const plan = structuredTradePlan(item);
      const signal = item?.signal && typeof item.signal === "object" ? item.signal : {};
      const action = String(item?.action || signal?.action || plan?.action || "").replace(/\s+/g, " ").trim();
      return action || "-";
    }

    function signalDirectionText(item) {
      const direction = signalDirectionLabel(item) || "-";
      const scenario = signalScenarioText(item);
      if (isPlanRoleItem(item) && isPlanRoleLabel(scenario)) return direction;
      return scenario && scenario !== "-" ? `${direction} / ${scenario}` : direction;
    }

    function signalNumberLines(item) {
  item = indicatorFactsForDisplay(item);
      const signal = item?.signal && typeof item.signal === "object" ? item.signal : {};
      const plan = structuredTradePlan(item) || {};
      const metricSources = [item?.metrics, signal?.metrics, plan?.metrics]
        .filter(value => value && typeof value === "object" && !Array.isArray(value));
      const metricKeys = new Set(metricSources.flatMap(value => Object.keys(value).map(key => String(key).toLowerCase())));
      const lines = [];
      const seen = new Set();
      const append = line => {
        const key = tooltipLineKey(line);
        if (!key || seen.has(key)) return;
        lines.push(line);
        seen.add(key);
      };
      metricSources.flatMap(tooltipMetricLines).forEach(append);
      const score = Number(item?.score ?? plan?.score);
      const rvol = Number(item?.rvol ?? item?.option_rvol);
      const atr = Number(item?.atr ?? item?.move_atr);
      if (!metricKeys.has("score") && Number.isFinite(score) && score > 0) {
        append(tooltipLabeledLine("SCORE", String(Math.round(score)), "meta", "strength"));
      }
      if (!metricKeys.has("rvol") && Number.isFinite(rvol) && rvol > 0) {
        append(tooltipLabeledLine("RVOL", rvol.toFixed(2), "meta", "strength"));
      }
      if (!metricKeys.has("atr") && Number.isFinite(atr) && atr > 0) {
        append(tooltipLabeledLine("ATR", atr.toFixed(2), "meta", "strength"));
      }
      const quality = firstTypedField([item, signal, plan], ["quality_text", "quality_label", "quality_note"]);
      const risk = firstTypedField([item, signal, plan], ["risk", "risk_text", "risk_note", "distance"]);
      const bar = firstTypedField([item, signal, plan], ["bar", "bar_text", "candle", "candle_text"]);
      const regime = firstTypedField([item, signal, plan], ["regime", "regime_text"]);
      if (quality) append(tooltipLabeledLine("QUALITY", quality, "meta", tooltipTypedTokenRole(item?.quality?.tone) || "type"));
      if (risk) append(tooltipLabeledLine("RISK", risk, "meta", tooltipTypedTokenRole(item?.risk?.tone) || "negative"));
      if (bar) append(tooltipLabeledLine("BAR", bar, "meta", "type"));
      if (regime) append(tooltipLabeledLine("REGIME", regime, "meta", "type"));
      return lines.slice(0, 10);
    }

    function usableServiceText(value) {
      const text = String(value || "").replace(/\s+/g, " ").trim();
      if (!text || text === "-") return "";
      return text;
    }

    function usableServiceLines(value) {
      return Array.from(new Set(
        tooltipContextValueLines(value)
          .map(usableServiceText)
          .filter(Boolean),
      ));
    }

    function normalizedRiskBlocks(item) {
  item = indicatorFactsForDisplay(item);
      const blocks = Array.isArray(item?.risk?.blocks) ? item.risk.blocks : [];
      return Array.from(new Set(blocks
        .map(fact => String(fact?.code || "").trim().toLowerCase())
        .filter(side => side === "long" || side === "short")));
    }

    function riskBlocksOppositeDirection(item, directionText) {
      const direction = String(directionText || "").trim().toUpperCase();
      if (!direction) return false;
      const blocked = normalizedRiskBlocks(item);
      if (direction === "LONG") return blocked.includes("short");
      if (direction === "SHORT") return blocked.includes("long");
      return false;
    }

    function signalProLines(item) {
  item = indicatorFactsForDisplay(item);
      const plan = structuredTradePlan(item);
      const direct = usableServiceLines(item?.pro || item?.for);
      if (direct.length) return direct;
      const signal = item?.signal && typeof item.signal === "object" ? item.signal : {};
      const risk = firstTypedField([item, signal, plan], ["risk", "risk_text", "risk_note"]);
      const direction = signalDirectionLabel(item);
      if (risk && riskBlocksOppositeDirection(item, direction)) return usableServiceLines(risk);
      const support = firstTypedField([item, signal, plan], ["pro_text", "support", "support_text", "confirmation", "edge"]);
      return usableServiceLines(support);
    }

    function signalConLines(item) {
  item = indicatorFactsForDisplay(item);
      const plan = structuredTradePlan(item);
      const direct = usableServiceLines(item?.con || item?.against);
      if (direct.length) return direct;
      const direction = signalDirectionLabel(item);
      const signal = item?.signal && typeof item.signal === "object" ? item.signal : {};
      const risk = firstTypedField([plan, item, signal], ["blocked_reason", "risk_first", "scenario_lock", "invalid", "risk", "risk_text", "risk_note"]);
      if (risk) {
        if (riskBlocksOppositeDirection(item, direction)) return [];
        return usableServiceLines(risk);
      }
      const action = signalActionText(item);
      const actionCode = String(action || "").trim().toUpperCase().replace(/[_-]+/g, " ").replace(/\s+/g, " ");
      if (["WAIT", "WATCH", "RISK WAIT", "BLOCK", "BLOCKED", "FILTER", "FILTERED", "HOLD FILTERED"].includes(actionCode)) {
        return usableServiceLines(action);
      }
      return [];
    }

    function signalWaitingText(item) {
  item = indicatorFactsForDisplay(item);
      const direct = firstTypedField([item], ["waiting", "wait", "trigger_label", "trigger_event"]);
      if (direct) return direct;
      const trigger = standardTriggerText(item);
      if (trigger && trigger !== "-") return trigger;
      return "-";
    }

    function readableSignalActionText(action) {
      const text = normalizeCompactKey(action);
      if (text === "ENTRY FILLED") return "В позиции; следуем к цели";
      if (text === "TARGET HIT") return `${TAKE_MARK} Тейк`;
      if (text === "STOP HIT") return `${STOP_MARK} Стоп`;
      if (text === "TRAIL STOP HIT") return `${STOP_MARK} Трейлинг стоп`;
      if (continuationIntentText(text)) return "⚡ Продолжение";
      if (goIntentText(text)) return `${GO_MARK} Вход`;
      if (readyIntentText(text)) return `${READY_MARK} Готовность`;
      if (trailIntentText(text)) return `${TRAIL_MARK} Следовать`;
      if (takeIntentText(text)) return `${TAKE_MARK} Тейк`;
      if (text === "WATCH") return `${WATCH_MARK} Наблюдение`;
      if (text === "WAIT ENTRY") return `${CANDIDATE_MARK} Ждать вход`;
      if (waitIntentText(text)) return `${WAIT_MARK} Ждать`;
      if (text === "RISK WAIT") return "🛡 Риск-пауза";
      if (text === "DATA GAP") return "⚠ Нет данных";
      if (text === "WAIT") return "Ждать";
      if (text === "-") return "Недоступно";
      return String(action || "-");
    }

    function serviceTooltipLines(item) {
      const out = [];
      const numberLines = signalNumberLines(item);
      const pro = signalProLines(item);
      const con = signalConLines(item);
      const wait = usableServiceText(signalWaitingText(item));
      out.push(...numberLines.slice(0, 3));
      pro.forEach(value => out.push(tooltipLine(`+ ${value}`, "pro")));
      con.forEach(value => out.push(tooltipLine(`- ${value}`, "con")));
      if (wait) out.push(tooltipLabeledLine("Ждем", wait, "meta", "type"));
      return out.slice(0, 8);
    }

    function lifecycleExecutionTooltip(item, role = "") {
      const plan = structuredTradePlan(item) || {};
      const title = planLineTitle(item, overlaySignalSource(item));
      const tradeDirection = signalTradeDirectionText(item);
      const scenario = signalScenarioText(item);
      const direction = isPlanRoleItem(item) && isPlanRoleLabel(scenario)
        ? tradeDirection
        : scenario && scenario !== "-" ? `${tradeDirection} / ${scenario}` : tradeDirection;
      const action = readableSignalActionText(signalActionText(item));
      const entry = standardPlanEntry(item);
      const stop = standardPlanStop(item);
      const target = standardPlanTarget(item);
      const trigger = finiteField(item, ["price", "exit_price", "fill_price"]) ?? standardPlanDisplayPrice(item);
      const triggerLabel = role === "entry_fill"
        ? "ENTRY FILL"
        : role === "target_hit"
        ? "TARGET HIT"
        : role === "trail_stop_hit"
        ? "TRAIL STOP HIT"
        : role === "stop_hit"
        ? "STOP HIT"
        : "EVENT";
      const lines = [
        tooltipLine(`${title} ${triggerLabel}`, "title", tooltipTokenRoles([
          [triggerLabel, tooltipActionTokenRole(triggerLabel, role)],
          [tradeDirection, tooltipDirectionTokenRole(tradeDirection)],
        ])),
        tooltipLine(`DIR: ${direction}`, "meta", tooltipTokenRoles([
          ["DIR", "type"],
          [tradeDirection, tooltipDirectionTokenRole(tradeDirection)],
        ])),
        tooltipLabeledLine(
          "ACTION",
          action,
          "meta",
          tooltipActionTokenRole(signalActionText(item), role),
          tradeDirection,
        ),
        tooltipPriceLine("TRIGGER PRICE", trigger, "entry"),
        tooltipPriceLine("ENTRY", entry ?? plan.entry ?? plan.trigger, "entry"),
        tooltipPriceLine("STOP", stop ?? plan.stop ?? plan.invalidation, "stop"),
        tooltipPriceLine("TARGET", target ?? plan.target, "target"),
      ];
      return { lines: lines.filter(line => tooltipLineText(line).trim()) };
    }

    const standardSignalTooltipCache = new Map();
    const standardTableTooltipCache = new Map();
    let standardTooltipCacheScope = "";

    function standardTooltipScopeKey() {
      const snapshot = state.snapshot || {};
      const meta = snapshot?.meta || {};
      const visible = snapshot?.bars?.length ? visibleBars(snapshot) : {};
      const bars = visible?.bars || [];
      const first = bars[0];
      const last = bars[bars.length - 1];
      const renderVersions = typeof chartRenderVersions === "object" ? chartRenderVersions : {};
      return [
        exactIdentityText(meta.instrument_id ?? state.instrumentId),
        exactIdentityText(meta.route_fingerprint ?? instrumentRouteFingerprint()),
        String(meta.timeframe || state.timeframe || ""),
        String(meta.chart_range || state.range || ""),
        meta.analysis_updated_at || "",
        Number(meta.chart_canonical_revision || 0),
        Number(meta.chart_quality_revision || 0),
        Number(renderVersions.bars || 0),
        Number(renderVersions.indicators || 0),
        Number(renderVersions.settings || 0),
        visible?.start ?? "",
        visible?.end ?? "",
        first?.ts || "",
        last?.ts || "",
      ].join("|");
    }

    function resetStandardTooltipCachesIfNeeded() {
      const scope = standardTooltipScopeKey();
      if (scope === standardTooltipCacheScope) return;
      standardTooltipCacheScope = scope;
      standardSignalTooltipCache.clear();
      standardTableTooltipCache.clear();
    }

    function compactTooltipCacheValue(value) {
      if (value === null || value === undefined) return "";
      if (typeof value === "object") {
        try {
          // Tooltip sections nest deeper than the chart's bounded visual key serializer.
          const serialized = JSON.stringify(value);
          return typeof chartHashParts === "function" ? chartHashParts([serialized]) : serialized;
        } catch (_) {
          return "";
        }
      }
      return String(value).slice(0, 160);
    }

    function standardSignalTooltipCacheKey(item, options = {}) {
  item = indicatorFactsForDisplay(item);
      return [
        compactTooltipCacheValue(item),
        compactTooltipCacheValue(options.source),
        options.force ? "force" : "",
      ].join("|");
    }

    function standardSignalTooltip(item, options = {}) {
      resetStandardTooltipCachesIfNeeded();
      const cacheKey = standardSignalTooltipCacheKey(item, options);
      if (standardSignalTooltipCache.has(cacheKey)) return standardSignalTooltipCache.get(cacheKey);
      const actionCard = actionCardFromItem(item);
      let result = "";
      if (actionCard) {
        result = actionCardTooltip(actionCard);
      } else if (!options.force && !isSignalLikeItem(item)) {
        result = "";
      } else {
        const source = overlaySignalSource(item, options.source || "");
        const role = String(item?.role || item?.kind || "").toLowerCase();
        const title = isPlanRoleItem(item) ? planLineTitle(item, source) : signalNoteTitle(item, source);
        if (["entry_fill", "target_hit", "stop_hit", "trail_stop_hit"].includes(role)) {
          result = lifecycleExecutionTooltip(item, role);
        } else {
          const intentMark = signalIntentMark(item, role);
          const markedTitle = title;
          const actionText = readableSignalActionText(signalActionText(item));
          const markedAction = intentMark && !actionText.includes(intentMark) ? `${intentMark} ${actionText}` : actionText;
          if (role === "stop") {
            result = { lines: [
              tooltipLine(`${markedTitle} ${STOP_MARK}`, "title", tooltipTokenRoles([[STOP_MARK, "negative"]])),
              ...tooltipPlanLines(item, false),
            ] };
          } else if (["entry", "target", "trail", "track"].includes(role)) {
            const direction = normalizedOverlaySignal(item).direction;
            result = { lines: [
              tooltipLine(markedTitle, "title"),
              tooltipLine(`DIR: ${signalDirectionText(item)}`, "meta", tooltipTokenRoles([
                ["DIR", "type"],
                [direction, tooltipDirectionTokenRole(direction)],
              ])),
              tooltipLabeledLine(
                "ACTION",
                markedAction,
                "meta",
                tooltipActionTokenRole(signalActionText(item), role),
                direction,
              ),
              ...tooltipPlanLines(item),
            ] };
          } else {
            const triggerText = signalWaitingText(item);
            const planLines = tooltipPlanLines(item);
            const direction = normalizedOverlaySignal(item).direction;
            const pro = signalProLines(item);
            const con = signalConLines(item);
            const numberLines = signalNumberLines(item);
            const baseLines = [
              tooltipLine(markedTitle, "title"),
              tooltipLine(`DIR: ${signalDirectionText(item)}`, "meta", tooltipTokenRoles([
                ["DIR", "type"],
                [direction, tooltipDirectionTokenRole(direction)],
              ])),
              tooltipLabeledLine(
                "ACTION",
                markedAction,
                "meta",
                tooltipActionTokenRole(signalActionText(item), role),
                direction,
              ),
              tooltipLabeledLine("TRIGGER", triggerText, "entry", "type"),
              ...planLines,
              tooltipLine("--------------------", "separator"),
              ...pro.map(value => tooltipLine(`+ ${value}`, "pro")),
              ...con.map(value => tooltipLine(`- ${value}`, "con")),
              numberLines.length ? tooltipLine("NUMBERS", "plan") : "",
              ...numberLines,
            ].filter(line => tooltipLineText(line).trim());
            const contextLines = typedTooltipContextLines(item, baseLines);
            result = { lines: [
              ...baseLines,
              contextLines.length ? tooltipLine("CONTEXT", "plan") : "",
              ...contextLines,
            ].filter(line => tooltipLineText(line).trim()) };
          }
        }
      }
      standardSignalTooltipCache.set(cacheKey, result);
      return result;
    }

    function standardOverlayTooltip(item, options = {}) {
      const title = signalNoteTitle(item, options.source || overlaySignalSource(item));
      const direction = signalDirectionLabel(item);
      const action = signalActionText(item);
      const trigger = signalWaitingText(item);
      const lines = [tooltipLine(title, "title")];
      if (direction) lines.push(tooltipLine(`DIR: ${signalDirectionText(item)}`, "meta", tooltipTokenRoles([
        ["DIR", "type"],
        [direction, tooltipDirectionTokenRole(direction)],
      ])));
      if (action && action !== "-") {
        const readableAction = readableSignalActionText(action);
        lines.push(tooltipLabeledLine(
          "ACTION",
          readableAction,
          "meta",
          tooltipActionTokenRole(action),
          direction,
        ));
      }
      const price = finiteField(item, ["price", "level", "y1"]);
      const top = finiteField(item, ["top"]);
      const bottom = finiteField(item, ["bottom"]);
      if (Number.isFinite(price)) lines.push(tooltipPriceLine("PRICE", price, "entry"));
      if (Number.isFinite(top) || Number.isFinite(bottom)) {
        const topText = formatPlanNumber(top);
        const bottomText = formatPlanNumber(bottom);
        lines.push(tooltipLine(`ZONE: ${topText} / ${bottomText}`, "entry", tooltipTokenRoles([
          ["ZONE", "type"],
          [topText, "price"],
          [bottomText, "price"],
        ])));
      }
      if (trigger && trigger !== "-" && !lines.some(line => tooltipLineKey(line).includes(tooltipLineKey(trigger)))) {
        lines.push(tooltipLabeledLine("TRIGGER", trigger, "entry", "type"));
      }
      const planLines = tooltipPlanLines(item);
      lines.push(...planLines);
      const pro = signalProLines(item);
      const con = signalConLines(item);
      const numberLines = signalNumberLines(item);
      if (pro.length || con.length || numberLines.length) lines.push(tooltipLine("--------------------", "separator"));
      pro.forEach(value => lines.push(tooltipLine(`+ ${value}`, "pro")));
      con.forEach(value => lines.push(tooltipLine(`- ${value}`, "con")));
      if (numberLines.length) lines.push(tooltipLine("NUMBERS", "plan"), ...numberLines);
      const contextLines = typedTooltipContextLines(item, lines);
      if (contextLines.length) lines.push(tooltipLine("CONTEXT", "plan"), ...contextLines);
      return { lines: lines.filter(line => tooltipLineText(line).trim()) };
    }

    function standardTableTooltip(overlay, column = null) {
      resetStandardTooltipCachesIfNeeded();
      const table = overlay?.table || {};
      const cells = Array.isArray(column?.cells) ? column.cells : Array.isArray(column) ? column : [];
      const cacheKey = [
        compactTooltipCacheValue(overlay),
        compactTooltipCacheValue(column),
      ].join("|");
      if (standardTableTooltipCache.has(cacheKey)) return standardTableTooltipCache.get(cacheKey);
      const source = normalizedSignalSource(overlay?.source || table?.id || table?.title || "", "INDICATOR");
      const tooltipMode = String(column?.tooltip_mode || table?.tooltip_mode || overlay?.tooltip_mode || "").trim().toLowerCase();
      const detailsOnly = tooltipMode === "details";
      const lines = [tooltipLine(`[${source} TABLE]`, "title", tooltipTokenRoles([[source, "type"]]))];
      if (column) {
        const [section, value, context] = cells.map(cell => (
          indicatorTableCellText(cell).replace(/\s+/g, " ").trim()
        ));
        if (section) lines.push(tooltipLabeledLine("Section", section, "meta", "type"));
        if (value) lines.push(tooltipLabeledLine("Value", value, "meta", tooltipTypedTokenRole(column?.tone)));
        if (context) lines.push(tooltipLabeledLine("Context", context, "meta"));
      } else {
        const title = String(table?.title || "").replace(/\s+/g, " ").trim();
        if (title) lines.push(tooltipLabeledLine("Title", title, "meta", "type"));
      }
      const columnContext = column && typeof column === "object" ? column : {};
      const direction = signalDirectionLabel(columnContext) || signalDirectionLabel(table);
      if (direction) lines.push(tooltipLabeledLine("Direction", direction, "meta", tooltipDirectionTokenRole(direction)));
      const plan = structuredTradePlan(overlay) || structuredTradePlan(table);
      const planEntry = plan ? formatPlanNumber(plan.entry ?? plan.trigger) : "";
      const planStop = plan ? formatPlanNumber(plan.stop ?? plan.invalidation) : "";
      const planTarget = plan ? formatPlanNumber(plan.target) : "";
      const planTrigger = plan ? String(plan.reason || plan.code || "") : "";
      if (planEntry || planStop || planTarget || planTrigger) {
        lines.push(
          tooltipLabeledLine("Entry", planEntry || "-", "entry", "price"),
          tooltipLabeledLine("Stop", planStop || "-", "stop", "price"),
          tooltipLabeledLine("Target", planTarget || "-", "target", "price"),
          tooltipLabeledLine("Trigger", planTrigger || "-", "entry", "type"),
        );
      }
      const scopedTooltipFacts = { ...overlay, table, ...columnContext };
      const advisoryLines = typedTooltipContextLines(scopedTooltipFacts, lines);
      if (advisoryLines.length && (detailsOnly || advisoryLines.length > 1)) {
        lines.push(
          tooltipLine("--------------------", "separator"),
          tooltipLine(detailsOnly ? "DETAILS" : "ADVISOR", "plan"),
          ...advisoryLines,
        );
      }
      const tooltipSections = typedTooltipSections(scopedTooltipFacts);
      tooltipSections.forEach(section => {
        lines.push(
          tooltipLine("--------------------", "separator"),
          tooltipLine(section.title, "plan"),
          ...section.lines,
        );
      });
      const tooltipTables = typedTooltipTables(scopedTooltipFacts);
      const serviceSeen = new Set(lines.map(tooltipLineKey).filter(Boolean));
      const service = (detailsOnly ? [] : serviceTooltipLines({ ...overlay, source: overlay?.source || table?.id }))
        .filter(line => {
          const key = tooltipLineKey(line);
          if (!key || serviceSeen.has(key)) return false;
          serviceSeen.add(key);
          return true;
        });
      if (service.length) lines.push(tooltipLine("--------------------", "separator"), ...service);
      const result = { lines: lines.filter(line => tooltipLineText(line).trim()) };
      if (tooltipTables.length) result.tables = tooltipTables;
      standardTableTooltipCache.set(cacheKey, result);
      return result;
    }
