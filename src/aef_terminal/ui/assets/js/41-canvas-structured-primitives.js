    function canvasMotionGlyphText(event) {
      const type = typeof event?.type === "string" ? event.type : "";
      const side = ["call", "put", "net"].includes(event?.side) ? event.side : "";
      if (type === "strengthening") return side === "put" ? "<<<" : ">>>";
      if (type === "weakening") return side === "put" ? "<<<" : side === "call" ? ">>>" : "<<<";
      if (type === "sign_flip_positive") return "+++";
      if (type === "sign_flip_negative") return "---";
      if (type === "roll_up") return "↑";
      if (type === "roll_down") return "↓";
      if (type === "live_activity_call") return ">>>";
      if (type === "live_activity_put") return "<<<";
      if (type === "live_activity_two_sided") return "<>";
      if (type === "live_approach_call") return ">>>";
      if (type === "live_approach_put") return "<<<";
      if (type === "live_approach") return "!!!";
      if (type === "live_cross_call") return ">>>";
      if (type === "live_cross_put") return "<<<";
      if (type === "live_cross") return "!!!";
      if (type === "live_near") return "<>";
      if (type === "live_two_sided") return "===";
      return "";
    }

    function drawCanvasMotionGlyph(ctx, event, x, y, color, options = {}) {
      const glyph = canvasMotionGlyphText(event);
      if (!glyph) return;
      const frameMs = Number(options.frameMs) || 120;
      const elapsed = Math.max(0, Date.now() - Number(event.startedAt || 0));
      const side = ["call", "put", "net"].includes(event?.side)
        ? event.side
        : ["call", "put", "net"].includes(options.side) ? options.side : "";
      const optionOutward = Number(options.outwardSign);
      const outward = Number.isFinite(optionOutward) && optionOutward !== 0
        ? Math.sign(optionOutward)
        : side === "put" ? -1 : side === "call" ? 1 : glyph === "<<<" ? -1 : glyph === ">>>" ? 1 : 0;
      const phase = (elapsed % 900) / 900;
      const activeBase = Math.floor(elapsed / Math.max(frameMs, 1)) % glyph.length;
      const activeIndex = outward < 0 ? glyph.length - 1 - activeBase : activeBase;
      const travel = outward === 0 ? 0 : phase * 5 * outward;
      const align = String(options.align || "left");
      ctx.save();
      ctx.textAlign = align;
      ctx.textBaseline = "middle";
      let cursor = x + travel;
      const step = Number(options.step) || 6;
      if (align === "right") cursor -= step * (glyph.length - 1);
      for (let index = 0; index < glyph.length; index += 1) {
        ctx.font = index === activeIndex ? `1000 10px ${CANVAS_MONO_FONT}` : `650 9px ${CANVAS_MONO_FONT}`;
        ctx.fillStyle = rgbaFromCssColor(color, index === activeIndex ? 1.0 : 0.58);
        ctx.fillText(glyph[index], cursor + index * step, y);
      }
      ctx.restore();
    }

    function drawCanvasBidirectionalLadderSide(ctx, sideBar, y, geometry, options = {}) {
      const magnitudeFact = typeof sideBar.magnitude === "number" && Number.isFinite(sideBar.magnitude)
        ? sideBar.magnitude
        : typeof sideBar.value === "number" && Number.isFinite(sideBar.value)
          ? sideBar.value
          : null;
      const normalized = typeof sideBar.normalized === "number" && Number.isFinite(sideBar.normalized)
        ? sideBar.normalized
        : null;
      const magnitude = Math.abs(magnitudeFact ?? 0);
      if (!(magnitude > 0) || normalized === null || !(normalized > 0)) return;
      const sideW = Number(geometry.sideW) || 0;
      const panelLeft = Number(geometry.panelLeft) || 0;
      const panelW = Number(geometry.panelW) || 0;
      const barH = Number(geometry.barH) || 8;
      const barW = Math.max(3, sideW * normalized);
      const barX = sideBar.sign > 0 ? sideBar.baseX : sideBar.baseX - barW;
      const alpha = clamp(0.28 + normalized * 0.50, 0.28, 0.78);
      ctx.fillStyle = rgbaFromCssColor(sideBar.color, alpha);
      ctx.fillRect(barX, y - barH * 0.5, barW, barH);
      ctx.strokeStyle = rgbaFromCssColor(sideBar.color, clamp(0.24 + normalized * 0.52, 0.24, 0.76));
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(sideBar.baseX, y);
      ctx.lineTo(sideBar.baseX + sideBar.sign * barW, y);
      ctx.stroke();
      if (sideBar.motionEvent) {
        const motionX = sideBar.baseX + sideBar.sign * (barW + 7);
        const frameMs = Number(sideBar.motionEvent?.frameMs) || options.motionFrameMs;
        drawCanvasMotionGlyph(ctx, sideBar.motionEvent, motionX, y, sideBar.color, { align: sideBar.align, frameMs, outwardSign: sideBar.sign });
      }
      if (normalized < Number(options.minLabelNorm || 0.20)) return;
      const sideLabel = typeof options.formatValue === "function"
        ? options.formatValue(sideBar.value)
        : Math.round(sideBar.value).toLocaleString("en-US");
      ctx.font = `900 8px ${CANVAS_MONO_FONT}`;
      const labelW = Math.min(ctx.measureText(sideLabel).width + 8, Math.max(sideW - 4, 28));
      const rawLabelX = sideBar.sign > 0 ? sideBar.baseX + Math.max(barW, labelW + 6) * 0.5 : sideBar.baseX - Math.max(barW, labelW + 6) * 0.5;
      const labelX = sideBar.sign > 0
        ? clamp(rawLabelX, sideBar.baseX + labelW * 0.5 + 2, panelLeft + panelW - labelW * 0.5 - 3)
        : clamp(rawLabelX, panelLeft + labelW * 0.5 + 3, sideBar.baseX - labelW * 0.5 - 2);
      ctx.fillStyle = rgbaFromCssColor(sideBar.color, 0.70);
      roundedRectPath(ctx, labelX - labelW * 0.5, y - 6, labelW, 12, 3);
      ctx.fill();
      ctx.textAlign = "center";
      ctx.fillStyle = readableTextForBg(rgbaFromCssColor(sideBar.color, 0.70), css("--text"));
      ctx.fillText(sideLabel, labelX, y + 0.5, labelW - 4);
    }
