    const optionDriftState = {
      requestKey: "",
      activeRequestKey: "",
      payload: null,
      loading: false,
      error: "",
      loadedAtMs: 0,
      generation: 0,
      controller: null,
    };

    function cancelOptionDriftRead() {
      optionDriftState.generation += 1;
      optionDriftState.controller?.abort();
      optionDriftState.controller = null;
      optionDriftState.activeRequestKey = "";
      optionDriftState.loading = false;
    }

    function optionDriftScope(snapshot = state.snapshot) {
      const meta = snapshot?.meta || {};
      const requestedMode = String(
        state.indicators?.optionDrift?.captureMode || "live",
      ).toLowerCase();
      return {
        instrumentId: exactIdentityText(meta.instrument_id || state.instrumentId),
        routeFingerprint: exactIdentityText(
          meta.route_fingerprint || instrumentRouteFingerprint(),
        ),
        captureMode: requestedMode === "live" ? "live" : "request",
      };
    }

    function optionDriftScopeKey(scope) {
      return JSON.stringify([
        scope.instrumentId,
        scope.routeFingerprint,
        scope.captureMode,
      ]);
    }

    function optionDriftPayloadMatchesScope(payload, scope) {
      return Boolean(
        payload?.ok
        && payload.advisory_only === true
        && payload.decision_eligible === false
        && exactIdentityText(payload.instrument_id) === scope.instrumentId
        && exactIdentityText(payload.route_fingerprint) === scope.routeFingerprint
        && String(payload.capture_mode || "") === scope.captureMode
      );
    }

    async function syncOptionDrift(options = {}) {
      if (options.reset || !indicatorCalcForId("option_drift")) {
        cancelOptionDriftRead();
        optionDriftState.requestKey = "";
        optionDriftState.payload = null;
        optionDriftState.error = "";
        optionDriftState.loadedAtMs = 0;
        return null;
      }
      const scope = optionDriftScope(options.snapshot || state.snapshot);
      if (!scope.instrumentId || !scope.routeFingerprint) {
        cancelOptionDriftRead();
        return null;
      }
      const requestKey = optionDriftScopeKey(scope);
      const recentlyLoaded = (
        optionDriftState.requestKey === requestKey
        && Date.now() - optionDriftState.loadedAtMs < 10_000
      );
      if (
        !options.force
        && (recentlyLoaded || optionDriftState.activeRequestKey === requestKey)
      ) return optionDriftState.payload;
      cancelOptionDriftRead();
      const generation = optionDriftState.generation;
      const controller = new AbortController();
      optionDriftState.controller = controller;
      optionDriftState.activeRequestKey = requestKey;
      optionDriftState.loading = true;
      optionDriftState.error = "";
      try {
        const query = new URLSearchParams({
          instrument_id: scope.instrumentId,
          route_fingerprint: scope.routeFingerprint,
          capture_mode: scope.captureMode,
        });
        const payload = await fetchJson(
          `/api/indicators/option-drift/state?${query.toString()}`,
          {
            timeoutMs: 7000,
            dedupe: false,
            cache: "no-store",
            signal: controller.signal,
          },
        );
        const currentScope = optionDriftScope(state.snapshot);
        if (
          optionDriftState.generation !== generation
          || optionDriftState.controller !== controller
          || controller.signal.aborted
          || !indicatorCalcForId("option_drift")
          || optionDriftScopeKey(currentScope) !== requestKey
          || !optionDriftPayloadMatchesScope(payload, currentScope)
        ) return null;
        optionDriftState.payload = payload;
        optionDriftState.requestKey = requestKey;
        optionDriftState.loadedAtMs = Date.now();
        if (typeof bumpChartRenderVersion === "function") bumpChartRenderVersion("indicators");
        if (state.snapshot) {
          renderCharts(state.snapshot, { overlayImmediate: true, reason: "option-drift" });
        }
        return payload;
      } catch (error) {
        if (
          optionDriftState.generation === generation
          && optionDriftState.controller === controller
          && !controller.signal.aborted
        ) {
          optionDriftState.error = requestErrorMessage(error, "Option Drift unavailable");
        }
        return null;
      } finally {
        if (
          optionDriftState.generation === generation
          && optionDriftState.controller === controller
        ) {
          optionDriftState.controller = null;
          optionDriftState.activeRequestKey = "";
          optionDriftState.loading = false;
        }
      }
    }

    function optionDriftMoney(value) {
      const number = Number(value);
      if (!Number.isFinite(number)) return "-";
      const absolute = Math.abs(number);
      const sign = number < 0 ? "-" : number > 0 ? "+" : "";
      if (absolute >= 1_000_000) return `${sign}$${(absolute / 1_000_000).toFixed(1)}M`;
      if (absolute >= 1_000) return `${sign}$${(absolute / 1_000).toFixed(0)}K`;
      return `${sign}$${absolute.toFixed(0)}`;
    }

    function optionDriftActivityMoney(value) {
      const number = Number(value);
      if (!Number.isFinite(number) || number < 0) return "-";
      if (number >= 1_000_000) return `$${(number / 1_000_000).toFixed(1)}M`;
      if (number >= 1_000) return `$${(number / 1_000).toFixed(0)}K`;
      return `$${number.toFixed(0)}`;
    }

    function optionDriftPresentation(code) {
      const presentations = {
        CALL_CONTROL: { text: "CALL", arrow: "↑", direction: "long", tone: "positive", actionClass: "ok" },
        PUT_CONTROL: { text: "PUT", arrow: "↓", direction: "short", tone: "negative", actionClass: "bad" },
        CONFLICT: { text: "CHOP", arrow: "↔", direction: "", tone: "warning", actionClass: "warn" },
        TRANSITION: { text: "SHIFT", arrow: "⇅", direction: "", tone: "warning", actionClass: "warn" },
        BALANCE: { text: "EVEN", arrow: "→", direction: "", tone: "flat", actionClass: "warn" },
        STALE: { text: "STALE", arrow: "·", direction: "", tone: "warning", actionClass: "bad" },
        BASELINING: { text: "BASE", arrow: "…", direction: "", tone: "flat", actionClass: "warn" },
      };
      return presentations[String(code || "").toUpperCase()] || presentations.BASELINING;
    }

    function optionDriftDirectionalValue(kind, value) {
      const number = Number(value);
      if (!Number.isFinite(number) || number === 0) {
        return { arrow: "→", direction: "", market: "NEUTRAL", tone: "flat" };
      }
      const bullish = kind === "put" ? number < 0 : number > 0;
      return bullish
        ? { arrow: "↑", direction: "long", market: "BULLISH", tone: "positive" }
        : { arrow: "↓", direction: "short", market: "BEARISH", tone: "negative" };
    }

    function optionDriftCurrentDirectionContext(kind, value) {
      const number = Number(value);
      const view = optionDriftDirectionalValue(kind, number);
      const label = kind === "put" ? "PUT" : kind === "balance" ? "BALANCE" : "CALL";
      if (!Number.isFinite(number) || number === 0) {
        return `NOW: ${view.arrow} ${view.market}. ${label} drift is zero, so this cell has no inferred market direction.`;
      }
      const sign = number > 0 ? "positive (+)" : "negative (-)";
      const reason = kind === "put"
        ? number > 0
          ? "Put prices are stronger than the underlying move alone explains."
          : "Put prices are weaker than the underlying move alone explains."
        : kind === "call"
          ? number > 0
            ? "Call prices are stronger than the underlying move alone explains."
            : "Call prices are weaker than the underlying move alone explains."
          : number > 0
            ? "Call-side drift exceeds put-side drift."
            : "Put-side drift exceeds call-side drift.";
      return `NOW: ${view.arrow} ${view.market}. ${label} drift is ${sign} at ${optionDriftMoney(number)}. ${reason}`;
    }

    function optionDriftSignMap(kind) {
      if (kind === "put") {
        return "PUT sign map: + means bearish put strength; - means bullish put weakness; zero means neutral.";
      }
      if (kind === "balance") {
        return "BALANCE sign map: + means bullish call dominance; - means bearish put dominance; zero means even.";
      }
      return "CALL sign map: + means bullish call strength; - means bearish call weakness; zero means neutral.";
    }

    function optionDriftChopContext(metrics) {
      const score = Number(metrics?.chop_score || 0);
      if (score >= 70) {
        return `NOW: HIGH CONFLICT at ${score.toFixed(0)}%. Two-sided or crossing pressure overrides a clean directional read.`;
      }
      if (score >= 55) {
        return `NOW: TRANSITIONAL CHOP at ${score.toFixed(0)}%. Direction is less stable even if one side currently leads.`;
      }
      return `NOW: LOW CHOP at ${score.toFixed(0)}%. Chop alone does not block a directional state.`;
    }

    function optionDriftTheoryLine(text, role = "meta", tokenRoles = {}) {
      return { text, role, token_roles: tokenRoles };
    }

    function optionDriftTheorySections(kind) {
      const commonLimit = optionDriftTheoryLine(
        "Ограничение: это эвристика по снимкам option volume и цен, не поток сделок и не доказательство buyer/seller initiation.",
        "meta",
        { "не поток сделок": "warning", "buyer/seller initiation": "warning" },
      );
      const sections = {
        table: [
          optionDriftTheoryLine(
            "Option Drift оценивает, какая сторона опционов показывает движение цены сверх изменения, объяснённого базовым активом.",
            "role",
            { "Option Drift": "type" },
          ),
          optionDriftTheoryLine(
            "DRIFT = новая активность × остаточное движение цены опциона.",
            "meta",
            { DRIFT: "type" },
          ),
          optionDriftTheoryLine(
            "Остаток = изменение цены опциона − option delta × изменение цены базового актива.",
            "meta",
            { "option delta": "type" },
          ),
          optionDriftTheoryLine(
            "↑ — bullish-уклон, ↓ — bearish-уклон, → — нейтрально; ↔ и ⇅ обозначают конфликт или переход, а не движение цены.",
            "flow",
            { "↑": "positive", "↓": "negative", "↔": "warning", "⇅": "warning" },
          ),
          optionDriftTheoryLine(
            "CALL + читается bullish, PUT + читается bearish; знак минус разворачивает соответствующую трактовку.",
            "flow",
            { CALL: "call", PUT: "put", "CALL +": "positive", "PUT +": "negative" },
          ),
          commonLimit,
        ],
        state: [
          optionDriftTheoryLine(
            "STATE объединяет баланс CALL/PUT, CHOP, свежесть и качество данных в одно справочное состояние рынка.",
            "role",
            { STATE: "type", CALL: "call", PUT: "put", CHOP: "warning" },
          ),
          optionDriftTheoryLine(
            "↑ CALL — call-control; ↓ PUT — put-control; → EVEN — баланс; ↔ CHOP — конфликт; ⇅ SHIFT — переход.",
            "flow",
            { "↑ CALL": "positive", "↓ PUT": "negative", "→ EVEN": "muted", "↔ CHOP": "warning", "⇅ SHIFT": "warning" },
          ),
          optionDriftTheoryLine(
            "… BASE означает недостаточную базу; · STALE — устаревший последний принятый снимок.",
            "meta",
            { "… BASE": "muted", "· STALE": "warning" },
          ),
          optionDriftTheoryLine(
            "CONF измеряет покрытие и надёжность входных данных; это не вероятность будущего движения и не сила направления.",
            "meta",
            { CONF: "strength", "не вероятность": "warning" },
          ),
          commonLimit,
        ],
        call: [
          optionDriftTheoryLine(
            "CALL DRIFT — остаточное изменение цен call-опционов, взвешенное вновь появившейся активностью.",
            "role",
            { "CALL DRIFT": "call", CALL: "call" },
          ),
          optionDriftTheoryLine(
            "+CALL → ↑ bullish: call сильнее движения, объяснённого базовым активом. -CALL → ↓ bearish: call слабее.",
            "flow",
            { "+CALL": "positive", "↑ bullish": "positive", "-CALL": "negative", "↓ bearish": "negative" },
          ),
          optionDriftTheoryLine(
            "ACT = новый volume × средняя цена опциона × multiplier; показатель без направления, поэтому у него нет знака +/−.",
            "meta",
            { ACT: "strength", volume: "type", multiplier: "type" },
          ),
          commonLimit,
        ],
        put: [
          optionDriftTheoryLine(
            "PUT DRIFT — остаточное изменение цен put-опционов, взвешенное вновь появившейся активностью.",
            "role",
            { "PUT DRIFT": "put", PUT: "put" },
          ),
          optionDriftTheoryLine(
            "+PUT → ↓ bearish: put сильнее движения, объяснённого базовым активом. -PUT → ↑ bullish: put слабее.",
            "flow",
            { "+PUT": "negative", "↓ bearish": "negative", "-PUT": "positive", "↑ bullish": "positive" },
          ),
          optionDriftTheoryLine(
            "ACT = новый volume × средняя цена опциона × multiplier; показатель без направления, поэтому у него нет знака +/−.",
            "meta",
            { ACT: "strength", volume: "type", multiplier: "type" },
          ),
          commonLimit,
        ],
        balance: [
          optionDriftTheoryLine(
            "BALANCE = CALL DRIFT − PUT DRIFT. Положительный результат означает call-доминирование, отрицательный — put-доминирование.",
            "role",
            { BALANCE: "type", "CALL DRIFT": "call", "PUT DRIFT": "put" },
          ),
          optionDriftTheoryLine(
            "+BALANCE → ↑ bullish; -BALANCE → ↓ bearish; около нуля → EVEN.",
            "flow",
            { "+BALANCE": "positive", "↑ bullish": "positive", "-BALANCE": "negative", "↓ bearish": "negative", EVEN: "muted" },
          ),
          optionDriftTheoryLine(
            "SHARE = BALANCE / (gross CALL activity + gross PUT activity). Это относительный перевес, не вероятность.",
            "meta",
            { SHARE: "strength", BALANCE: "type", CALL: "call", PUT: "put", "не вероятность": "warning" },
          ),
          commonLimit,
        ],
        chop: [
          optionDriftTheoryLine(
            "CHOP оценивает конфликт по близости BALANCE к нулю, пересечениям направления, неэффективному пути цены и двусторонней активности.",
            "role",
            { CHOP: "warning", BALANCE: "type" },
          ),
          optionDriftTheoryLine(
            "↔ — глиф двустороннего конфликта, а не направление цены. Ниже 55% — low chop; 55–69% — transition; 70%+ — conflict.",
            "flow",
            { "↔": "warning", "55–69%": "warning", "70%+": "negative" },
          ),
          optionDriftTheoryLine(
            "#N — число принятых сопоставимых интервалов. 1m означает, что в live-режиме учитываются только закрытые минутные бакеты.",
            "meta",
            { "#N": "type", "1m": "strength", live: "type" },
          ),
          optionDriftTheoryLine(
            "Высокий CHOP снижает доверие к чистому направлению, но сам по себе не является сигналом входа.",
            "meta",
            { "Высокий CHOP": "warning", "не является сигналом входа": "warning" },
          ),
          commonLimit,
        ],
      };
      return [{ title: `THEORY · ${String(kind || "table").toUpperCase()}`, lines: sections[kind] || sections.table }];
    }

    function optionDriftStateContext(stateCode) {
      const contexts = {
        CALL_CONTROL: "CALL: bullish call control; balance is at least +20% and chop is below 55%.",
        PUT_CONTROL: "PUT: bearish put control; balance is at most -20% and chop is below 55%.",
        CONFLICT: "CHOP: opposing pressure; chop reached 70% or the balance just crossed direction.",
        TRANSITION: "SHIFT: direction is changing, weak, or chop is between 55% and 70%.",
        BALANCE: "EVEN: call/put balance is inside the neutral ±12% band.",
        STALE: "STALE: the newest accepted GEX observation is more than 15 minutes old.",
        BASELINING: "BASE: fewer than 3 comparable intervals, confidence below 30%, or no gross activity.",
      };
      return contexts[String(stateCode || "").toUpperCase()] || contexts.BASELINING;
    }

    function optionDriftTableOverlays(snapshot) {
      const scope = optionDriftScope(snapshot);
      const payload = optionDriftState.payload;
      if (
        !indicatorCalcForId("option_drift")
        || indicatorVisibleForId("option_drift") === false
        || !optionDriftPayloadMatchesScope(payload, scope)
      ) return [];
      const metrics = payload.metrics || {};
      const quality = payload.quality || {};
      const presentation = optionDriftPresentation(payload.state_code);
      const callDirection = optionDriftDirectionalValue("call", metrics.call_drift);
      const putDirection = optionDriftDirectionalValue("put", metrics.put_drift);
      const balanceDirection = optionDriftDirectionalValue("balance", metrics.balance);
      const liveMinuteActive = quality.live_minute_active === true;
      const intervalLabel = `#${Number(quality.accepted_intervals || 0)}${liveMinuteActive ? " · 1m" : ""}`;
      return [{
        type: "table",
        source: "option_drift",
        tone: presentation.tone,
        table: {
          id: "option_drift",
          title: "Option Drift",
          tone: presentation.tone,
          state_code: payload.state_code,
          context_lines: [
            "Advisory heuristic from incremental option volume and delta-adjusted option-price residuals.",
            "Arrows are market bias, not value movement: ↑ bullish, ↓ bearish, → neutral or even.",
            "State glyphs: ↔ conflict or two-sided, ⇅ transition, … baselining, · stale.",
            "Signs: CALL + is bullish; PUT + is bearish; BALANCE + is bullish. A minus reverses each reading.",
            "ACT is unsigned gross premium activity; # is the accepted-interval counter; 1m means closed live minutes.",
            "Not a trade-print feed and never used by decisions or execution.",
          ],
          tooltip_sections: optionDriftTheorySections("table"),
          columns: [
            {
              cells: ["STATE", `${presentation.arrow} ${presentation.text}`, `CONF ${Math.round(Number(metrics.confidence || 0) * 100)}%`],
              tone: presentation.tone,
              direction: presentation.direction,
              tooltip_mode: "details",
              context_lines: [
                optionDriftStateContext(payload.state_code),
                `Current state glyph: ${presentation.arrow} ${presentation.text}. It describes the aggregate market state, not movement since refresh.`,
                "State glyph legend: ↑ call control; ↓ put control; ↔ conflict; ⇅ transition; → even; … baselining; · stale.",
                "CONF is data confidence and coverage, not forecast probability or directional conviction.",
                "At least 3 accepted intervals are required; the interval-count factor is full at 6.",
                liveMinuteActive
                  ? "Live one-minute sampling is active; only completed minute buckets are admitted."
                  : "Using persisted history; a live minute bucket has not been admitted yet.",
              ],
              tooltip_sections: optionDriftTheorySections("state"),
            },
            {
              cells: ["CALL", `${callDirection.arrow} ${optionDriftMoney(metrics.call_drift)}`, `ACT ${optionDriftActivityMoney(metrics.call_gross_premium)}`],
              tone: callDirection.tone,
              direction: callDirection.direction,
              tooltip_mode: "details",
              context_lines: [
                optionDriftCurrentDirectionContext("call", metrics.call_drift),
                optionDriftSignMap("call"),
                "The signed amount is option-price residual after subtracting the move explained by option delta, weighted by new volume.",
                "+/- is the residual sign, not buyer/seller initiation and not change since the previous refresh.",
                `ACT ${optionDriftActivityMoney(metrics.call_gross_premium)} is unsigned gross call-premium activity; it measures magnitude only.`,
              ],
              tooltip_sections: optionDriftTheorySections("call"),
            },
            {
              cells: ["PUT", `${putDirection.arrow} ${optionDriftMoney(metrics.put_drift)}`, `ACT ${optionDriftActivityMoney(metrics.put_gross_premium)}`],
              tone: putDirection.tone,
              direction: putDirection.direction,
              tooltip_mode: "details",
              context_lines: [
                optionDriftCurrentDirectionContext("put", metrics.put_drift),
                optionDriftSignMap("put"),
                "The signed amount is option-price residual after subtracting the move explained by option delta, weighted by new volume.",
                "+/- is the residual sign, not buyer/seller initiation and not change since the previous refresh.",
                `ACT ${optionDriftActivityMoney(metrics.put_gross_premium)} is unsigned gross put-premium activity; it measures magnitude only.`,
              ],
              tooltip_sections: optionDriftTheorySections("put"),
            },
            {
              cells: ["BALANCE", `${balanceDirection.arrow} ${optionDriftMoney(metrics.balance)}`, `SHARE ${(Number(metrics.balance_ratio || 0) * 100).toFixed(0)}%`],
              tone: balanceDirection.tone,
              direction: balanceDirection.direction,
              tooltip_mode: "details",
              context_lines: [
                optionDriftCurrentDirectionContext("balance", metrics.balance),
                optionDriftSignMap("balance"),
                "BALANCE = CALL drift - PUT drift, so positive put strength reduces the balance and negative put weakness raises it.",
                `SHARE ${(Number(metrics.balance_ratio || 0) * 100).toFixed(0)}% is signed balance divided by unsigned CALL + PUT activity.`,
                "Rounded cells can differ slightly from arithmetic on the displayed values.",
              ],
              tooltip_sections: optionDriftTheorySections("balance"),
            },
            {
              cells: ["CHOP", `↔ ${Number(metrics.chop_score || 0).toFixed(0)}%`, intervalLabel],
              tone: Number(metrics.chop_score) >= 70 ? "warning" : "flat",
              tooltip_mode: "details",
              context_lines: [
                optionDriftChopContext(metrics),
                "↔ beside CHOP is a two-sided/conflict glyph, not bullish or bearish direction and not a refresh change.",
                "CHOP combines balance proximity, direction crossings, inefficient spot travel, and two-sided activity.",
                "70%+ is conflict; 55–69% is transition unless another state gate takes priority.",
                `Current inputs: ${Number(metrics.crossings || 0)} balance crossing(s), ${(Number(metrics.price_efficiency || 0) * 100).toFixed(0)}% price efficiency, ${(Number(metrics.two_sided_ratio || 0) * 100).toFixed(0)}% two-sided activity.`,
                `#${Number(quality.accepted_intervals || 0)} is the accepted comparison-interval counter, not option delta or a trade count.`,
                liveMinuteActive
                  ? `${Number(quality.live_minute_samples || 0)} completed one-minute live samples are buffered.`
                  : "Persisted observations are used until the first live minute closes.",
              ],
              tooltip_sections: optionDriftTheorySections("chop"),
            },
          ],
        },
      }];
    }

    function optionDriftStateKey() {
      const payload = optionDriftState.payload;
      const config = state.indicators?.optionDrift || {};
      return JSON.stringify([
        optionDriftState.requestKey,
        payload?.revision || "",
        optionDriftState.loading ? "loading" : "idle",
        optionDriftState.error,
        config.tablePosition || "bottom",
      ]);
    }

    function renderOptionDriftPanel(_snapshot, context) {
      const node = context.mount("advisory-note", "indicator-note");
      if (!node) return;
      if (!context.calcEnabled) {
        node.textContent = "Off. No GEX history is read and no market state is projected.";
        return;
      }
      if (optionDriftState.error) {
        node.textContent = optionDriftState.error;
        return;
      }
      const payload = optionDriftState.payload;
      if (!payload) {
        node.textContent = optionDriftState.loading ? "Reading persisted GEX snapshots…" : "Waiting for GEX snapshots.";
        return;
      }
      const metrics = payload.metrics || {};
      const quality = payload.quality || {};
      const presentation = optionDriftPresentation(payload.state_code);
      const correctedContracts = Number(quality.corrected_contract_observations || 0);
      node.textContent = [
        `${presentation.text} · advisory heuristic only`,
        `${Number(quality.accepted_intervals || 0)} comparable intervals`,
        `${String(payload.capture_mode || "live")} history lane`,
        quality.live_minute_active === true
          ? `${Number(quality.live_minute_samples || 0)} closed 1m live samples`
          : "persisted cadence",
        `${Math.round(Number(metrics.covered_volume_ratio || 0) * 100)}% priced volume coverage`,
        correctedContracts > 0
          ? `${correctedContracts} small counter correction${correctedContracts === 1 ? "" : "s"} excluded`
          : "no counter corrections",
        `entitlement ${String(quality.market_data_entitlement || "unknown")}`,
        "not used by decisions or execution",
      ].join(" · ");
    }

    registerIndicatorRuntimeStateRef("option_drift_state", () => {
      if (!indicatorCalcForId("option_drift")) {
        return {
          text: "OFF",
          muted: true,
          actionClass: "bad",
          countsAsSignal: false,
          details: "Calc is off; Option Drift performs no reads or projection.",
        };
      }
      if (optionDriftState.loading && !optionDriftState.payload) {
        return {
          text: "LOAD",
          muted: false,
          actionClass: "warn",
          countsAsSignal: false,
          details: "Reading exact-route persisted GEX snapshots.",
        };
      }
      if (optionDriftState.error) {
        return {
          text: "ERROR",
          muted: false,
          actionClass: "bad",
          countsAsSignal: false,
          details: optionDriftState.error,
        };
      }
      const payload = optionDriftState.payload;
      if (!payload) {
        return {
          text: "WAIT",
          muted: false,
          actionClass: "warn",
          countsAsSignal: false,
          details: "Waiting for comparable persisted GEX snapshots.",
        };
      }
      const presentation = optionDriftPresentation(payload.state_code);
      const metrics = payload.metrics || {};
      const quality = payload.quality || {};
      return {
        text: presentation.text,
        muted: false,
        actionClass: presentation.actionClass,
        countsAsSignal: false,
        details: [
          "Advisory heuristic only; never consumed by decisions or execution.",
          `Balance ${optionDriftMoney(metrics.balance)}.`,
          `Chop ${Number(metrics.chop_score || 0).toFixed(0)}%.`,
          `Confidence ${Math.round(Number(metrics.confidence || 0) * 100)}%.`,
          `${Number(quality.accepted_intervals || 0)} comparable intervals.`,
        ].join(" "),
      };
    });

    registerIndicatorLifecycleSync(
      "option_drift",
      syncOptionDrift,
      { poll: true },
    );
    registerIndicatorProcessEffect("option_drift_reload", () => {
      void syncOptionDrift({ force: true });
      return false;
    });
    registerIndicatorControlEffect("option_drift_capture_mode", () => {
      optionDriftState.requestKey = "";
      optionDriftState.payload = null;
      void syncOptionDrift({ force: true });
      return false;
    });
    registerIndicatorPanelRenderer("option_drift", renderOptionDriftPanel);
    registerIndicatorOverlayContribution("option_drift", {
      layer: "tables",
      enabled: () => (
        indicatorCalcForId("option_drift")
        && indicatorVisibleForId("option_drift") !== false
      ),
      collect: optionDriftTableOverlays,
      stateKey: optionDriftStateKey,
    });
