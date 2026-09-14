let gexTrailLayerCache = null;
const GEX_TOOLTIP_SOURCE_LEVELS = "gex-levels";
const GEX_TOOLTIP_SOURCE_FRONT = "gex-front";
const GEX_TOOLTIP_SOURCE_HISTORY = "gex-history";
function gexKindClass(levelOrKind) {
      if (!levelOrKind || typeof levelOrKind !== "object") return "unknown";
      const kindClass = levelOrKind.kind_class;
      return typeof kindClass === "string"
        && ["call", "put", "positive_node", "negative_node", "neutral_node", "flip"].includes(kindClass)
        ? kindClass
        : "unknown";
    }
function gexKindColor(kind, alpha = 0.92) {
      const kindClass = gexKindClass(kind);
      if (kindClass === "call" || kindClass === "positive_node") return themeColor("blue", alpha);
      if (kindClass === "put" || kindClass === "negative_node") return themeColor("amber", alpha);
      if (kindClass === "flip") return themeColor("purple", alpha);
      return themeColor("gold", alpha);
    }

    function gexFlowText(value) {
      const raw = Math.abs(gexNullableNumber(value) ?? 0);
      if (!raw) return "";
      if (raw >= 1_000_000_000) return `$${(raw / 1_000_000_000).toFixed(1)}B/pt`;
      if (raw >= 1_000_000) return `$${Math.round(raw / 1_000_000)}M/pt`;
      if (raw >= 1_000) return `$${Math.round(raw / 1_000)}K/pt`;
      if (raw < 0.01) return `$${raw.toExponential(2)}/pt`;
      if (raw < 10) return `$${raw.toFixed(2)}/pt`;
      if (raw < 100) return `$${raw.toFixed(1)}/pt`;
      return `$${Math.round(raw)}/pt`;
    }

    function gexDollarText(value) {
      const raw = gexNullableNumber(value);
      if (raw === null) return "";
      const sign = raw < 0 ? "-" : "";
      const abs = Math.abs(raw);
      if (abs >= 1_000_000_000) return `${sign}$${(abs / 1_000_000_000).toFixed(1)}B`;
      if (abs >= 1_000_000) return `${sign}$${(abs / 1_000_000).toFixed(abs < 10_000_000 ? 1 : 0)}M`;
      if (abs >= 1_000) return `${sign}$${Math.round(abs / 1_000)}K`;
      if (abs === 0) return "$0";
      if (abs < 0.01) return `${sign}$${abs.toExponential(2)}`;
      if (abs < 10) return `${sign}$${abs.toFixed(2)}`;
      if (abs < 100) return `${sign}$${abs.toFixed(1)}`;
      return `${sign}$${Math.round(abs)}`;
    }

    function gexLevelRole(level) {
      const side = ["above", "below", "inside"].includes(level?.spot_side)
        ? level.spot_side
        : "unknown";
      const role = side === "below"
        ? "ниже текущей цены"
        : side === "above"
          ? "выше текущей цены"
          : side === "inside"
            ? "цена на уровне"
            : "модельная GEX-концентрация";
      const action = side === "inside"
        ? "Дождитесь наблюдаемой реакции цены; уровень контекстный, не является самостоятельным входом."
        : side === "unknown"
          ? "Spot недоступен."
          : "Наблюдайте rejection или acceptance; модельный GEX сам по себе не делает уровень support/resistance.";
      return {
        role,
        action,
      };
    }

    function gexGlobalRegimeText(gex) {
      const regime = typeof gex?.global_gamma_regime === "string" ? gex.global_gamma_regime : "UNKNOWN";
      if (regime === "POSITIVE_ESTIMATE") return "G+ EST — модель предполагает положительную net gamma в выбранной near-term цепочке.";
      if (regime === "NEGATIVE_ESTIMATE") return "G- EST — модель предполагает отрицательную net gamma в выбранной near-term цепочке.";
      return "GEX regime UNKNOWN: классификация не опубликована.";
    }

    function gexComparisonScopeText(gex) {
      const scope = gex?.comparison_scope;
      if (!scope || typeof scope !== "object" || Array.isArray(scope)) return "Scope: unknown";
      const expiries = Array.isArray(scope.expiries)
        && scope.expiries.length
        && scope.expiries.every(expiry => typeof expiry === "string" && /^\d{8}$/.test(expiry))
        ? scope.expiries.join(", ")
        : "unknown";
      const seriesCount = Array.isArray(scope.series) ? scope.series.length : null;
      const strikeCount = Number.isInteger(scope.strike_count) && scope.strike_count > 0
        ? scope.strike_count
        : null;
      return [
        `Scope: ${expiries}`,
        seriesCount !== null ? `${seriesCount} qualified series` : "",
        strikeCount !== null ? `${strikeCount} exact selected strikes` : "",
      ].filter(Boolean).join(" · ");
    }

    function gexOpenInterestAsOfLabel(gex) {
      return gexOpenInterestAsOf(gex) === "previous_settlement"
        ? "previous settlement"
        : "unknown";
    }

    function gexFreshnessText(gex) {
      const status = typeof gex?.status === "string" ? gex.status : "";
      if (gexStatusWarn(status)) return "Свежесть: данные неполные/устарели — не торговать по GEX, обновите снимок.";
      return `Свежесть: снимок ${gexSnapshotAgeText(gex)} · OI ${gexOpenInterestAsOfLabel(gex)}.`;
    }

    const GEX_WAIT_CODES = new Set(["WAIT"]);
    const GEX_MIGRATION_CODES = new Set(["MIGRATION"]);

    function gexStatusWarn(status) {
      return status !== "ok";
    }

    function gexDynamicsStructurePresentation(structureState, latest) {
      const stateCode = typeof structureState === "string" ? structureState : "UNKNOWN";
      const motionArrow = motion => motion?.state === "UP" ? "↑" : motion?.state === "DOWN" ? "↓" : "↕";
      const presentation = {
        LEVELS_HELD: {
          compactLabel: "LEVELS HELD",
          compactTendency: "SAME RANGE",
          tooltipLabel: "УРОВНИ УДЕРЖАНЫ",
          tooltipTendency: "повторная работа прежнего диапазона и его ключевых концентраций остаётся базовым сценарием",
          tone: "cyan",
        },
        SHIFT_HIGHER: {
          compactLabel: "MAP SHIFTED ↑",
          compactTendency: "RANGE HIGHER",
          tooltipLabel: "КАРТА СДВИНУТА ↑",
          tooltipTendency: "рабочая карта концентраций сместилась выше; удержание в более высокой части диапазона стало структурно вероятнее",
          tone: "up",
        },
        SHIFT_LOWER: {
          compactLabel: "MAP SHIFTED ↓",
          compactTendency: "RANGE LOWER",
          tooltipLabel: "КАРТА СДВИНУТА ↓",
          tooltipTendency: "рабочая карта концентраций сместилась ниже; удержание в более низкой части диапазона стало структурно вероятнее",
          tone: "down",
        },
        WALLS_WIDENING: {
          compactLabel: "WALLS WIDENED",
          compactTendency: "RANGE WIDER",
          tooltipLabel: "СТЕНЫ РАСШИРЕНЫ",
          tooltipTendency: "расстояние между Call Wall и Put Wall выросло; более широкая амплитуда внутри обновлённой карты стала вероятнее",
          tone: "purple",
        },
        WALLS_NARROWING: {
          compactLabel: "WALLS NARROWED",
          compactTendency: "ROTATION TIGHTER",
          tooltipLabel: "СТЕНЫ СУЖЕНЫ",
          tooltipTendency: "расстояние между Call Wall и Put Wall сократилось; более тесная ротация между стенами стала вероятнее",
          tone: "cyan",
        },
        CALL_WALL_MOVING: {
          compactLabel: `CALL WALL ${motionArrow(latest?.call_wall_motion)}`,
          compactTendency: "NEW CALL ZONE",
          tooltipLabel: `CALL-СТЕНА ${motionArrow(latest?.call_wall_motion)}`,
          tooltipTendency: `Call-концентрация перестроена ${latest?.call_wall_motion?.state === "UP" ? "выше" : "ниже"}; новая Call-зона становится главным контекстом карты`,
          tone: latest?.call_wall_motion?.state === "UP" ? "up" : "down",
        },
        PUT_WALL_MOVING: {
          compactLabel: `PUT WALL ${motionArrow(latest?.put_wall_motion)}`,
          compactTendency: "NEW PUT ZONE",
          tooltipLabel: `PUT-СТЕНА ${motionArrow(latest?.put_wall_motion)}`,
          tooltipTendency: `Put-концентрация перестроена ${latest?.put_wall_motion?.state === "UP" ? "выше" : "ниже"}; новая Put-зона становится главным контекстом карты`,
          tone: latest?.put_wall_motion?.state === "UP" ? "up" : "down",
        },
        GAMMA_FLIP_MOVING: {
          compactLabel: `ZERO GAMMA ${motionArrow(latest?.gamma_flip_motion)}`,
          compactTendency: "BOUNDARY SHIFTED",
          tooltipLabel: `ГРАНИЦА ZERO GAMMA ${motionArrow(latest?.gamma_flip_motion)}`,
          tooltipTendency: `модельная граница Zero Gamma перестроена ${latest?.gamma_flip_motion?.state === "UP" ? "выше" : "ниже"}; обновлённая граница задаёт новый режимный ориентир`,
          tone: "purple",
        },
        MIXED_MIGRATION: {
          compactLabel: "MIXED ROTATION",
          compactTendency: "MIXED BIAS",
          tooltipLabel: "СМЕШАННАЯ РОТАЦИЯ",
          tooltipTendency: "уровни перестраиваются разнонаправленно; карта показывает ротацию без единого структурного направления",
          tone: "gold",
        },
        UNKNOWN: {
          compactLabel: "NO COMPARISON",
          compactTendency: "WAIT FOR PAIR",
          tooltipLabel: "НЕТ СРАВНЕНИЯ",
          tooltipTendency: "для аналитики нужна сопоставимая пара снимков одной цепочки",
          tone: "gold",
        },
      }[stateCode];
      return presentation || null;
    }

    function gexFlowConfirmationText(flowState) {
      const state = typeof flowState === "string" ? flowState : "";
      if (state === "ACTIVE") return "выше RVOL-порога";
      if (state === "QUIET") return "без прироста";
      if (state === "THIN") return "ниже RVOL-порога";
      if (state === "BASELINING") return "формируется baseline";
      if (state === "UNKNOWN") return "нет сравнения";
      return "";
    }

    function gexDynamicsFlowText(latest) {
      const activity = latest?.activity_state && typeof latest.activity_state === "object" ? latest.activity_state : null;
      if (!activity) return gexFlowConfirmationText("UNKNOWN");
      const callState = typeof activity?.call?.state === "string" ? activity.call.state : "UNKNOWN";
      const putState = typeof activity?.put?.state === "string" ? activity.put.state : "UNKNOWN";
      return `Активность · Call ${gexFlowConfirmationText(callState)} · Put ${gexFlowConfirmationText(putState)}`;
    }

    function compactGexMagnitude(value) {
      const raw = Math.abs(gexNullableNumber(value) ?? 0);
      if (!raw) return "";
      const fixed = (number, digits = 1) => {
        const text = Number(number).toFixed(digits);
        return text.endsWith(".0") ? text.slice(0, -2) : text;
      };
      if (raw >= 1_000_000_000) return `${fixed(raw / 1_000_000_000, raw < 10_000_000_000 ? 1 : 0)}B`;
      if (raw >= 1_000_000) return `${fixed(raw / 1_000_000, raw < 10_000_000 ? 1 : 0)}M`;
      if (raw >= 1_000) return `${fixed(raw / 1_000, raw < 10_000 ? 1 : 0)}K`;
      return `${Math.round(raw)}`;
    }

    function gexLevelIntensity(level) {
      if (!level) return "";
      const absGex = gexNullableNumber(level.abs_gex);
      return absGex !== null && Math.abs(absGex) > 0
        ? compactGexMagnitude(absGex)
        : "";
    }

    function gexHistoryTimeLabel(snap) {
      const start = new Date(gexNullableNumber(snap.tsMs) ?? NaN);
      const endMs = gexNullableNumber(snap.valid_until_unix_ms);
      const end = new Date(endMs ?? NaN);
      const time = date => `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
      return Number.isFinite(start.getTime()) && Number.isFinite(end.getTime())
        ? `${time(start)}-${time(end)}`
        : Number.isFinite(start.getTime()) ? time(start) : "GEX";
    }

    function gexHistoryCaptureLabel(snap) {
      const cadence = snap?.capture_mode === "live" ? "STRM" : snap?.capture_mode === "request" ? "RQST" : "?";
      return `${cadence} ${gexMarketDataEntitlementLabel(snap, true)}`;
    }

    function gexDynamicsModeLine(stateCode, latest, structureInfo) {
      const code = typeof stateCode === "string" ? stateCode : "";
      if (GEX_WAIT_CODES.has(code)) {
        const quality = typeof latest?.comparison_quality === "string"
          ? latest.comparison_quality
          : "UNKNOWN";
        if (quality === "SCOPE_CHANGED") return "DATA · BASELINE REBASE";
        return `DATA · ${quality.replaceAll("_", " ")}`;
      }
      if (code === "NEAR") {
        const sides = [...new Set(
          (Array.isArray(latest?.proximities) ? latest.proximities : [])
            .map(item => item?.side)
            .filter(side => side === "CALL" || side === "PUT"),
        )];
        return `TEST ${sides.length ? sides.join("+") : "GEX"} · ${structureInfo.compactTendency}`;
      }
      if (GEX_MIGRATION_CODES.has(code)) return `NEW MAP · ${structureInfo.compactTendency}`;
      return `BASE · ${structureInfo.compactTendency}`;
    }

    function gexDynamicsDetailLabel(detailCode, stateCode) {
      const detail = typeof detailCode === "string" ? detailCode : "";
      if (detail === "need_history") return "нужна история";
      if (detail === "insufficient_structure") return "недостаточно структуры";
      if (detail === "stale") return "сопоставимая пара устарела";
      if (detail === "gap") return "слишком большой интервал";
      if (detail === "scope_changed") return "область цепочки изменилась; baseline сравнения собирается заново";
      if (detail === "invalid") return "снимок не прошёл проверку";
      if (detail === "migration") return "миграция уровней";
      if (detail === "price_near_concentration") return "цена рядом с концентрацией";
      if (detail === "no_key_level_migration") return "ключевые уровни без миграции";
      const code = typeof stateCode === "string" ? stateCode : "";
      if (GEX_WAIT_CODES.has(code)) return "нужна история";
      if (GEX_MIGRATION_CODES.has(code)) return "миграция уровней";
      if (code === "NEAR") return "цена рядом с концентрацией";
      return code === "NO_KEY_LEVEL_MIGRATION" ? "ключевые уровни без миграции" : "состояние неизвестно";
    }

    function gexDynamicsTooltipLine(text, role = "meta", tokenRoles = {}) {
      const line = typeof tooltipLine === "function" ? tooltipLine(text, role) : { text: String(text || ""), role };
      const source = String(text || "");
      const roles = { ...(tokenRoles && typeof tokenRoles === "object" ? tokenRoles : {}) };
      const addRole = (token, roleName) => {
        if (source.includes(token)) roles[token] = roles[token] || roleName;
      };
      ["+GEX"].forEach(token => addRole(token, "positive"));
      ["-GEX"].forEach(token => addRole(token, "negative"));
      ["Zero Gamma", "WATCH", "MIGRATION", "NEAR", "NO KEY LEVEL MIGRATION"].forEach(token => addRole(token, "type"));
      if (Object.keys(roles).length) line.token_roles = roles;
      return line;
    }

    function gexDynamicsTooltipPayload(lines, maxLines = 28) {
      return {
        lines: (lines || []).filter(line => {
          return typeof tooltipLineText === "function" ? tooltipLineText(line).trim() : String(line || "").trim();
        }),
        max_lines: maxLines,
      };
    }

    function gexDynamicsTokenColor(token, fallbackColor) {
      const value = String(token || "");
      if (/^(\+GEX)$/i.test(value)) return themeColor("up", 0.96);
      if (/^(-GEX)$/i.test(value)) return themeColor("down", 0.96);
      if (/^(Zero Gamma|MIGRATION|NEAR)$/i.test(value)) return themeColor("purple", 0.96);
      if (/^(WATCH)$/i.test(value)) return themeColor("cyan", 0.94);
      return fallbackColor;
    }

    function drawGexDynamicsRichText(ctx, text, x, y, maxWidth, baseColor) {
      const source = String(text || "");
      const pattern = /(Zero Gamma|\+GEX|-GEX|WATCH|MIGRATION|NEAR)/g;
      let cursor = 0;
      let drawX = x;
      let match = null;
      const right = x + Math.max(Number(maxWidth) || 0, 0);
      const drawPart = (part, color) => {
        if (!part || drawX >= right) return;
        ctx.fillStyle = color;
        const remaining = Math.max(right - drawX, 0);
        ctx.fillText(part, drawX, y, remaining);
        drawX += Math.min(ctx.measureText(part).width, remaining);
      };
      while ((match = pattern.exec(source)) !== null) {
        drawPart(source.slice(cursor, match.index), baseColor);
        drawPart(match[0], gexDynamicsTokenColor(match[0], baseColor));
        cursor = match.index + match[0].length;
      }
      drawPart(source.slice(cursor), baseColor);
    }

    function gexDynamicsStatus(snapshot, gex) {
      const config = state.indicators?.gexContext;
      if (!config?.dynamicsVisible) return null;
      if (
        !gexContextHasDisplayData(gex)
        || !["ok", "degraded", "stale", "refresh_error"].includes(gex.status)
      ) return null;
      const activeRevision = gexPayloadRevision(gex);
      if (!activeRevision) return null;
      const availabilityStatus = (
        text,
        eventLine,
        message,
        title = "СИНХРОНИЗАЦИЯ",
      ) => {
        const tooltipLines = [
          gexDynamicsTooltipLine(`Динамика GEX · ${title}`, "title"),
          gexDynamicsTooltipLine(message, "meta"),
          gexDynamicsTooltipLine("Неподтверждённые метрики не отображаются.", "meta"),
        ];
        return {
          text,
          eventLine,
          tone: "gold",
          watchTooltip: tooltipLines,
          eventTooltip: tooltipLines,
          tooltip: gexDynamicsTooltipPayload(tooltipLines),
        };
      };
      const dynamics = snapshot?.gex_dynamics;
      if (!dynamics) {
        return availabilityStatus(
          "GEX CMP SYNC",
          "WAITING FOR CURRENT GEX ANALYSIS",
          "Ожидание текущего engine-анализа GEX.",
        );
      }
      if (dynamics.contract !== "gex-dynamics-v1") return null;
      const latest = dynamics.latest;
      if (!latest || typeof latest !== "object") {
        return availabilityStatus(
          "GEX CMP WAIT",
          "WAITING FOR COMPARABLE GEX HISTORY",
          "Ожидание сопоставимой пары подтверждённых GEX-снимков.",
          "ОЖИДАНИЕ",
        );
      }
      const latestMode = typeof latest.capture_mode === "string" ? latest.capture_mode : "";
      const activeEntitlement = gexMarketDataEntitlement(gex);
      const latestProviderSymbol = exactIdentityText(latest.provider_symbol);
      const activeProviderSymbol = gexPayloadProviderSymbol(gex);
      const latestCapturedMs = Date.parse(latest.captured_at || "");
      const activeCapturedMs = Date.parse(gex.captured_at || "");
      if (
        exactIdentityText(latest.instrument_id) !== exactIdentityText(gex.instrument_id)
        || exactIdentityText(latest.route_fingerprint) !== exactIdentityText(gex.route_fingerprint)
        || !latestProviderSymbol
        || latestProviderSymbol !== activeProviderSymbol
        || !activeEntitlement
        || latest.source !== GEX_SOURCE_BY_CAPTURE_MODE[latestMode]
        || !Number.isFinite(latestCapturedMs)
        || !Number.isFinite(activeCapturedMs)
      ) return null;
      const sameLaneAndScope = latestMode === gex.capture_mode
        && gexComparisonScopesEqual(latest.comparison_scope, gex.comparison_scope);
      if (
        latestCapturedMs > activeCapturedMs
        || (sameLaneAndScope && gexPayloadRevision(latest) !== activeRevision)
      ) {
        return availabilityStatus(
          "GEX CMP SYNC",
          "SYNCING CURRENT GEX REVISION",
          "Синхронизация аналитики с текущей ревизией GEX.",
        );
      }
      if (!sameLaneAndScope) {
        const rebaseTooltipLines = [
          gexDynamicsTooltipLine("Динамика GEX · ПЕРЕБАЗИРОВКА", "title"),
          gexDynamicsTooltipLine("Текущая область GEX изменилась; сравнение с предыдущей областью запрещено.", "risk"),
          gexDynamicsTooltipLine("Собирается новая сопоставимая пара снимков.", "meta"),
        ];
        return {
          text: "GEX CMP REBASE",
          eventLine: "BASELINING CURRENT GEX SCOPE",
          tone: "gold",
          watchTooltip: rebaseTooltipLines,
          eventTooltip: rebaseTooltipLines,
          tooltip: gexDynamicsTooltipPayload(rebaseTooltipLines),
        };
      }
      if (
        latest.context_only !== true
        || latest.action !== "WAIT"
        || latest.direction !== "flat"
        || typeof latest.score !== "number"
        || !Number.isFinite(latest.score)
        || latest.score !== 0
      ) return null;
      const stateCode = typeof latest.state === "string" ? latest.state : "";
      if (!["WAIT", "MIGRATION", "NEAR", "NO_KEY_LEVEL_MIGRATION"].includes(stateCode)) return null;
      const stateText = ({
        WAIT: "ОЖИДАНИЕ",
        MIGRATION: "МИГРАЦИЯ",
        NEAR: "ЦЕНА У КОНЦЕНТРАЦИИ",
        NO_KEY_LEVEL_MIGRATION: "КЛЮЧЕВЫЕ УРОВНИ БЕЗ МИГРАЦИИ",
      })[stateCode] || "НЕИЗВЕСТНО";
      const comparisonQuality = typeof latest.comparison_quality === "string" ? latest.comparison_quality : "UNKNOWN";
      if (!["READY", "NEED_HISTORY", "STALE", "GAP", "SCOPE_CHANGED", "INVALID"].includes(comparisonQuality)) return null;
      if (stateCode !== "WAIT" && comparisonQuality !== "READY") return null;
      const structureState = typeof latest.structure_state === "string"
        ? latest.structure_state
        : "";
      const structureInfo = gexDynamicsStructurePresentation(structureState, latest);
      if (!structureInfo) return null;
      if (stateCode === "WAIT" ? structureState !== "UNKNOWN" : structureState === "UNKNOWN") return null;
      const motionStates = [
        latest.call_wall_motion?.state,
        latest.put_wall_motion?.state,
        latest.gamma_flip_motion?.state,
        latest.wall_span_motion?.state,
      ];
      if (motionStates.some(value => !["UP", "DOWN", "UNCHANGED", "UNKNOWN"].includes(value))) return null;
      const globalGammaRegime = typeof latest.global_gamma_regime === "string" ? latest.global_gamma_regime : "UNKNOWN";
      const regimeInfo = {
        POSITIVE_ESTIMATE: {
          short: "+GEX",
          role: "positive",
          text: "+GEX · модельный знак net GEX выбранной цепочки положительный.",
        },
        NEGATIVE_ESTIMATE: {
          short: "-GEX",
          role: "negative",
          text: "-GEX · модельный знак net GEX выбранной цепочки отрицательный.",
        },
        UNKNOWN: {
          short: "GEX?",
          role: "meta",
          text: "GEX? · знак net GEX для сопоставленного снимка не опубликован.",
        },
      }[globalGammaRegime] || {
        short: "GEX?",
        role: "meta",
        text: `GEX? · неподдерживаемый код ${globalGammaRegime}.`,
      };
      const qualityText = ({
        READY: "Качество READY · соседние снимки сопоставимы.",
        NEED_HISTORY: "Качество NEED HISTORY · нужна ещё одна точка истории.",
        STALE: "Качество STALE · последняя сопоставимая пара устарела.",
        GAP: "Качество GAP · интервал между снимками слишком велик.",
        SCOPE_CHANGED: "Качество SCOPE CHANGED · новая область цепочки сбросила baseline; сравнение возобновится со следующей сопоставимой парой.",
        INVALID: "Качество INVALID · снимок не прошёл каноническую проверку.",
        UNKNOWN: "Качество UNKNOWN · состояние сравнения не опубликовано.",
      })[comparisonQuality] || `Качество UNKNOWN · неподдерживаемый код ${comparisonQuality}.`;
      const comparisonIssue = String(latest.comparison_issue || "").trim();
      const flowText = String(gexDynamicsFlowText(latest) || "");
      const modeLine = gexDynamicsModeLine(stateCode, latest, structureInfo);
      const motionStateText = {
        UP: "ВЫШЕ",
        DOWN: "НИЖЕ",
        UNCHANGED: "БЕЗ ИЗМЕНЕНИЙ",
        UNKNOWN: "НЕИЗВЕСТНО",
      };
      const motionLine = [
        ["CALL-СТЕНА", latest.call_wall_motion],
        ["PUT-СТЕНА", latest.put_wall_motion],
        ["ZERO GAMMA", latest.gamma_flip_motion],
        ["РАЗМАХ", latest.wall_span_motion],
      ].map(([label, motion]) => {
        const motionState = typeof motion?.state === "string" ? motion.state : "UNKNOWN";
        const delta = gexNullableNumber(motion?.delta);
        return `${label} ${motionStateText[motionState]}${delta !== null ? ` Δ ${delta > 0 ? "+" : ""}${fmt(delta)}` : ""}`;
      }).join(" · ");
      const proximityLine = (Array.isArray(latest.proximities) ? latest.proximities : [])
        .map(item => {
          const price = gexNullableNumber(item?.price);
          const distanceAtr = gexNullableNumber(item?.distance_atr);
          const side = item?.side === "CALL" || item?.side === "PUT" ? item.side : "?";
          return `${side} ${price !== null ? fmt(price) : "?"} · ${distanceAtr !== null ? distanceAtr.toFixed(2) : "?"} ATR`;
        })
        .join(" · ");
      const detail = gexDynamicsDetailLabel(latest.detail_code, stateCode);
      const modelScopeText = "Область модели: выбранная цепочка и её GEX-карта; фактический dealer inventory и точная вероятность движения не измеряются.";
      const comparisonRebasing = stateCode === "WAIT" && comparisonQuality === "SCOPE_CHANGED";
      const presentationTitle = comparisonRebasing
        ? "ПЕРЕБАЗИРОВКА СРАВНЕНИЯ"
        : structureInfo.tooltipLabel;
      const presentationTendency = comparisonRebasing
        ? "новая область цепочки формирует собственный baseline без сравнения с прежней областью"
        : structureInfo.tooltipTendency;
      const presentationLead = comparisonRebasing
        ? `Статус: ${presentationTendency}.`
        : `Базовый сценарий: ${presentationTendency}.`;
      const watchTooltip = [
        gexDynamicsTooltipLine(`Динамика GEX · ${presentationTitle}`, "title"),
        gexDynamicsTooltipLine(presentationLead, comparisonRebasing ? "risk" : "plan"),
        gexDynamicsTooltipLine(regimeInfo.text, regimeInfo.role),
        gexDynamicsTooltipLine(qualityText, comparisonQuality === "READY" ? "meta" : "risk"),
        gexDynamicsTooltipLine(`Состояние сравнения: ${stateText} · ${detail}.`, "meta"),
        gexDynamicsTooltipLine(modelScopeText, "meta"),
      ];
      const eventTooltip = [
        gexDynamicsTooltipLine(`Динамика GEX · ${presentationTitle}`, "title"),
        gexDynamicsTooltipLine(presentationLead, comparisonRebasing ? "risk" : "plan"),
        gexDynamicsTooltipLine(regimeInfo.text, regimeInfo.role),
        gexDynamicsTooltipLine(qualityText, comparisonQuality === "READY" ? "meta" : "risk"),
        comparisonRebasing
          ? gexDynamicsTooltipLine(`Состояние сравнения: ${stateText} · ${detail}.`, "meta")
          : comparisonIssue
            ? gexDynamicsTooltipLine(`Диагностика: ${comparisonIssue}.`, "risk")
            : "",
        comparisonRebasing ? "" : gexDynamicsTooltipLine(motionLine, "meta"),
        !comparisonRebasing && proximityLine
          ? gexDynamicsTooltipLine(`Цена у карты: ${proximityLine}`, "meta")
          : "",
        !comparisonRebasing && flowText
          ? gexDynamicsTooltipLine(flowText, "flow")
          : "",
        gexDynamicsTooltipLine(modelScopeText, "meta"),
      ].filter(Boolean);
      const tooltip = gexDynamicsTooltipPayload(eventTooltip);
      return {
        text: comparisonRebasing
          ? "GEX · BASELINE REBASE"
          : stateCode === "WAIT"
          ? `GEX · ${structureInfo.compactLabel}`
          : `${regimeInfo.short} · ${structureInfo.compactLabel}`,
        eventLine: modeLine,
        tone: structureInfo.tone,
        watchTooltip,
        eventTooltip,
        tooltip,
      };
    }

    function gexSnapshotAgeText(gex) {
      const capturedMs = Date.parse(gex?.captured_at || "");
      if (!Number.isFinite(capturedMs)) return "no time";
      const ageMinutes = Math.max(0, (Date.now() - capturedMs) / 60000);
      return ageMinutes < 1 ? "<1m" : `${Math.round(ageMinutes)}m`;
    }

    function drawGexDynamicsSidebarBadge(ctx, gex, pad, width, snapshot, options = {}) {
      if (!gex) return;
      const dyn = gexDynamicsStatus(snapshot, gex);
      if (dyn && !shouldHidePerIndicatorPlanDetail()) {
        const x0 = 4;
        const companionRight = options.panelRight ?? (typeof gexLeftCompanionRight === "function"
          ? Number(gexLeftCompanionRight(pad, width, snapshot)) || 0
          : 0);
        const availableW = Math.max(0, companionRight - x0 - 4);
        if (availableW < 52) return;
        const dynY = 54;
        const dynW = availableW;
        const dynTone = ["up", "down", "purple", "cyan", "gold"].includes(dyn.tone)
          ? dyn.tone
          : "gold";
        const dynColor = themeColor(dynTone, 0.98);
        ctx.save();
        const eventLine = String(dyn.eventLine || "");
        const dynH = eventLine ? 32 : 19;
        const dynTextColor = readableTextForBg(rgbaFromCssColor(dynColor, 0.34), css("--text"));
        ctx.strokeStyle = rgbaFromCssColor(dynColor, 0.92);
        ctx.lineWidth = 1.35;
        ctx.shadowColor = rgbaFromCssColor(dynColor, 0.38);
        ctx.shadowBlur = 8;
        roundedRectPath(ctx, x0, dynY, dynW, dynH, 4);
        ctx.fillStyle = css("--panel");
        ctx.fill();
        ctx.fillStyle = rgbaFromCssColor(dynColor, 0.30);
        ctx.fill();
        ctx.stroke();
        ctx.shadowBlur = 0;
        ctx.lineWidth = 1;
        ctx.fillStyle = dynTextColor;
        ctx.textAlign = "left";
        ctx.textBaseline = eventLine ? "top" : "middle";
        if (eventLine) {
          ctx.font = "900 9px -apple-system, BlinkMacSystemFont, sans-serif";
          drawGexDynamicsRichText(ctx, dyn.text, x0 + 6, dynY + 5, Math.max(dynW - 12, 18), dynTextColor);
          ctx.font = "850 8px -apple-system, BlinkMacSystemFont, sans-serif";
          drawGexDynamicsRichText(ctx, eventLine, x0 + 6, dynY + 18, Math.max(dynW - 12, 18), dynTextColor);
        } else {
          ctx.font = "900 9px -apple-system, BlinkMacSystemFont, sans-serif";
          drawGexDynamicsRichText(ctx, dyn.text, x0 + 6, dynY + dynH / 2 + 0.5, Math.max(dynW - 12, 18), dynTextColor);
        }
        registerCanvasTooltip(
          options.tooltipChart || "price",
          x0 + dynW / 2,
          dynY + dynH / 2,
          dynW,
          dynH,
          () => dyn.tooltip || "Контекст динамики GEX",
          null,
          canvasLayerZIndex("tooltips"),
          GEX_TOOLTIP_SOURCE_FRONT,
        );
        ctx.restore();
      }
    }

    function gexStatusDiagnosticLines(gex, visibility = null) {
      const diagnostics = gex?.diagnostics || {};
      const get = (key) => {
        const value = diagnostics[key];
        return value === null || value === undefined || value === "" ? null : value;
      };
      const boolText = (value) => value === null ? "-" : value ? "true" : "false";
      const boolValue = (value) => {
        return typeof value === "boolean" ? value : null;
      };
      const numberText = (value, digits = 0) => {
        const number = gexNullableNumber(value);
        if (number === null) return null;
        return digits ? number.toFixed(digits) : String(Math.round(number));
      };
      const requestedContracts = numberText(get("diag_contracts_requested"));
      const optionRows = numberText(get("diag_option_rows"));
      const requestedStrikes = numberText(get("diag_strikes_requested"));
      const strikeRows = numberText(get("diag_strike_rows"));
      const gammaRows = numberText(get("diag_gamma_rows"));
      const usableGammaRows = numberText(get("diag_usable_gamma_rows"));
      const oiRows = numberText(get("diag_open_interest_rows"));
      const usableGexRows = numberText(get("diag_usable_gex_rows"));
      const nonzeroRows = numberText(get("diag_nonzero_gex_rows"));
      const requestSeconds = numberText(gex?.request_seconds, 2);
      const timeoutPhase = get("diag_collection_timeout_phase");
      const comparisonScopeText = gexComparisonScopeText(gex);
      const live = gex?.live && typeof gex.live === "object" ? gex.live : null;
      const payloadVisibility = gex?.visibility_summary && typeof gex.visibility_summary === "object" ? gex.visibility_summary : null;
      const refreshRequest = gex?.refresh_request && typeof gex.refresh_request === "object" ? gex.refresh_request : null;
      const refreshRequested = refreshRequest?.requested && typeof refreshRequest.requested === "object" ? refreshRequest.requested : {};
      const refreshCached = refreshRequest?.cached && typeof refreshRequest.cached === "object" ? refreshRequest.cached : {};
      const refreshResult = refreshRequest?.result && typeof refreshRequest.result === "object" ? refreshRequest.result : {};
      const displayContextSource = String(gex?.display_context_source || "").trim();
      const displayContextCapturedAt = String(gex?.display_context_captured_at || gexContextCapturedAt(gex) || "").trim();
      const latestAttempt = gex?.latest_attempt && typeof gex.latest_attempt === "object" ? gex.latest_attempt : null;
      const displayContextText = gex?.preserved_context
        ? [
            `Display: ${displayContextSource === "history" ? "last usable history" : "previous usable context"}`,
            `levels ${(Array.isArray(gex?.levels) ? gex.levels.length : 0)}`,
            `expiry rows ${(Array.isArray(gex?.expiry_profile) ? gex.expiry_profile.length : 0)}`,
            displayContextCapturedAt ? `from ${displayContextCapturedAt}` : "",
            latestAttempt?.status ? `latest attempt ${latestAttempt.status}` : "",
          ].filter(Boolean).join(" · ")
        : "";
      const latestAttemptText = latestAttempt
        ? [
            `Latest attempt: ${latestAttempt.status || "unknown"}`,
            latestAttempt.source && latestAttempt.capture_mode ? `${latestAttempt.source}+${latestAttempt.capture_mode}` : "source unavailable",
            latestAttempt.captured_at ? `captured ${latestAttempt.captured_at}` : "",
            "separate from displayed provenance",
          ].filter(Boolean).join(" · ")
        : "";
      const latestAttemptMessageLines = latestAttempt?.message
        ? gexWrapTooltipLine("Latest attempt message:", latestAttempt.message, 88)
        : [];
      const payloadVisibilityText = payloadVisibility
        ? [
            `Payload: levels ${numberText(payloadVisibility.output_level_count) || "0"}/${numberText(payloadVisibility.candidate_level_count) || "0"}`,
            `expiry rows ${numberText(payloadVisibility.expiry_profile_row_count) || "0"}`,
            `raw strikes ${numberText(payloadVisibility.nonzero_strike_count) || "0"}/${numberText(payloadVisibility.raw_strike_count) || "0"}`,
            `weak ${numberText(payloadVisibility.weak_level_count) || "0"}`,
            (gexNullableNumber(payloadVisibility.hidden_by_max_levels) ?? 0) > 0 ? `max hidden ${numberText(payloadVisibility.hidden_by_max_levels)}` : "",
          ].filter(Boolean).join(" · ")
        : "";
      const refreshRequestText = refreshRequest
        ? [
            `Refresh: ${refreshRequest.status || gex?.refresh_status || "-"}`,
            `requested strikes ${numberText(refreshRequested.requested_strike_limit) || "-"}`,
            refreshCached.selected_strike_count ? `showing cache strikes ${numberText(refreshCached.selected_strike_count)}` : "",
            refreshResult.selected_strike_count ? `result strikes ${numberText(refreshResult.selected_strike_count)}` : "",
          ].filter(Boolean).join(" · ")
        : "";
      const liveFrameAgeSeconds = live?.last_frame_at
        ? Math.max(0, (Date.now() - Date.parse(live.last_frame_at)) / 1000)
        : NaN;
      return [
        "--------------------",
        "GEX DATA",
        visibility?.text || "",
        displayContextText,
        latestAttemptText,
        ...latestAttemptMessageLines,
        payloadVisibilityText,
        refreshRequestText,
        `Status: ${gex?.status || "cache"} · Source: ${gex?.source || "unknown"}`,
        `Broker market data: ${gexMarketDataEntitlementLabel(gex)} · Capture cadence: ${gex?.capture_mode === "live" ? "STREAM" : "REQUEST"}`,
        live ? `Stream cadence: active ${boolText(boolValue(live.active))} · publishable ${boolText(boolValue(live.publishable))} · warming ${boolText(boolValue(live.warming))} · holding ${boolText(boolValue(live.holding_last_publishable))}` : "",
        live ? `Stream frame: seq ${numberText(live.frame_seq) || "-"} · subs ${numberText(live.subscriptions) || "-"} · age ${Number.isFinite(liveFrameAgeSeconds) ? `${Math.round(liveFrameAgeSeconds)}s` : "-"}` : "",
        live?.persistence_error ? `Persistence: ERROR · ${live.persistence_error}` : live && live.persistence_ok !== undefined ? `Persistence: ${live.persistence_ok ? "ok" : "pending"}` : "",
        gex?.provider_symbol ? `Provider symbol: ${gex.provider_symbol} · Spot: ${numberText(gex.spot, 2) || "-"}` : "",
        get("diag_generic_ticks") ? `Request: ticks ${get("diag_generic_ticks")} · streaming only` : "",
        requestSeconds ? `Timing: request ${requestSeconds}s` : "",
        requestedContracts || optionRows ? `Contracts: received ${optionRows || "0"}/${requestedContracts || "0"}` : "",
        requestedStrikes || strikeRows ? `Strikes: received ${strikeRows || "0"}/${requestedStrikes || "0"}` : "",
        gammaRows || oiRows || usableGexRows ? `Rows: gamma ${usableGammaRows || gammaRows || "0"}/${gammaRows || "0"} · OI ${oiRows || "0"} · usable GEX ${usableGexRows || "0"} · nonzero ${nonzeroRows || "0"}` : "",
        `Decision: ${gex?.decision_authoritative ? "authoritative" : "not authoritative"}`,
        get("diag_collection_timeout") ? `Timeout: ${timeoutPhase || "option market data"}` : "",
        comparisonScopeText,
      ].filter(Boolean);
    }

    function gexWrapTooltipLine(prefix, text, limit = 88) {
      const source = String(text || "").replace(/\s+/g, " ").trim();
      if (!source) return [];
      const label = String(prefix || "").trim();
      const firstPrefix = label ? `${label} ` : "";
      const lines = [];
      let remaining = source;
      let currentPrefix = firstPrefix;
      while (remaining.length) {
        const budget = Math.max(28, Number(limit) - currentPrefix.length);
        if (remaining.length <= budget) {
          lines.push(`${currentPrefix}${remaining}`.trim());
          break;
        }
        const cut = remaining.lastIndexOf(" ", budget);
        const index = cut >= Math.floor(budget * 0.55) ? cut : budget;
        lines.push(`${currentPrefix}${remaining.slice(0, index).trim()}`.trim());
        remaining = remaining.slice(index).trim();
        currentPrefix = label ? "  " : "";
      }
      return lines;
    }

    function drawGexViewLegend(ctx, pad, priceH, width) {
      if (!gexFocusMode()) return;
      const plotRight = width - pad.right;
      const boxW = 336;
      const boxH = 54;
      if (plotRight - pad.left < boxW + 24) return;
      const x0 = plotRight - boxW - 8;
      const y0 = pad.top + 8;
      const lines = [
        "GEX EST  модельная карта, не сигнал",
        "C/P = концентрации   G+/G- = узлы   ZERO G = режим",
        "Activity = валовый volume; направление позиции неизвестно",
      ];
      ctx.save();
      ctx.fillStyle = isLightTheme() ? "rgba(255,255,255,0.78)" : "rgba(15,23,42,0.68)";
      ctx.strokeStyle = isLightTheme() ? "rgba(100,116,139,0.32)" : "rgba(148,163,184,0.24)";
      roundedRectPath(ctx, x0, y0, boxW, boxH, 5);
      ctx.fill();
      ctx.stroke();
      ctx.font = "800 10px -apple-system, BlinkMacSystemFont, sans-serif";
      ctx.textAlign = "left";
      ctx.textBaseline = "top";
      const textColor = isLightTheme() ? "#0f172a" : "#e2e8f0";
      const accent = isLightTheme() ? "#64748b" : "#94a3b8";
      lines.forEach((line, index) => {
        ctx.fillStyle = index === 0 ? textColor : accent;
        ctx.fillText(line, x0 + 10, y0 + 8 + index * 14, boxW - 20);
      });
      registerCanvasTooltip(
        "price",
        x0 + boxW / 2,
        y0 + boxH / 2,
        boxW,
        boxH,
        [
          "Mini GEX view legend",
          "Все значения зависят от выбранной цепочки и C+/P- sign convention.",
          "Zero Gamma не является автоматической поддержкой или сопротивлением.",
        ].join("\\n"),
        null,
        canvasLayerZIndex("tooltips"),
        GEX_TOOLTIP_SOURCE_FRONT,
      );
      ctx.restore();
    }

    function drawGexContext(ctx, snapshot, visible, pad, priceH, width, y, xStep, scaleInfo = null) {
      const cfg = state.indicators.gexContext;
      const layer = String(scaleInfo?.layer || "all");
      if (!["all", "trail", "levels", "foreground"].includes(layer)) {
        throw new Error(`Unknown GEX render layer: ${layer}`);
      }
      const drawTrail = layer === "all" || layer === "trail";
      const drawLevels = layer === "all" || layer === "levels";
      const drawForeground = layer === "all" || layer === "foreground";
      const gex = activeGexContext(snapshot);
      const statusContext = gex || gexStatusContext(snapshot);
      if (drawForeground && typeof renderOptionBoardButton === "function") {
        renderOptionBoardButton();
      }
      if (!gexLayerVisible()) {
        if (drawForeground) renderGexSidebarChrome(null, false);
        return;
      }
      if (!statusContext) {
        if (drawForeground) renderGexSidebarChrome(null, true);
        return;
      }
      if (!gex) {
        if (drawForeground) renderGexSidebarChrome(statusContext, true);
        return;
      }
      const levels = drawLevels ? gexVisibleLevels(gex.levels || [], cfg) : [];
      const zoneMode = gexZoneMode(cfg.zoneStyle);
      const drawZoneBands = zoneMode === "zones";
      const drawZoneLines = zoneMode === "zones" || zoneMode === "lines";
      const drawStrikeMigration = zoneMode === "strike";
      const plotLeft = pad.left;
      const plotRight = width - pad.right;
      ctx.save();
      if (drawTrail) {
        if (drawZoneBands || drawZoneLines) {
          drawCachedGexHistoryTrail(ctx, gex, snapshot, visible, pad, priceH, width, y, xStep, scaleInfo);
        } else if (drawStrikeMigration) {
          drawGexStrikeMigrationMarkers(ctx, gex, snapshot, visible, pad, priceH, width, y, xStep);
        }
      }
      if (drawLevels) {
        if (drawZoneLines && levels.length) {
          for (const level of levels) {
            const price = gexNullableNumber(level.price);
            if (price === null) continue;
            const half = gexNullableNumber(level.zone_half_width);
            if (half === null) continue;
            const top = y(price + half);
            const bottom = y(price - half);
            if (bottom < pad.top || top > pad.top + priceH) continue;
            const color = gexKindColor(level, 0.96);
            const strength = gexLevelStrengthValue(level);
            const renderStrength = strength ?? 0;
            const strengthNote = gexLevelStrengthNote(strength, level);
            const alpha = clamp(0.035 + renderStrength * 0.15, 0.04, 0.24);
            if (drawZoneBands) {
              const grad = ctx.createLinearGradient(0, top, 0, bottom);
              grad.addColorStop(0, rgbaFromCssColor(color, 0));
              grad.addColorStop(0.50, rgbaFromCssColor(color, alpha));
              grad.addColorStop(1, rgbaFromCssColor(color, 0));
              ctx.fillStyle = grad;
              ctx.fillRect(plotLeft, top, plotRight - plotLeft, Math.max(bottom - top, 1));
            }
            ctx.strokeStyle = rgbaFromCssColor(color, clamp(0.38 + renderStrength * 0.42, 0.42, 0.88));
            ctx.lineWidth = clamp(1 + renderStrength * 2.0, 1, 4.2);
            ctx.setLineDash([8, 5]);
            ctx.beginPath();
            ctx.moveTo(plotLeft, y(price));
            ctx.lineTo(plotRight, y(price));
            ctx.stroke();
            ctx.shadowBlur = 0;
            {
              const processLabel = typeof gexLevelProcessLabel === "function" ? gexLevelProcessLabel(level) : "";
              const label = `${processLabel || compactLineLabel(level.kind || "GEX")} ${gexLevelPowerLabel(level, strength)}`;
              ctx.setLineDash([]);
              ctx.font = "800 9px -apple-system, BlinkMacSystemFont, sans-serif";
              ctx.textAlign = "right";
              ctx.fillStyle = rgbaFromCssColor(color, 0.90);
              ctx.fillText(label, width - pad.right - 8, clamp(y(price) - 5, pad.top + 12, pad.top + priceH - 6));
            }
            registerCanvasTooltip(
              "price",
              (plotLeft + plotRight) / 2,
              y(price),
              plotRight - plotLeft,
              Math.max(bottom - top, 14),
              () => gexLevelTooltip(gex, level, { strength, strengthNote }),
              null,
              canvasLayerZIndex("tooltips"),
              GEX_TOOLTIP_SOURCE_LEVELS,
            );
          }
        }
        const flip = gexNullableNumber(gex.gamma_flip);
        if (drawZoneLines && flip !== null) {
          const yy = y(flip);
          if (yy >= pad.top && yy <= pad.top + priceH) {
            const color = themeColor("purple", 0.95);
            ctx.strokeStyle = color;
            ctx.lineWidth = 2.5;
            ctx.setLineDash([10, 6]);
            ctx.beginPath();
            ctx.moveTo(plotLeft, yy);
            ctx.lineTo(plotRight, yy);
            ctx.stroke();
            {
              ctx.setLineDash([]);
              ctx.font = "900 10px -apple-system, BlinkMacSystemFont, sans-serif";
              ctx.textAlign = "right";
              ctx.fillStyle = color;
              ctx.fillText(`ZERO G ${fmt(flip)}`, width - pad.right - 8, clamp(yy + 13, pad.top + 12, pad.top + priceH - 4));
            }
            registerCanvasTooltip(
              "price",
              (plotLeft + plotRight) / 2,
              yy,
              plotRight - plotLeft,
              18,
              () => [
                `Estimated Zero Gamma ${fmt(flip)}`,
                "Корень переоценённой цепочки · sticky-strike IV · C+/P- inventory convention.",
                `${gexComparisonScopeText(gex)} · OI ${gexOpenInterestAsOfLabel(gex)}.`,
                "Это граница модельного режима, не entry и не support/resistance.",
                "Текущий знак режима публикуется отдельно и не выводится только из положения spot относительно линии.",
              ].join("\\n"),
              null,
              canvasLayerZIndex("tooltips"),
              GEX_TOOLTIP_SOURCE_LEVELS,
            );
          }
        }
      }
      if (drawForeground && scaleInfo?.drawProfile !== false) drawGexProfileCompanion(ctx, snapshot, visible, pad, priceH, width, y);
      if (drawForeground) {
        renderGexSidebarChrome(gex, true);
        drawGexDynamicsSidebarBadge(ctx, gex, pad, width, snapshot);
        drawGexViewLegend(ctx, pad, priceH, width);
      }
      ctx.restore();
    }
