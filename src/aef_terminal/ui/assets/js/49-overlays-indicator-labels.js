    const SIGNAL_LABEL_OFFSET_PX = 10;

    function indicatorDirectionArrow(item = null) {
      const direction = String(item?.direction || "").toLowerCase();
      const absRole = String(item?.absorption_role || "").toLowerCase();
      const side = String(item?.side || item?.anchor || "").toLowerCase();
      if (direction === "short" || absRole === "resistance" || side === "above") return "↓";
      if (direction === "long" || absRole === "support" || side === "below") return "↑";
      return "";
    }

    function indicatorCodeLabel(item = null) {
      if (item?.terminal_climax) return "CLX★";
      if (item?.fuel) return "FUEL";
      const explicit = String(item?.compact_label || "").trim();
      if (explicit) return explicit;
      const code = String(item?.code || item?.state || "").toUpperCase();
      if (code === "POI_LONG" || code === "POI_SHORT") return "POI";
      if (code.startsWith("GRAIL_")) return "GR";
      if (code === "SM_BUY" || code === "SM_SELL") return "SM";
      if (code === "REV_WARN") return "REV";
      if (code === "BREAK_OF_STRUCTURE") return "BOS";
      if (code === "CHANGE_OF_CHARACTER") return "CHO";
      if (code === "SPRING" || /\bSPR/.test(code)) return "SPR";
      if (code === "UPTHRUST" || /\bUT\b/.test(code)) return "UT";
      if (code === "EXH_UP" || code === "EXH_DN" || /\bEXH/.test(code)) return "EXH";
      if (code === "FUEL_UP" || code === "FUEL_DN" || code === "IMP_UP" || code === "IMP_DN" || /IMPULSE|FUEL/.test(code)) return "IMP";
      if (code === "PB_UP" || code === "PB_DN" || /\bPB\d*/.test(code)) return item?.indian_count ? `I${String(item.indian_count).slice(0, 2)}` : `PB${String(item?.pb_stage || "").slice(0, 1)}`;
      if (code === "ABS_LVL") return "LVL";
      if (code === "ABS") return "EFF";
      if (code === "DER_ANOMALY") {
        const triggerCode = String(item?.trigger_event?.code || "").toLowerCase();
        if (triggerCode === "momentum_long_wait_exhaustion") return "SQZ";
        if (triggerCode === "momentum_short_wait_exhaustion") return "LIQ";
        return "DER";
      }
      if (code === "VW_RECLAIM" || code === "VW_REJECT") return "VW";
      if (code === "NO_SUPPLY") return "NS";
      if (code === "NO_DEMAND") return "ND";
      if (/SWEEP/.test(code)) return "SWP";
      if (/^(BRK|FIB)[-\s]?[LS]$/.test(code)) return code.slice(0, 3);
      if (/^OTF/.test(code)) return "OTF";
      const compact = compactLabelToken(code, 3).replace(/[↑↓▲▼]/g, "");
      return compact.slice(0, 3) || "SIG";
    }

    function indicatorGlyph(item = null) {
      const mark = signalIntentMark(item);
      if (mark) return mark;
      const glyph = String(item?.glyph || "").trim();
      return /^[^\w\d\s]{1,4}$/.test(glyph) ? glyph : "";
    }

    function buildIndicatorLabelLines(item = null, options = {}) {
      if (!item) return [];
      const declaredLines = (Array.isArray(item.lines) ? item.lines : [item.lines])
        .map(line => String(line ?? "").trim())
        .filter(Boolean)
        .slice(0, 3);
      if (declaredLines.length) return declaredLines;
      if (!(item.code || item.state || item.fuel || item.terminal_climax)) {
        const declaredLabel = String(item.label || item.event || item.compact_label || "").trim();
        return declaredLabel ? declaredLabel.split(/\r?\n/).map(line => line.trim()).filter(Boolean).slice(0, 3) : [];
      }
      if (item.terminal_climax || item.fuel) {
        const label = volumeStructureText(item.code, item);
        const out = [label || (item.terminal_climax ? "CLX★" : "FUEL")];
        if (options.includeScore && Number.isFinite(Number(item?.score))) out.push(String(Math.round(item.score)));
        return out.slice(0, 3);
      }
      const glyphKind = String(item?.glyph_kind || "").toLowerCase();
      const codeText = String(item.code || item.state || "").toUpperCase();
      const arrow = indicatorDirectionArrow(item);
      if (glyphKind === "absorption_wall" || codeText === "ABS_TRAP") {
        const rawMark = signalIntentMark(item);
        const mark = rawMark.includes(GO_MARK) ? GO_MARK : rawMark.replace(/[↑↓▲▼]/g, "");
        return [mark || arrow, "ABS"].filter(Boolean).slice(0, 2);
      }
      const glyph = indicatorGlyph(item);
      const code = String(options.code || indicatorCodeLabel(item)).slice(0, 3);
      const score = Number(options.score ?? item?.score);
      const first = glyph ? `${glyph}${arrow}` : arrow;
      const out = [];
      if (first) out.push(first);
      if (code) out.push(code);
      if ((options.includeScore || item?.label_score === true) && Number.isFinite(score)) out.push(String(Math.round(score)));
      if (item?.trend_star && !out.includes("★")) out.push("★");
      return out.slice(0, 3);
    }

    function drawIndicatorTextLabel(ctx, anchorX, anchorY, lines, options = {}) {
      if (normalizeIndicatorLabelStyle(state.settings.indicatorLabelStyle) === "box") {
        return drawPointerLabel(ctx, anchorX, anchorY, lines, {
          ...options,
          bg: options.bg || options.color || themeColor("gold", 0.90),
          text: options.text || null,
          minWidth: options.minWidth ?? 24,
          padX: options.padX ?? 4,
          padY: options.padY ?? 3,
          lineH: options.lineH ?? 9,
          font: options.font || "900 9px -apple-system, BlinkMacSystemFont, sans-serif",
        });
      }
      return drawStackedTextMark(ctx, anchorX, anchorY, lines, {
        ...options,
        outline: false,
        lineH: options.lineH ?? 10,
        symbolSize: options.symbolSize ?? 11,
        font: options.font || "900 9px -apple-system, BlinkMacSystemFont, sans-serif",
      });
    }

    function verticalIndicatorTextLines(label = null, options = {}) {
      if (!label || typeof label !== "object") return [];
      const text = String(label.text || "").replace(/[↑↓▲▼★]/g, "").trim();
      const arrow = String(label.arrow || "").trim();
      const rawScore = Number(label.score);
      const includeScore = options.includeScore !== false;
      const score = includeScore && Number.isFinite(rawScore) ? String(Math.min(99, Math.max(0, Math.round(rawScore)))) : "";
      const chars = [...text].filter(char => char.trim());
      const scoreChars = [...score].filter(char => char.trim());
      const full = [arrow, ...chars, ...scoreChars].filter(Boolean);
      const lineH = Math.max(Number(options.lineH) || 8, 1);
      const availableHeight = Number(options.availableHeight);
      const maxLines = Number.isFinite(availableHeight)
        ? Math.max(1, Math.floor(Math.max(availableHeight, lineH) / lineH))
        : full.length;
      if (full.length <= maxLines) return full;
      if (maxLines <= 1) return [arrow || chars[0] || score].filter(Boolean);
      if (maxLines === 2) return [arrow || chars[0], scoreChars[0]].filter(Boolean);
      const reservedScoreLines = Math.min(scoreChars.length, Math.max(0, maxLines - 2));
      const codeSlots = Math.max(1, maxLines - 1 - reservedScoreLines);
      return [arrow, ...chars.slice(0, codeSlots), ...scoreChars.slice(0, reservedScoreLines)].filter(Boolean);
    }

    function drawVerticalIndicatorTextLabel(ctx, anchorX, centerY, label = null, options = {}) {
      const lineH = Math.max(Number(options.lineH) || 8, 1);
      const minY = Number.isFinite(Number(options.minY)) ? Number(options.minY) : -Infinity;
      const maxY = Number.isFinite(Number(options.maxY)) ? Number(options.maxY) : Infinity;
      const availableHeight = Math.max(0, maxY - minY);
      const lines = verticalIndicatorTextLines(label, { lineH, availableHeight, includeScore: options.includeScore });
      if (!lines.length) return null;
      const totalH = Math.max(lineH, lines.length * lineH);
      let boundedCenter = centerY;
      if (Number.isFinite(minY) && Number.isFinite(maxY)) {
        const low = minY + totalH / 2;
        const high = maxY - totalH / 2;
        boundedCenter = low <= high ? clamp(centerY, low, high) : clamp(centerY, minY, maxY);
      }
      const firstY = boundedCenter - (totalH - lineH) / 2;
      const minX = Number.isFinite(Number(options.minX)) ? Number(options.minX) : -Infinity;
      const maxX = Number.isFinite(Number(options.maxX)) ? Number(options.maxX) : Infinity;
      const hitW = Math.max(Number(options.hitWidth) || 14, 1);
      const textX = Number.isFinite(minX) && Number.isFinite(maxX)
        ? clamp(anchorX, minX + hitW / 2, maxX - hitW / 2)
        : anchorX;
      ctx.save();
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.font = options.font || "900 8px -apple-system, BlinkMacSystemFont, sans-serif";
      const rawTextColor = options.text || options.color || css("--text");
      ctx.fillStyle = options.calibrateText === false
        ? rawTextColor
        : calibratedCanvasColor(rawTextColor, { minRatio: 3.6, baseColor: options.textBaseColor });
      const outlineColor = typeof options.outline === "string" ? options.outline : "";
      const arrow = String(label?.arrow || "").trim();
      const baseFont = options.font || "900 8px -apple-system, BlinkMacSystemFont, sans-serif";
      const arrowFont = options.arrowFont || baseFont;
      if (outlineColor) {
        ctx.lineWidth = Number(options.outlineWidth) || 2;
        ctx.strokeStyle = outlineColor;
      }
      lines.forEach((line, lineIndex) => {
        const text = String(line || "");
        const ty = firstY + lineIndex * lineH;
        ctx.font = text === arrow ? arrowFont : baseFont;
        if (outlineColor) ctx.strokeText(text, textX, ty);
        ctx.fillText(text, textX, ty);
      });
      ctx.restore();
      return {
        x: textX - hitW / 2,
        y: firstY - lineH / 2,
        width: hitW,
        height: totalH,
      };
    }

    function indicatorLabelStackStep(lines, lineH = 10) {
      const textLines = Array.isArray(lines) ? lines : [lines];
      return Math.max(1, textLines.length) * Math.max(lineH, 12) + 5;
    }

    function stackedLabelOffset(labelStacks, index, side, lines, baseOffset = SIGNAL_LABEL_OFFSET_PX) {
      if (!labelStacks || index === undefined) return baseOffset;
      const stackSide = side || "center";
      const key = `${index}:${stackSide}`;
      const stackIndex = Number(labelStacks.get(key) || 0);
      labelStacks.set(key, stackIndex + 1);
      const step = indicatorLabelStackStep(lines);
      return baseOffset + stackIndex * step;
    }

    function stackedOverlayLabels(labels, byTs, labelStacks = null) {
      const prepared = [];
      for (const [sequence, item] of labels.entries()) {
        const index = byTs.get(item._ts_key || timestampKey(item.ts));
        const price = Number(item.price);
        if (index === undefined || !Number.isFinite(price)) continue;
        const lines = buildIndicatorLabelLines(item);
        const anchor = item.anchor || item.side;
        const side = anchor === "below" ? "below" : anchor === "center" ? "center" : "above";
        const stackSide = side || "center";
        const key = `${index}:${stackSide}`;
        const stackIndex = labelStacks ? Number(labelStacks.get(key) || 0) : 0;
        if (labelStacks) labelStacks.set(key, stackIndex + 1);
        if (!lines.length) continue;
        const step = indicatorLabelStackStep(lines);
        prepared.push({
          ...item,
          _label_lines: lines,
          _stack_index: stackIndex,
          _stack_sequence: sequence,
          _stack_offset_px: SIGNAL_LABEL_OFFSET_PX + stackIndex * step,
        });
      }
      return prepared.sort((left, right) => {
        const layer = Number(right._stack_index || 0) - Number(left._stack_index || 0);
        return layer || Number(left._stack_sequence || 0) - Number(right._stack_sequence || 0);
      });
    }

    function overlayActionTier(item) {
      const card = item?.action_card && typeof item.action_card === "object" ? item.action_card : null;
      if (card?.tier) return String(card.tier).toLowerCase();
      const role = String(item?.role || item?.kind || "").toLowerCase();
      if (["entry_fill", "target_hit", "stop_hit", "trail_stop_hit", "entry", "stop", "target", "trail", "track"].includes(role)) return "plan";
      if (["go", "arm", "ready", "watch"].includes(String(item?.action || "").toLowerCase())) return "plan";
      return "context";
    }

    function typedIndicatorSignalOverlaySource(item) {
      if (item?.signal_overlay !== true) return "";
      return String(item?.source || item?.signal?.source || "").trim().toLowerCase();
    }

    function latestIndicatorSignalOverlays(overlays) {
      const candidates = [];
      const latestBySource = new Map();
      for (const item of Array.isArray(overlays) ? overlays : []) {
        const type = String(item?.type || "").toLowerCase();
        if (type !== "marker" && type !== "label") continue;
        if (isLifecycleExecutionMarker(item)) continue;
        const source = typedIndicatorSignalOverlaySource(item);
        const timestamp = Date.parse(item?.ts || "");
        if (!source || !Number.isFinite(timestamp)) continue;
        candidates.push({ item, source, timestamp });
        const current = latestBySource.get(source);
        if (current === undefined || timestamp > current) latestBySource.set(source, timestamp);
      }
      return new Set(
        candidates
          .filter(candidate => latestBySource.get(candidate.source) === candidate.timestamp)
          .map(candidate => candidate.item),
      );
    }

    function isLatestIndicatorSignalOverlay(item, latestSignalOverlays = null) {
      return latestSignalOverlays instanceof Set && latestSignalOverlays.has(item);
    }

    function allowOverlayLabelByDensity(
      item,
      bars = null,
      snapshot = null,
      latestSignalOverlays = null,
    ) {
      const barSeries = bars
        || (typeof visibleBars === "function" ? visibleBars(snapshot || state.snapshot)?.bars : null)
        || state.snapshot?.bars
        || [];
      const snap = snapshot || state.snapshot;
      if (isLifecycleExecutionMarker(item)) {
        return allowLifecycleExecutionMarkerByDensity(item, snap);
      }
      const mode = advisorEffectiveSignalDensity();
      if (mode === "research") return true;
      const tier = overlayActionTier(item);
      if (mode === "minimal") return tier === "command";
      const lifecycle = activeIndicatorLifecycle(snap);
      const latestTs = advisorLatestBarTs(barSeries);
      const itemTsKeys = overlayTimestampKeys(item);
      const onLatestBar = Boolean(latestTs && itemTsKeys.includes(latestTs));
      if (lifecycle) {
        const lifecycleSource = String(lifecycle.source || "").toLowerCase();
        const itemSource = String(item?.source || "").toLowerCase();
        const openedTs = timestampKey(lifecycle.opened_ts);
        const onOpenedBar = Boolean(openedTs && itemTsKeys.includes(openedTs));
        if (lifecycleSource && itemSource === lifecycleSource && onOpenedBar) return true;
      }
      if (isLatestIndicatorSignalOverlay(item, latestSignalOverlays)) return true;
      if (lifecycle) return false;
      return onLatestBar && (tier === "command" || tier === "plan");
    }

    function drawStackedTextMark(ctx, anchorX, anchorY, lines, options = {}) {
      const textLines = (Array.isArray(lines) ? lines : [lines]).map(line => String(line)).filter(Boolean);
      if (!textLines.length || !Number.isFinite(anchorX) || !Number.isFinite(anchorY)) return null;
      const side = options.side === "below" ? "below" : "above";
      const offset = Number.isFinite(Number(options.offsetPx)) ? Number(options.offsetPx) : 8;
      const lineH = Number.isFinite(Number(options.lineH)) ? Number(options.lineH) : 10;
      const minX = Number.isFinite(Number(options.minX)) ? Number(options.minX) : 0;
      const maxX = Number.isFinite(Number(options.maxX)) ? Number(options.maxX) : Infinity;
      const minY = Number.isFinite(Number(options.minY)) ? Number(options.minY) : -Infinity;
      const maxY = Number.isFinite(Number(options.maxY)) ? Number(options.maxY) : Infinity;
      const absorptionWall = options.absorptionWall === true;
      const absorptionActionLines = absorptionWall ? textLines.slice(0, 1) : [];
      const absorptionWallDown = String(options.wallDirection || "").toLowerCase() === "down";
      const shieldOnly = textLines.length === 1 && textLines[0] === "🛡";
      const symbolSize = Number.isFinite(Number(options.symbolSize)) ? Number(options.symbolSize) : 20;
      const shieldSize = Number.isFinite(Number(options.shieldSize)) ? Number(options.shieldSize) : 18;
      const absorptionActionH = absorptionActionLines.length ? Math.max(9, symbolSize * 0.52) : 0;
      const totalH = absorptionWall ? symbolSize + absorptionActionH : shieldOnly ? shieldSize : textLines.length * lineH;
      const x0 = clamp(anchorX, minX + 8, maxX - 8);
      const y0 = clamp(side === "below" ? anchorY + offset : anchorY - offset - totalH, minY + 2, maxY - totalH - 2);
      const color = options.color || css("--text");
      const outline = canvasTextOutlineColor();
      ctx.save();
      if (absorptionWall) {
        const s = symbolSize;
        if (absorptionActionLines.length) {
          ctx.font = `800 ${Math.max(11, Math.round(absorptionActionH + 1))}px -apple-system, BlinkMacSystemFont, sans-serif`;
          ctx.textAlign = "center";
          ctx.textBaseline = "top";
          ctx.lineWidth = canvasTextOutlineWidth(3);
          ctx.strokeStyle = outline;
          ctx.fillStyle = color;
          const actionLine = absorptionActionLines[0];
          const mirrorAction = actionLine.includes(GO_MARK) && (absorptionWallDown || shouldMirrorGoMark(textLines, options));
          drawLabelTextLine(ctx, actionLine, x0, y0, mirrorAction, {
            stroke: shouldStrokeCanvasText(),
          });
        }
        const top = y0 + absorptionActionH;
        const drawTop = absorptionWallDown ? top + s : top;
        const yDir = absorptionWallDown ? -1 : 1;
        const left = x0 - s * 0.58;
        const right = x0 + s * 0.58;
        const yy = value => drawTop + yDir * value;
        const baseY = yy(s * 0.84);
        const joint = { x: x0 - s * 0.16, y: baseY };
        const drawGlyph = () => {
          ctx.beginPath();
          ctx.moveTo(left, baseY);
          ctx.lineTo(right, baseY);
          ctx.moveTo(left + s * 0.04, yy(s * 0.20));
          ctx.lineTo(joint.x, joint.y);
          ctx.moveTo(x0 - s * 0.35, yy(s * 0.24));
          ctx.lineTo(x0 - s * 0.20, yy(s * 0.44));
          for (const shift of [0, s * 0.32]) {
            const startX = joint.x + shift;
            const startY = baseY;
            const endX = startX + s * 0.47;
            const endY = yy(s * 0.14);
            ctx.moveTo(startX, startY);
            ctx.lineTo(endX, endY);
            ctx.moveTo(endX, endY);
            ctx.lineTo(endX - s * 0.04, endY + yDir * s * 0.22);
            ctx.moveTo(endX, endY);
            ctx.lineTo(endX - s * 0.22, endY + yDir * s * 0.04);
          }
        };
        ctx.lineCap = "round";
        ctx.lineJoin = "round";
        ctx.strokeStyle = outline;
        ctx.lineWidth = Math.max(1.2, s * 0.245);
        drawGlyph();
        ctx.stroke();
        ctx.strokeStyle = color;
        ctx.lineWidth = Math.max(0.8, s * 0.123);
        drawGlyph();
        ctx.stroke();
        ctx.restore();
        return { x: x0, y: y0 + totalH / 2, width: symbolSize + 12, height: totalH + offset };
      }
      if (shieldOnly) {
        const s = shieldSize;
        const top = y0;
        const cx = x0;
        const halfW = s * 0.44;
        ctx.beginPath();
        ctx.moveTo(cx, top);
        ctx.lineTo(cx + halfW, top + s * 0.18);
        ctx.quadraticCurveTo(cx + halfW * 0.88, top + s * 0.68, cx, top + s);
        ctx.quadraticCurveTo(cx - halfW * 0.88, top + s * 0.68, cx - halfW, top + s * 0.18);
        ctx.closePath();
        ctx.lineWidth = canvasTextOutlineWidth(3);
        ctx.strokeStyle = outline;
        ctx.stroke();
        ctx.fillStyle = color;
        ctx.fill();
        ctx.restore();
        return { x: x0, y: y0 + totalH / 2, width: shieldSize + 6, height: totalH + offset };
      }
      ctx.font = options.font || "800 9px -apple-system, BlinkMacSystemFont, sans-serif";
      ctx.textAlign = "center";
      ctx.textBaseline = "top";
      ctx.lineWidth = canvasTextOutlineWidth(3);
      ctx.strokeStyle = outline;
      ctx.fillStyle = color;
      const mirrorGo = shouldMirrorGoMark(textLines, options);
      textLines.forEach((line, lineIndex) => {
        const py = y0 + lineIndex * lineH;
        drawLabelTextLine(ctx, line, x0, py, mirrorGo, {
          stroke: options.outline !== false && shouldStrokeCanvasText(),
        });
      });
      ctx.restore();
      return { x: x0, y: y0 + totalH / 2, width: 30, height: totalH + offset };
    }
