const GEX_SOURCE_BY_CAPTURE_MODE = Object.freeze({
      request: "gex:ibkr",
      live: "gex:ibkr-live",
    });
const GEX_READ_MODEL_STATUS_CODES = new Set([
      "degraded",
      "disabled",
      "error",
      "failed",
      "loading",
      "missing",
      "refresh_blocked",
      "refresh_error",
      "session_closed",
      "session_unknown",
      "sleeping",
      "timeout",
      "unavailable",
      "unsupported",
    ]);
const GEX_MARKET_DATA_ENTITLEMENTS = Object.freeze([
      "live",
      "frozen",
      "delayed",
      "delayed_frozen",
      "unknown",
    ]);
const GEX_COMPARISON_SCOPE_FIELDS = Object.freeze([
      "capture_mode",
      "strike_count",
      "strike_ladder",
      "contract_con_ids",
      "expiries",
      "futures_options",
      "series",
      "risk_free_rate",
      "dividend_yield",
      "market_data_entitlement",
    ]);
const GEX_OPTION_SERIES_IDENTITY_FIELDS = Object.freeze([
      "expiry",
      "trading_class",
      "exchange",
      "multiplier",
    ]);
const GEX_AWARE_CAPTURED_AT_PATTERN = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$/;
const GEX_PUBLIC_LEVEL_FIELDS = Object.freeze([
      "price",
      "kind",
      "kind_class",
      "strength",
      "power_class",
      "selection_rank",
      "net_gex",
      "call_gex",
      "put_gex",
      "abs_gex",
      "abs_flow_1pt",
      "distance_from_spot",
      "zone_half_width",
      "spot_side",
      "option_volume_context",
    ]);
const GEX_OPTION_VOLUME_CURRENT_FIELDS = Object.freeze([
      "call_volume", "put_volume", "total_volume", "call_oi", "put_oi", "total_oi",
      "turnover", "rank",
    ]);
const GEX_OPTION_VOLUME_EVENT_FIELDS = Object.freeze([
      "call_volume_delta", "put_volume_delta", "total_volume_delta", "bias",
      "call_participation", "put_participation", "turnover", "acceleration",
      "flow_per_point", "spot_move_points", "material", "interaction",
      "cross_direction", "state", "window_seconds", "source",
    ]);
const GEX_EXPIRY_PROFILE_FIELDS = Object.freeze([
      "expiry", "strike", "call_gex", "put_gex", "net_gex", "abs_gex",
    ]);
const GEX_HISTORY_WIRE_FIELDS = Object.freeze([
      "captured_at", "timestamp_unix_ms", "capture_mode", "source",
      "capture_revision", "option_universe_expires_at",
      "market_data_entitlement", "open_interest_as_of", "comparison_scope",
      "spot", "gamma_flip", "levels", "valid_until_unix_ms", "projection_mode",
      "projection_bucket_minutes",
    ]);
// Admitted GEX read models are immutable by contract. The websocket path attaches
// retained history before admission; no consumer mutates payloads or nested lanes after it.
const gexPayloadAdmissionCache = new WeakMap();
const gexFrozenHistoryAdmissionCache = new WeakSet();

    function gexExpiryIsCanonical(expiry) {
      if (typeof expiry !== "string" || !/^\d{8}$/.test(expiry)) return false;
      const year = Number(expiry.slice(0, 4));
      const month = Number(expiry.slice(4, 6));
      const day = Number(expiry.slice(6, 8));
      const date = new Date(Date.UTC(year, month - 1, day));
      return date.getUTCFullYear() === year
        && date.getUTCMonth() === month - 1
        && date.getUTCDate() === day;
    }

    function gexOptionUniverseExpiryMs(payload) {
      const value = payload?.option_universe_expires_at;
      if (
        typeof value !== "string"
        || !GEX_AWARE_CAPTURED_AT_PATTERN.test(value)
      ) return null;
      const expiryMs = Date.parse(value);
      return Number.isFinite(expiryMs) ? expiryMs : null;
    }

    function gexExpiryProfileIsCanonical(rows) {
      if (!Array.isArray(rows)) return false;
      const seen = new Set();
      let previousExpiry = null;
      let previousStrike = null;
      for (const row of rows) {
        if (
          !row
          || typeof row !== "object"
          || Object.keys(row).length !== GEX_EXPIRY_PROFILE_FIELDS.length
          || GEX_EXPIRY_PROFILE_FIELDS.some(field => !Object.prototype.hasOwnProperty.call(row, field))
          || !gexExpiryIsCanonical(row.expiry)
        ) return false;
        const numeric = ["strike", "call_gex", "put_gex", "net_gex", "abs_gex"]
          .map(field => gexNullableNumber(row[field]));
        if (
          numeric.some(value => value === null)
          || row.strike <= 0
          || row.call_gex < 0
          || row.put_gex > 0
          || row.abs_gex <= 0
          || Math.abs(row.net_gex - row.call_gex - row.put_gex) > 1e-6
          || Math.abs(row.abs_gex - Math.abs(row.call_gex) - Math.abs(row.put_gex)) > 1e-6
        ) return false;
        const key = JSON.stringify([row.expiry, row.strike]);
        if (
          seen.has(key)
          || (
            previousExpiry !== null
            && (
              row.expiry < previousExpiry
              || (row.expiry === previousExpiry && row.strike <= previousStrike)
            )
          )
        ) return false;
        seen.add(key);
        previousExpiry = row.expiry;
        previousStrike = row.strike;
      }
      return true;
    }

    function gexContextCapturedAt(payload) {
      return typeof payload?.captured_at === "string" ? payload.captured_at : "";
    }

    function gexMarketDataEntitlement(payload) {
      const entitlement = payload?.market_data_entitlement;
      return typeof entitlement === "string" && GEX_MARKET_DATA_ENTITLEMENTS.includes(entitlement)
        ? entitlement
        : "";
    }

    function gexMarketDataEntitlementLabel(payload, compact = false) {
      const entitlement = gexMarketDataEntitlement(payload);
      if (compact) {
        return {
          live: "RT",
          frozen: "FRZ",
          delayed: "DLY",
          delayed_frozen: "DLY-FRZ",
          unknown: "ENT?",
        }[entitlement] || "ENT?";
      }
      return {
        live: "REALTIME",
        frozen: "FROZEN",
        delayed: "DELAYED",
        delayed_frozen: "DELAYED FROZEN",
        unknown: "ENTITLEMENT UNKNOWN",
      }[entitlement] || "ENTITLEMENT UNKNOWN";
    }

    function gexComparisonScopeIsCanonical(scope, options = {}) {
      const allowUnknownEntitlement = options.allowUnknownEntitlement === true;
      if (
        !scope
        || typeof scope !== "object"
        || Array.isArray(scope)
        || Object.keys(scope).length !== GEX_COMPARISON_SCOPE_FIELDS.length
        || GEX_COMPARISON_SCOPE_FIELDS.some(field => !Object.prototype.hasOwnProperty.call(scope, field))
        || !["request", "live"].includes(scope.capture_mode)
        || !Number.isInteger(scope.strike_count)
        || scope.strike_count <= 0
        || !Array.isArray(scope.strike_ladder)
        || scope.strike_ladder.length !== scope.strike_count
        || !scope.strike_ladder.every((strike, index) => (
          typeof strike === "number"
          && Number.isFinite(strike)
          && strike > 0
          && (index === 0 || strike > scope.strike_ladder[index - 1])
        ))
        || !Array.isArray(scope.contract_con_ids)
        || scope.contract_con_ids.length < scope.strike_count * 2
        || scope.contract_con_ids.length % 2 !== 0
        || !scope.contract_con_ids.every((conId, index) => (
          Number.isInteger(conId)
          && conId > 0
          && (index === 0 || conId > scope.contract_con_ids[index - 1])
        ))
        || !Array.isArray(scope.expiries)
        || !scope.expiries.length
        || typeof scope.futures_options !== "boolean"
        || !Array.isArray(scope.series)
        || !scope.series.length
        || typeof scope.risk_free_rate !== "number"
        || !Number.isFinite(scope.risk_free_rate)
        || scope.risk_free_rate < 0
        || scope.risk_free_rate > 0.25
        || typeof scope.dividend_yield !== "number"
        || !Number.isFinite(scope.dividend_yield)
        || scope.dividend_yield < 0
        || scope.dividend_yield > 0.25
        || !GEX_MARKET_DATA_ENTITLEMENTS.includes(scope.market_data_entitlement)
        || (scope.market_data_entitlement === "unknown" && !allowUnknownEntitlement)
      ) return false;

      const expirySet = new Set();
      let previousExpiry = null;
      for (const expiry of scope.expiries) {
        if (
          !gexExpiryIsCanonical(expiry)
          || expirySet.has(expiry)
          || (previousExpiry !== null && expiry <= previousExpiry)
        ) return false;
        expirySet.add(expiry);
        previousExpiry = expiry;
      }

      const seriesExpiries = [];
      let previousSeries = null;
      for (const identity of scope.series) {
        if (
          !identity
          || typeof identity !== "object"
          || Array.isArray(identity)
          || Object.keys(identity).length !== GEX_OPTION_SERIES_IDENTITY_FIELDS.length
          || GEX_OPTION_SERIES_IDENTITY_FIELDS.some(field => !Object.prototype.hasOwnProperty.call(identity, field))
          || typeof identity.expiry !== "string"
          || !expirySet.has(identity.expiry)
          || typeof identity.trading_class !== "string"
          || !identity.trading_class
          || identity.trading_class !== identity.trading_class.trim()
          || typeof identity.exchange !== "string"
          || !identity.exchange
          || identity.exchange !== identity.exchange.trim()
          || typeof identity.multiplier !== "number"
          || !Number.isFinite(identity.multiplier)
          || identity.multiplier <= 0
        ) return false;
        if (previousSeries !== null) {
          const outOfOrder = identity.expiry < previousSeries.expiry
            || (
              identity.expiry === previousSeries.expiry
              && identity.trading_class < previousSeries.trading_class
            )
            || (
              identity.expiry === previousSeries.expiry
              && identity.trading_class === previousSeries.trading_class
              && identity.exchange < previousSeries.exchange
            )
            || (
              identity.expiry === previousSeries.expiry
              && identity.trading_class === previousSeries.trading_class
              && identity.exchange === previousSeries.exchange
              && identity.multiplier <= previousSeries.multiplier
            );
          if (outOfOrder) return false;
        }
        if (seriesExpiries[seriesExpiries.length - 1] !== identity.expiry) {
          seriesExpiries.push(identity.expiry);
        }
        previousSeries = identity;
      }
      return seriesExpiries.length === scope.expiries.length
        && seriesExpiries.every((expiry, index) => expiry === scope.expiries[index]);
    }

    function gexComparisonScopesEqual(left, right) {
      if (!gexComparisonScopeIsCanonical(left) || !gexComparisonScopeIsCanonical(right)) return false;
      if (
        left.capture_mode !== right.capture_mode
        || left.strike_count !== right.strike_count
        || left.futures_options !== right.futures_options
        || left.risk_free_rate !== right.risk_free_rate
        || left.dividend_yield !== right.dividend_yield
        || left.market_data_entitlement !== right.market_data_entitlement
        || left.expiries.length !== right.expiries.length
        || left.series.length !== right.series.length
        || left.strike_ladder.length !== right.strike_ladder.length
        || left.contract_con_ids.length !== right.contract_con_ids.length
        || left.expiries.some((expiry, index) => expiry !== right.expiries[index])
        || left.strike_ladder.some((strike, index) => strike !== right.strike_ladder[index])
        || left.contract_con_ids.some((conId, index) => conId !== right.contract_con_ids[index])
      ) return false;
      return left.series.every((identity, index) => {
        const other = right.series[index];
        return identity.expiry === other.expiry
          && identity.trading_class === other.trading_class
          && identity.exchange === other.exchange
          && identity.multiplier === other.multiplier;
      });
    }

    function gexOpenInterestAsOf(payload) {
      return payload?.open_interest_as_of === "previous_settlement"
        ? "previous_settlement"
        : "";
    }

    function gexPayloadMarketDataEntitlementIsCanonical(payload, options = {}) {
      const entitlement = gexMarketDataEntitlement(payload);
      const comparisonScope = payload?.comparison_scope;
      const requireComparisonScope = options.requireComparisonScope !== false;
      const requireDecisionAuthority = options.requireDecisionAuthority !== false;
      const comparisonScopeIsCanonical = comparisonScope === undefined
        ? !requireComparisonScope
        : Boolean(
          gexComparisonScopeIsCanonical(comparisonScope, {
            allowUnknownEntitlement: entitlement === "unknown",
          })
          && comparisonScope.capture_mode === payload?.capture_mode
          && comparisonScope.market_data_entitlement === entitlement
        );
      const decisionAuthorityIsCanonical = !requireDecisionAuthority || (
        typeof payload?.decision_authoritative === "boolean"
        && (
          payload.decision_authoritative === false
          || (
            ["ok", "degraded"].includes(payload?.status)
            && payload?.frame_complete === true
            && entitlement === "live"
          )
        )
      );
      return Boolean(
        entitlement
        && comparisonScopeIsCanonical
        && decisionAuthorityIsCanonical
        && (
          entitlement !== "unknown"
          || (payload?.status === "partial" && payload?.decision_authoritative === false)
        )
      );
    }

    function gexPayloadRevision(payload) {
      const captureMode = typeof payload?.capture_mode === "string" ? payload.capture_mode : "";
      if (payload?.source !== GEX_SOURCE_BY_CAPTURE_MODE[captureMode]) return "";
      const capturedAt = gexContextCapturedAt(payload);
      const revision = typeof payload?.capture_revision === "string" ? payload.capture_revision : "";
      if (
        !GEX_AWARE_CAPTURED_AT_PATTERN.test(capturedAt)
        || !GEX_AWARE_CAPTURED_AT_PATTERN.test(revision)
      ) return "";
      const capturedMs = Date.parse(capturedAt);
      const revisionMs = Date.parse(revision);
      if (!Number.isFinite(capturedMs) || !Number.isFinite(revisionMs)) return "";
      if (captureMode === "request") return revisionMs === capturedMs ? revision : "";
      if (captureMode !== "live") return "";
      const bucketMs = GEX_LIVE_CAPTURE_REVISION_MS;
      return revisionMs === Math.floor(capturedMs / bucketMs) * bucketMs ? revision : "";
    }


    function gexHistoryTimestampMs(row) {
      const capturedAt = row?.captured_at;
      const timestampUnixMs = row?.timestamp_unix_ms;
      if (typeof capturedAt !== "string" || !GEX_AWARE_CAPTURED_AT_PATTERN.test(capturedAt)) return null;
      if (typeof timestampUnixMs !== "number" || !Number.isInteger(timestampUnixMs)) return null;
      const parsedMs = Date.parse(capturedAt);
      return Number.isFinite(parsedMs) && parsedMs === timestampUnixMs ? parsedMs : null;
    }

    function gexLevelMotionIsCanonical(motion) {
      if (!motion || typeof motion !== "object" || Array.isArray(motion)) return false;
      const sides = Object.keys(motion);
      if (!sides.length || sides.some(side => !["call", "put", "net"].includes(side))) return false;
      const valueFields = ["type", "side", "from", "to", "delta", "delta_pct", "window_seconds"];
      const rollFields = ["type", "side", "from_price", "to_price", "window_seconds"];
      const hasExactFields = (event, expected) => {
        const fields = Object.keys(event);
        return fields.length === expected.length
          && fields.every(field => expected.includes(field))
          && expected.every(field => Object.prototype.hasOwnProperty.call(event, field));
      };
      for (const side of sides) {
        const event = motion[side];
        if (!event || typeof event !== "object" || Array.isArray(event) || event.side !== side) return false;
        const type = event.type;
        const windowSeconds = gexNullableNumber(event.window_seconds);
        if (windowSeconds === null || windowSeconds <= 0) return false;
        if (["roll_up", "roll_down"].includes(type)) {
          if (!["call", "put"].includes(side) || !hasExactFields(event, rollFields)) return false;
          const previous = gexNullableNumber(event.from_price);
          const current = gexNullableNumber(event.to_price);
          if (previous === null || current === null || previous === current) return false;
          if ((type === "roll_up") !== (current > previous)) return false;
          continue;
        }
        if (
          !["strengthening", "weakening", "sign_flip_positive", "sign_flip_negative"].includes(type)
          || !hasExactFields(event, valueFields)
        ) return false;
        const previous = gexNullableNumber(event.from);
        const current = gexNullableNumber(event.to);
        const delta = gexNullableNumber(event.delta);
        const deltaPct = event.delta_pct;
        if (
          previous === null
          || current === null
          || delta === null
          || Math.abs(delta - (current - previous)) > 1e-3
          || (deltaPct !== null && gexNullableNumber(deltaPct) === null)
        ) return false;
        if (type === "sign_flip_positive" && !(previous < 0 && current > 0)) return false;
        if (type === "sign_flip_negative" && !(previous > 0 && current < 0)) return false;
      }
      return true;
    }

    function gexPublicLevelsAreCanonical(levels, spot) {
      const exactSpot = gexNullableNumber(spot);
      if (!Array.isArray(levels) || exactSpot === null || exactSpot <= 0) return false;
      const ranks = [];
      const prices = [];
      const kindClasses = {
        CALL_WALL: "call",
        PUT_WALL: "put",
        POS_GAMMA_NODE: "positive_node",
        NEG_GAMMA_NODE: "negative_node",
        GEX_NODE: "neutral_node",
      };
      for (const level of levels) {
        if (!level || typeof level !== "object") return false;
        const fields = Object.keys(level);
        if (
          ![GEX_PUBLIC_LEVEL_FIELDS.length, GEX_PUBLIC_LEVEL_FIELDS.length + 1].includes(fields.length)
          || fields.some(field => !GEX_PUBLIC_LEVEL_FIELDS.includes(field) && field !== "motion")
          || GEX_PUBLIC_LEVEL_FIELDS.some(field => !Object.prototype.hasOwnProperty.call(level, field))
          || (Object.prototype.hasOwnProperty.call(level, "motion") && !gexLevelMotionIsCanonical(level.motion))
        ) return false;
        if (typeof level.price !== "number" || !Number.isFinite(level.price) || level.price <= 0) return false;
        if (typeof level.selection_rank !== "number" || !Number.isInteger(level.selection_rank) || level.selection_rank <= 0) return false;
        const strength = gexNullableNumber(level.strength);
        const expectedPowerClass = strength !== null && strength < 0.10
          ? "WEAK"
          : strength !== null && strength >= 0.85
            ? "EXTREME"
            : strength !== null && strength >= 0.55
              ? "STRONG"
              : "MEDIUM";
        const numericFacts = [
          "net_gex", "call_gex", "put_gex", "abs_gex", "abs_flow_1pt",
          "distance_from_spot", "zone_half_width",
        ].map(field => gexNullableNumber(level[field]));
        if (
          !Object.prototype.hasOwnProperty.call(kindClasses, level.kind)
          || level.kind_class !== kindClasses[level.kind]
          || strength === null
          || strength < 0
          || strength > 1
          || level.power_class !== expectedPowerClass
          || numericFacts.some(value => value === null)
          || level.abs_gex <= 0
          || level.call_gex < 0
          || level.put_gex > 0
          || level.abs_flow_1pt <= 0
          || level.zone_half_width <= 0
          || Math.abs(level.net_gex - (level.call_gex + level.put_gex)) > 1e-6
          || Math.abs(level.abs_gex - (Math.abs(level.call_gex) + Math.abs(level.put_gex))) > 1e-6
        ) return false;
        const expectedDistance = level.price - exactSpot;
        const expectedAbsFlow = level.abs_gex / (exactSpot * 0.01);
        const expectedSpotSide = level.price > exactSpot * 1.000001
          ? "above"
          : level.price < exactSpot * 0.999999
            ? "below"
            : "inside";
        if (
          Math.abs(level.distance_from_spot - expectedDistance) > 1e-8
          || Math.abs(level.abs_flow_1pt - expectedAbsFlow) > Math.max(1e-6, Math.abs(expectedAbsFlow) * 1e-9)
          || level.spot_side !== expectedSpotSide
        ) return false;
        const volumeContext = level.option_volume_context;
        const current = volumeContext?.current;
        const event = volumeContext?.event;
        if (
          !volumeContext
          || typeof volumeContext !== "object"
          || Object.keys(volumeContext).length !== 2
          || !Object.prototype.hasOwnProperty.call(volumeContext, "current")
          || !Object.prototype.hasOwnProperty.call(volumeContext, "event")
          || !current
          || typeof current !== "object"
          || Object.keys(current).length !== GEX_OPTION_VOLUME_CURRENT_FIELDS.length
          || GEX_OPTION_VOLUME_CURRENT_FIELDS.some(field => !Object.prototype.hasOwnProperty.call(current, field))
        ) return false;
        const currentNumbers = [
          "call_volume", "put_volume", "total_volume", "call_oi", "put_oi", "total_oi", "turnover",
        ];
        if (currentNumbers.some(field => current[field] !== null && (gexNullableNumber(current[field]) === null || current[field] < 0))) return false;
        if (current.rank !== null && (!Number.isInteger(current.rank) || current.rank <= 0)) return false;
        const volumeSidesComplete = current.call_volume !== null && current.put_volume !== null;
        const oiSidesComplete = current.call_oi !== null && current.put_oi !== null;
        if ((current.total_volume !== null) !== volumeSidesComplete) return false;
        if ((current.total_oi !== null) !== oiSidesComplete) return false;
        if (current.total_volume !== null
          && Math.abs(current.total_volume - current.call_volume - current.put_volume) > 1e-6) return false;
        if (current.total_oi !== null
          && Math.abs(current.total_oi - current.call_oi - current.put_oi) > 1e-6) return false;
        if (current.total_volume !== null && current.total_oi !== null && current.total_oi > 0) {
          if (current.turnover === null || Math.abs(current.turnover - current.total_volume / current.total_oi) > 5e-5) return false;
        } else if (current.turnover !== null) return false;
        if (event !== null && (
          typeof event !== "object"
          || Object.keys(event).length !== GEX_OPTION_VOLUME_EVENT_FIELDS.length
          || GEX_OPTION_VOLUME_EVENT_FIELDS.some(field => !Object.prototype.hasOwnProperty.call(event, field))
        )) return false;
        if (event !== null) {
          const nonnegativeEventNumbers = [
            "call_volume_delta", "put_volume_delta", "total_volume_delta", "turnover",
            "acceleration", "flow_per_point", "window_seconds",
          ];
          if (nonnegativeEventNumbers.some(field => event[field] !== null
            && (gexNullableNumber(event[field]) === null || event[field] < 0))) return false;
          if (["bias", "call_participation", "put_participation"].some(field => event[field] !== null
            && gexNullableNumber(event[field]) === null)) return false;
          if (event.spot_move_points !== null && gexNullableNumber(event.spot_move_points) === null) return false;
          if (
            event.call_volume_delta === null
            || event.put_volume_delta === null
            || event.total_volume_delta === null
            || Math.abs(event.total_volume_delta - event.call_volume_delta - event.put_volume_delta) > 1e-6
            || typeof event.material !== "boolean"
            || !["", "up", "down"].includes(event.cross_direction)
            || ![
              "", "activity_away_from_level", "price_approaching_with_activity",
              "price_crossed_with_activity", "price_near_level", "price_stationary_near_activity",
              "spot_moved_away_with_activity", "two_sided_activity_near_level",
            ].includes(event.interaction)
            || ![
              "accelerating", "call_participation", "high_turnover", "put_participation",
              "quiet", "two_sided",
            ].includes(event.state)
            || (event.bias !== null && (event.bias < -1 || event.bias > 1))
            || (event.call_participation !== null && (event.call_participation < 0 || event.call_participation > 1))
            || (event.put_participation !== null && (event.put_participation < 0 || event.put_participation > 1))
            || event.source !== "broker_volume_delta"
          ) return false;
          if (event.total_volume_delta > 0) {
            const expectedBias = (event.call_volume_delta - event.put_volume_delta) / event.total_volume_delta;
            const expectedCallParticipation = event.call_volume_delta / event.total_volume_delta;
            const expectedPutParticipation = event.put_volume_delta / event.total_volume_delta;
            if (
              event.bias === null
              || event.call_participation === null
              || event.put_participation === null
              || Math.abs(event.bias - expectedBias) > 5e-5
              || Math.abs(event.call_participation - expectedCallParticipation) > 5e-5
              || Math.abs(event.put_participation - expectedPutParticipation) > 5e-5
            ) return false;
          } else if (
            event.bias !== null
            || event.call_participation !== null
            || event.put_participation !== null
          ) return false;
        }
        ranks.push(level.selection_rank);
        prices.push(level.price);
      }
      const orderedRanks = ranks.slice().sort((left, right) => left - right);
      if (
        new Set(prices).size !== prices.length
        || new Set(ranks).size !== ranks.length
        || !orderedRanks.every((rank, index) => rank === index + 1)
      ) return false;
      const peakAbsGex = Math.max(0, ...levels.map(level => level.abs_gex));
      return levels.every(level => Math.abs(level.strength - level.abs_gex / peakAbsGex) <= 5.1e-5);
    }

    function gexHistoryRowIsCanonical(row) {
      if (!row || typeof row !== "object") return false;
      if (
        Object.keys(row).length !== GEX_HISTORY_WIRE_FIELDS.length
        || GEX_HISTORY_WIRE_FIELDS.some(field => !Object.prototype.hasOwnProperty.call(row, field))
      ) return false;
      const captureMode = row.capture_mode;
      if (typeof captureMode !== "string") return false;
      if (row.source !== GEX_SOURCE_BY_CAPTURE_MODE[captureMode]) return false;
      if (!gexPayloadMarketDataEntitlementIsCanonical(row, { requireDecisionAuthority: false })) return false;
      if (gexMarketDataEntitlement(row) === "unknown" || !gexOpenInterestAsOf(row)) return false;
      if (!gexPayloadRevision(row)) return false;
      const timestampMs = gexHistoryTimestampMs(row);
      const validUntilMs = row.valid_until_unix_ms;
      const optionUniverseExpiresAt = row.option_universe_expires_at;
      const optionUniverseExpiryMs = optionUniverseExpiresAt === null
        ? null
        : gexOptionUniverseExpiryMs(row);
      const spot = gexNullableNumber(row.spot);
      const gammaFlip = row.gamma_flip === null ? null : gexNullableNumber(row.gamma_flip);
      const projectionMode = row.projection_mode;
      const projectionBucketMinutes = row.projection_bucket_minutes;
      if (
        timestampMs === null
        || !Number.isInteger(validUntilMs)
        || validUntilMs <= timestampMs
        || (
          optionUniverseExpiresAt !== null
          && (
            optionUniverseExpiryMs === null
            || optionUniverseExpiryMs <= timestampMs
            || validUntilMs > optionUniverseExpiryMs
          )
        )
        || spot === null
        || spot <= 0
        || (row.gamma_flip !== null && (gammaFlip === null || gammaFlip <= 0))
        || !Array.isArray(row.levels)
        || (!row.levels.length && gammaFlip === null)
      ) return false;
      if (projectionMode === "interval") {
        if (projectionBucketMinutes !== null) return false;
      } else if (
        projectionMode !== "sample"
        || ![15, 30].includes(projectionBucketMinutes)
      ) return false;
      return gexPublicLevelsAreCanonical(row.levels, row.spot);
    }

    function gexHistoryRowKey(row) {
      if (!gexHistoryRowIsCanonical(row)) return "";
      return JSON.stringify([row.source, row.capture_mode, row.captured_at]);
    }

    function gexHistoryRowsAreCanonical(rows) {
      if (!Array.isArray(rows)) return false;
      const keys = new Set();
      let previousTimestamp = -Infinity;
      for (const row of rows) {
        if (!gexHistoryRowIsCanonical(row)) return false;
        const key = JSON.stringify([row.source, row.capture_mode, row.captured_at]);
        const timestamp = gexHistoryTimestampMs(row);
        if (!key || timestamp === null || keys.has(key) || timestamp < previousTimestamp) return false;
        keys.add(key);
        previousTimestamp = timestamp;
      }
      return true;
    }

    function gexLiveFrameMayAdvance(
      sessionId,
      frameSeq,
      acceptedSessionId,
      acceptedFrameSeq,
      allowExactRecovery = false,
    ) {
      if (!acceptedSessionId) return true;
      const sessionTime = Date.parse(sessionId);
      const acceptedSessionTime = Date.parse(acceptedSessionId);
      if (
        !Number.isFinite(sessionTime)
        || !Number.isFinite(acceptedSessionTime)
        || sessionTime < acceptedSessionTime
        || (sessionTime === acceptedSessionTime && sessionId !== acceptedSessionId)
      ) return false;
      if (sessionId !== acceptedSessionId) return true;
      if (frameSeq > acceptedFrameSeq) return true;
      return allowExactRecovery && frameSeq === acceptedFrameSeq;
    }

    function gexHistoryRemovalKeyIsCanonical(value) {
      if (typeof value !== "string") return false;
      try {
        const parts = JSON.parse(value);
        if (!Array.isArray(parts) || parts.length !== 3) return false;
        const [source, captureMode, capturedAt] = parts;
        return source === GEX_SOURCE_BY_CAPTURE_MODE[captureMode]
          && typeof capturedAt === "string"
          && GEX_AWARE_CAPTURED_AT_PATTERN.test(capturedAt)
          && Number.isFinite(Date.parse(capturedAt))
          && JSON.stringify(parts) === value;
      } catch (_) {
        return false;
      }
    }

    function mergeGexHistoryMessage(message, previousPayload, expectedIdentity) {
      const type = typeof message?.type === "string" ? message.type : "";
      const revision = message?.history_revision;
      const expectedInstrumentId = exactIdentityText(expectedIdentity?.instrument_id);
      const expectedRouteFingerprint = exactIdentityText(expectedIdentity?.route_fingerprint);
      if (
        !gexPayloadIdentityMatchesRequest(
          message,
          expectedInstrumentId,
          expectedRouteFingerprint,
        )
        || !Number.isInteger(revision)
        || revision < 0
      ) return null;
      const previousProvided = Boolean(
        previousPayload
        && typeof previousPayload === "object"
      );
      const previousCandidate = previousProvided
        ? previousPayload
        : {};
      const previousMatchesIdentity = gexPayloadIdentityMatchesRequest(
        previousCandidate,
        expectedInstrumentId,
        expectedRouteFingerprint,
      );
      const previous = previousMatchesIdentity ? previousCandidate : {};
      const hasMarketDataAuthority = Object.prototype.hasOwnProperty.call(
        previous,
        "market_data_entitlement",
      );
      const messageProviderSymbol = gexPayloadProviderSymbol(message);
      const previousProviderSymbol = gexPayloadProviderSymbol(previous);
      const marketDataEntitlement = gexMarketDataEntitlement(previous);
      if (previousProvided && !previousMatchesIdentity) return null;
      if (
        previousMatchesIdentity
        && messageProviderSymbol !== previousProviderSymbol
      ) return null;
      if (
        type === "gex_history_delta"
        && !previousMatchesIdentity
      ) return null;
      if (
        hasMarketDataAuthority
        && (
          !marketDataEntitlement
          || marketDataEntitlement === "unknown"
        )
      ) return null;
      let history = Array.isArray(previous.history) ? previous.history : [];
      if (type === "gex_history_snapshot") {
        if (!gexHistoryRowsAreCanonical(message.history)) return null;
        history = message.history;
      } else if (type === "gex_history_delta") {
        const baseRevision = message.base_history_revision;
        const previousRevision = previous.history_revision ?? 0;
        if (!Number.isInteger(baseRevision) || baseRevision !== previousRevision) return null;
        if (!gexHistoryRowsAreCanonical(history)) return null;
        const removedKeys = Array.isArray(message.removed_history_keys) ? message.removed_history_keys : [];
        const upserts = Array.isArray(message.upserts) ? message.upserts : [];
        if (!removedKeys.every(gexHistoryRemovalKeyIsCanonical) || !gexHistoryRowsAreCanonical(upserts)) return null;
        const rows = new Map(history.map(row => [gexHistoryRowKey(row), row]));
        for (const key of removedKeys) {
          if (!rows.has(key)) return null;
          rows.delete(key);
        }
        for (const row of upserts) rows.set(gexHistoryRowKey(row), row);
        history = [...rows.values()].sort((left, right) => {
          const leftTs = gexHistoryTimestampMs(left);
          const rightTs = gexHistoryTimestampMs(right);
          if (leftTs !== rightTs) return leftTs - rightTs;
          return gexHistoryRowKey(left).localeCompare(gexHistoryRowKey(right));
        });
        if (!gexHistoryRowsAreCanonical(history)) return null;
      } else {
        return null;
      }
      return {
        ...previous,
        provider_symbol: messageProviderSymbol,
        ...(hasMarketDataAuthority ? { market_data_entitlement: marketDataEntitlement } : {}),
        instrument_id: expectedInstrumentId,
        route_fingerprint: expectedRouteFingerprint,
        history,
        history_revision: revision,
        history_status: message.history_status || previous.history_status,
      };
    }


    function gexPayloadProviderSymbol(payload) {
      return exactIdentityText(payload?.provider_symbol);
    }

    function gexPayloadIdentityMatchesRequest(payload, requestInstrumentId, requestRouteFingerprint) {
      const expectedInstrumentId = exactIdentityText(requestInstrumentId);
      const expectedRouteFingerprint = exactIdentityText(requestRouteFingerprint);
      return Boolean(
        expectedInstrumentId
        && expectedRouteFingerprint
        && exactIdentityText(payload?.instrument_id) === expectedInstrumentId
        && exactIdentityText(payload?.route_fingerprint) === expectedRouteFingerprint
        && gexPayloadProviderSymbol(payload)
      );
    }

    function gexStatusPayloadMatchesRequest(payload, requestInstrumentId, requestRouteFingerprint, expectedCaptureMode) {
      const captureMode = ["request", "live"].includes(expectedCaptureMode) ? expectedCaptureMode : "";
      return Boolean(
        captureMode
        && gexPayloadIdentityMatchesRequest(payload, requestInstrumentId, requestRouteFingerprint)
        && payload?.capture_mode === captureMode
        && payload?.source === GEX_SOURCE_BY_CAPTURE_MODE[captureMode]
        && payload?.local_only !== true
        && payload?.ok === false
        && GEX_READ_MODEL_STATUS_CODES.has(payload?.status)
        && typeof payload?.message === "string"
        && Array.isArray(payload?.levels)
        && payload.levels.length === 0
      );
    }

    function freezeAdmittedGexValue(value, seen = new WeakSet()) {
      if (!value || typeof value !== "object" || seen.has(value)) return value;
      if (
        Array.isArray(value)
        && Object.isFrozen(value)
        && gexFrozenHistoryAdmissionCache.has(value)
      ) return value;
      seen.add(value);
      Object.values(value).forEach(item => freezeAdmittedGexValue(item, seen));
      return Object.freeze(value);
    }

    function gexOptionUniverseAuthorityIsCanonical(payload, nowMs = Date.now()) {
      if (!Object.prototype.hasOwnProperty.call(payload, "option_universe_expires_at")) {
        return false;
      }
      if (typeof payload?.decision_authoritative !== "boolean") return false;
      if (payload.option_universe_expires_at === null) {
        return payload.decision_authoritative === false;
      }
      const expiryMs = gexOptionUniverseExpiryMs(payload);
      const capturedAt = payload?.captured_at;
      const capturedMs = typeof capturedAt === "string"
        && GEX_AWARE_CAPTURED_AT_PATTERN.test(capturedAt)
        ? Date.parse(capturedAt)
        : NaN;
      if (
        expiryMs === null
        || !Number.isFinite(capturedMs)
        || expiryMs <= capturedMs
      ) return false;
      return payload.decision_authoritative === false || expiryMs > nowMs;
    }

    function gexPayloadMatchesRequest(payload, requestInstrumentId, requestRouteFingerprint, expectedCaptureMode) {
      if (!payload || typeof payload !== "object" || Array.isArray(payload)) return false;
      const expectedInstrumentId = exactIdentityText(requestInstrumentId);
      const expectedRouteFingerprint = exactIdentityText(requestRouteFingerprint);
      const expectedMode = typeof expectedCaptureMode === "string" ? expectedCaptureMode : "";
      const expectedSource = GEX_SOURCE_BY_CAPTURE_MODE[expectedMode] || "";
      const hasHistory = Object.prototype.hasOwnProperty.call(payload, "history");
      const liveEnvelope = payload.live;
      const cachedAdmission = gexPayloadAdmissionCache.get(payload);
      if (
        cachedAdmission
        && Object.isFrozen(payload)
        && cachedAdmission.instrumentId === expectedInstrumentId
        && cachedAdmission.routeFingerprint === expectedRouteFingerprint
        && cachedAdmission.expectedMode === expectedMode
      ) return gexOptionUniverseAuthorityIsCanonical(payload);
      if (
        !expectedInstrumentId
        || !expectedRouteFingerprint
        || !gexPayloadIdentityMatchesRequest(
          payload,
          expectedInstrumentId,
          expectedRouteFingerprint,
        )
      ) return false;
      const providerSymbol = gexPayloadProviderSymbol(payload);
      const liveSessionId = liveEnvelope?.session_id;
      const liveFrameSeq = liveEnvelope?.frame_seq;
      const preservedLiveHistoryContext = Boolean(
        expectedMode === "live"
        && payload?.ok === true
        && payload?.status === "stale"
        && payload?.stale === true
        && payload?.preserved_context === true
        && payload?.display_context_source === "history"
        && payload?.decision_authoritative === false
        && !Object.prototype.hasOwnProperty.call(payload, "live")
      );
      const liveEnvelopeIsCanonical = expectedMode !== "live" || Boolean(
        preservedLiveHistoryContext
        || (
          liveEnvelope
          && typeof liveEnvelope === "object"
          && !Array.isArray(liveEnvelope)
          && typeof liveSessionId === "string"
          && GEX_AWARE_CAPTURED_AT_PATTERN.test(liveSessionId)
          && Number.isFinite(Date.parse(liveSessionId))
          && Number.isInteger(liveFrameSeq)
          && liveFrameSeq > 0
        )
      );
      const historyIsCanonical = !hasHistory
        || (
          Array.isArray(payload.history)
          && Object.isFrozen(payload.history)
          && gexFrozenHistoryAdmissionCache.has(payload.history)
        )
        || gexHistoryRowsAreCanonical(payload.history);
      const admitted = Boolean(
        expectedSource
        && Boolean(providerSymbol)
        && payload?.capture_mode === expectedMode
        && payload?.source === expectedSource
        && Boolean(gexPayloadRevision(payload))
        && gexPublicLevelsAreCanonical(payload?.levels, payload?.spot)
        && gexExpiryProfileIsCanonical(payload?.expiry_profile)
        && gexPayloadMarketDataEntitlementIsCanonical(payload)
        && Boolean(gexOpenInterestAsOf(payload))
        && gexOptionUniverseAuthorityIsCanonical(payload)
        && historyIsCanonical
        && liveEnvelopeIsCanonical
      );
      if (admitted) {
        freezeAdmittedGexValue(payload);
        if (hasHistory && Array.isArray(payload.history) && Object.isFrozen(payload.history)) {
          gexFrozenHistoryAdmissionCache.add(payload.history);
        }
        gexPayloadAdmissionCache.set(payload, {
          instrumentId: expectedInstrumentId,
          routeFingerprint: expectedRouteFingerprint,
          expectedMode,
        });
      } else {
        gexPayloadAdmissionCache.delete(payload);
      }
      return admitted;
    }

    function gexRouteMismatchPayload(payload, requestInstrumentId, requestRouteFingerprint, expectedCaptureMode) {
      const expectedInstrumentId = exactIdentityText(requestInstrumentId);
      const actualInstrumentId = exactIdentityText(payload?.instrument_id) || "-";
      const expectedRouteFingerprint = exactIdentityText(requestRouteFingerprint);
      const actualRouteFingerprint = exactIdentityText(payload?.route_fingerprint) || "-";
      const actualProviderSymbol = gexPayloadProviderSymbol(payload) || "-";
      const expectedMode = typeof expectedCaptureMode === "string" ? expectedCaptureMode : "";
      const expectedSource = GEX_SOURCE_BY_CAPTURE_MODE[expectedMode] || "";
      const receivedMode = typeof payload?.capture_mode === "string" ? payload.capture_mode : null;
      const receivedSource = typeof payload?.source === "string" ? payload.source : null;
      const actualMode = receivedMode || "-";
      const actualSource = receivedSource || "-";
      const mismatchDiagnostics = {
        rejected_instrument_id: actualInstrumentId,
        expected_instrument_id: expectedInstrumentId,
        rejected_route_fingerprint: actualRouteFingerprint,
        expected_route_fingerprint: expectedRouteFingerprint,
        received_provider_symbol: actualProviderSymbol,
        rejected_source: actualSource,
        expected_source: expectedSource,
        rejected_capture_mode: actualMode,
        expected_capture_mode: expectedMode,
      };
      const message = `GEX payload contract mismatch: expected ${expectedInstrumentId || "-"} / ${expectedRouteFingerprint || "-"} / ${expectedSource}+${expectedMode}, received ${actualInstrumentId} / ${actualRouteFingerprint} / ${actualSource}+${actualMode} (provider metadata ${actualProviderSymbol}); keeping previous valid GEX context.`;
      return {
        ok: false,
        enabled: true,
        provider_symbol: actualProviderSymbol === "-"
          ? exactIdentityText(instrumentForId(expectedInstrumentId)?.provider_symbol)
          : actualProviderSymbol,
        instrument_id: expectedInstrumentId,
        route_fingerprint: expectedRouteFingerprint,
        source: expectedSource,
        capture_mode: expectedMode,
        local_only: true,
        status: "error",
        message,
        levels: [],
        expiry_profile: [],
        latest_attempt: {
          source: receivedSource,
          capture_mode: receivedMode,
          captured_at: typeof payload?.captured_at === "string" ? payload.captured_at : null,
          status: "error",
          message,
          diagnostics: { contract_mismatch: mismatchDiagnostics },
        },
      };
    }
