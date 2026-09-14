function channelMasterScore(object, axis, level, shift = 0, ghost = false) {
  const offset = Number(object?.offset);
  if (!object?.id || !Number.isFinite(offset) || Math.abs(offset) < 0.000001) return null;
  const context = state.snapshot?.indicators?.channel_master;
  const channels = context?.channels;
  const channel = Array.isArray(channels)
    ? channels.find(item => String(item?.id || "") === String(object.id))
    : null;
  const rows = Array.isArray(channel?.levels) ? channel.levels : [];
  const effectiveLevel = Number(level || 0) + Number(shift || 0) / offset;
  const row = rows.find(item => Math.abs(Number(item?.level) - effectiveLevel) <= 0.001);
  const nearest = context?.nearest;
  const interaction = context?.interaction_model;
  const isNearest = (
    String(nearest?.channel_id || "") === String(object.id)
    && Math.abs(Number(nearest?.level) - effectiveLevel) <= 0.001
  );
  const interactionMatches = (
    String(interaction?.channel_id || "") === String(object.id)
    && Math.abs(Number(interaction?.level) - effectiveLevel) <= 0.001
  );
  const modelProbabilities = interactionMatches && interaction?.probabilities
    && typeof interaction.probabilities === "object"
    ? interaction.probabilities
    : null;
  const modelState = interactionMatches
    ? String(interaction?.state || "").trim().toLowerCase()
    : "";
  const modelLevelPrice = Number(interaction?.level_price);
  const modelDistanceAtr = interaction?.distance_atr == null
    ? Number.NaN
    : Number(interaction.distance_atr);
  const modelBreakoutTarget = interaction?.breakout_target == null
    ? Number.NaN
    : Number(interaction.breakout_target);
  const modelBounceTarget = interaction?.bounce_target == null
    ? Number.NaN
    : Number(interaction.bounce_target);
  const primaryPath = String(row?.primary_path || "").trim().toLowerCase();
  const label = String(row?.label || "").trim();
  const analysisTs = String(context?.analysis_ts || "").trim();
  const decisionAnchorTs = String(context?.decision_anchor_ts || "").trim();
  const decisionBarOffset = Number(context?.decision_bar_offset);
  const decisionAvailableAt = String(context?.decision_available_at || "").trim();
  const latestConfirmed = typeof latestConfirmedIndicatorBar === "function"
    ? latestConfirmedIndicatorBar(state.snapshot)
    : null;
  const latestConfirmedTs = String(latestConfirmed?.ts || "").trim();
  const analysisKey = typeof timestampKey === "function" ? timestampKey(analysisTs) : analysisTs;
  const decisionAnchorKey = typeof timestampKey === "function"
    ? timestampKey(decisionAnchorTs)
    : decisionAnchorTs;
  const decisionAvailableKey = typeof timestampKey === "function"
    ? timestampKey(decisionAvailableAt)
    : decisionAvailableAt;
  const latestConfirmedKey = typeof timestampKey === "function"
    ? timestampKey(latestConfirmedTs)
    : latestConfirmedTs;
  const analysisAtMs = Date.parse(analysisTs);
  const decisionAvailableAtMs = Date.parse(decisionAvailableAt);
  if (
    !row
    || !label
    || !analysisTs
    || !decisionAnchorTs
    || !decisionAvailableAt
    || !Number.isSafeInteger(decisionBarOffset)
    || decisionBarOffset < 1
    || !analysisKey
    || !decisionAvailableKey
    || decisionAnchorKey !== analysisKey
    || !Number.isFinite(analysisAtMs)
    || !Number.isFinite(decisionAvailableAtMs)
    || decisionAvailableAtMs <= analysisAtMs
    || (latestConfirmedKey && latestConfirmedKey !== analysisKey)
    || !["break", "bounce"].includes(primaryPath)
    || typeof row.no_fade !== "boolean"
    || !Number.isFinite(Number(row.decision_price))
  ) return null;
  return {
    label,
    isNearest,
    ghost: row.ghost === true,
    ghostCopy: Number(row.ghost_copy || 0),
    decisionAnchorTs,
    decisionBarOffset,
    decisionAvailableAt,
    decisionPrice: Number(row.decision_price),
    levelPrice: interactionMatches && Number.isFinite(modelLevelPrice)
      ? modelLevelPrice
      : Number(row.price),
    breakScore: Number(row.break_score),
    bounceScore: Number(row.bounce_score),
    rawBreakScore: Number(row.raw_break_score),
    primaryPath,
    noFade: row.no_fade,
    near: interactionMatches ? interaction?.near === true : row.near === true,
    reclaim: row.reclaim === true,
    distanceAtr: interactionMatches && Number.isFinite(modelDistanceAtr)
      ? modelDistanceAtr
      : Number(row.distance_atr),
    impulseAtr: Number(row.impulse_atr),
    volumeRatio: Number(row.volume_ratio),
    wickReject: row.wick_reject === true,
    acceptance: row.acceptance === true,
    acceptedBars: Number(row.accepted_bars || 0),
    thinImpulseProbe: row.thin_impulse_probe === true,
    trendWithBreak: row.trend_with_break === true,
    channelCoord: Number(row.channel_coord),
    coordDistance: Number(row.coord_distance),
    breakoutTarget: interactionMatches && Number.isFinite(modelBreakoutTarget)
      ? modelBreakoutTarget
      : Number(row.breakout_target),
    bounceTarget: interactionMatches && Number.isFinite(modelBounceTarget)
      ? modelBounceTarget
      : Number(row.bounce_target),
    modelState,
    modelReasonCode: interactionMatches ? String(interaction?.reason_code || "") : "",
    modelPhase: interactionMatches ? String(interaction?.phase || "") : "",
    modelPredictedClass: interactionMatches
      ? String(interaction?.predicted_class || "")
      : "",
    modelTopClass: interactionMatches ? String(interaction?.top_class || "") : "",
    modelDecisionState: interactionMatches
      ? String(interaction?.decision_state || "")
      : "",
    modelConfidence: Number(interaction?.confidence),
    modelTop2Margin: Number(interaction?.top2_margin),
    modelArtifactId: interactionMatches ? String(interaction?.artifact_id || "") : "",
    modelReversalProbability: Number(modelProbabilities?.reversal),
    modelBreakoutProbability: Number(modelProbabilities?.accepted_breakout),
    modelUnclearProbability: Number(modelProbabilities?.unclear),
    modelNoTouchProbability: Number(modelProbabilities?.no_touch),
  };
}

function channelMasterDecisionAnchor(axis, score) {
  if (
    !axis
    || !score?.decisionAnchorTs
    || !Number.isSafeInteger(score.decisionBarOffset)
    || score.decisionBarOffset < 1
    || !Number.isFinite(score.decisionPrice)
  ) return null;
  return pointToScreen(
    {
      anchorTs: score.decisionAnchorTs,
      barOffset: score.decisionBarOffset,
      price: score.decisionPrice,
    },
    axis,
  );
}

function channelMasterTooltip(score, path = "") {
  if (!score) return "";
  const pathTitle = path === "break"
    ? `Heuristic break path H${score.breakScore}`
    : path === "bounce"
      ? `Heuristic bounce path H${score.bounceScore}`
      : "";
  const modelReady = (
    score.modelState === "ready"
    && Number.isFinite(score.modelReversalProbability)
    && Number.isFinite(score.modelBreakoutProbability)
    && Number.isFinite(score.modelUnclearProbability)
    && Number.isFinite(score.modelNoTouchProbability)
  );
  const modelLine = modelReady
    ? `ML ${String(score.modelPhase || "").replaceAll("_", " ").toUpperCase()} · Break ${Math.round(score.modelBreakoutProbability * 100)}% / Bounce ${Math.round(score.modelReversalProbability * 100)}% / No touch ${Math.round(score.modelNoTouchProbability * 100)}% / Ambiguous ${Math.round(score.modelUnclearProbability * 100)}%${score.modelDecisionState === "abstain" ? ` · ABSTAIN (confidence ${Math.round(score.modelConfidence * 100)}%, margin ${Math.round(score.modelTop2Margin * 100)}%)` : ""}`
    : score.modelState
      ? `ML ${score.modelState.toUpperCase()} · ${String(score.modelReasonCode || "no model").replaceAll("_", " ")}`
      : "ML unavailable · no confirmed 1m interaction";
  return [
    "Channel Master",
    pathTitle,
    `${score.label} ${fmt(score.levelPrice)}`,
    score.ghost ? `Wave ghost copy ${score.ghostCopy}` : "",
    modelLine,
    `Heuristic H-break ${score.breakScore} / H-bounce ${score.bounceScore} (not a probability)`,
    `Heuristic approach H${score.rawBreakScore}`,
    score.noFade ? "NO FADE: approach pressure favors break/retest" : "",
    `Primary ${score.primaryPath.toUpperCase()}`,
    `Break target ${fmt(score.breakoutTarget)}`,
    `Bounce target ${fmt(score.bounceTarget)}`,
    `Distance ${Number(score.distanceAtr).toFixed(2)} ATR`,
    `Impulse ${Number(score.impulseAtr).toFixed(2)} ATR / Volume ${Number(score.volumeRatio).toFixed(2)}x`,
    score.acceptance ? "Acceptance behind line" : "",
    score.reclaim ? "Confirmed reclaim" : "",
    score.thinImpulseProbe ? "Thin impulse probe: prefer bounce confirmation" : "",
    score.wickReject ? "Wick rejection active" : "",
    score.trendWithBreak ? "EMA/VWAP supports break" : "EMA/VWAP does not support break",
    Math.abs(Number(score.rawBreakScore) - Number(score.breakScore)) >= 8
      ? `Raw heuristic break H${score.rawBreakScore}`
      : "",
  ].filter(Boolean).join("\n");
}

function drawChannelMasterArrow(
  ctx,
  axis,
  score,
  direction,
  color,
  alpha,
  x0,
  y0,
  xStep,
  lineWidth = 1.5,
) {
  const target = direction === "break" ? score.breakoutTarget : score.bounceTarget;
  if (!Number.isFinite(target)) return null;
  const rightEdge = (
    Number(axis?.width) || x0 + xStep * 8
  ) - (Number(axis?.pad?.right) || PRICE_AXIS_WIDTH) - 8;
  if (!Number.isFinite(rightEdge) || x0 >= rightEdge) return null;
  const x1 = Math.min(rightEdge, x0 + xStep * 8);
  const y1 = axis.y(target);
  if (!Number.isFinite(y1)) return null;
  ctx.save();
  ctx.globalAlpha = alpha;
  ctx.strokeStyle = color;
  ctx.fillStyle = color;
  ctx.lineWidth = clamp(Number(lineWidth) || 1.5, 1, 3);
  ctx.setLineDash(direction === "break" ? [6, 5] : [2, 5]);
  ctx.beginPath();
  ctx.moveTo(x0, y0);
  ctx.lineTo(x1, y1);
  ctx.stroke();
  ctx.setLineDash([]);
  const angle = Math.atan2(y1 - y0, x1 - x0);
  ctx.beginPath();
  ctx.moveTo(x1, y1);
  ctx.lineTo(
    x1 - Math.cos(angle - 0.55) * 8,
    y1 - Math.sin(angle - 0.55) * 8,
  );
  ctx.lineTo(
    x1 - Math.cos(angle + 0.55) * 8,
    y1 - Math.sin(angle + 0.55) * 8,
  );
  ctx.closePath();
  ctx.fill();
  ctx.restore();
  return {
    x0,
    y0,
    x1,
    y1,
    midX: (x0 + x1) / 2,
    midY: (y0 + y1) / 2,
  };
}

function registerChannelMasterArrowTooltip(arrow, score, direction) {
  if (!arrow || !score || state.indicators?.channelMaster?.tooltips === false) return;
  const width = Math.max(Math.abs(arrow.x1 - arrow.x0), 32);
  const height = Math.max(Math.abs(arrow.y1 - arrow.y0), 32);
  registerCanvasTooltip(
    "price",
    arrow.midX,
    arrow.midY,
    width,
    height,
    () => channelMasterTooltip(score, direction),
    null,
    canvasLayerZIndex("tooltips") + 8,
  );
  registerCanvasTooltip(
    "price",
    arrow.x1,
    arrow.y1,
    34,
    34,
    () => channelMasterTooltip(score, direction),
    null,
    canvasLayerZIndex("tooltips") + 9,
  );
}

function drawChannelMasterHints(ctx, axis, score) {
  if (!score?.near || !axis?.bars?.length) return;
  const anchor = channelMasterDecisionAnchor(axis, score);
  const left = Number(axis?.pad?.left) || 0;
  const rightPad = Number.isFinite(Number(axis?.pad?.right))
    ? Number(axis.pad.right)
    : PRICE_AXIS_WIDTH;
  const right = Number(axis?.width) - rightPad;
  const top = Number(axis?.pad?.top) || 0;
  const bottom = top + (
    Number.isFinite(Number(axis?.priceH)) ? Number(axis.priceH) : 0
  );
  const x0 = Number(anchor?.x);
  const y0 = Number(anchor?.y);
  if (!Number.isFinite(x0) || !Number.isFinite(y0)) return;
  if (x0 < left || x0 > right || y0 < top || y0 > bottom) return;
  const modelDirection = (
    score.modelState === "ready"
    && score.modelDecisionState === "directional"
    && score.modelPredictedClass === "accepted_breakout"
    && Number.isFinite(score.modelBreakoutProbability)
  )
    ? "break"
    : (
      score.modelState === "ready"
      && score.modelDecisionState === "directional"
      && score.modelPredictedClass === "reversal"
      && Number.isFinite(score.modelReversalProbability)
    )
      ? "bounce"
      : "";
  const direction = modelDirection || score.primaryPath;
  const arrowScore = modelDirection === "break"
    ? score.modelBreakoutProbability * 100
    : modelDirection === "bounce"
      ? score.modelReversalProbability * 100
      : direction === "break"
        ? score.breakScore
        : score.bounceScore;
  const color = direction === "break" ? css("--green") : css("--red");
  const alpha = direction === "break"
    ? clamp(arrowScore / 140, 0.18, 0.58)
    : clamp(arrowScore / 150, 0.16, 0.50);
  const arrow = drawChannelMasterArrow(
    ctx,
    axis,
    score,
    direction,
    color,
    alpha,
    x0,
    y0,
    axis.xStep || 8,
    3,
  );
  registerChannelMasterArrowTooltip(
    arrow,
    score,
    modelDirection ? "" : direction,
  );
}

function queueChannelMasterHints(frameState, ctx, axis, score, options = {}) {
  if (
    !frameState
    || !axis?.bars?.length
    || (!score?.near && !options.mark)
  ) return;
  const distance = Math.abs(Number(score.distanceAtr));
  if (!Number.isFinite(distance)) return;
  const current = frameState.nearestArrowCandidate;
  if (current && distance > current.distance + 0.000001) return;
  if (
    current
    && Math.abs(distance - current.distance) <= 0.000001
    && (current.mark || !options.mark)
  ) return;
  frameState.nearestArrowCandidate = {
    ctx,
    axis,
    score,
    distance,
    drawArrow: options.drawArrow !== false && score.near,
    mark: options.mark || null,
  };
}

function flushChannelMasterHints(frameState) {
  const candidate = frameState?.nearestArrowCandidate;
  if (frameState) frameState.nearestArrowCandidate = null;
  if (!candidate) return;
  if (candidate.mark && typeof drawDrawingSegmentLabel === "function") {
    drawDrawingSegmentLabel(
      candidate.ctx,
      candidate.axis,
      candidate.mark.segment,
      candidate.mark.text,
      candidate.mark.color,
      candidate.mark.options,
    );
  }
  if (candidate.drawArrow) {
    drawChannelMasterHints(candidate.ctx, candidate.axis, candidate.score);
  }
}

function decorateChannelMasterBand(payload, frameState) {
  const {
    ctx,
    object,
    axis,
    toScreen,
    level,
    shift,
    ghost,
    lineOffset,
  } = payload || {};
  const config = state.indicators?.channelMaster || {};
  if (state.drawing?.hidden) return;
  const score = channelMasterScore(object, axis, level, shift, ghost);
  const segment = screenLineEndpoints(
    object?.points?.[0],
    object?.points?.[1],
    axis,
    toScreen,
    object?.extendRight !== false,
    lineOffset,
    object?.anchorProjection,
  );
  if (!score || !segment) return;
  const offset = Math.max(Math.abs(Number(object.offset) || 0), 0.000001);
  const renderedGhostCopy = ghost
    ? Math.max(1, Math.round(Math.abs(Number(shift) || 0) / offset))
    : 0;
  const scoreMatchesRenderedBand = (
    (!score.ghost && !ghost)
    || (
      score.ghost
      && ghost
      && Number(score.ghostCopy) === renderedGhostCopy
    )
  );
  const suppressArrowForModelAbstention = (
    score.modelState === "ready"
    && score.modelDecisionState === "abstain"
  );
  const decisionAnchor = channelMasterDecisionAnchor(axis, score);
  if (!decisionAnchor) return;
  let mark = null;
  if (config.marks !== false && score.near && scoreMatchesRenderedBand) {
    const modelProbabilities = {
      accepted_breakout: score.modelBreakoutProbability,
      reversal: score.modelReversalProbability,
      unclear: score.modelUnclearProbability,
      no_touch: score.modelNoTouchProbability,
    };
    const modelProbabilitiesReady = (
      score.modelState === "ready"
      && Object.values(modelProbabilities).every(Number.isFinite)
    );
    const modelDirectional = (
      modelProbabilitiesReady
      && score.modelDecisionState === "directional"
      && ["accepted_breakout", "reversal"].includes(
        score.modelPredictedClass,
      )
    );
    const modelAbstains = (
      modelProbabilitiesReady && score.modelDecisionState === "abstain"
    );
    const modelClass = modelDirectional ? score.modelPredictedClass : "";
    const markerColor = modelClass === "accepted_breakout"
      ? css("--green")
      : modelClass === "reversal"
        ? css("--red")
        : modelClass === "unclear"
          ? css("--muted")
          : modelAbstains
            ? css("--muted")
            : score.noFade
            ? css("--gold")
            : score.primaryPath === "break"
              ? css("--green")
              : css("--red");
    const markerScore = score.noFade
      ? score.rawBreakScore
      : score.primaryPath === "break"
        ? score.breakScore
        : score.bounceScore;
    const heuristicMode = score.noFade
      ? "NO FADE"
      : score.primaryPath === "break"
        ? "H-BRK"
        : "H-BNC";
    const modelMode = modelClass === "accepted_breakout"
      ? "ML BRK"
      : modelClass === "reversal"
        ? "ML BNC"
        : "ML ?";
    const markerGhost = score.ghost ? `G${score.ghostCopy} ` : "";
    const modelPercent = modelDirectional
      ? Math.round(Number(modelProbabilities[modelClass]) * 100)
      : modelAbstains
        ? Math.round(Number(score.modelConfidence) * 100)
      : null;
    mark = {
      segment,
      text: modelDirectional || modelAbstains
        ? `${markerGhost}${modelMode} ${modelPercent}% · ${heuristicMode} ${Math.round(markerScore)}`
        : `${markerGhost}${heuristicMode} ${Math.round(markerScore)}`,
      color: markerColor,
      options: {
        anchorPoint: decisionAnchor,
        labelOnly: true,
      },
    };
  } else if (config.marks !== false && score.isNearest && scoreMatchesRenderedBand) {
    mark = {
      segment,
      text: `WAIT · ${Number(score.distanceAtr).toFixed(2)} ATR`,
      color: css("--muted"),
      options: {
        anchorPoint: decisionAnchor,
        labelOnly: true,
      },
    };
  }
  if (config.tooltips !== false) {
    for (const ratio of [0.22, 0.5, 0.78]) {
      const hitX = segment[0].x + (segment[1].x - segment[0].x) * ratio;
      const hitY = segment[0].y + (segment[1].y - segment[0].y) * ratio;
      registerCanvasTooltip(
        "price",
        hitX,
        hitY,
        132,
        28,
        () => channelMasterTooltip(score),
      );
    }
  }
  if (config.arrows !== false || mark) {
    queueChannelMasterHints(frameState, ctx, axis, score, {
      drawArrow: (
        config.arrows !== false
        && !suppressArrowForModelAbstention
      ),
      mark,
    });
  }
}

let channelMasterAnalysisReloadTimer = null;

function scheduleChannelMasterAnalysisReload(commit = {}) {
  const drawings = Array.isArray(commit.drawings) ? commit.drawings : [];
  const hasChannels = drawings.some(item => item?.type === "channel");
  const snapshotHadChannels = Boolean(
    state.snapshot?.indicators?.channel_master?.count,
  );
  if (!hasChannels && !snapshotHadChannels) return;
  const instrumentId = exactIdentityText(commit.instrumentId);
  const routeFingerprint = exactIdentityText(commit.routeFingerprint);
  const timeframe = String(commit.timeframe || "");
  const scopeKey = String(commit.scopeKey || "");
  const drawingSignature = String(commit.drawingSignature || "");
  if (!instrumentId || !routeFingerprint || !timeframe || !scopeKey || !drawingSignature) return;
  if (channelMasterAnalysisReloadTimer) clearTimeout(channelMasterAnalysisReloadTimer);
  channelMasterAnalysisReloadTimer = setTimeout(() => {
    channelMasterAnalysisReloadTimer = null;
    if (!indicatorCalcForId("channel_master")) return;
    if (
      exactIdentityText(state.instrumentId) !== instrumentId
      || instrumentRouteFingerprint() !== routeFingerprint
      || String(state.timeframe || "") !== timeframe
      || serverPendingDrawingWrites.has(scopeKey)
      || serverQueuedDrawingWrites.has(scopeKey)
      || serverDirtyDrawingScopes.has(scopeKey)
      || serverPersistedDrawingSignatures.get(scopeKey) !== drawingSignature
    ) return;
    load();
  }, 280);
}

registerIndicatorDrawingDecorator("channel_master", {
  begin: () => ({ nearestArrowCandidate: null }),
  decorate: (event, payload, frameState) => {
    if (event === "channel_band") {
      decorateChannelMasterBand(payload, frameState);
    }
  },
  end: (_env, frameState) => {
    flushChannelMasterHints(frameState);
  },
  commit: scheduleChannelMasterAnalysisReload,
});
