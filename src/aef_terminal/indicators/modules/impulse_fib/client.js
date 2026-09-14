registerIndicatorOverlayFilter("impulse_fib", (item, group) => {
  const impulseStyle = group.style || "both";
  const showClassicBoxes = impulseStyle === "zones" || impulseStyle === "both";
  const showClassicLines = impulseStyle === "lines" || impulseStyle === "both";
  const overlayRole = String(item.role || "").toLowerCase();
  const isContinuationPattern = overlayRole === "continuation_pattern";
  const isPbZone = overlayRole === "pullback_zone" || String(item.id || "").startsWith("impulse-pb-");
  const isPlanOverlay = ["entry", "stop", "target", "trail", "track", "entry_fill", "target_hit", "stop_hit", "trail_stop_hit"].includes(overlayRole);
  if (isPlanOverlay) return [item];
  if (isContinuationPattern) return group.patterns === false ? [] : [item];
  if (isPbZone && !group.pbZones) return [];
  if (!isPbZone && item.type === "box" && (!group.zones || !showClassicBoxes)) return [];
  if (!isPbZone && item.type === "line" && (!group.zones || !showClassicLines)) return [];
  if (item.type === "label" && !group.labels) return [];
  const overlay = { ...item };
  if (overlay.type === "box") {
    const zoneAlpha = clamp(Number(group.zoneOpacity) || 10, 0, 30) / 100;
    overlay.bg = rgbaFromCssColor(overlay.bg || themeColor("gold"), zoneAlpha);
  }
  return [overlay];
});
