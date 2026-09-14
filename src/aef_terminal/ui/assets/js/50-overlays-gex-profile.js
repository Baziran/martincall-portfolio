let gexProfileFadeRenderFrame = 0;
let gexProfileProcessStateCache = {};
const GEX_PROFILE_PROCESS_HOLD_MS = 12000;
const GEX_PROFILE_PROCESS_PULSE_MS = 3800;
const GEX_EXPIRY_PROFILE_LANE_GAP = 30;
const GEX_SIDEBAR_WIDTH_MIN = 154;
const GEX_SIDEBAR_WIDTH_MAX = 420;
const GEX_SIDEBAR_WIDTH_DEFAULT = 248;
const GEX_SIDEBAR_DRAWABLE_RATIO_MAX = 0.55;

    function gexProfileMode(value) {
      const mode = String(value || "mini");
      return ["mini", "ladder", "expiry"].includes(mode) ? mode : "mini";
    }

    function compactGexExpiry(value) {
      const text = String(value || "");
      if (/^\d{8}$/.test(text)) return `${text.slice(4, 6)}/${text.slice(6, 8)}`;
      if (/^\d{4}-\d{2}-\d{2}$/.test(text)) return `${text.slice(5, 7)}/${text.slice(8, 10)}`;
      return text;
    }

    function gexSidebarRequestedWidth(value = layout?.gexSidebarWidth) {
      const requested = Number(value);
      return Math.round(clamp(
        Number.isFinite(requested) ? requested : GEX_SIDEBAR_WIDTH_DEFAULT,
        GEX_SIDEBAR_WIDTH_MIN,
        GEX_SIDEBAR_WIDTH_MAX,
      ));
    }

    function gexSidebarShellVisible() {
      if (typeof gexSidebarIsDocked === "function" && gexSidebarIsDocked()) return false;
      return typeof gexLayerVisible === "function"
        ? gexLayerVisible()
        : Boolean(state?.indicators?.gexContext?.enabled || state?.settings?.gexFocusMode);
    }

    function gexProfileGutterGeometry(pad, width, options = {}) {
      const panelLeft = 2;
      const frameWidth = Math.max(0, Number(width) || 0);
      const rightPad = Math.max(0, Number(pad?.right) || 0);
      const leftPad = Math.max(0, Number(pad?.left) || 0);
      const reserve = Math.max(0, leftPad - CHART_LEFT_PAD);
      const plotRight = Math.max(panelLeft, frameWidth - rightPad);
      const drawableWidth = Math.max(0, plotRight - panelLeft);
      const requestedWidth = gexSidebarRequestedWidth(options.requestedWidth);
      const shellVisible = options.shellVisible === undefined
        ? gexSidebarShellVisible()
        : Boolean(options.shellVisible);
      const responsiveMax = Math.floor(
        drawableWidth * GEX_SIDEBAR_DRAWABLE_RATIO_MAX,
      );
      const effectiveWidth = shellVisible
        ? Math.max(0, Math.min(requestedWidth, responsiveMax))
        : 0;
      const panelRight = panelLeft + effectiveWidth;
      return {
        drawableWidth,
        effectiveWidth,
        panelRight,
        requestedWidth,
        reserve,
        responsiveMax,
        shellVisible,
        panelLeft,
        panelW: effectiveWidth,
        plotRight,
        circlesLeft: panelRight + 2,
        circlesRight: reserve > 0
          ? Math.max(panelRight + 4, leftPad - 8)
          : Math.max(panelRight + 2, Math.min(plotRight - 12, panelRight + 94)),
      };
    }

    function applyGexSidebarGeometryToDom(geometry) {
      if (!geometry) return;
      const requestedWidth = gexSidebarRequestedWidth(geometry.requestedWidth);
      const effectiveWidth = Math.max(0, Number(geometry.effectiveWidth) || 0);
      const rootStyle = document.documentElement.style;
      const requestedCssWidth = `${requestedWidth}px`;
      if (rootStyle.getPropertyValue("--gex-sidebar-requested-width") !== requestedCssWidth) {
        rootStyle.setProperty("--gex-sidebar-requested-width", requestedCssWidth);
      }
      const effectiveCssWidth = `${effectiveWidth}px`;
      if (rootStyle.getPropertyValue("--gex-sidebar-effective-width") !== effectiveCssWidth) {
        rootStyle.setProperty("--gex-sidebar-effective-width", effectiveCssWidth);
      }
      const panelRightCss = `${Math.max(0, Number(geometry.panelRight) || 0)}px`;
      if (rootStyle.getPropertyValue("--gex-sidebar-panel-right") !== panelRightCss) {
        rootStyle.setProperty("--gex-sidebar-panel-right", panelRightCss);
      }
      const widthInput = document.getElementById("gex-sidebar-width");
      if (widthInput && widthInput.value !== String(requestedWidth)) {
        widthInput.value = String(requestedWidth);
      }
      const widthValue = document.getElementById("gex-sidebar-width-value");
      const widthValueText = `${requestedWidth} px`;
      if (widthValue && widthValue.textContent !== widthValueText) {
        widthValue.textContent = widthValueText;
      }
      const resizer = document.getElementById("gex-sidebar-resizer");
      if (resizer) {
        if (resizer.getAttribute("aria-valuemin") !== String(GEX_SIDEBAR_WIDTH_MIN)) {
          resizer.setAttribute("aria-valuemin", String(GEX_SIDEBAR_WIDTH_MIN));
        }
        if (resizer.getAttribute("aria-valuemax") !== String(GEX_SIDEBAR_WIDTH_MAX)) {
          resizer.setAttribute("aria-valuemax", String(GEX_SIDEBAR_WIDTH_MAX));
        }
        const ariaValueNow = String(requestedWidth);
        if (resizer.getAttribute("aria-valuenow") !== ariaValueNow) {
          resizer.setAttribute("aria-valuenow", ariaValueNow);
        }
        const roundedEffectiveWidth = Math.round(effectiveWidth);
        const ariaValueText = roundedEffectiveWidth === requestedWidth
          ? `${requestedWidth} px`
          : `${requestedWidth} px requested, ${roundedEffectiveWidth} px visible`;
        if (resizer.getAttribute("aria-valuetext") !== ariaValueText) {
          resizer.setAttribute("aria-valuetext", ariaValueText);
        }
      }
    }

    function gexExpiryRowsForVisibleStrikes(rows, visibleLevels) {
      const source = gexExpiryProfileIsCanonical(rows) ? rows : [];
      const selectedLevels = Array.isArray(visibleLevels) ? visibleLevels : [];
      if (!source.length || !selectedLevels.length) return [];
      const selectedStrikes = new Set(
        selectedLevels
          .map(level => gexNumericStrikeKey(level?.price))
          .filter(strike => strike !== null),
      );
      return source.filter(row => selectedStrikes.has(gexNumericStrikeKey(row?.strike)));
    }

    function gexLeftCompanionRight(pad, width, snapshot = state.snapshot) {
      const gutter = gexProfileGutterGeometry(pad, width);
      if (!gutter.shellVisible) return Number(pad?.left) || 0;
      return Math.max(
        Number(pad?.left) || 0,
        Math.min(gutter.plotRight - 8, gutter.panelRight + 2),
      );
    }

    function gexProfileGeometryRows(rows, y, pad, priceH, options = {}) {
      const source = Array.isArray(rows) ? rows : [];
      const includeEmpty = Boolean(options.includeEmpty);
      const maxAbs = Math.max(1e-9, ...source.map(gexProfileRowMagnitude));
      const projected = source
        .map(row => {
          const strike = row?.expiry
            ? gexNullableNumber(row?.strike)
            : gexNullableNumber(row?.price);
          const rawY = strike !== null ? y(strike) : NaN;
          const strength = gexLevelStrengthValue(row);
          const magnitude = gexProfileRowMagnitude(row);
          return {
            ...row,
            strike: strike ?? NaN,
            profile_ratio: clamp(
              strength !== null ? strength : magnitude / maxAbs,
              0,
              1,
            ),
            raw_y: rawY,
          };
        });
      const laneCounts = new Map();
      for (const row of projected) {
        if (!row?.expiry) continue;
        const strikeKey = gexNumericStrikeKey(row.strike);
        if (strikeKey === null) continue;
        laneCounts.set(strikeKey, (laneCounts.get(strikeKey) || 0) + 1);
      }
      const laneIndexes = new Map();
      return projected
        .sort((left, right) => {
          const strikeDelta = right.strike - left.strike;
          if (strikeDelta) return strikeDelta;
          return String(left.expiry || "").localeCompare(String(right.expiry || ""));
        })
        .map(row => {
          const strikeKey = gexNumericStrikeKey(row.strike);
          const laneCount = row?.expiry && strikeKey !== null ? laneCounts.get(strikeKey) || 1 : 1;
          const laneIndex = row?.expiry && strikeKey !== null ? laneIndexes.get(strikeKey) || 0 : 0;
          if (row?.expiry && strikeKey !== null) laneIndexes.set(strikeKey, laneIndex + 1);
          const laneOffset = (laneIndex - (laneCount - 1) * 0.5) * GEX_EXPIRY_PROFILE_LANE_GAP;
          const profileY = row.raw_y + laneOffset;
          return {
            ...row,
            profile_lane_index: laneIndex,
            profile_lane_count: laneCount,
            profile_y: profileY,
          };
        })
        .filter(row => (
          Number.isFinite(row.strike)
          && (includeEmpty || row.profile_ratio > 0)
          && row.raw_y >= pad.top - 10
          && row.raw_y <= pad.top + priceH + 10
        ));
    }

    function gexProfileRowMagnitude(row) {
      return Math.abs(gexNullableNumber(row?.abs_gex) ?? 0);
    }

    function gexProfileProcessKey(row = null, level = null) {
      const source = level || row || {};
      const identityKey = typeof gexContextKey === "function" ? gexContextKey() : "";
      if (typeof gexMotionPriceKey === "function") return `${identityKey}|${gexMotionPriceKey(source)}`;
      const expiry = typeof source?.expiry === "string" ? source.expiry : "";
      const price = expiry
        ? gexNullableNumber(source?.strike)
        : gexNullableNumber(source?.price);
      const priceKey = price !== null ? String(Math.round(price * 1000) / 1000) : String(source?.kind || "");
      const kind = typeof source?.kind_class === "string"
        ? source.kind_class
        : typeof source?.kind === "string"
          ? source.kind
          : "";
      return [identityKey, priceKey, expiry, kind].filter(Boolean).join("|");
    }

    function gexProfileProcessDisplay(level = null, row = null) {
      const source = level || row;
      const stateCode = gexOptionVolumeInteraction(source);
      const label = gexOptionVolumeInteractionLabel(source);
      const key = gexProfileProcessKey(row, level);
      const now = Date.now();
      if (!key) return { label, stateCode, active: false, held: false, changedAt: 0, expiresAt: 0 };
      const cached = gexProfileProcessStateCache[key];
      const meaningful = label && stateCode !== "activity_away_from_level";
      if (meaningful) {
        if (!cached || cached.stateCode !== stateCode || cached.label !== label) {
          gexProfileProcessStateCache[key] = {
            stateCode,
            label,
            changedAt: now,
            expiresAt: now + GEX_PROFILE_PROCESS_HOLD_MS,
          };
        }
        const activeCache = gexProfileProcessStateCache[key];
        return {
          label,
          stateCode,
          active: now - Number(activeCache.changedAt || 0) <= GEX_PROFILE_PROCESS_HOLD_MS,
          held: false,
          changedAt: activeCache.changedAt,
          expiresAt: activeCache.expiresAt,
        };
      }
      if (cached && Number(cached.expiresAt || 0) > now && cached.label) {
        return {
          label: cached.label,
          stateCode: cached.stateCode,
          active: true,
          held: true,
          changedAt: cached.changedAt,
          expiresAt: cached.expiresAt,
        };
      }
      if (cached && Number(cached.expiresAt || 0) <= now) delete gexProfileProcessStateCache[key];
      return { label: "", stateCode, active: false, held: false, changedAt: 0, expiresAt: 0 };
    }

    function gexProfileRowNetValue(row) {
      return gexNullableNumber(row?.net_gex);
    }

    function gexProfileSideValue(row, side = "net") {
      const sideName = typeof side === "string" ? side : "net";
      if (sideName === "call") return gexNullableNumber(row?.call_gex);
      if (sideName === "put") return gexNullableNumber(row?.put_gex);
      return gexProfileRowNetValue(row);
    }

    function gexProfileSideColor(side, alpha = 0.88) {
      return side === "call"
        ? themeColor("blue", alpha)
        : themeColor("amber", alpha);
    }

    function gexProfileFadeAlpha(gex) {
      if (gex?.live?.active || gex?.source === "gex:ibkr-live") return 1;
      const ts = Date.parse(gex?.captured_at || "");
      if (!Number.isFinite(ts)) return 1;
      const ageMs = Date.now() - ts;
      if (ageMs < 0 || ageMs > 900) return 1;
      return clamp(0.36 + ageMs / 900 * 0.64, 0.36, 1);
    }

    function scheduleGexProfileFadeFrame(gex) {
      const cfg = state.indicators?.gexContext || {};
      if (!gexLayerVisible() || !cfg.profile) {
        if (gexProfileFadeRenderFrame) {
          cancelAnimationFrame(gexProfileFadeRenderFrame);
          gexProfileFadeRenderFrame = 0;
        }
        return;
      }
      const alpha = gexProfileFadeAlpha(gex);
      if (!uiMotionEnabled() || alpha >= 1 || gexProfileFadeRenderFrame) return;
      gexProfileFadeRenderFrame = requestAnimationFrame(() => {
        gexProfileFadeRenderFrame = 0;
        if (
          gexLayerVisible()
          && state.indicators?.gexContext?.profile
          && state.snapshot
          && typeof renderPriceGexFrontFromGeometry === "function"
        ) {
          const frontCanvas = document.getElementById("price-gex-front");
          if (!frontCanvas) return;
          const { w, h } = canvasContext("price-gex-front");
          renderPriceGexFrontFromGeometry(
            state.snapshot,
            priceRenderGeometry(state.snapshot, w, h),
          );
        }
      });
    }

    function drawGexMiniProfile(ctx, gex, rows, levels, snapshot, pad, priceH, width, y, cfg, options = {}) {
      if (!rows.length && !levels.length) return;
      const plotRight = width - pad.right;
      const profileMaxW = clamp((plotRight - pad.left) * 0.075, 46, 92);
      const gutter = options.gutter || gexProfileGutterGeometry(pad, width);
      const left = gutter.reserve > 0 ? Math.max(gutter.panelLeft, pad.left - profileMaxW - 14) : pad.left + 8;
      ctx.save();
      for (const row of rows) {
        const yy = gexNullableNumber(row.profile_y) ?? y(row.strike);
        if (yy < pad.top - 4 || yy > pad.top + priceH + 4) continue;
        const level = gexLevelForExactStrike(levels, row.strike);
        const netSign = gexGammaSign(row);
        const color = level
          ? gexKindColor(level, 0.72)
          : netSign === null || netSign === 0
            ? (state.settings.theme === "light" ? "rgba(71,85,105,0.72)" : "rgba(148,163,184,0.72)")
            : netSign > 0 ? themeColor("blue", 0.45) : themeColor("amber", 0.48);
        const barW = Math.max(2, profileMaxW * row.profile_ratio);
        const barX = left;
        ctx.fillStyle = color;
        ctx.fillRect(barX, yy - 2, barW, 4);
        registerCanvasTooltip(
          options.tooltipChart || "price",
          barX + Math.max(barW / 2, 6),
          yy,
          Math.max(barW, 12),
          12,
          () => gexProfileRowTooltip(gex, row, level, "Mini GEX profile", row.profile_ratio),
          null,
          canvasLayerZIndex("tooltips"),
          GEX_TOOLTIP_SOURCE_FRONT,
        );
      }
      {
        const labelLevels = (levels || [])
          .map(level => ({ level, price: gexNullableNumber(level.price), strength: gexLevelStrengthValue(level) }))
          .filter(item => item.price !== null && y(item.price) >= pad.top - 8 && y(item.price) <= pad.top + priceH + 8)
          .sort((a, b) => a.price - b.price);
        const usedY = [];
        for (const item of labelLevels) {
          const { level, price, strength } = item;
          const renderStrength = strength ?? 0;
          const yy = y(price);
          const color = gexKindColor(level, 0.94);
          const powerLabel = gexLevelPowerLabel(level, strength);
          const sign = gexGammaSign(level);
          const signLabel = sign > 0 ? "+" : sign < 0 ? "-" : sign === 0 ? "0" : "?";
          const label = `${signLabel} ${compactLineLabel(level.kind || "GEX")} ${powerLabel} ${fmt(price)}`;
          let labelY = clamp(yy - 12, pad.top + 12, pad.top + priceH - 6);
          while (usedY.some(value => Math.abs(value - labelY) < 12) && labelY < pad.top + priceH - 6) labelY += 12;
          usedY.push(labelY);
          if (gexZoneMode(cfg.zoneStyle) === "lines") {
            ctx.strokeStyle = rgbaFromCssColor(color, clamp(0.30 + renderStrength * 0.28, 0.34, 0.64));
            ctx.lineWidth = clamp(1 + renderStrength * 1.4, 1, 2.4);
            ctx.setLineDash([8, 5]);
            ctx.beginPath();
            ctx.moveTo(left, yy);
            ctx.lineTo(plotRight, yy);
            ctx.stroke();
          }
          ctx.setLineDash([]);
          ctx.font = "900 10px -apple-system, BlinkMacSystemFont, sans-serif";
          ctx.textAlign = "left";
          ctx.textBaseline = "middle";
          ctx.fillStyle = color;
          const labelX = left + 2;
          drawCanvasText(ctx, label, labelX, labelY, undefined, { outlineWidth: 3 });
          const labelW = Math.min(ctx.measureText(label).width + 12, 260);
          registerCanvasTooltip(
            options.tooltipChart || "price",
            labelX + labelW / 2,
            labelY,
            labelW,
            18,
            () => gexLevelTooltip(gex, level, { strength }),
            null,
            canvasLayerZIndex("tooltips"),
            GEX_TOOLTIP_SOURCE_FRONT,
          );
        }
      }
      ctx.restore();
    }

    function drawGexPriceProfileMarkers(ctx, gex, rows, pad, priceH, width, y, mode, levels = []) {
      if (!rows.length) return;
      const gutter = gexProfileGutterGeometry(pad, width);
      const baseX = gutter.circlesLeft;
      const maxX = gutter.circlesRight;
      const title = mode === "expiry" ? "expiry strike gamma" : "strike gamma";
      const selectedProfilePeak = mode === "expiry" ? Math.max(1e-9, ...rows.map(gexProfileRowMagnitude)) : 0;
      ctx.save();
      rows
        .slice()
        .sort((a, b) => b.profile_ratio - a.profile_ratio)
        .forEach(row => {
          const yy = gexNullableNumber(row.profile_y) ?? y(row.strike);
          if (yy < pad.top - 8 || yy > pad.top + priceH + 8) return;
          const profileRatio = mode === "expiry"
            ? clamp(gexProfileRowMagnitude(row) / selectedProfilePeak, 0, 1)
            : row.profile_ratio;
          const level = gexLevelForExactStrike(levels, row.strike);
          const netSign = gexGammaSign(row);
          const color = mode !== "expiry" && level
            ? gexKindColor(level, 0.92)
            : netSign === null || netSign === 0
              ? (state.settings.theme === "light" ? "rgba(71,85,105,0.82)" : "rgba(148,163,184,0.78)")
              : (netSign > 0 ? themeColor("blue", 0.92) : themeColor("amber", 0.88));
          const radius = mode === "expiry"
            ? clamp(3.2 + profileRatio * 5, 4, 8)
            : clamp(3.2 + profileRatio * 8.5, 4, 11);
          const cx = clamp(baseX + profileRatio * 54, baseX, maxX);
          ctx.fillStyle = rgbaFromCssColor(color, clamp(0.14 + profileRatio * 0.30, 0.16, 0.44));
          roundedRectPath(ctx, cx - radius - 6, yy - radius - 6, (radius + 6) * 2, (radius + 6) * 2, 0);
          ctx.fill();
          ctx.strokeStyle = rgbaFromCssColor(color, clamp(0.42 + profileRatio * 0.42, 0.46, 0.84));
          ctx.lineWidth = clamp(1.2 + profileRatio * 1.8, 1.2, 3);
          roundedRectPath(ctx, cx - radius, yy - radius, radius * 2, radius * 2, 0);
          ctx.stroke();
          ctx.fillStyle = rgbaFromCssColor(color, 0.92);
          const coreRadius = Math.max(2, radius * 0.36);
          roundedRectPath(ctx, cx - coreRadius, yy - coreRadius, coreRadius * 2, coreRadius * 2, coreRadius);
          ctx.fill();
          registerCanvasTooltip(
            "price",
            cx,
            yy,
            radius * 4,
            radius * 4,
            () => gexProfileRowTooltip(gex, row, level, `GEX ${title}`, profileRatio),
            null,
            canvasLayerZIndex("tooltips"),
            GEX_TOOLTIP_SOURCE_FRONT,
          );
        });
      ctx.restore();
    }

    function drawGexStrikeMigrationMarkers(ctx, gex, snapshot, visible, pad, priceH, width, y, xStep) {
      if (!snapshot?.bars?.length) return;
      const bars = visible?.bars || [];
      if (!bars.length) return;
      const cfg = state.indicators.gexContext || {};
      const history = gexHistoryRenderLane(gex);
      const rows = history
        .map(item => ({ ...item, tsMs: gexHistoryTimestampMs(item) }))
        .filter(item => Number.isFinite(item.tsMs) && Array.isArray(item.levels) && item.levels.length);
      const ordered = rows
        .sort((left, right) => {
          if (left.tsMs !== right.tsMs) return left.tsMs - right.tsMs;
          const leftLane = `${left.source || ""}|${left.capture_mode || ""}`;
          const rightLane = `${right.source || ""}|${right.capture_mode || ""}`;
          return leftLane.localeCompare(rightLane);
        });
      if (!ordered.length) return;
      const timeWindow = visibleTimeWindowForBars(bars, 3);
      const nowMs = Date.now();
      const visibleOrdered = timeWindow
        ? ordered.filter(item => gexHistoryIntersectsVisible(item, timeWindow))
        : ordered;
      if (!visibleOrdered.length) return;
      const quality = gexTrailQuality(ordered, visible, xStep, cfg);
      const sampleStep = Math.max(1, quality.sampleStep);
      const drawRows = sampleStep <= 1
        ? visibleOrdered
        : visibleOrdered.filter((_, index) => index === visibleOrdered.length - 1 || (visibleOrdered.length - 1 - index) % sampleStep === 0);
      const plotLeft = pad.left;
      const plotRight = width - pad.right;
      const fadeSpanMs = Math.max(nowMs - ordered[0].tsMs, 60_000);
      const currentLevels = Array.isArray(gex?.levels) ? gex.levels : [];
      const x = chartX(pad, xStep);
      const timeLookup = genericOverlayLookupForVisible(visible, bars, snapshot.bars);
      ctx.save();
      ctx.lineCap = "round";
      for (const snap of drawRows) {
        const cx = xForGexHistoryTime(timeLookup, snap.tsMs, xStep, x);
        if (!Number.isFinite(cx)) continue;
        if (cx < plotLeft - xStep || cx > plotRight + xStep) continue;
        const ageRatio = clamp((nowMs - snap.tsMs) / fadeSpanMs, 0, 1);
        const levels = gexVisibleLevels(snap.levels || [], cfg);
        for (const level of levels) {
          const price = gexNullableNumber(level.price);
          if (price === null) continue;
          const currentLevel = gexLevelForExactStrike(currentLevels, price);
          const yy = y(price);
          if (yy < pad.top - 14 || yy > pad.top + priceH + 14) continue;
          const strength = gexLevelStrengthValue(level);
          const renderStrength = strength ?? 0;
          const color = gexKindColor(level, 0.96);
          const alpha = clamp((0.22 + renderStrength * 0.62) * (1 - ageRatio * 0.66), 0.12, 0.84);
          const radius = clamp(2.8 + renderStrength * 7.2, 3, 10);
          ctx.fillStyle = rgbaFromCssColor(color, clamp(0.10 + renderStrength * 0.10, 0.10, 0.20));
          roundedRectPath(ctx, cx - radius, yy - radius, radius * 2, radius * 2, 0);
          ctx.fill();
          ctx.strokeStyle = rgbaFromCssColor(color, clamp(alpha * 0.62, 0.24, 0.56));
          ctx.lineWidth = clamp(0.9 + renderStrength * 1.2, 0.9, 2.1);
          roundedRectPath(ctx, cx - radius, yy - radius, radius * 2, radius * 2, 0);
          ctx.stroke();
          registerCanvasTooltip(
            "price",
            cx,
            yy,
            radius * 4,
            radius * 4,
            () => gexLevelTooltip(snap, level, {
              title: `GEX ${gexHistoryCaptureLabel(snap)} strike migration ${gexHistoryTimeLabel(snap)} · ${level.kind || "level"} ${fmt(price)}`,
              strength,
              temporalScope: "historical",
              snapshotLabel: gexHistoryTimeLabel(snap),
              currentLevel,
            }),
            null,
            canvasLayerZIndex("tooltips"),
            GEX_TOOLTIP_SOURCE_HISTORY,
          );
        }
      }
      ctx.restore();
    }

    function drawGexSplitProfile(ctx, gex, rows, snapshot, visible, pad, priceH, width, y, title = "GEX PROFILE", levels = null, options = {}) {
      const levelRows = Array.isArray(levels) ? levels : (Array.isArray(gex?.levels) ? gex.levels : []);
      const layoutRows = rows
        .map(row => ({ row, level: gexLevelForExactStrike(levelRows, row.strike) }));
      const gutter = options.gutter || gexProfileGutterGeometry(pad, width);
      const panelW = gutter.panelW;
      const panelLeft = gutter.panelLeft;
      const centerW = clamp(panelW * 0.25, 40, 54);
      const centerX = panelLeft + panelW * 0.5;
      const labelLeft = centerX - centerW * 0.5;
      const labelRight = centerX + centerW * 0.5;
      const leftBarBaseX = labelLeft - 4;
      const rightBarBaseX = labelRight + 4;
      const sideW = Math.max(18, Math.min(labelLeft - panelLeft - 12, panelLeft + panelW - labelRight - 12));
      const panelTop = 2;
      const panelH = Math.max(pad.top + priceH - 4, 48);
      const sidebarOpacity = 0.58;
      const sideScaleMax = Math.max(1e-9, ...layoutRows.map(item => Math.max(
        Math.abs(gexProfileSideValue(item.row, "call") ?? 0),
        Math.abs(gexProfileSideValue(item.row, "put") ?? 0),
        Math.abs(gexProfileSideValue(item.row, "net") ?? 0) * 0.35,
      )));
      ctx.save();
      ctx.fillStyle = state.settings.theme === "light" ? `rgba(255,255,255,${sidebarOpacity})` : `rgba(2,6,23,${sidebarOpacity})`;
      ctx.strokeStyle = state.settings.theme === "light" ? "rgba(30,41,59,0.18)" : "rgba(148,163,184,0.18)";
      ctx.lineWidth = 1;
      roundedRectPath(ctx, panelLeft, panelTop, panelW, panelH, 5);
      ctx.fill();
      ctx.stroke();
      ctx.strokeStyle = state.settings.theme === "light" ? "rgba(15,23,42,0.18)" : "rgba(226,232,240,0.14)";
      ctx.beginPath();
      ctx.moveTo(centerX - centerW * 0.5, panelTop + 6);
      ctx.lineTo(centerX - centerW * 0.5, panelTop + panelH - 6);
      ctx.moveTo(centerX + centerW * 0.5, panelTop + 6);
      ctx.lineTo(centerX + centerW * 0.5, panelTop + panelH - 6);
      ctx.stroke();
      if (!layoutRows.length) {
        ctx.restore();
        return;
      }
      ctx.textAlign = "left";
      ctx.textBaseline = "top";
      ctx.font = "800 9px -apple-system, BlinkMacSystemFont, sans-serif";
      ctx.fillStyle = state.settings.theme === "light" ? "rgba(15,23,42,0.72)" : "rgba(226,232,240,0.72)";
      ctx.fillText(title, panelLeft + 8, panelTop + 8);
      ctx.font = `800 9px ${CANVAS_MONO_FONT}`;
      ctx.textBaseline = "middle";
      for (const item of layoutRows) {
        const { row, level } = item;
        const expiryScoped = Boolean(row?.expiry);
        const yy = gexNullableNumber(row.profile_y) ?? y(row.strike);
        const callValue = gexProfileSideValue(row, "call");
        const putValue = gexProfileSideValue(row, "put");
        const netSign = gexGammaSign(row);
        const callMagnitude = Math.abs(callValue ?? 0);
        const putMagnitude = Math.abs(putValue ?? 0);
        const hasSideValue = callMagnitude > 0 || putMagnitude > 0;
        const dominantSide = callMagnitude >= putMagnitude ? "call" : "put";
        const motionSource = expiryScoped ? row : (level || row);
        const color = hasSideValue
          ? (dominantSide === "call" ? gexProfileSideColor("call", 0.88) : gexProfileSideColor("put", 0.88))
          : (state.settings.theme === "light" ? "rgba(71,85,105,0.82)" : "rgba(148,163,184,0.78)");
        const callNorm = clamp(callMagnitude / sideScaleMax, 0, 1);
        const putNorm = clamp(putMagnitude / sideScaleMax, 0, 1);
        const rowNorm = Math.max(callNorm, putNorm, gexNullableNumber(row.profile_ratio) ?? 0);
        const barH = clamp(5 + rowNorm * 9, 5, 14);
        if (netSign < 0) {
          const negativeRowTop = Math.max(panelTop + 1, yy - 11);
          const negativeRowBottom = Math.min(panelTop + panelH - 1, yy + 11);
          if (negativeRowBottom > negativeRowTop) {
            ctx.fillStyle = themeColor(
              "amber",
              state.settings.theme === "light" ? 0.18 : 0.22,
            );
            ctx.fillRect(
              panelLeft + 2,
              negativeRowTop,
              panelW - 4,
              negativeRowBottom - negativeRowTop,
            );
          }
        }
        const sideBars = [
          {
            side: "put",
            value: putValue,
            magnitude: putMagnitude,
            normalized: putNorm,
            baseX: leftBarBaseX,
            sign: -1,
            color: gexProfileSideColor("put", 0.88),
            align: "right",
            motionEvent: motionSource && typeof gexMotionEventForLevel === "function" ? gexMotionEventForLevel(motionSource, "put") : null,
          },
          {
            side: "call",
            value: callValue,
            magnitude: callMagnitude,
            normalized: callNorm,
            baseX: rightBarBaseX,
            sign: 1,
            color: gexProfileSideColor("call", 0.88),
            align: "left",
            motionEvent: motionSource && typeof gexMotionEventForLevel === "function" ? gexMotionEventForLevel(motionSource, "call") : null,
          },
        ];
        for (const sideBar of sideBars) {
          drawCanvasBidirectionalLadderSide(ctx, sideBar, yy, { sideW, panelLeft, panelW, barH }, {
            formatValue: value => gexDollarText(value),
            minLabelNorm: 0.20,
            motionFrameMs: GEX_MOTION_FRAME_MS,
          });
        }
        ctx.fillStyle = state.settings.theme === "light" ? "rgba(241,245,249,0.92)" : "rgba(15,23,42,0.92)";
        ctx.strokeStyle = rgbaFromCssColor(color, clamp(0.16 + row.profile_ratio * 0.38, 0.16, 0.54));
        const typeLabel = gexLevelTypeLabel(level, row);
        const powerLabel = level ? gexLevelPowerDisplay(level, gexLevelStrengthValue(level)) : "";
        const structureLabel = `${typeLabel} ${powerLabel || Math.round(row.profile_ratio * 100)}`.trim();
        const processDisplay = gexProfileProcessDisplay(expiryScoped ? null : level, row);
        const processLabel = expiryScoped
          ? compactGexExpiry(row.expiry)
          : processDisplay.label || gexOptionVolumeCompactLabel(motionSource);
        const processAgeMs = Date.now() - Number(processDisplay.changedAt || 0);
        const processPulse = processDisplay.active ? clamp(1 - processAgeMs / GEX_PROFILE_PROCESS_PULSE_MS, 0, 1) : 0;
        roundedRectPath(ctx, labelLeft, yy - 12, centerW, 24, 3);
        if (processDisplay.active) {
          ctx.fillStyle = rgbaFromCssColor(color, clamp(0.16 + processPulse * 0.20, 0.16, 0.36));
        }
        ctx.fill();
        if (processDisplay.held) ctx.setLineDash([3, 2]);
        ctx.strokeStyle = rgbaFromCssColor(color, clamp(0.28 + row.profile_ratio * 0.32 + processPulse * 0.34, 0.28, 0.88));
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.textAlign = "center";
        ctx.fillStyle = rgbaFromCssColor(color, processDisplay.held ? 0.82 : 0.96);
        ctx.font = "900 8.5px -apple-system, BlinkMacSystemFont, sans-serif";
        ctx.fillText(structureLabel, centerX, yy - 3, centerW - 4);
        ctx.font = "900 8px -apple-system, BlinkMacSystemFont, sans-serif";
        ctx.fillText(processLabel, centerX, yy + 7);
        const netMotionEvent = motionSource && typeof gexMotionEventForLevel === "function" ? gexMotionEventForLevel(motionSource, "net") : null;
        if (netMotionEvent) drawCanvasMotionGlyph(ctx, netMotionEvent, centerX + centerW * 0.5 + 8, yy, color, { align: "left", frameMs: Number(netMotionEvent?.frameMs) || GEX_MOTION_FRAME_MS });
        queueCanvasTooltipFront(
          options.tooltipChart || "price",
          centerX,
          yy,
          panelW,
          Math.max(barH, 18),
          () => gexProfileRowTooltip(gex, row, level, title, row.profile_ratio),
          null,
          canvasLayerZIndex("tooltips"),
          GEX_TOOLTIP_SOURCE_FRONT,
        );
      }
      ctx.restore();
    }

    function drawGexProfileCompanion(ctx, snapshot, visible, pad, priceH, width, y, options = {}) {
      if (!options.gutter && typeof gexSidebarIsDocked === "function" && gexSidebarIsDocked()) return;
      const cfg = state.indicators.gexContext;
      if (!gexLayerVisible() || !cfg.profile) {
        scheduleGexProfileFadeFrame(null);
        scheduleGexMotionAnimation();
        return;
      }
      const gex = activeGexContext(snapshot);
      if (!gex) return;
      if (pruneGexMotionEvents()) scheduleGexMotionAnimation();
      const levels = gexVisibleLevels(gex.levels || [], cfg);
      const profileMode = gexProfileMode(cfg.profileStyle);
      const includeEmptyRows = profileMode === "ladder" || profileMode === "expiry";
      const levelRows = profileMode === "expiry"
        ? []
        : gexProfileGeometryRows(levels, y, pad, priceH, {
          includeEmpty: includeEmptyRows,
        });
      const fadeAlpha = gexProfileFadeAlpha(gex);
      scheduleGexProfileFadeFrame(gex);
      ctx.save();
      ctx.globalAlpha *= fadeAlpha;
      if (profileMode === "ladder") {
        drawGexSplitProfile(ctx, gex, levelRows, snapshot, visible, pad, priceH, width, y, "GEX LEVELS", levels, options);
      } else if (profileMode === "expiry") {
        const expiryProfile = gexExpiryRowsForVisibleStrikes(gex.expiry_profile, levels);
        if (expiryProfile.length) {
          const expiryRows = gexProfileGeometryRows(expiryProfile, y, pad, priceH, {
            includeEmpty: includeEmptyRows,
          });
          drawGexSplitProfile(ctx, gex, expiryRows, snapshot, visible, pad, priceH, width, y, "GEX EXPIRY", levels, options);
          if (expiryRows.length && !options.gutter) {
            drawGexPriceProfileMarkers(ctx, gex, expiryRows, pad, priceH, width, y, "expiry", levels);
          }
        }
      } else if (levels.length) {
        drawGexMiniProfile(ctx, gex, levelRows, levels, snapshot, pad, priceH, width, y, cfg, options);
      }
      ctx.restore();
    }

    function renderGexDockFromGeometry(snapshot, geometry) {
      if (!gexSidebarIsDocked() || !workspaceDockSurfaceIsActive("gex")) return;
      const host = document.getElementById("gex-dock-profile");
      const surface = document.getElementById("gex-dock-surface");
      const priceFrame = document.getElementById("price-frame");
      if (!host || !surface || !priceFrame) return;
      const priceRect = priceFrame.getBoundingClientRect();
      host.style.top = `${priceRect.top - surface.getBoundingClientRect().top}px`;
      host.style.height = `${priceRect.height}px`;
      const { ctx, w, h } = canvasContext("gex-dock-canvas");
      ctx.clearRect(0, 0, w, h);
      resetCanvasTooltips("gex-dock");
      state.tooltipFront = state.tooltipFront || {};
      state.tooltipFront["gex-dock"] = [];
      if (!snapshot?.bars?.length || !geometry) return;
      const { visible, pad, priceH, y } = geometry;
      const dockPad = { ...pad, left: CHART_LEFT_PAD, right: 2 };
      const gutter = { panelLeft: 2, panelW: Math.max(0, w - 4), reserve: 0 };
      ctx.save();
      ctx.beginPath();
      ctx.rect(0, 0, w, h);
      ctx.clip();
      drawGexProfileCompanion(ctx, snapshot, visible, dockPad, priceH, w, y, {
        gutter,
        tooltipChart: "gex-dock",
      });
      if (gexLayerVisible()) drawGexDynamicsSidebarBadge(ctx, activeGexContext(snapshot), dockPad, w, snapshot, {
        panelRight: w - 2,
        tooltipChart: "gex-dock",
      });
      ctx.restore();
      flushCanvasTooltipFront("gex-dock");
    }
