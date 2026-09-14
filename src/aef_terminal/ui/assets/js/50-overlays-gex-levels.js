function gexLevelStrengthValue(level) {
      const rawStrength = gexNullableNumber(level?.strength);
      return rawStrength !== null && rawStrength >= 0 && rawStrength <= 1 ? rawStrength : null;
    }

    function gexLevelPowerBucket(level, strength = null, options = {}) {
      const suppliedStrength = gexNullableNumber(strength);
      const candidate = suppliedStrength ?? gexLevelStrengthValue(level);
      const value = candidate !== null && candidate >= 0 && candidate <= 1 ? candidate : null;
      const hasStrength = value !== null;
      const publishedCode = typeof level?.power_class === "string" ? level.power_class : "UNKNOWN";
      const code = hasStrength && ["WEAK", "MEDIUM", "STRONG", "EXTREME"].includes(publishedCode)
        ? publishedCode
        : "UNKNOWN";
      const historical = options.temporalScope === "historical";
      const profileScope = historical ? "профиля этого снимка" : "текущего профиля";
      const labels = { WEAK: "WEAK", MEDIUM: "MED", STRONG: "STR", EXTREME: "EXT", UNKNOWN: "GEX" };
      const descriptions = {
        WEAK: `Gross GEX страйка ниже 10% от пикового gross GEX ${profileScope}.`,
        MEDIUM: `Средняя доля gross GEX страйка относительно пика ${profileScope}.`,
        STRONG: "Высокая доля gross GEX страйка; это не вероятность реакции цены.",
        EXTREME: `Gross GEX страйка не менее 85% от пика ${profileScope}; требуется наблюдаемая реакция цены.`,
        UNKNOWN: "Класс силы не был опубликован backend.",
      };
      const percent = hasStrength ? Math.round(value * 100) : null;
      return {
        code,
        short: labels[code] || "GEX",
        label: percent === null ? "GEX UNKNOWN" : `${labels[code] || "GEX"} ${percent}%`,
        tooltip: descriptions[code] || descriptions.UNKNOWN,
        strength: value,
      };
    }

    function gexLevelIsWeak(level, strength = null) {
      const value = gexLevelPowerBucket(level, strength).strength;
      return value !== null && value < 0.10;
    }

    function gexLevelSelectionRank(level) {
      const rank = level?.selection_rank;
      return typeof rank === "number" && Number.isInteger(rank) && rank > 0 ? rank : null;
    }

    function gexLevelsHaveCanonicalSelectionRanks(levels) {
      const source = Array.isArray(levels) ? levels : [];
      const ranks = source.map(gexLevelSelectionRank);
      const orderedRanks = ranks.slice().sort((left, right) => left - right);
      return ranks.every(rank => rank !== null)
        && new Set(ranks).size === ranks.length
        && orderedRanks.every((rank, index) => rank === index + 1);
    }

    function gexVisibleLevels(levels, cfg = state.indicators?.gexContext || {}) {
      const source = Array.isArray(levels) ? levels : [];
      const requested = cfg?.displayLevels;
      const limit = GEX_DISPLAY_LEVEL_COUNTS.includes(requested)
        ? requested
        : GEX_DEFAULT_DISPLAY_LEVEL_COUNT;
      if (!gexLevelsHaveCanonicalSelectionRanks(source)) return [];
      const ranked = source.map(level => ({ level, selectionRank: gexLevelSelectionRank(level) }));
      const selected = ranked
        .sort((left, right) => left.selectionRank - right.selectionRank)
        .slice(0, limit);
      return selected
        .map(item => item.level)
        .sort((left, right) => {
          const leftStrike = gexNumericStrikeKey(left?.price);
          const rightStrike = gexNumericStrikeKey(right?.price);
          if (leftStrike === null) return rightStrike === null ? 0 : 1;
          if (rightStrike === null) return -1;
          return leftStrike - rightStrike;
        });
    }

    function gexLevelVisibilitySummary(levels, cfg = state.indicators?.gexContext || {}) {
      const source = Array.isArray(levels) ? levels : [];
      const visible = gexVisibleLevels(source, cfg);
      const hidden = Math.max(0, source.length - visible.length);
      const weak = visible.filter(level => gexLevelIsWeak(level)).length;
      const canonicalRanks = gexLevelsHaveCanonicalSelectionRanks(source);
      return {
        total: source.length,
        visible: visible.length,
        hidden,
        canonical_ranks: canonicalRanks,
        text: [
          `Displayed levels: ${visible.length}/${source.length}.`,
          weak ? `Weak included: ${weak}.` : "",
          hidden && canonicalRanks ? `${hidden} outside the selected display count.` : "",
          !canonicalRanks && source.length ? "Selection ranks invalid: level rendering is blocked." : "",
        ].filter(Boolean).join(" "),
      };
    }

    function gexLevelPowerLabel(level, strength = null) {
      const bucket = gexLevelPowerBucket(level, strength);
      return bucket.strength === null
        ? "GEX?"
        : `${bucket.short} ${Math.round(bucket.strength * 100)}`;
    }

    function gexLevelPowerCompact(level, strength = null) {
      return gexLevelPowerLabel(level, strength).replace("WEAK ", "W").replace("MED ", "M").replace("STR ", "S").replace("EXT ", "X");
    }

    function gexLevelPowerGlyphs(level, strength = null) {
      const bucket = gexLevelPowerBucket(level, strength);
      const glyphs = [];
      if (bucket.code === "EXTREME") glyphs.push("★");
      return glyphs.join("");
    }

    function gexLevelPowerDisplay(level, strength = null) {
      const compact = gexLevelPowerCompact(level, strength);
      const glyphs = gexLevelPowerGlyphs(level, strength);
      return glyphs ? `${glyphs}${compact}` : compact;
    }

    function gexLevelStrengthNote(strength, level = null, options = {}) {
      return gexLevelPowerBucket(level, strength, options).tooltip;
    }

    function gexGammaSign(level) {
      const netGex = gexNullableNumber(level?.net_gex);
      return netGex === null ? null : netGex === 0 ? 0 : netGex > 0 ? 1 : -1;
    }

    function gexGammaModeLines(sign) {
      if (sign > 0) {
        return [
          "Локальный знак strike: +GEX в опубликованной модельной экспозиции.",
          "Это не определяет общий гамма-режим и не доказывает действия маркет-мейкеров.",
          "Торгуйте только наблюдаемую реакцию цены, потока и структуры.",
        ];
      }
      if (sign < 0) {
        return [
          "Локальный знак strike: -GEX в опубликованной модельной экспозиции.",
          "Это не определяет общий гамма-режим и не доказывает действия маркет-мейкеров.",
          "Торгуйте только наблюдаемую реакцию цены, потока и структуры.",
        ];
      }
      if (sign === 0) {
        return [
          "Локальный net GEX равен нулю.",
          "Exact Call/Put GEX выше показывают, компенсировались ли стороны или обе экспозиции равны нулю.",
          "Gross GEX и относительная сила могут оставаться высокими только при ненулевых сторонах.",
          "Это не торговый сигнал; торгуйте только наблюдаемую реакцию цены.",
        ];
      }
      return ["Локальный net GEX недоступен; торгуйте только наблюдаемую реакцию цены."];
    }

    function gexTooltipLine(text, role = "", tokenRoles = {}) {
      const line = tooltipLine(text, role);
      const source = String(text || "").toUpperCase();
      const roles = { ...(tokenRoles && typeof tokenRoles === "object" ? tokenRoles : {}) };
      if (source.includes("CALL")) roles.CALL = roles.CALL || "call";
      if (source.includes("PUT")) roles.PUT = roles.PUT || "put";
      if (source.includes("+GEX")) roles["+GEX"] = roles["+GEX"] || "positive";
      if (source.includes("-GEX")) roles["-GEX"] = roles["-GEX"] || "negative";
      for (const token of ["CALL_WALL", "PUT_WALL", "POS_GAMMA_NODE", "NEG_GAMMA_NODE", "ZERO_GAMMA"]) {
        if (source.includes(token)) roles[token] = roles[token] || "type";
      }
      if (Object.keys(roles).length) line.token_roles = roles;
      return line;
    }

    function gexTooltipPayload(lines, maxLines = 32) {
      return {
        lines: (lines || []).filter(line => {
          return tooltipLineText(line).trim();
        }),
        max_lines: maxLines,
      };
    }

    function gexTooltipAppend(base, extraLines = []) {
      const lines = [...base.lines];
      return gexTooltipPayload([...lines, ...extraLines]);
    }

    function gexCurrentVolumeTooltipLines(row, options = {}) {
      const callVolume = gexOptionVolumeNumber(row, "current", "call_volume");
      const putVolume = gexOptionVolumeNumber(row, "current", "put_volume");
      const totalVolume = gexOptionVolumeNumber(row, "current", "total_volume");
      const turnoverSinceOpen = gexOptionVolumeNumber(row, "current", "turnover");
      const currentVolumeRank = gexOptionVolumeNumber(row, "current", "rank");
      const historical = options.temporalScope === "historical";
      const volumeLabel = historical ? "Volume at snapshot" : "Current volume";
      const turnoverLabel = historical ? "Volume / OI at snapshot" : "Current volume / OI";
      if (callVolume === null && putVolume === null && totalVolume === null) return [];
      return [
        gexTooltipLine(
          `${volumeLabel}: C ${callVolume !== null ? Math.round(callVolume).toLocaleString("en-US") : "-"} / P ${putVolume !== null ? Math.round(putVolume).toLocaleString("en-US") : "-"} / total ${totalVolume !== null ? Math.round(totalVolume).toLocaleString("en-US") : "-"}`,
          "flow",
        ),
        turnoverSinceOpen !== null
          ? gexTooltipLine(
            `${turnoverLabel}: ${(turnoverSinceOpen * 100).toFixed(turnoverSinceOpen >= 1 ? 0 : 1)}%${currentVolumeRank !== null ? ` · volume rank #${Math.round(currentVolumeRank)}` : ""}`,
            "meta",
          )
          : null,
      ].filter(Boolean);
    }

    function gexLevelTooltip(gex, level, options = {}) {
      const price = gexNullableNumber(level?.price);
      const strength = gexNullableNumber(options.strength) ?? gexLevelStrengthValue(level);
      const historical = options.temporalScope === "historical";
      const bucket = gexLevelPowerBucket(level, strength, options);
      const strengthNote = options.strengthNote || gexLevelStrengthNote(strength, level, options);
      const sign = gexGammaSign(level);
      const signLabel = sign > 0 ? "+GEX" : sign < 0 ? "-GEX" : sign === 0 ? "NEUTRAL GEX" : "";
      const typeLabel = gexLevelTypeLabel(level);
      const titleBase = options.title || `${typeLabel} ${price !== null ? fmt(price) : ""}`;
      const title = signLabel && !String(titleBase).includes(signLabel) ? `${titleBase} · ${signLabel}` : titleBase;
      const role = gexLevelRole(level);
      const flow = gexFlowText(level?.abs_flow_1pt);
      const netGexOverride = options.netGexValue;
      const typedNetGexOverride = gexNullableNumber(netGexOverride);
      const netGexValue = typedNetGexOverride ?? gexNullableNumber(level?.net_gex);
      const callGexValue = gexNullableNumber(level?.call_gex);
      const putGexValue = gexNullableNumber(level?.put_gex);
      const currentLevel = options.currentLevel && typeof options.currentLevel === "object" ? options.currentLevel : null;
      const currentStrength = gexLevelStrengthValue(currentLevel);
      const currentType = currentLevel ? gexLevelTypeLabel(currentLevel) : "";
      const currentPower = currentLevel && currentStrength !== null
        ? gexLevelPowerDisplay(currentLevel, currentStrength)
        : "";
      const currentProcess = currentLevel ? gexOptionVolumeInteractionLabel(currentLevel) : "";
      const currentComparison = historical && price !== null
        ? currentLevel
          ? `Сейчас на strike ${fmt(price)}: ${[currentType, currentPower, currentProcess].filter(Boolean).join(" · ")}.`
          : `Сейчас strike ${fmt(price)} не входит в опубликованный live-набор уровней.`
        : "";
      const profilePeakLabel = historical ? "snapshot gross-GEX peak" : "current gross-GEX peak";
      const dataLine = [
        `Gross strength: ${strength !== null ? `${Math.round(strength * 100)}% of ${profilePeakLabel}` : "unavailable"}`,
        gexNullableNumber(level?.abs_gex) !== null ? `gross ${gexDollarText(level.abs_gex)}` : "",
        flow ? `flow ${flow}` : "",
      ].filter(Boolean).join(" · ");
      const processLabel = gexOptionVolumeInteractionLabel(level);
      return gexTooltipPayload([
        gexTooltipLine(title, "title"),
        historical
          ? gexTooltipLine(`Исторический снимок${options.snapshotLabel ? ` ${options.snapshotLabel}` : ""}: GEX, volume и strength ниже относятся к этому моменту.`, "meta")
          : null,
        currentComparison ? gexTooltipLine(currentComparison, "plan") : null,
        gexTooltipLine(`Net GEX ${netGexValue !== null ? gexDollarText(netGexValue) : "-"}`, "meta"),
        gexTooltipLine(`Call GEX ${callGexValue !== null ? gexDollarText(callGexValue) : "-"} / Put GEX ${putGexValue !== null ? gexDollarText(putGexValue) : "-"}`, "flow"),
        ...gexGammaModeLines(sign).map((line, index) => gexTooltipLine(line, index === 0 && sign === 1 ? "positive" : index === 0 && sign === -1 ? "negative" : "meta")),
        gexTooltipLine(`${historical ? "Роль на момент снимка" : "Роль сейчас"}: ${role.role}.`, "role"),
        gexTooltipLine(`План: ${role.action}`, "plan"),
        processLabel ? gexTooltipLine(`Процесс: ${processLabel}`, "flow") : null,
        ...gexCurrentVolumeTooltipLines(level, options),
        gexTooltipLine(`Сила: ${bucket.label}.`, strength !== null ? "strength" : "risk"),
        gexTooltipLine(dataLine, "meta"),
        gexTooltipLine(`${strengthNote} Gross = |Call GEX| + |Put GEX|; это относительная концентрация, не вероятность.`, "strength"),
      ]);
    }

    function gexNumericStrikeKey(value) {
      return typeof value === "number" && Number.isFinite(value) ? value : null;
    }

    function gexLevelForExactStrike(levels, strike) {
      const targetKey = gexNumericStrikeKey(strike);
      if (targetKey === null) return null;
      return (levels || []).find(level => {
        return gexNumericStrikeKey(level?.price) === targetKey;
      }) || null;
    }

    function gexLevelTypeLabel(level, row = null) {
      const kindClass = gexKindClass(level);
      if (kindClass === "flip") return "ZERO G";
      if (kindClass === "call") return "CALL";
      if (kindClass === "put") return "PUT";
      if (kindClass === "positive_node") return "G+";
      if (kindClass === "negative_node") return "G-";
      if (kindClass === "neutral_node") return "GEX";
      if (row?.net_gex === null || row?.net_gex === undefined || row?.net_gex === "") return "GEX";
      const netGex = gexNullableNumber(row.net_gex);
      return netGex === null || netGex === 0 ? "GEX" : netGex > 0 ? "G+" : "G-";
    }

    function gexProfileRowTooltip(gex, row, level, title, strength) {
      const strengthScope = /expiry/i.test(String(title || "")) ? " of current selected-profile peak" : "";
      const expiryScoped = Boolean(row?.expiry);
      const netValue = gexProfileRowNetValue(row);
      const callValue = gexProfileSideValue(row, "call");
      const putValue = gexProfileSideValue(row, "put");
      const profileSign = netValue === null ? null : netValue > 0 ? 1 : netValue < 0 ? -1 : 0;
      const profileSignLabel = profileSign > 0 ? "+GEX" : profileSign < 0 ? "-GEX" : profileSign === 0 ? "NEUTRAL GEX" : "GEX?";
      const flowSource = expiryScoped ? row : (level || row);
      const structuralLabel = level
        ? `${gexLevelTypeLabel(level, row)} ${gexLevelPowerDisplay(level, gexLevelStrengthValue(level))}`.trim()
        : "";
      const event = gexOptionVolumeSection(flowSource, "event");
      const materialActivity = event?.material === true;
      const callDelta = gexOptionVolumeNumber(flowSource, "event", "call_volume_delta");
      const putDelta = gexOptionVolumeNumber(flowSource, "event", "put_volume_delta");
      const totalDelta = gexOptionVolumeNumber(flowSource, "event", "total_volume_delta");
      const callParticipation = gexOptionVolumeNumber(flowSource, "event", "call_participation");
      const putParticipation = gexOptionVolumeNumber(flowSource, "event", "put_participation");
      const turnover = gexOptionVolumeNumber(flowSource, "event", "turnover");
      const acceleration = gexOptionVolumeNumber(flowSource, "event", "acceleration");
      const windowSeconds = gexOptionVolumeNumber(flowSource, "event", "window_seconds");
      const completeVolumeDelta = callDelta !== null && putDelta !== null && totalDelta !== null;
      const interactionLabel = gexOptionVolumeInteractionLabel(flowSource)
        || gexOptionVolumeCompactLabel(flowSource);
      const currentVolumeLines = gexCurrentVolumeTooltipLines(flowSource);
      const liveFlowLines = materialActivity
        ? [
          interactionLabel ? gexTooltipLine(`Interaction: ${interactionLabel}`, "plan") : null,
          completeVolumeDelta
            ? gexTooltipLine(
              `Stream Δ volume: C ${Math.round(callDelta).toLocaleString("en-US")} / P ${Math.round(putDelta).toLocaleString("en-US")} / total ${Math.round(totalDelta).toLocaleString("en-US")}`,
              "flow",
            )
            : null,
          callParticipation !== null && putParticipation !== null
            ? gexTooltipLine(`Participation: C ${Math.round(callParticipation * 100)}% / P ${Math.round(putParticipation * 100)}%`, "flow")
            : null,
          turnover !== null ? gexTooltipLine(`Δ volume / OI: ${(turnover * 100).toFixed(2)}%`, "meta") : null,
          acceleration !== null ? gexTooltipLine(`Acceleration vs rolling median: ×${acceleration.toFixed(2)}`, "meta") : null,
          windowSeconds !== null ? gexTooltipLine(`Stream window: ${Math.round(windowSeconds)}s`, "meta") : null,
        ].filter(Boolean)
        : [];
      const profileLines = [
        gexTooltipLine(`Strike ${fmt(row.strike)}`, "entry"),
        row.expiry ? gexTooltipLine(`Expiry ${compactGexExpiry(row.expiry)}`, "meta") : null,
        gexTooltipLine(`${profileSignLabel} profile`, profileSign > 0 ? "positive" : profileSign < 0 ? "negative" : "meta"),
        gexTooltipLine(`Call GEX ${callValue !== null ? gexDollarText(callValue) : "-"} / Put GEX ${putValue !== null ? gexDollarText(putValue) : "-"}`, "flow"),
        ...(level ? [] : currentVolumeLines),
        ...liveFlowLines,
        gexTooltipLine(`Relative gross-GEX share ${Math.round(strength * 100)}%${strengthScope || " of current profile peak"}; gross = |Call GEX| + |Put GEX|, not probability.`, "strength"),
      ].filter(Boolean);
      if (!level || expiryScoped) {
        return gexTooltipPayload([
          gexTooltipLine(`${title} ${fmt(row.strike)} · ${structuralLabel || profileSignLabel}`, "title"),
          gexTooltipLine(`Net GEX ${netValue !== null ? gexDollarText(netValue) : "-"}`, "meta"),
          structuralLabel ? gexTooltipLine(`Aggregate strike structure: ${structuralLabel}`, "role") : null,
          ...gexGammaModeLines(profileSign).map((line, index) => gexTooltipLine(line, index === 0 ? (profileSign > 0 ? "positive" : profileSign < 0 ? "negative" : "meta") : "meta")),
          gexTooltipLine("PROFILE", "plan"),
          ...profileLines,
        ]);
      }
      return gexTooltipAppend(
        gexLevelTooltip(gex, level, {
          title: `${title} ${fmt(row.strike)} · ${level.kind || "GEX level"}`,
          netGexValue: netValue,
        }),
        [
          gexTooltipLine("--------------------", "separator"),
          gexTooltipLine("PROFILE", "plan"),
          ...profileLines,
        ],
      );
    }
