from __future__ import annotations

import subprocess
from pathlib import Path


CHANNEL_MASTER_PATH = Path("src/aef_terminal/indicators/modules/channel_master/client.js")
DRAWING_GEOMETRY_PATH = Path("src/aef_terminal/ui/assets/js/60-drawing-geometry.js")


def _function_block(source: str, name: str, next_name: str) -> str:
    return (
        f"function {name}"
        + source.split(f"function {name}", 1)[1].split(
            f"function {next_name}",
            1,
        )[0]
    )


def test_channel_master_uses_typed_decision_anchor_for_live_hint_geometry() -> None:
    channel_master = CHANNEL_MASTER_PATH.read_text(encoding="utf-8")
    geometry = DRAWING_GEOMETRY_PATH.read_text(encoding="utf-8")
    hints = channel_master.split("function drawChannelMasterHints", 1)[1].split(
        "function queueChannelMasterHints",
        1,
    )[0]
    compact_hints = " ".join(hints.split())

    assert 'const analysisTs = String(context?.analysis_ts || "").trim();' in channel_master
    assert 'const decisionAnchorTs = String(context?.decision_anchor_ts || "").trim();' in (
        channel_master
    )
    assert "decisionBarOffset = Number(context?.decision_bar_offset)" in channel_master
    assert 'const decisionAvailableAt = String(context?.decision_available_at || "").trim();' in (
        channel_master
    )
    assert "decisionAnchorTs," in channel_master
    assert "decisionBarOffset," in channel_master
    assert "decisionAvailableAt," in channel_master
    assert "decisionPrice: Number(row.decision_price)" in channel_master
    assert "currentBarTs" not in channel_master
    assert "analysis_slot" not in channel_master
    assert "currentBarSlot" not in channel_master
    assert "barSlotForAbsoluteIndex(axis, axis.allBars.length - 1)" not in channel_master
    assert "centerBarSlot: score.currentBarSlot" not in channel_master
    assert "spanBars: 6" not in channel_master
    assert channel_master.count("labelOnly: true") == 2
    assert "startRatio: 0.815" not in channel_master
    assert "const anchor = channelMasterDecisionAnchor(axis, score);" in compact_hints
    assert "anchorTs: score.decisionAnchorTs" in channel_master
    assert "barOffset: score.decisionBarOffset" in channel_master
    assert "price: score.decisionPrice" in channel_master
    assert "anchorPoint: decisionAnchor" in channel_master
    assert "axis.bars.length - 1" not in hints
    assert "screenIndexForBarSlot(axis, centerBarSlot)" not in geometry
    assert "centerBarSlot" not in geometry
    assert "Math.max(Number(axis.xStep) || 1, 1)" not in geometry


def test_channel_master_client_renders_typed_backend_semantics() -> None:
    source = CHANNEL_MASTER_PATH.read_text(encoding="utf-8")

    assert "const context = state.snapshot?.indicators?.channel_master;" in source
    assert "const channels = context?.channels;" in source
    assert "const nearest = context?.nearest;" in source
    assert "score.isNearest && scoreMatchesRenderedBand" in source
    assert "WAIT · ${Number(score.distanceAtr).toFixed(2)} ATR" in source
    assert "(!score?.near && !options.mark)" in source
    assert "drawArrow: options.drawArrow !== false && score.near" in source
    assert 'const analysisTs = String(context?.analysis_ts || "").trim();' in source
    assert 'const decisionAnchorTs = String(context?.decision_anchor_ts || "").trim();' in source
    assert (
        'const decisionAvailableAt = String(context?.decision_available_at || "").trim();' in source
    )
    assert 'typeof latestConfirmedIndicatorBar === "function"' in source
    assert "latestConfirmedIndicatorBar(state.snapshot)" in source
    assert 'const latestConfirmedTs = String(latestConfirmed?.ts || "").trim();' in source
    assert "latestConfirmedKey !== analysisKey" in source
    assert "decisionAnchorKey !== analysisKey" in source
    assert "currentBarTs" not in source
    assert "analysis_slot" not in source
    assert "barSlotForAbsoluteIndex(axis, axis.allBars.length - 1)" not in source
    assert 'const primaryPath = String(row?.primary_path || "")' in source
    assert 'typeof row.no_fade !== "boolean"' in source
    assert "const direction = modelDirection || score.primaryPath;" in source
    assert "noFade: row.no_fade" in source
    assert "breakScore >= bounceScore" not in source
    assert "rawBreakScore >= 70" not in source
    assert 'const direction = breakPrimary ? "break" : "bounce";' not in source


def test_channel_master_far_nearest_level_renders_wait_mark_without_arrow(
    tmp_path: Path,
) -> None:
    source = CHANNEL_MASTER_PATH.read_text(encoding="utf-8")
    score_source = _function_block(
        source,
        "channelMasterScore",
        "channelMasterTooltip",
    )
    queue_source = _function_block(
        source,
        "queueChannelMasterHints",
        "flushChannelMasterHints",
    )
    flush_source = _function_block(
        source,
        "flushChannelMasterHints",
        "decorateChannelMasterBand",
    )
    decorate_source = (
        "function decorateChannelMasterBand"
        + source.split("function decorateChannelMasterBand", 1)[1].split(
            "let channelMasterAnalysisReloadTimer",
            1,
        )[0]
    )
    script_path = tmp_path / "channel-master-wait-mark.js"
    script_path.write_text(
        "\n".join(
            (
                "const labels = [];",
                "const anchorPoints = [];",
                "const state = {",
                "  drawing: { hidden: false },",
                "  indicators: { channelMaster: { marks: true, tooltips: false, arrows: false } },",
                "  snapshot: { bars: [{ ts: '2026-01-01T12:00:00+00:00' }], indicators: { channel_master: null } },",
                "};",
                "const latestConfirmedIndicatorBar = snapshot => snapshot?.bars?.[snapshot.bars.length - 1] || null;",
                "const timestampKey = value => new Date(value).toISOString();",
                "const pointToScreen = point => { anchorPoints.push(point); return { x: 90, y: 10 }; };",
                "const css = value => value;",
                "const screenLineEndpoints = () => [{ x: 0, y: 10 }, { x: 100, y: 10 }];",
                "const registerCanvasTooltip = () => {};",
                "const drawDrawingSegmentLabel = (_ctx, _axis, _segment, text) => labels.push(text);",
                score_source,
                queue_source,
                flush_source,
                decorate_source,
                "const row = {",
                "  channel_id: 'channel-a', level: 0, label: 'LOW', price: 100,",
                "  decision_price: 100.5,",
                "  break_score: 70, bounce_score: 30, raw_break_score: 70,",
                "  primary_path: 'break', no_fade: false, near: false,",
                "  distance_atr: 0.73, ghost: false, ghost_copy: 0,",
                "  reclaim: false, impulse_atr: 0.2, volume_ratio: 1,",
                "  wick_reject: false, acceptance: false, accepted_bars: 0,",
                "  thin_impulse_probe: false, trend_with_break: true,",
                "  channel_coord: 0.2, coord_distance: 0.2,",
                "  breakout_target: 102, bounce_target: 98,",
                "};",
                "const context = {",
                "  analysis_ts: '2026-01-01T12:00:00+00:00',",
                "  decision_anchor_ts: '2026-01-01T12:00:00+00:00',",
                "  decision_bar_offset: 1, decision_available_at: '2026-01-01T12:05:00+00:00',",
                "  channels: [{ id: 'channel-a', levels: [row] }],",
                "  nearest: row,",
                "};",
                "state.snapshot.indicators.channel_master = context;",
                "const payload = {",
                "  ctx: {}, object: { id: 'channel-a', offset: 10, points: [{}, {}] },",
                "  axis: { bars: [{}] }, toScreen: () => ({}), level: 0, shift: 0,",
                "  ghost: false, lineOffset: 0,",
                "};",
                "let frame = { nearestArrowCandidate: null };",
                "decorateChannelMasterBand(payload, frame);",
                "if (frame.nearestArrowCandidate?.drawArrow !== false) throw new Error('far mark queued an arrow');",
                "flushChannelMasterHints(frame);",
                "if (labels.join('|') !== 'WAIT · 0.73 ATR') throw new Error(`missing WAIT mark: ${labels}`);",
                "labels.length = 0;",
                "context.nearest = { ...row, level: 1 };",
                "frame = { nearestArrowCandidate: null };",
                "decorateChannelMasterBand(payload, frame);",
                "flushChannelMasterHints(frame);",
                "if (labels.length) throw new Error('non-nearest far level rendered a mark');",
                "context.nearest = row;",
                "row.near = true;",
                "frame = { nearestArrowCandidate: null };",
                "decorateChannelMasterBand(payload, frame);",
                "flushChannelMasterHints(frame);",
                "if (labels.join('|') !== 'H-BRK 70') throw new Error(`near mark lost precedence: ${labels}`);",
                "labels.length = 0;",
                "state.snapshot.bars = [{ ts: '2026-01-01T12:05:00+00:00' }];",
                "frame = { nearestArrowCandidate: null };",
                "decorateChannelMasterBand(payload, frame);",
                "flushChannelMasterHints(frame);",
                "if (labels.length) throw new Error(`stale confirmed-ts decision rendered: ${labels}`);",
                "state.snapshot.bars = [{ ts: '2026-01-01T12:00:00+00:00' }];",
                "context.decision_available_at = context.analysis_ts;",
                "if (channelMasterScore(payload.object, payload.axis, 0, 0, false) !== null) throw new Error('non-causal decision rendered');",
                "context.decision_available_at = '2026-01-01T12:05:00+00:00';",
                "context.interaction_model = {",
                "  state: 'ready', reason_code: 'prediction_ready', phase: 'post_touch',",
                "  channel_id: 'channel-a', level: 0, level_price: 100.25,",
                "  near: true, distance_atr: 0.04, breakout_target: 103, bounce_target: 97,",
                "  predicted_class: 'accepted_breakout', decision_state: 'directional', artifact_id: 'model-a',",
                "  probabilities: { reversal: 0.16, accepted_breakout: 0.74, unclear: 0.10, no_touch: 0.0 },",
                "};",
                "const projectedScore = channelMasterScore(payload.object, payload.axis, 0, 0, false);",
                "if (projectedScore.distanceAtr !== 0.04) throw new Error('stale parent distance');",
                "if (projectedScore.breakoutTarget !== 103 || projectedScore.bounceTarget !== 97) throw new Error('stale parent target');",
                "const decisionAnchor = channelMasterDecisionAnchor(payload.axis, projectedScore);",
                "if (!decisionAnchor || anchorPoints.at(-1)?.anchorTs !== context.analysis_ts || anchorPoints.at(-1)?.barOffset !== 1 || anchorPoints.at(-1)?.price !== 100.5) throw new Error('typed decision anchor missing');",
                "frame = { nearestArrowCandidate: null };",
                "decorateChannelMasterBand(payload, frame);",
                "flushChannelMasterHints(frame);",
                "if (labels.join('|') !== 'ML BRK 74% · H-BRK 70') throw new Error(`ML mark missing: ${labels}`);",
            )
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        ["node", str(script_path)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_segment_label_uses_current_decision_anchor_on_clipped_geometry(tmp_path: Path) -> None:
    geometry = DRAWING_GEOMETRY_PATH.read_text(encoding="utf-8")
    channel_master = CHANNEL_MASTER_PATH.read_text(encoding="utf-8")
    clip_source = _function_block(geometry, "clipLineToRect", "drawingClipRect")
    label_source = _function_block(geometry, "drawDrawingSegmentLabel", "getThemeAdjustedColor")
    arrow_source = _function_block(
        channel_master, "drawChannelMasterArrow", "registerChannelMasterArrowTooltip"
    )
    script_path = tmp_path / "channel-master-anchor.js"
    script_path.write_text(
        "\n".join(
            (
                "const PRICE_AXIS_WIDTH = 0;",
                "const clamp = (value, low, high) => Math.min(Math.max(value, low), high);",
                "const isLightTheme = () => false;",
                clip_source,
                label_source,
                arrow_source,
                "function context() {",
                "  return { save() {}, restore() {}, setLineDash() {}, measureText(text) { return { width: text.length * 5 }; },",
                "    fillRect() {}, strokeRect() {}, fillText() {} };",
                "}",
                "function render(width, options = {}) {",
                "  const ctx = context();",
                "  const axis = { width, priceH: 200, pad: { left: 0, right: 0, top: 0 } };",
                "  const segment = [{ x: -20, y: 35 }, { x: width + 20, y: 35 + (width + 40) * 0.25 }];",
                "  return drawDrawingSegmentLabel(ctx, axis, segment, 'BRK 80%', '#0f0', options);",
                "}",
                "function check(condition, message) { if (!condition) throw new Error(message); }",
                "for (const width of [80, 220, 260]) {",
                "  const mark = render(width);",
                "  check(mark !== null, 'clipped label must render');",
                "  check(Math.abs(mark.centerX - (mark.x0 + mark.x1) * 0.5) < 1e-9, 'label must center on clipped x geometry');",
                "  check(Math.abs(mark.centerY - (40 + mark.centerX * 0.25)) < 1e-9, 'midpoint must lie on the channel level');",
                "  check(!Object.hasOwn(mark, 'centerBarSlot'), 'slot authority leaked into label geometry');",
                "  const anchorX = width - 24;",
                "  const anchored = render(width, { anchorPoint: { x: anchorX, y: 40 + anchorX * 0.25 } });",
                "  check(Math.abs(anchored.centerX - anchorX) < 1e-9, 'current decision x must own label placement');",
                "  check(Math.abs(anchored.centerY - (40 + anchorX * 0.25)) < 1e-9, 'anchored label must remain on the channel');",
                "}",
                "const plain = render(220); const legacyOption = render(220, { centerBarSlot: 495, spanBars: 6 });",
                "check(JSON.stringify(plain) === JSON.stringify(legacyOption), 'legacy slot options must have no label authority');",
                "const offscreen = drawDrawingSegmentLabel(context(), { width: 220, priceH: 200, pad: { left: 0, right: 0, top: 0 } }, [{ x: -50, y: 10 }, { x: -10, y: 20 }], 'WAIT', '#0f0');",
                "check(offscreen === null, 'fully offscreen segment must stay hidden');",
                "const blockedArrow = drawChannelMasterArrow({}, { width: 200, pad: { right: 0 }, y: () => 50 }, { breakoutTarget: 101 }, 'break', '#0f0', 1, 195, 50, 5);",
                "check(blockedArrow === null, 'arrow must not reverse when current bar has no right runway');",
            )
        ),
        encoding="utf-8",
    )

    result = subprocess.run(["node", str(script_path)], capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr
