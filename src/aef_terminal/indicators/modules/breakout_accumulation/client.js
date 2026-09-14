function breakoutAccumulationCodeText(value) {
  const code = String(value || "").trim().toLowerCase();
  const labels = {
    trap_call: "TRAP CALL", trap_put: "TRAP PUT",
    second_call: "SECOND CALL", second_put: "SECOND PUT",
    retest_call: "RETEST CALL", retest_put: "RETEST PUT",
    join_call: "JOIN CALL", join_put: "JOIN PUT",
    go_call: "GO CALL", go_put: "GO PUT",
    probe_call: "PROBE CALL", probe_put: "PROBE PUT",
    rotation_call: "ROTATION CALL", rotation_put: "ROTATION PUT",
    rebound_call: "REBOUND CALL", rebound_put: "REBOUND PUT",
    ready_call: "READY CALL", ready_put: "READY PUT", ready_both: "READY BOTH",
    wide_range: "WIDE RANGE", forming_range: "FORMING RANGE", coil: "COIL",
    far_stop_call: "FAR STOP CALL", far_stop_put: "FAR STOP PUT",
    midline_fail: "MIDLINE FAIL", break_fail: "BREAK FAIL", no_follow: "NO FOLLOW",
    stop_hit: "STOP HIT", opposite: "OPPOSITE", timeout: "TIMEOUT", cancel: "CANCEL",
    enter: "ENTER", enter_now: "ENTER NOW", exit: "EXIT", exit_wait: "EXIT / WAIT",
    hold: "HOLD", watch: "WATCH", wait: "WAIT", watch_edges: "WATCH EDGES",
    call_level_ready: "CALL LEVEL READY", put_level_ready: "PUT LEVEL READY", edge_only: "EDGE ONLY",
  };
  return labels[code] || code.replace(/_/g, " ").toUpperCase();
}

function breakoutAccumulationRangeLabel(item) {
  const role = String(item?.role || "").toLowerCase();
  return item?.range_label === true
    || item?.overlay_group === "range_label"
    || role === "breakout_accumulation_range_label";
}

registerIndicatorOverlayFilter("breakout_accumulation", (item, group) => {
  if (item.type === "label" && !group.labels) return [];
  if (item.type === "label" && !group.rangeLabels && breakoutAccumulationRangeLabel(item)) return [];
  const overlayGroup = String(item.overlay_group || "").toLowerCase();
  const rangePrimitive = overlayGroup === "range_box" || overlayGroup === "range_level";
  if (item.type === "box" && overlayGroup === "range_box" && !group.rangeBox) return [];
  if (item.type === "line" && overlayGroup === "range_level" && !group.rangeLevels) return [];
  if (item.type === "line" && !group.rangeLabels && breakoutAccumulationRangeLabel(item)) return [{ ...item, label: "" }];
  if (item.type === "line" && !rangePrimitive && !group.levels) return [];
  return [item];
});

registerIndicatorTableModel("breakout_accumulation", table => {
  const model = table?.model && typeof table.model === "object" ? table.model : {};
  const direction = String(model.direction || "flat").toLowerCase();
  const plan = model.plan && typeof model.plan === "object" ? model.plan : {};
  const callScore = Number(model.call_score || 0);
  const putScore = Number(model.put_score || 0);
  const qualityTone = callScore >= putScore + 8 ? "positive" : putScore >= callScore + 8 ? "negative" : "warning";
  const eventText = breakoutAccumulationCodeText(model.event_code || model.setup_code) || "-";
  return [
    {
      cells: ["ACT", breakoutAccumulationCodeText(model.action_code || "wait"), breakoutAccumulationCodeText(model.state_code || "wait")],
      tone: direction,
      scenario: "Breakout/Accumulation state",
    },
    {
      cells: ["SIDE", direction === "long" ? "LONG" : direction === "short" ? "SHORT" : "FLAT", eventText],
      tone: direction,
      scenario: "Preferred side",
    },
    {
      cells: [
        "Q",
        { parts: [{ kind: "integer", prefix: "C", value: callScore }, { kind: "integer", prefix: "P", value: putScore }] },
        { parts: [{ kind: "integer", prefix: "Coil ", value: model.coil_score }, { kind: "fixed", prefix: "R ", value: model.range_atr, digits: 1 }] },
      ],
      tone: qualityTone,
      scenario: "Quality / pressure",
    },
    {
      cells: ["RANGE", { kind: "price", prefix: "H ", value: model.upper, empty: "--" }, { kind: "price", prefix: "L ", value: model.lower, empty: "--" }],
      scenario: "Active BAR range",
    },
    {
      cells: ["BANDS", { kind: "price", prefix: "C ", value: model.call_watch, empty: "--" }, { kind: "price", prefix: "P ", value: model.put_watch, empty: "--" }],
      tone: direction,
      scenario: "Call / put watch rails",
    },
    {
      cells: [
        "PLAN",
        { kind: "price", prefix: "E ", value: plan.visible ? plan.entry : null, empty: "--" },
        { parts: [{ kind: "price", prefix: "T ", value: plan.target, empty: "--" }, { kind: "price", prefix: "S ", value: plan.stop, empty: "--" }] },
      ],
      tone: plan.visible ? "warning" : "neutral",
      scenario: "Trade plan",
    },
  ];
});
