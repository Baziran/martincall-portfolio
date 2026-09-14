    let managedIndicatorOverlayDiagnostics = null;
    function beginManagedIndicatorOverlayDiagnostics() {
      managedIndicatorOverlayDiagnostics = {
        collected: 0,
        renderable: 0,
        policy_hidden: 0,
        axis_culled: 0,
        invalid: 0,
        invalid_by_source: {},
      };
    }

    function recordManagedIndicatorOverlayCount(field, amount = 1) {
      if (!managedIndicatorOverlayDiagnostics) return;
      managedIndicatorOverlayDiagnostics[field] = Number(managedIndicatorOverlayDiagnostics[field] || 0) + amount;
    }

    function recordManagedIndicatorOverlayInvalid(source, reason) {
      if (!managedIndicatorOverlayDiagnostics) return;
      recordManagedIndicatorOverlayCount("invalid");
      const sourceKey = String(source || "overlay");
      const reasonKey = String(reason || "invalid_contract");
      const bySource = managedIndicatorOverlayDiagnostics.invalid_by_source;
      bySource[sourceKey] = bySource[sourceKey] || {};
      bySource[sourceKey][reasonKey] = Number(bySource[sourceKey][reasonKey] || 0) + 1;
    }

    function publishManagedIndicatorOverlayDiagnostics(renderable) {
      if (!managedIndicatorOverlayDiagnostics) return;
      managedIndicatorOverlayDiagnostics.renderable = Number(renderable || 0);
      if (window.mcTelemetrySet) {
        window.mcTelemetrySet("indicator_overlays.invalid_drop_count", managedIndicatorOverlayDiagnostics.invalid);
        window.mcTelemetrySet("indicator_overlays.diagnostics", {
          ...managedIndicatorOverlayDiagnostics,
          invalid_by_source: Object.fromEntries(
            Object.entries(managedIndicatorOverlayDiagnostics.invalid_by_source)
              .map(([source, reasons]) => [source, { ...reasons }]),
          ),
        });
      }
    }

    function managedOverlayFiniteNumber(value) {
      return value !== null && value !== undefined && value !== "" && Number.isFinite(Number(value));
    }

    function managedOverlayValidTimestamp(value) {
      if (typeof value !== "string") return false;
      const text = value.trim();
      return Boolean(text)
        && (text.endsWith("Z") || text.endsWith("+00:00"))
        && Number.isFinite(Date.parse(text));
    }

    function managedOverlayHasRenderableText(item) {
      const values = [
        ...(Array.isArray(item?.lines) ? item.lines : [item?.lines]),
        item?.label,
        item?.glyph,
        item?.event,
        item?.code,
        item?.state,
        item?.compact_label,
      ];
      if (values.some(value => String(value ?? "").trim())) return true;
      return item?.fuel === true || item?.terminal_climax === true || Boolean(String(item?.glyph_kind || "").trim());
    }

    function managedOverlayTimestampAxis(snapshot) {
      const bars = snapshot?.bars;
      if (!Array.isArray(bars)) return { keys: new Set(), firstMs: null, lastMs: null };
      const firstTimestamp = bars[0]?.ts || "";
      const lastTimestamp = bars[bars.length - 1]?.ts || "";
      return {
        length: bars.length,
        firstTimestamp,
        lastTimestamp,
        firstMs: Date.parse(firstTimestamp),
        lastMs: Date.parse(lastTimestamp),
        keys: new Set(bars.map(bar => timestampKey(bar?.ts)).filter(Boolean)),
      };
    }

    function managedOverlayPreflightReason(item, snapshot, timestampAxis = null) {
      if (!item || typeof item !== "object" || Array.isArray(item)) return "invalid_item";
      const type = String(item.type || "label").toLowerCase();
      if (!["box", "custom", "line", "marker", "label", "table"].includes(type)) return "invalid_type";
      if (item.contract && item.contract !== "overlay-contract-v1") return "invalid_contract";
      if (item.retention && !["active", "history"].includes(item.retention)) return "invalid_retention";
      if (type === "custom") {
        if (
          typeof item.renderer_ref !== "string"
          || !item.renderer_ref.trim()
          || typeof item.render_key !== "string"
          || !item.render_key.trim()
          || !item.payload
          || typeof item.payload !== "object"
          || Array.isArray(item.payload)
        ) return "invalid_custom_contract";
        return "";
      }
      if (type === "table") return "";
      if (type === "box" || type === "line") {
        if (!managedOverlayValidTimestamp(item.start_ts || item.ts)) return "invalid_start_timestamp";
        const projected = Object.prototype.hasOwnProperty.call(item, "end_anchor_ts")
          || Object.prototype.hasOwnProperty.call(item, "end_bar_offset");
        if (projected) {
          if (
            !managedOverlayValidTimestamp(item.end_anchor_ts)
            || !Number.isInteger(Number(item.end_bar_offset))
            || Number(item.end_bar_offset) < 1
          ) return "invalid_projected_end";
        } else if (!managedOverlayValidTimestamp(item.end_ts || item.ts)) {
          return "invalid_end_timestamp";
        }
        if (type === "box") {
          if (!managedOverlayFiniteNumber(item.top) || !managedOverlayFiniteNumber(item.bottom)) return "invalid_box_price";
        } else {
          const y1 = item.y1 ?? item.price;
          const y2 = item.y2 ?? item.y1 ?? item.price;
          if (!managedOverlayFiniteNumber(y1) || !managedOverlayFiniteNumber(y2)) return "invalid_line_price";
        }
        return "";
      }
      if (!managedOverlayValidTimestamp(item.ts)) return "invalid_timestamp";
      if (!managedOverlayFiniteNumber(item.price)) return "invalid_price";
      if (
        type === "marker"
        && !(String(item.role || item.kind || "").toLowerCase() === "structure_pivot" && Number.isInteger(Number(item.pivot_number)))
        && !managedOverlayHasRenderableText(item)
      ) return "empty_marker_content";
      if (type === "label" && !managedOverlayHasRenderableText(item)) return "empty_label_content";
      const axis = timestampAxis || managedOverlayTimestampAxis(snapshot);
      if (!axis.keys.has(timestampKey(item.ts))) {
        const timestampMs = Date.parse(item.ts);
        if (
          Number.isFinite(timestampMs)
          && Number.isFinite(axis.firstMs)
          && Number.isFinite(axis.lastMs)
          && (timestampMs < axis.firstMs || timestampMs > axis.lastMs)
        ) return "outside_axis";
        return "orphan_timestamp";
      }
      return "";
    }

class OverlayManager {
      constructor() {
        this.providers = [];
      }

      register(provider) {
        if (provider && (typeof provider.collect === "function" || typeof provider.draw === "function")) this.providers.push(provider);
        return this;
      }

      collect(snapshot) {
        const overlays = [];
        const timestampAxis = managedOverlayTimestampAxis(snapshot);
        for (const provider of this.providers) {
          if (provider.enabled && !provider.enabled()) continue;
          const source = provider.source || "overlay";
          const layer = provider.layer || "signals";
          const collected = provider.collect(snapshot) || [];
          if (Array.isArray(collected)) recordManagedIndicatorOverlayCount("collected", collected.length);
          for (const item of collected) {
            const preflightReason = managedOverlayPreflightReason(item, snapshot, timestampAxis);
            if (preflightReason) {
              if (preflightReason === "outside_axis") {
                recordManagedIndicatorOverlayCount("axis_culled");
                continue;
              }
              recordManagedIndicatorOverlayInvalid(item?.source || source, preflightReason);
              continue;
            }
            const normalized = normalizeManagedOverlayItem(item, source, layer);
            if (!normalized) {
              recordManagedIndicatorOverlayInvalid(item?.source || source, "normalization_rejected");
              continue;
            }
            overlays.push({
              ...normalized,
              zIndex: Number.isFinite(Number(normalized.zIndex)) ? Number(normalized.zIndex) : overlayLayerZIndex(normalized.layer, normalized.type),
            });
          }
        }
        return overlays.sort((left, right) => {
          const z = Number(left.zIndex || 0) - Number(right.zIndex || 0);
          return z || String(left.source || "").localeCompare(String(right.source || ""));
        });
      }

      draw(phase, env) {
        const items = this.providers
          .filter(provider => typeof provider.draw === "function")
          .filter(provider => !phase || provider.phase === phase)
          .filter(provider => !provider.enabled || provider.enabled(env))
          .sort((left, right) => {
            const z = Number(left.zIndex ?? overlayLayerZIndex(left.layer, left.type)) - Number(right.zIndex ?? overlayLayerZIndex(right.layer, right.type));
            return z || String(left.source || left.id || "").localeCompare(String(right.source || right.id || ""));
          });
        for (const provider of items) provider.draw(env);
      }
    }

    let priceOverlayDrawManager = null;

    function overlayLayerZIndex(layer, type) {
      const layerValue = {
        background: 0,
        zones: 10,
        levels: 20,
        signals: 30,
        tables: 60,
        foreground: 80,
      }[String(layer || "").toLowerCase()] ?? 30;
      const typeValue = { box: 0, custom: 3, line: 5, marker: 12, label: 18, table: 35 }[String(type || "").toLowerCase()] ?? 10;
      return layerValue + typeValue;
    }

    function normalizeManagedOverlayItem(item, source = "overlay", layer = "signals") {
      if (!item || typeof item !== "object") return null;
      const allowedTypes = new Set(["box", "custom", "line", "marker", "label", "table"]);
      const allowedLayers = new Set(["background", "zones", "levels", "signals", "tables", "foreground"]);
      const type = String(item.type || "label").toLowerCase();
      if (!allowedTypes.has(type)) return null;
      if (
        type === "custom"
        && (
          typeof item.renderer_ref !== "string"
          || !item.renderer_ref.trim()
          || typeof item.render_key !== "string"
          || !item.render_key.trim()
          || !item.payload
          || typeof item.payload !== "object"
          || Array.isArray(item.payload)
        )
      ) return null;
      const nextLayer = String(item.layer || layer || "signals").toLowerCase();
      return {
        ...item,
        type,
        source: item.source || source,
        layer: allowedLayers.has(nextLayer) ? nextLayer : "signals",
        contract: item.contract || "overlay-contract-v1",
        _ts_key: timestampKey(item.ts),
      };
    }

    let indicatorOverlayManager = null;
    let managedIndicatorOverlayCacheKey = "";
    let managedIndicatorOverlayCache = [];
    const indicatorOverlayFilterExtensions = new Map();

    function registerIndicatorOverlayFilter(filterRef, filter) {
      const key = String(filterRef || "").trim();
      if (!key || typeof filter !== "function") throw new Error(`Invalid indicator overlay filter: ${key || "missing"}`);
      if (key === "generic_overlay_filter" || indicatorOverlayFilterExtensions.has(key)) {
        throw new Error(`Duplicate indicator overlay filter: ${key}`);
      }
      indicatorOverlayFilterExtensions.set(key, filter);
    }

    function managedIndicatorOverlaySources() {
      return Object.entries(window.INDICATOR_REGISTRY_MANIFEST || {})
        .filter(([, spec]) => spec?.pipeline_stage !== "ui")
        .filter(([, spec]) => spec?.overlay_collect !== false)
        .filter(([, spec]) => (spec?.overlay_contract || "overlay-contract-v1") === "overlay-contract-v1")
        .filter(([, spec]) => spec?.renderer_kind !== "service")
        .map(([id]) => id);
    }

    function managedOverlayItemKey(item) {
      if (!item) return "";
      return [
        item.ts || "",
        item.start_ts || "",
        item.end_ts || "",
        item.end_anchor_ts || "",
        item.end_bar_offset ?? "",
        item.type || "",
        item.contract || "",
        item.retention || "",
        item.renderer_ref || "",
        item.render_key ?? "",
        item.role || "",
        item.kind || "",
        item.smc_state || "",
        item.origin_end_ts || "",
        item.fill_ts || "",
        item.label || "",
        item.style || "",
        item.width ?? "",
        item.color || "",
        item.bg || "",
        item.border || "",
        item.opacity ?? "",
        item.label_position || "",
        item.label_side || "",
        item.label_font_size ?? "",
        item.label_gap_px ?? "",
        item.x_offset_px ?? "",
        item.label_anchor || "",
        item.label_style || "",
        item.pattern || "",
        item.pattern_color || "",
        item.line_variant || "",
        item.control_key || "",
        JSON.stringify(item.control_keys || []),
        JSON.stringify(item.badge_facts || item.badge_lines || []),
        item.code || "",
        item.signal_overlay === true ? "signal-overlay" : "",
        item.price ?? "",
        item.top ?? "",
        item.bottom ?? "",
        item.y1 ?? "",
        item.y2 ?? "",
      ].join(":");
    }

    function managedOverlaySourceKey(snapshot) {
      const indicators = snapshot?.indicators || {};
      const meta = snapshot?.meta || {};
      const snapshotOverlays = Array.isArray(snapshot?.overlays) ? snapshot.overlays : [];
      const snapshotOverlayKey = snapshotOverlays.map(managedOverlayItemKey).join(";");
      const vsaOverlays = Array.isArray(snapshot?.vsa_volume?.overlays)
        ? snapshot.vsa_volume.overlays
        : [];
      const vsaOverlayKey = vsaOverlays.map(managedOverlayItemKey).join(";");
      const sourceKeys = managedIndicatorOverlaySources().map(source => {
        const overlays = indicators[source]?.overlays || [];
        const overlayKey = overlays.map(managedOverlayItemKey).join(";");
        return `${source}:${overlays.length}:${overlayKey}`;
      }).join("|");
      return [
        meta.analysis_key || "",
        meta.analysis_updated_at || "",
        `snapshot:${snapshotOverlays.length}:${snapshotOverlayKey}`,
        `vsa_volume:${vsaOverlays.length}:${vsaOverlayKey}`,
        `vsa_display:${state.indicators.vsaVolume.visible !== false ? 1 : 0}:${state.indicators.vsaVolume.priceMarks !== false ? 1 : 0}`,
        indicatorOverlayContributionStateKey(),
        sourceKeys,
      ].join("|");
    }

    function managedIndicatorOverlayProviderStateKey() {
      return Object.entries(indicatorRegistry())
        .filter(([, spec]) => spec?.overlay_collect !== false)
        .map(([indicatorId, spec]) => {
          const stateKey = spec?.ui?.state_key || "";
          const group = stateKey ? state.indicators?.[stateKey] || {} : {};
          const controlKey = (spec.controls || [])
            .map(control => `${control.key}:${String(group?.[control.state_key || control.key] ?? control.default ?? "")}`)
            .join(",");
          return [
            indicatorId,
            group.enabled !== false ? "calc1" : "calc0",
            group.visible !== false ? "vis1" : "vis0",
            controlKey,
          ].join(":");
        })
        .join("|");
    }

    function genericIndicatorOverlayFilter(item, group) {
      const controlKeys = Array.isArray(item?.control_keys)
        ? item.control_keys.map(key => String(key || "").trim()).filter(Boolean)
        : [String(item?.control_key || "").trim()].filter(Boolean);
      if (controlKeys.some(controlKey => group?.[controlKey] === false)) return [];
      return item ? [item] : [];
    }

    function indicatorOverlayFilterRegistry() {
      return {
        generic_overlay_filter: (item, group) => genericIndicatorOverlayFilter(item, group),
        ...Object.fromEntries(indicatorOverlayFilterExtensions),
      };
    }

    function indicatorOverlayStateGroup(spec) {
      const stateKey = spec?.ui?.state_key || "";
      return stateKey ? state.indicators?.[stateKey] || {} : {};
    }

    function collectManifestIndicatorOverlays(snapshot, indicatorId, spec) {
      const items = snapshot?.indicators?.[indicatorId]?.overlays || [];
      const group = indicatorOverlayStateGroup(spec);
      const filters = indicatorOverlayFilterRegistry();
      const filterRef = spec?.overlay_filter_ref || "generic_overlay_filter";
      const filter = filters[filterRef];
      if (typeof filter !== "function") throw new Error(`Unknown indicator overlay filter: ${filterRef}`);
      return items.flatMap(item => {
        const filtered = filter(item, group, snapshot);
        if (!Array.isArray(filtered) || !filtered.length) {
          recordManagedIndicatorOverlayCount("policy_hidden");
          return [];
        }
        return filtered;
      });
    }

    function createIndicatorOverlayManager() {
      const manager = new OverlayManager();
      manager.register({
        source: "snapshot_overlays",
        layer: "foreground",
        collect: snap => Array.isArray(snap?.overlays) ? snap.overlays : [],
      });
      manager.register({
        source: "vsa_volume",
        layer: "signals",
        enabled: () => (
          state.indicators.vsaVolume.visible !== false
          && state.indicators.vsaVolume.priceMarks !== false
        ),
        collect: snap => (
          Array.isArray(snap?.vsa_volume?.overlays)
            ? snap.vsa_volume.overlays
            : []
        ),
      });
      indicatorOverlayContributionRegistry.forEach(contribution => {
        manager.register(contribution);
      });
      Object.entries(indicatorRegistry())
        .filter(([, spec]) => spec?.pipeline_stage !== "ui")
        .filter(([, spec]) => spec?.overlay_collect !== false)
        .filter(([, spec]) => (spec?.overlay_contract || "overlay-contract-v1") === "overlay-contract-v1")
        .filter(([, spec]) => spec?.renderer_kind !== "service")
        .sort((left, right) => {
          const leftOrder = numericValueOrFallback(left[1]?.ui?.runtime_order, 500);
          const rightOrder = numericValueOrFallback(right[1]?.ui?.runtime_order, 500);
          return leftOrder - rightOrder || left[0].localeCompare(right[0]);
        })
        .forEach(([indicatorId, spec]) => {
          manager.register({
            source: indicatorId,
            layer: spec?.overlay_layer || "signals",
            enabled: () => indicatorVisibleForId(indicatorId) !== false,
            collect: snap => collectManifestIndicatorOverlays(snap, indicatorId, spec),
          });
        });
      return manager;
    }

    function indicatorOverlayManagerInstance() {
      if (!indicatorOverlayManager) indicatorOverlayManager = createIndicatorOverlayManager();
      return indicatorOverlayManager;
    }

    function collectManagedIndicatorOverlays(snapshot, visible = null) {
      if (!snapshot || !Array.isArray(snapshot.bars)) return [];
      const currentVisible = visible || visibleBars(snapshot);
      const bars = currentVisible.bars || [];
      const first = bars[0];
      const middle = bars[Math.floor((bars.length - 1) / 2)];
      const last = bars[bars.length - 1];
      const key = [
        currentVisible.start ?? "",
        currentVisible.end ?? "",
        bars.length,
        typeof chartBarRenderKey === "function" ? chartBarRenderKey(first) : first?.ts || "",
        typeof chartBarRenderKey === "function" ? chartBarRenderKey(middle) : middle?.ts || "",
        typeof chartBarRenderKey === "function" ? chartBarRenderKey(last) : last?.ts || "",
        typeof advisorEffectiveSignalDensity === "function" ? advisorEffectiveSignalDensity() : "",
        typeof isAdvisorPresentationActive === "function" && isAdvisorPresentationActive() ? "advisor" : "classic",
        typeof indicatorPlanHandoffActive === "function" && indicatorPlanHandoffActive(snapshot) ? "handoff" : "normal",
        managedIndicatorOverlayProviderStateKey(),
        indicatorCanvasHookStateKey(),
        managedOverlaySourceKey(snapshot),
      ].join("~");
      if (key && key === managedIndicatorOverlayCacheKey) return managedIndicatorOverlayCache;
      beginManagedIndicatorOverlayDiagnostics();
      const overlays = indicatorOverlayManagerInstance().collect(snapshot);
      managedIndicatorOverlayCache = filterManagedIndicatorOverlays(
        overlays.sort((left, right) => Number(left.zIndex || 0) - Number(right.zIndex || 0)),
        snapshot,
        bars,
      );
      recordManagedIndicatorOverlayCount(
        "policy_hidden",
        Math.max(overlays.length - managedIndicatorOverlayCache.length, 0),
      );
      publishManagedIndicatorOverlayDiagnostics(managedIndicatorOverlayCache.length);
      managedIndicatorOverlayCacheKey = key;
      return managedIndicatorOverlayCache;
    }

    function registerUniqueProvider(manager, seen, provider) {
      if (!provider || !provider.id || seen.has(provider.id)) return;
      seen.add(provider.id);
      manager.register(provider);
    }

    function registerIndicatorPriceOverlayRenderers(manager, call) {
      const seen = new Set();
      registerUniqueProvider(manager, seen, {
        id: "generic-indicators",
        source: "indicators",
        layer: "signals",
        phase: "overlay",
        zIndex: overlayLayerZIndex("signals", "label"),
        enabled: env => !env.gexOnly,
        draw: env => call("drawGenericIndicatorOverlays", [env.ctx, env.snapshot, env.visible, env.pad, env.priceH, env.width, env.x, env.y, env.xStep, env.labelStacks]),
      });
      registerUniqueProvider(manager, seen, {
        id: "indicator-service-profiles",
        source: "indicator-services",
        layer: "foreground",
        phase: "profile",
        zIndex: overlayLayerZIndex("foreground", "box"),
        enabled: env => !env.gexOnly,
        draw: env => runIndicatorCanvasHooks("price_profile", env),
      });
    }

    function createPriceOverlayDrawManager() {
      const manager = new OverlayManager();
      const call = (name, args) => {
        if (typeof window !== "undefined" && typeof window[name] === "function") return window[name](...args);
        if (typeof globalThis !== "undefined" && typeof globalThis[name] === "function") return globalThis[name](...args);
        if (typeof self !== "undefined" && typeof self[name] === "function") return self[name](...args);
        throw new Error(`Overlay draw function is not available: ${name}`);
      };
      // gex-trail: drawn in the background phase alongside candles.
      // Only history data is rendered here; it changes infrequently
      // (history revision, zoom, theme) so the background cache is rarely busted.
      manager.register({
        id: "gex-trail",
        source: "gex_context",
        layer: "background",
        phase: "background",
        zIndex: overlayLayerZIndex("background", "box"),
        draw: env => call("drawGexContext", [env.ctx, env.snapshot, env.visible, env.pad, env.priceH, env.width, env.y, env.xStep, {
          minP: env.minP,
          maxP: env.maxP,
          height: env.height,
          drawProfile: false,
          layer: "trail",
        }]),
      });
      // gex-levels: the 0.5 Hz live stream repaints only the overlay surface.
      manager.register({
        id: "gex-levels",
        source: "gex_context",
        layer: "levels",
        phase: "gex",
        zIndex: overlayLayerZIndex("levels", "line"),
        draw: env => call("drawGexContext", [env.ctx, env.snapshot, env.visible, env.pad, env.priceH, env.width, env.y, env.xStep, {
          minP: env.minP,
          maxP: env.maxP,
          height: env.height,
          drawProfile: false,
          layer: "levels",
        }]),
      });
      manager.register({
        id: "gex-foreground",
        source: "gex_context",
        layer: "foreground",
        phase: "front",
        zIndex: overlayLayerZIndex("foreground", "table") + 10,
        draw: env => call("drawGexContext", [env.ctx, env.snapshot, env.visible, env.pad, env.priceH, env.width, env.y, env.xStep, {
          minP: env.minP,
          maxP: env.maxP,
          height: env.height,
          drawProfile: false,
          layer: "foreground",
        }]),
      });
      manager.register({
        id: "ema-233",
        source: "ema",
        layer: "levels",
        phase: "overlay",
        zIndex: overlayLayerZIndex("levels", "line"),
        enabled: env => !env.gexOnly,
        draw: env => call("drawEma233", [env.ctx, env.snapshot, env.visible, env.pad, env.x, env.y]),
      });
      manager.register({
        id: "vwap",
        source: "vwap",
        layer: "levels",
        phase: "overlay",
        zIndex: overlayLayerZIndex("levels", "line") + 1,
        enabled: env => !env.gexOnly,
        draw: env => call("drawVwapCurve", [env.ctx, env.snapshot, env.visible, env.x, env.y]),
      });
      manager.register({
        id: "advisor-readiness",
        source: "advisor",
        layer: "signals",
        phase: "overlay",
        zIndex: overlayLayerZIndex("signals", "label") + 4,
        enabled: env => !env.gexOnly && isAdvisorChartMode(),
        draw: env => call("drawAdvisorReadinessMark", [env.ctx, env.snapshot, env.bars, env.x, env.y, env.pad, env.width, env.priceH]),
      });
      registerIndicatorPriceOverlayRenderers(manager, call);
      manager.register({
        id: "session-boundaries",
        source: "session",
        layer: "foreground",
        phase: "overlay",
        zIndex: overlayLayerZIndex("foreground", "line") + 5,
        enabled: env => !env.gexOnly,
        draw: env => call("drawSessionBoundaries", [env.ctx, env.bars, env.pad, env.priceH, env.xStep, env.x, false]),
      });
      manager.register({
        id: "gex-profile-companion",
        source: "gex_context",
        layer: "foreground",
        phase: "front",
        zIndex: overlayLayerZIndex("foreground", "table"),
        draw: env => call("drawGexProfileCompanion", [env.ctx, env.snapshot, env.visible, env.pad, env.priceH, env.width, env.y]),
      });
      return manager;
    }

    function priceOverlayDrawManagerInstance() {
      if (!priceOverlayDrawManager) priceOverlayDrawManager = createPriceOverlayDrawManager();
      return priceOverlayDrawManager;
    }

    function drawManagedPriceOverlays(phase, env) {
      priceOverlayDrawManagerInstance().draw(phase, env);
    }
