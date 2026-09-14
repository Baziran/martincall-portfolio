function optionReversalTableColumns(table) {
  const stateText = String(table?.state || "WAIT").toUpperCase();
  const score = Number.isFinite(Number(table?.score))
    ? `Q${Math.round(Number(table.score))}`
    : "Q-";
  const target = table?.target || {};
  const metrics = table?.metrics || {};
  const symbol = String(target.local_symbol || target.right || "OPT").trim();
  const optionSide = String(table?.option_side || target.right || "-").toUpperCase();
  const triggerEvent = table?.trigger_event;
  const stateTone = stateText === "BUY"
    ? "positive"
    : stateText === "SELL"
      ? "warning"
      : stateText === "WATCH"
        ? "warning"
        : "flat";
  return [
    {
      cells: ["STATE", stateText, score],
      tone: stateTone,
      trigger_event: triggerEvent,
    },
    {
      cells: ["OPTION", symbol, optionSide],
      direction: table?.direction,
    },
    {
      cells: [
        "PREMIUM",
        { kind: "percent", value: metrics.compression_ratio, digits: 0 },
        { kind: "price", prefix: "PX ", value: metrics.option_price, digits: 4 },
      ],
    },
    {
      cells: [
        "TARGET",
        { kind: "price", prefix: "@ ", value: target.target_price, digits: 4 },
        { kind: "fixed", value: metrics.near_atr, digits: 2, suffix: " ATR" },
      ],
    },
    {
      cells: [
        "WHY",
        { kind: "domain_fact", value: triggerEvent, part: "primary" },
        { kind: "domain_fact", value: triggerEvent, part: "secondary" },
      ],
      trigger_event: triggerEvent,
    },
  ];
}

registerIndicatorTableModel("option_reversal", optionReversalTableColumns);
