function smcChannelAreaInactive(item) {
  const role = String(item?.role || "").toLowerCase();
  if (!["fvg", "retained_fvg", "order_block", "smc_box"].includes(role)) return false;
  const lifecycleState = String(item?.smc_state || item?.smc_lifecycle?.state || "").toLowerCase();
  return lifecycleState === "filled" || lifecycleState === "mitigated";
}

function smcCollapsedAreaEndTimestamp(item, snapshot) {
  const bars = Array.isArray(snapshot?.bars) ? snapshot.bars : [];
  const startKey = timestampKey(item?.start_ts);
  const originEndKey = timestampKey(item?.origin_end_ts);
  const startIndex = bars.findIndex(bar => timestampKey(bar?.ts) === startKey);
  if (startIndex < 0) return "";
  const originEndIndex = bars.findIndex(bar => timestampKey(bar?.ts) === originEndKey);
  const endIndex = Math.min(
    Math.max(originEndIndex, startIndex + 3),
    bars.length - 1,
  );
  return String(bars[endIndex]?.ts || "");
}

function displaySmcChannelOverlay(item, group, snapshot) {
  if (!smcChannelAreaInactive(item)) return item;
  if (group.triggeredAreas === false) return null;
  const collapsedEndTs = smcCollapsedAreaEndTimestamp(item, snapshot);
  if (!collapsedEndTs) return item;
  return { ...item, end_ts: collapsedEndTs, smc_collapsed: true };
}

registerIndicatorOverlayFilter("smc_channels", (item, group, snapshot) => {
  if (item.type === "box" && !group.boxes) return [];
  if (item.type === "label" && !group.labels) return [];
  const displayItem = displaySmcChannelOverlay(item, group, snapshot);
  if (!displayItem) return [];
  if (group.labels === false && (displayItem.type === "box" || displayItem.type === "line")) {
    return [{ ...displayItem, label: "" }];
  }
  return [displayItem];
});
