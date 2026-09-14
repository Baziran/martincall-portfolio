function lindaVolumeCodeText(code) {
  const key = String(code || "").toLowerCase();
  return ({
    wait: "WAIT",
    execute: "GO",
    armed: "ARMED",
    countertrend_watch: "CT WATCH",
    conflict: "CONFLICT",
    side_veto: "VETO",
    poi_not_hot: "WAIT POI",
    micro_break: "MICRO WARN",
    micro_watch: "MICRO",
    poi_confirmed: "POI",
    poi_watch: "POI WATCH",
    grail: "GRAIL",
    liquidity_reentry: "RE-ENTRY",
    liquidity_reclaim: "RECLAIM",
    trap_armed: "TRAP ARMED",
    trap_watch: "TRAP WATCH",
    climax_watch: "CLIMAX",
    fuel_watch: "FUEL",
    impulse_pullback_wait: "WAIT PB",
    pullback_watch: "PULLBACK",
    trigger_watch: "TRIGGER",
    no_short: "NO SHORT",
    no_long: "NO LONG",
    climax: "CLIMAX",
    fuel: "FUEL",
    countertrend_trap: "CT TRAP",
    shift: "SHIFT",
    vix: "VIX",
    void: "VOID",
    wide_range: "WIDE",
    thin_volume: "THIN",
    chop: "CHOP",
    clear: "CLEAR",
    smart_money: "SMART MONEY",
    poi_active: "POI ACTIVE",
    poi_wait: "WAIT POI",
    absorption_watch: "WATCH EDGE",
    indian_wait: "INDIAN",
    none: "-",
    plan_stop: "PLAN STOP",
    micro_range: "MICRO RANGE",
    poi_boundary: "POI INVALID",
    side_block: "SIDE BLOCK",
    vwap: "VWAP",
  })[key] || String(code || "-").replaceAll("_", " ").toUpperCase();
}

function lindaVolumeDetailCell(context) {
  const fact = context && typeof context === "object" ? context : {};
  if (fact.rr !== null && fact.rr !== undefined && Number.isFinite(Number(fact.rr))) {
    return { kind: "fixed", prefix: "RR ", value: fact.rr, digits: 2 };
  }
  if (fact.level !== null && fact.level !== undefined && Number.isFinite(Number(fact.level))) {
    const prefix = fact.relation === "above"
      ? "> "
      : fact.relation === "below"
        ? "< "
        : "";
    return { kind: "price", prefix, value: fact.level, empty: "-" };
  }
  if (fact.value !== null && fact.value !== undefined && Number.isFinite(Number(fact.value))) {
    return { kind: "fixed", value: fact.value, digits: 2 };
  }
  if (Number(fact.indian_count || 0) > 0) {
    const side = fact.direction === "long"
      ? "L"
      : fact.direction === "short"
        ? "S"
        : "";
    return `PB${Number(fact.indian_count)}${side ? ` ${side}` : ""}`;
  }
  return lindaVolumeCodeText(fact.detail_code || fact.relation || "-");
}

function lindaVolumeTableColumn(head, context) {
  const fact = context && typeof context === "object" ? context : {};
  const direction = String(fact.direction || "flat").toLowerCase();
  const stateCode = String(fact.state || "wait").toLowerCase();
  const tone = direction === "long"
    ? "long"
    : direction === "short"
      ? "short"
      : fact.tone === "danger"
        ? "danger"
        : fact.tone === "warn"
          ? "warn"
          : stateCode === "clear"
            ? "long"
            : ["wait", "none", "vwap"].includes(stateCode)
              ? "neutral"
              : "warn";
  return {
    cells: [head, lindaVolumeCodeText(stateCode), lindaVolumeDetailCell(fact)],
    tone,
    scenario: `linda_${head.toLowerCase()}`,
    trigger_event: { code: stateCode },
    metrics: fact,
  };
}

function lindaVolumeTableColumns(table) {
  const model = table && table.model && typeof table.model === "object"
    ? table.model
    : {};
  return [
    lindaVolumeTableColumn("ENTRY", model.entry),
    lindaVolumeTableColumn("RISK", model.risk),
    lindaVolumeTableColumn("OPP", model.opponent),
    lindaVolumeTableColumn("INV", model.invalidation),
  ];
}

function lindaVolumeOverlayFilter(item, group) {
  const setupRoleMap = {
    linda_setup_turtle_soup: "turtleSoup",
    linda_setup_turtle_soup_plus_one: "turtleSoupPlusOne",
    linda_setup_eighty_twenty: "eightyTwenty",
    linda_setup_the_anti: "theAnti",
    linda_setup_momentum_pinball: "momentumPinball",
    linda_setup_hv_squeeze: "hvSqueeze",
    linda_setup_adx_gapper: "adxGapper",
  };
  const overlayRole = String(item?.role || "").toLowerCase();
  const isPlanOverlay = ["entry", "stop", "target", "trail", "track"].includes(
    overlayRole,
  );
  const isGrailOverlay = overlayRole.startsWith("linda_grail_");
  const isIndianOverlay = overlayRole === "linda_indian_price_mark";
  const setupKey = setupRoleMap[overlayRole];
  if (isPlanOverlay && !group.plan) return [];
  if (isGrailOverlay && !group.grail) return [];
  if (isIndianOverlay && group.indians !== true) return [];
  if (setupKey && group[setupKey] !== true) return [];
  return [item];
}

function renderLindaPlaybookPanel(snapshot, context = {}) {
  if (context.calcEnabled === false) {
    context.controls
      ?.querySelector('[data-indicator-panel-key="playbook"]')
      ?.remove();
    return;
  }
  const node = context.mount?.(
    "playbook",
    "indicator-note linda-playbook-panel",
  );
  if (!node) return;
  const playbook = snapshot?.indicators?.linda_volume?.playbook_setups;
  if (!playbook || !Array.isArray(playbook.items) || !playbook.items.length) {
    node.textContent = "";
    node.style.display = "none";
    return;
  }
  node.style.display = "block";
  const lines = playbook.items.map(item => {
    const label = String(item.label || item.code || "?");
    const score = Number(item.score || 0);
    const direction = String(item.direction || "flat").toUpperCase();
    const action = String(item.action || "WATCH").toUpperCase();
    const theory = String(
      item.theory || item.candidate_reason || item.setup_summary || "",
    ).trim();
    const theoryShort = theory.length > 72
      ? `${theory.slice(0, 69)}...`
      : theory;
    return `${label} ${direction} ${action} ${Math.round(score)}${theoryShort ? ` — ${theoryShort}` : ""}`;
  });
  node.textContent = `Playbook latest (${playbook.symbol || state.symbol}): ${lines.join(" | ")}`;
}

registerIndicatorTableModel("linda_volume", lindaVolumeTableColumns);
registerIndicatorOverlayFilter("linda_volume", lindaVolumeOverlayFilter);
registerIndicatorPanelRenderer("linda_volume", renderLindaPlaybookPanel);
