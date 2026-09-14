    function applyCurrentScreenerQuote() {
      const row = currentQuoteRow();
      const payload = liveQuotePayloadFromScreenerRow(row, {
        allowStale: true,
        allowDelayed: true,
      });
      if (payload) return applyLiveQuoteToSnapshot(payload);
      invalidateLiveQuoteFromScreenerRow(row);
      return false;
    }

    function updateDocumentTitleFromScreenerQuote() {
      const row = currentQuoteRow();
      if (!row) return false;
      const quote = currentChartQuoteDisplay(LIVE_QUOTE_DISPLAY_TTL_MS);
      const symbol = row.display || row.key || state.symbol;
      if (!quote) {
        const title = symbol;
        const key = `${symbol}|no-quote`;
        if (state.lastDocumentTitleKey === key && document.title === title) return false;
        document.title = title;
        state.lastHeaderTitle = title;
        state.lastDocumentTitleKey = key;
        return true;
      }
      const changePct = Number(row.change_pct);
      const changePctText = Number.isFinite(changePct) ? signed(changePct, "%") : "";
      const arrow = Number(changePct) >= 0 ? "▲" : "▼";
      const title = `${symbol} ${arrow} ${fmt(quote.price)}${changePctText ? ` ${changePctText}` : ""}`;
      const key = `${symbol}|${quote.price}|${changePctText}`;
      if (state.lastDocumentTitleKey === key && document.title === title) return false;
      document.title = title;
      state.lastHeaderTitle = title;
      state.lastDocumentTitleKey = key;
      return true;
    }

    const LIVE_QUOTE_PAYLOAD_FIELDS = Object.freeze([
      "instrument_id",
      "route_fingerprint",
      "source",
      "price",
      "price_source",
      "bid",
      "ask",
      "last",
      "ts",
      "provider_ts",
      "received_at",
      "time_basis",
      "age_seconds",
      "status",
      "entitlement",
      "is_delayed",
      "is_stale",
      "last_provider_ts",
      "last_age_seconds",
      "last_status",
      "bid_ask_received_at",
      "bid_ask_age_seconds",
      "bid_ask_status",
    ]);
    const LIVE_QUOTE_STATUSES = new Set([
      "live",
      "stale",
      "frozen",
      "delayed",
      "delayed_frozen",
      "unknown",
      "unavailable",
    ]);

    function liveQuotePayloadHasExactFrame(quote) {
      if (
        !quote
        || typeof quote !== "object"
        || Array.isArray(quote)
        || LIVE_QUOTE_PAYLOAD_FIELDS.some(field => !Object.hasOwn(quote, field))
      ) return false;
      const nullableNumbers = [
        quote.bid,
        quote.ask,
        quote.last,
        quote.age_seconds,
        quote.last_age_seconds,
        quote.bid_ask_age_seconds,
      ];
      if (
        !Number.isFinite(quote.price)
        || nullableNumbers.some(value => value !== null && !Number.isFinite(value))
        || typeof quote.is_delayed !== "boolean"
        || typeof quote.is_stale !== "boolean"
        || typeof quote.source !== "string"
        || !quote.source
        || !LIVE_QUOTE_STATUSES.has(quote.status)
        || !LIVE_QUOTE_STATUSES.has(quote.last_status)
        || !LIVE_QUOTE_STATUSES.has(quote.bid_ask_status)
        || !["live", "frozen", "delayed", "delayed_frozen", "unknown"]
          .includes(quote.entitlement)
      ) return false;
      for (const value of [
        quote.ts,
        quote.provider_ts,
        quote.received_at,
        quote.last_provider_ts,
        quote.bid_ask_received_at,
      ]) {
        if (value !== null && (typeof value !== "string" || !value)) return false;
      }
      if (quote.price_source === "last") {
        return (
          quote.time_basis === "provider_event"
          && Number.isFinite(quote.last)
          && Math.abs(quote.price - quote.last) <= 0.000001
        );
      }
      if (quote.price_source !== "bid_ask_mid" || quote.time_basis !== "client_receive") {
        return false;
      }
      return (
        Number.isFinite(quote.bid)
        && Number.isFinite(quote.ask)
        && quote.ask >= quote.bid
        && Math.abs(quote.price - ((quote.bid + quote.ask) / 2)) <= 0.000001
      );
    }

    function liveQuoteMetaProjection(quoteDisplay) {
      return {
        price: quoteDisplay.price,
        bid: quoteDisplay.bid,
        ask: quoteDisplay.ask,
        last: quoteDisplay.last,
        quote_ts: quoteDisplay.ts,
        quote_provider_ts: quoteDisplay.providerTs,
        quote_received_at: quoteDisplay.receivedAt,
        price_source: quoteDisplay.priceSource,
        quote_time_basis: quoteDisplay.timeBasis,
        quote_status: quoteDisplay.status,
        quote_entitlement: quoteDisplay.entitlement,
        quote_is_delayed: quoteDisplay.isDelayed,
        quote_is_stale: quoteDisplay.isStale,
        quote_age_seconds: quoteDisplay.ageSeconds,
        last_provider_ts: quoteDisplay.lastProviderTs,
        last_age_seconds: quoteDisplay.lastAgeSeconds,
        last_status: quoteDisplay.lastStatus,
        bid_ask_received_at: quoteDisplay.bidAskReceivedAt,
        bid_ask_age_seconds: quoteDisplay.bidAskAgeSeconds,
        bid_ask_status: quoteDisplay.bidAskStatus,
      };
    }

    function unavailableLiveQuoteMetaProjection(price = null, priceSource = "unavailable") {
      return {
        price: Number.isFinite(price) ? price : null,
        bid: null,
        ask: null,
        last: null,
        quote_ts: null,
        quote_provider_ts: null,
        quote_received_at: null,
        price_source: priceSource,
        quote_time_basis: null,
        quote_status: "unavailable",
        quote_entitlement: "unknown",
        quote_is_delayed: false,
        quote_is_stale: true,
        quote_age_seconds: null,
        last_provider_ts: null,
        last_age_seconds: null,
        last_status: "unavailable",
        bid_ask_received_at: null,
        bid_ask_age_seconds: null,
        bid_ask_status: "unavailable",
      };
    }

    function quoteDisplayFromPayload(quote, options = {}) {
      const expectedInstrumentId = exactIdentityText(
        options.expectedInstrumentId ?? state.instrumentId,
      );
      const expectedRouteFingerprint = exactIdentityText(
        options.expectedRouteFingerprint ?? instrumentRouteFingerprint(),
      );
      if (
        !liveQuotePayloadHasExactFrame(quote)
        || exactIdentityText(quote.instrument_id) !== expectedInstrumentId
        || exactIdentityText(quote.route_fingerprint) !== expectedRouteFingerprint
      ) return null;
      const bid = quote.bid;
      const ask = quote.ask;
      const last = quote.last;
      const price = quote.price;
      const priceSource = String(quote.price_source || "");
      const timeBasis = String(quote.time_basis || "");
      const lastStatus = String(quote.last_status || "unavailable");
      const bidAskStatus = String(quote.bid_ask_status || "unavailable");
      const lastDisplayable = ["live", "stale", "delayed", "delayed_frozen"]
        .includes(lastStatus);
      const bidAskDisplayable = ["live", "stale", "delayed", "delayed_frozen"]
        .includes(bidAskStatus);
      const sourceClockIsCurrent = (
        (priceSource === "last" && timeBasis === "provider_event")
        || (priceSource === "bid_ask_mid" && timeBasis === "client_receive")
      );
      const selectedSourceIsDisplayable = priceSource === "last"
        ? lastDisplayable
        : priceSource === "bid_ask_mid" && bidAskDisplayable;
      const live = quote.status === "live" && quote.is_stale === false;
      const staleDisplay = options.allowStale === true
        && quote.status === "stale"
        && quote.is_stale === true;
      const delayedDisplay = options.allowDelayed === true
        && ["delayed", "delayed_frozen"].includes(quote.status)
        && quote.is_delayed === true
        && typeof quote.is_stale === "boolean";
      if (
        !Number.isFinite(price)
        || (!live && !staleDisplay && !delayedDisplay)
        || !sourceClockIsCurrent
        || !selectedSourceIsDisplayable
        || !String(quote.ts || "").trim()
      ) return null;
      if (options.maxAgeMs !== null && options.maxAgeMs !== undefined) {
        const maxAgeMs = Number(options.maxAgeMs);
        const nowMs = Number.isFinite(Number(options.nowMs))
          ? Number(options.nowMs)
          : Date.now();
        const quoteMs = Date.parse(quote.ts || "");
        if (
          !Number.isFinite(maxAgeMs)
          || maxAgeMs < 0
          || !Number.isFinite(quoteMs)
          || Math.abs(nowMs - quoteMs) > maxAgeMs
        ) return null;
      }
      return {
        price,
        priceSource,
        bid: bidAskDisplayable && Number.isFinite(bid) ? bid : null,
        ask: bidAskDisplayable && Number.isFinite(ask) ? ask : null,
        last: lastDisplayable && Number.isFinite(last) ? last : null,
        ts: quote.ts,
        providerTs: quote.provider_ts || null,
        receivedAt: quote.received_at || null,
        timeBasis,
        ageSeconds: Number.isFinite(quote.age_seconds) ? quote.age_seconds : null,
        status: quote.status,
        entitlement: quote.entitlement,
        isDelayed: quote.is_delayed,
        isStale: quote.is_stale,
        displayOnly: !live,
        lastProviderTs: quote.last_provider_ts || null,
        lastAgeSeconds: Number.isFinite(quote.last_age_seconds) ? quote.last_age_seconds : null,
        lastStatus,
        bidAskReceivedAt: quote.bid_ask_received_at || null,
        bidAskAgeSeconds: Number.isFinite(quote.bid_ask_age_seconds)
          ? quote.bid_ask_age_seconds
          : null,
        bidAskStatus,
      };
    }

    function currentLiveQuoteDisplay(maxAgeMs = LIVE_QUOTE_DISPLAY_TTL_MS) {
      return quoteDisplayFromPayload(state.liveQuote, { maxAgeMs });
    }

    function currentChartQuoteDisplay(maxAgeMs = LIVE_QUOTE_DISPLAY_TTL_MS) {
      const status = String(state.liveQuote?.status || "");
      return quoteDisplayFromPayload(state.liveQuote, {
        allowStale: true,
        allowDelayed: true,
        maxAgeMs: status === "live" ? maxAgeMs : null,
      });
    }

    function currentQuotePresentationState(nowMs = Date.now()) {
      const live = quoteDisplayFromPayload(state.liveQuote, {
        maxAgeMs: LIVE_QUOTE_DISPLAY_TTL_MS,
        nowMs,
      });
      if (live) return "live";
      const exactInstrumentId = exactIdentityText(state.instrumentId);
      const exactRouteFingerprint = instrumentRouteFingerprint();
      if (
        !state.liveQuote
        || exactIdentityText(state.liveQuote.instrument_id) !== exactInstrumentId
        || exactIdentityText(state.liveQuote.route_fingerprint) !== exactRouteFingerprint
      ) return "unavailable";
      const status = String(state.liveQuote.status || "unavailable").toLowerCase();
      if (status === "live") return "stale";
      return Object.hasOwn(UI_PRESENTATION_STATE_MATRIX, status) ? status : "unavailable";
    }
