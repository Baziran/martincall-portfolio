"use strict";

const QUOTE_PORT_LEASE_TIMEOUT_MS = 60_000;
const QUOTE_PORT_LEASE_SWEEP_MS = 10_000;
const QUOTE_GROUP_IDLE_GRACE_MS = 5_000;
const QUOTE_LIVE_PREVIEW_TTL_MS = 10_000;
const QUOTE_STATE_MESSAGE_TYPES = new Set([
  "quote_snapshot",
  "quote_delta",
  "quote_heartbeat",
  "option_targets_snapshot",
  "fast_indicators_snapshot",
  "quote_aux_status",
]);
const QUOTE_ROW_CONTRACT = QUOTE_STREAM_ROW_CONTRACT_MANIFEST;
if (
  !QUOTE_ROW_CONTRACT
  || QUOTE_ROW_CONTRACT.version !== 1
  || !Array.isArray(QUOTE_ROW_CONTRACT.required_fields)
) throw new TypeError("QUOTE_STREAM_ROW_CONTRACT_MANIFEST_INVALID");
const quoteGroups = new Map();
const quotePortStates = new WeakMap();

function quoteRowObjectsComplete(row) {
  return Object.entries(QUOTE_ROW_CONTRACT.nullable_object_contracts).every(
    ([field, contract]) => {
      const value = row[field];
      if (value === null) return true;
      if (!value || typeof value !== "object" || Array.isArray(value)) return false;
      const keys = Object.keys(value);
      if (
        keys.length !== contract.required_fields.length
        || contract.required_fields.some(key => !Object.hasOwn(value, key))
      ) return false;
      if (contract.integer_fields.some(key => !Number.isInteger(value[key]))) return false;
      if (contract.non_empty_text_fields.some(
        key => typeof value[key] !== "string" || !value[key],
      )) return false;
      return Object.entries(contract.enum_fields).every(
        ([key, allowed]) => allowed.includes(value[key]),
      );
    },
  );
}

function quoteProtocolError(code, message) {
  const error = new Error(String(message || code || "quote worker protocol error"));
  error.code = String(code || "QUOTE_WORKER_PROTOCOL_ERROR");
  return error;
}

function quoteExactIdentity(value, field) {
  if (typeof value !== "string" || !value) {
    throw quoteProtocolError(
      "QUOTE_WORKER_IDENTITY_INVALID",
      `${field} must be an exact non-empty string`,
    );
  }
  return value;
}

function quoteIdentityKey(instrumentId, routeFingerprint) {
  return JSON.stringify([
    quoteExactIdentity(instrumentId, "instrument_id"),
    quoteExactIdentity(routeFingerprint, "route_fingerprint"),
  ]);
}

function quoteCacheRevision(message) {
  const epoch = quoteExactIdentity(message?.cache_epoch, "cache_epoch");
  const generation = message?.cache_generation;
  if (!Number.isSafeInteger(generation) || generation < 0) {
    throw quoteProtocolError(
      "QUOTE_WORKER_CACHE_REVISION_INVALID",
      "cache_generation must be a non-negative integer",
    );
  }
  return { epoch, generation };
}

function quoteExactSequence(value, code) {
  if (!Number.isSafeInteger(value) || value <= 0) {
    throw quoteProtocolError(code, "sequence must be an exact positive safe integer");
  }
  return value;
}

function quoteExactRowsRevision(value) {
  if (!Number.isSafeInteger(value) || value < 0) {
    throw quoteProtocolError(
      "QUOTE_WORKER_ROWS_REVISION_INVALID",
      "rows revision must be an exact non-negative safe integer",
    );
  }
  return value;
}

function quoteFixedSubscription(raw) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    throw quoteProtocolError("QUOTE_WORKER_SUBSCRIPTION_INVALID", "subscription must be an object");
  }
  const interval = quoteExactIdentity(raw.interval, "interval");
  if (!Array.isArray(raw.routes) || !raw.routes.length) {
    throw quoteProtocolError("QUOTE_WORKER_SUBSCRIPTION_INVALID", "subscription routes are required");
  }
  const routes = raw.routes.map((route, index) => {
    if (!route || typeof route !== "object" || Array.isArray(route)) {
      throw quoteProtocolError(
        "QUOTE_WORKER_SUBSCRIPTION_INVALID",
        `subscription route ${index} must be an object`,
      );
    }
    return {
      instrument_id: quoteExactIdentity(route.instrument_id, `routes[${index}].instrument_id`),
      route_fingerprint: quoteExactIdentity(
        route.route_fingerprint,
        `routes[${index}].route_fingerprint`,
      ),
    };
  });
  const pairs = routes.map(route => [route.instrument_id, route.route_fingerprint]);
  const routeKeys = pairs.map(pair => quoteIdentityKey(pair[0], pair[1]));
  if (new Set(routeKeys).size !== routeKeys.length) {
    throw quoteProtocolError("QUOTE_WORKER_SUBSCRIPTION_INVALID", "subscription routes must be unique");
  }
  const sortedPairs = [...pairs].sort((left, right) => (
    left[0].localeCompare(right[0]) || left[1].localeCompare(right[1])
  ));
  if (JSON.stringify(pairs) !== JSON.stringify(sortedPairs)) {
    throw quoteProtocolError(
      "QUOTE_WORKER_SUBSCRIPTION_INVALID",
      "subscription routes must use canonical exact order",
    );
  }
  return {
    interval,
    routes,
    pairs,
    routeKeys,
    routeKeySet: new Set(routeKeys),
    key: JSON.stringify([pairs, interval]),
  };
}

function quoteSameRouteSet(leftKeys, rightKeys) {
  const rightKeySet = new Set(rightKeys);
  return leftKeys.length === rightKeys.length
    && new Set(leftKeys).size === leftKeys.length
    && rightKeySet.size === rightKeys.length
    && leftKeys.every(key => rightKeySet.has(key));
}

function quoteValidatedSocketUrl(rawUrl, fixed) {
  const url = new URL(quoteExactIdentity(rawUrl, "ws_url"), self.location.href);
  const expectedProtocol = self.location.protocol === "https:" ? "wss:" : "ws:";
  if (
    url.protocol !== expectedProtocol
    || url.host !== self.location.host
    || url.pathname !== "/ws/quotes"
    || url.username
    || url.password
    || url.hash
  ) {
    throw quoteProtocolError(
      "QUOTE_WORKER_SOCKET_URL_INVALID",
      "quote socket URL must be the same-origin /ws/quotes endpoint",
    );
  }
  const queryNames = Array.from(url.searchParams.keys());
  if (
    url.searchParams.getAll("routes").length !== 1
    || url.searchParams.getAll("interval").length !== 1
    || queryNames.some(name => name !== "routes" && name !== "interval")
    || url.searchParams.get("interval") !== fixed.interval
  ) {
    throw quoteProtocolError(
      "QUOTE_WORKER_SOCKET_URL_INVALID",
      "quote socket query must match the fixed interval and routes",
    );
  }
  let rawRoutes;
  try {
    rawRoutes = JSON.parse(url.searchParams.get("routes") || "null");
  } catch (_) {
    throw quoteProtocolError("QUOTE_WORKER_SOCKET_URL_INVALID", "quote socket routes are invalid JSON");
  }
  if (!Array.isArray(rawRoutes)) {
    throw quoteProtocolError("QUOTE_WORKER_SOCKET_URL_INVALID", "quote socket routes must be an array");
  }
  const socketRouteKeys = rawRoutes.map((route, index) => {
    if (!route || typeof route !== "object" || Array.isArray(route)) {
      throw quoteProtocolError(
        "QUOTE_WORKER_SOCKET_URL_INVALID",
        `quote socket route ${index} must be an object`,
      );
    }
    return quoteIdentityKey(route.instrument_id, route.route_fingerprint);
  });
  if (!quoteSameRouteSet(socketRouteKeys, fixed.routeKeys)) {
    throw quoteProtocolError(
      "QUOTE_WORKER_SOCKET_URL_INVALID",
      "quote socket routes do not match the fixed subscription",
    );
  }
  return url.toString();
}

function quoteWorkerEnvelope(group, payload) {
  return {
    ...payload,
    key: group.key,
    shared_ports: group.ports.size,
    shared_groups: quoteGroups.size,
    worker_sent_at: Date.now(),
  };
}

function quotePost(port, payload) {
  try {
    port.postMessage(payload);
    return true;
  } catch (_) {
    return false;
  }
}

function quoteBroadcast(group, payload) {
  const envelope = quoteWorkerEnvelope(group, payload);
  const failedPorts = [];
  for (const port of group.ports.keys()) {
    if (!quotePost(port, envelope)) failedPorts.push(port);
  }
  for (const port of failedPorts) quoteReleasePortSubscription(port, group.key, false);
}

function quotePortError(port, key, error, options = {}) {
  const exactKey = typeof key === "string" ? key : "";
  quotePost(port, {
    type: "error",
    key: exactKey,
    code: String(error?.code || "QUOTE_WORKER_ERROR"),
    message: String(error?.message || error || "quote worker error"),
    fatal: options.fatal === true,
    shared_ports: 0,
    shared_groups: quoteGroups.size,
    worker_sent_at: Date.now(),
  });
  if (options.fatal === true) {
    quotePost(port, {
      type: "close",
      key: exactKey,
      code: 4003,
      reason: String(error?.message || error || "quote worker protocol error"),
      restart: false,
      shared_ports: 0,
      shared_groups: quoteGroups.size,
      worker_sent_at: Date.now(),
    });
  }
}

function quoteScheduleIdleClose(group) {
  if (group.ports.size || group.idleTimer || quoteGroups.get(group.key) !== group) return;
  group.idleTimer = setTimeout(() => {
    group.idleTimer = 0;
    if (group.ports.size || quoteGroups.get(group.key) !== group) return;
    quoteTerminateGroup(group, {
      code: 1000,
      reason: "quote worker idle",
      notify: false,
      restart: false,
    });
  }, QUOTE_GROUP_IDLE_GRACE_MS);
}

function quoteDeletePortSubscriptionState(port, key) {
  const portState = quotePortStates.get(port);
  if (!portState) return;
  portState.subscriptions.delete(key);
  if (!portState.subscriptions.size) quotePortStates.delete(port);
}

function quoteReleasePortSubscription(port, key, notifyLeaseExpiry = false) {
  const group = quoteGroups.get(key);
  quoteDeletePortSubscriptionState(port, key);
  if (!group || !group.ports.has(port)) return;
  group.ports.delete(port);
  if (notifyLeaseExpiry) {
    quotePost(port, quoteWorkerEnvelope(group, {
      type: "close",
      code: 4000,
      reason: "quote transport lease expired",
      lease_expired: true,
      restart: false,
    }));
  }
  quoteScheduleIdleClose(group);
}

function quoteAcknowledgePortClose(port, key, group, code, reason) {
  const closeCode = Number(code);
  quotePost(port, {
    type: "close",
    key,
    code: Number.isFinite(closeCode) ? closeCode : 1000,
    reason: typeof reason === "string" ? reason : String(reason ?? ""),
    restart: false,
    close_ack: true,
    shared_ports: group?.ports.size || 0,
    shared_groups: quoteGroups.size,
    worker_sent_at: Date.now(),
  });
}

function quoteReleasePort(port) {
  const portState = quotePortStates.get(port);
  if (!portState) return;
  for (const key of Array.from(portState.subscriptions)) {
    quoteReleasePortSubscription(port, key, false);
  }
  quotePortStates.delete(port);
}

function quoteTerminateGroup(group, options = {}) {
  if (quoteGroups.get(group.key) !== group) return;
  quoteGroups.delete(group.key);
  if (group.idleTimer) clearTimeout(group.idleTimer);
  group.idleTimer = 0;
  const socket = group.socket;
  if (socket) {
    socket.onopen = null;
    socket.onmessage = null;
    socket.onerror = null;
    socket.onclose = null;
  }
  if (options.notify !== false) {
    quoteBroadcast(group, {
      type: "close",
      code: Number(options.code || 1006),
      reason: String(options.reason || "quote transport closed"),
      restart: options.restart === true,
    });
  }
  for (const port of group.ports.keys()) {
    quoteDeletePortSubscriptionState(port, group.key);
  }
  group.ports.clear();
  if (socket && socket.readyState <= WebSocket.OPEN) {
    try {
      socket.close(
        Number(options.code || 1000),
        String(options.reason || "quote worker close").slice(0, 120),
      );
    } catch (_) {
      // The group is already detached; a close failure cannot retain a lease.
    }
  }
  group.socket = null;
}

function quoteRestartGroup(group, error) {
  quoteBroadcast(group, {
    type: "error",
    code: String(error?.code || "QUOTE_WORKER_PROTOCOL_CORRUPTION"),
    message: String(error?.message || error || "quote stream protocol corruption"),
    fatal: true,
  });
  quoteTerminateGroup(group, {
    code: 4002,
    reason: String(error?.message || "quote protocol corruption"),
    notify: true,
    restart: true,
  });
}

function quoteServerRouteKeys(group, subscription) {
  if (!subscription || typeof subscription !== "object" || Array.isArray(subscription)) {
    throw quoteProtocolError("QUOTE_WORKER_SERVER_SNAPSHOT_INVALID", "server subscription is missing");
  }
  if (subscription.interval !== group.fixed.interval || !Array.isArray(subscription.routes)) {
    throw quoteProtocolError(
      "QUOTE_WORKER_SERVER_SNAPSHOT_INVALID",
      "server subscription interval or routes do not match",
    );
  }
  return subscription.routes.map((route, index) => {
    if (!route || typeof route !== "object" || Array.isArray(route)) {
      throw quoteProtocolError(
        "QUOTE_WORKER_SERVER_SNAPSHOT_INVALID",
        `server subscription route ${index} is invalid`,
      );
    }
    return quoteIdentityKey(route.instrument_id, route.route_fingerprint);
  });
}

function quoteRowsByIdentity(group, rows, options = {}) {
  if (!Array.isArray(rows)) {
    throw quoteProtocolError("QUOTE_WORKER_ROWS_INVALID", "quote rows must be an array");
  }
  const nextRows = new Map();
  for (const row of rows) {
    if (!row || typeof row !== "object" || Array.isArray(row)) {
      throw quoteProtocolError("QUOTE_WORKER_ROWS_INVALID", "quote row must be an object");
    }
    const missingFields = QUOTE_ROW_CONTRACT.required_fields.filter(
      field => !Object.hasOwn(row, field),
    );
    if (missingFields.length) {
      throw quoteProtocolError(
        "QUOTE_WORKER_ROWS_INVALID",
        `quote row is incomplete: ${missingFields.join(", ")}`,
      );
    }
    const typedRow = (
      !QUOTE_ROW_CONTRACT.number_fields.some(
        field => row[field] !== null && !Number.isFinite(row[field]),
      )
      && !QUOTE_ROW_CONTRACT.boolean_fields.some(field => typeof row[field] !== "boolean")
      && !QUOTE_ROW_CONTRACT.time_fields.some(field => (
        row[field] !== null && (typeof row[field] !== "string" || !row[field])
      ))
      && !QUOTE_ROW_CONTRACT.text_fields.some(field => typeof row[field] !== "string")
      && !QUOTE_ROW_CONTRACT.non_empty_text_fields.some(field => !row[field])
      && !QUOTE_ROW_CONTRACT.nullable_text_fields.some(
        field => row[field] !== null && typeof row[field] !== "string",
      )
      && !QUOTE_ROW_CONTRACT.status_fields.some(
        field => !QUOTE_ROW_CONTRACT.statuses.includes(row[field]),
      )
      && QUOTE_ROW_CONTRACT.entitlements.includes(
        row[QUOTE_ROW_CONTRACT.entitlement_field],
      )
      && quoteRowObjectsComplete(row)
    );
    if (!typedRow) {
      throw quoteProtocolError("QUOTE_WORKER_ROWS_INVALID", "quote row fields are malformed");
    }
    const key = quoteIdentityKey(row.instrument_id, row.route_fingerprint);
    if (!group.fixed.routeKeySet.has(key) || nextRows.has(key)) {
      throw quoteProtocolError(
        "QUOTE_WORKER_ROWS_INVALID",
        "quote rows must be unique members of the fixed subscription",
      );
    }
    nextRows.set(key, row);
  }
  if (options.complete === true && !quoteSameRouteSet(Array.from(nextRows.keys()), group.fixed.routeKeys)) {
    throw quoteProtocolError(
      "QUOTE_WORKER_ROWS_INVALID",
      "quote snapshot must contain every fixed subscription route",
    );
  }
  return nextRows;
}

function quoteValidatedIntervalItems(group, items, itemType) {
  const isPriceAlertRuntime = itemType === "price alert runtime";
  const code = isPriceAlertRuntime
    ? "QUOTE_WORKER_PRICE_ALERT_RUNTIME_INVALID"
    : "QUOTE_WORKER_TARGETS_INVALID";
  const collectionLabel = isPriceAlertRuntime ? "price alert runtime" : "option targets";
  const ids = new Set();
  if (!Array.isArray(items)) {
    throw quoteProtocolError(code, `${collectionLabel} must be an array`);
  }
  for (const item of items) {
    if (!item || typeof item !== "object" || Array.isArray(item)) {
      throw quoteProtocolError(code, `${itemType} must be an object`);
    }
    if (isPriceAlertRuntime) {
      if (typeof item.id !== "string" || !item.id || ids.has(item.id)) {
        throw quoteProtocolError(
          code,
          "price alert runtime ids must be unique non-empty strings",
        );
      }
      ids.add(item.id);
    }
    const key = quoteIdentityKey(item.instrument_id, item.route_fingerprint);
    if (!group.fixed.routeKeySet.has(key) || item.timeframe !== group.fixed.interval) {
      throw quoteProtocolError(
        code,
        `${itemType} must match the fixed route and interval`,
      );
    }
  }
  return items;
}

function quoteValidatedFastIndicatorScopes(group, scopes) {
  if (!Array.isArray(scopes)) {
    throw quoteProtocolError(
      "QUOTE_WORKER_FAST_INDICATORS_INVALID",
      "fast indicator scopes must be an array",
    );
  }
  const seen = new Set();
  for (const scope of scopes) {
    if (!scope || typeof scope !== "object" || Array.isArray(scope)) {
      throw quoteProtocolError(
        "QUOTE_WORKER_FAST_INDICATORS_INVALID",
        "fast indicator scope must be an object",
      );
    }
    const routeKey = quoteIdentityKey(
      scope.instrument_id,
      scope.route_fingerprint,
    );
    const scopeKey = JSON.stringify([
      scope.instrument_id,
      scope.route_fingerprint,
      scope.timeframe,
    ]);
    if (
      !group.fixed.routeKeySet.has(routeKey)
      || scope.timeframe !== group.fixed.interval
      || seen.has(scopeKey)
      || typeof scope.bar_timeframe !== "string"
      || !scope.bar_timeframe
      || !Number.isSafeInteger(scope.bar_canonical_generation)
      || scope.bar_canonical_generation < 0
      || !Number.isSafeInteger(scope.revision)
      || scope.revision <= 0
      || scope.indicator_id !== "option_reversal"
      || scope.authority !== "advisory_only"
      || !scope.by_target_id
      || typeof scope.by_target_id !== "object"
      || Array.isArray(scope.by_target_id)
    ) {
      throw quoteProtocolError(
        "QUOTE_WORKER_FAST_INDICATORS_INVALID",
        "fast indicator scope violates the fixed route contract",
      );
    }
    seen.add(scopeKey);
  }
  return scopes;
}

function quoteLiveBarFreshnessAt(bar, receivedAt) {
  const gatewayTimestamp = typeof bar.gateway_ts === "string"
    ? Date.parse(bar.gateway_ts)
    : Number.NaN;
  return Number.isFinite(gatewayTimestamp) ? gatewayTimestamp : receivedAt;
}

function quotePruneLiveBars(group, now) {
  const cutoff = now - QUOTE_LIVE_PREVIEW_TTL_MS;
  for (const [key, materialized] of group.state.liveBarsByIdentity) {
    if (materialized.freshnessAt < cutoff) group.state.liveBarsByIdentity.delete(key);
  }
}

function quoteValidateLiveFrame(group, message, receivedAt) {
  if (message.interval !== group.fixed.interval || !Array.isArray(message.bars)) {
    throw quoteProtocolError("QUOTE_WORKER_LIVE_FRAME_INVALID", "live candle frame interval is invalid");
  }
  const sequence = quoteExactSequence(message.sequence, "QUOTE_WORKER_LIVE_SEQUENCE_INVALID");
  if (sequence <= group.state.liveSequence) {
    throw quoteProtocolError(
      "QUOTE_WORKER_LIVE_SEQUENCE_INVALID",
      "live candle sequence must advance monotonically",
    );
  }
  let lossAfterSequence = null;
  if (message.loss_after_sequence !== null && message.loss_after_sequence !== undefined) {
    lossAfterSequence = message.loss_after_sequence;
    if (
      !Number.isSafeInteger(lossAfterSequence)
      || lossAfterSequence <= 0
      || lossAfterSequence > sequence
    ) {
      throw quoteProtocolError(
        "QUOTE_WORKER_LIVE_LOSS_INVALID",
        "live candle loss marker must be a positive sequence",
      );
    }
  }
  const barsByIdentity = new Map();
  for (const bar of message.bars) {
    if (!bar || typeof bar !== "object" || Array.isArray(bar)) {
      throw quoteProtocolError("QUOTE_WORKER_LIVE_FRAME_INVALID", "live candle must be an object");
    }
    const key = quoteIdentityKey(bar.instrument_id, bar.route_fingerprint);
    if (
      !group.fixed.routeKeySet.has(key)
      || bar.timeframe !== group.fixed.interval
      || barsByIdentity.has(key)
    ) {
      throw quoteProtocolError(
        "QUOTE_WORKER_LIVE_FRAME_INVALID",
        "live candles must be unique members of the fixed route and interval",
      );
    }
    barsByIdentity.set(key, {
      bar,
      freshnessAt: quoteLiveBarFreshnessAt(bar, receivedAt),
    });
  }
  return { barsByIdentity, lossAfterSequence, sequence };
}

function quoteConsumeLiveFrame(group, message, receivedAt) {
  const liveFrame = quoteValidateLiveFrame(group, message, receivedAt);
  if (liveFrame.lossAfterSequence !== null) group.state.liveBarsByIdentity.clear();
  const cutoff = receivedAt - QUOTE_LIVE_PREVIEW_TTL_MS;
  for (const [key, materialized] of liveFrame.barsByIdentity) {
    if (materialized.freshnessAt < cutoff) {
      group.state.liveBarsByIdentity.delete(key);
    } else {
      group.state.liveBarsByIdentity.set(key, materialized);
    }
  }
  quotePruneLiveBars(group, receivedAt);
  group.state.liveSequence = liveFrame.sequence;
  group.state.liveLossAfterSequence = liveFrame.lossAfterSequence;
}

function quoteConsumeServerMessage(group, message, receivedAt) {
  if (!message || typeof message !== "object" || Array.isArray(message)) {
    throw quoteProtocolError("QUOTE_WORKER_SERVER_MESSAGE_INVALID", "quote message must be an object");
  }
  if (message.type === "live_candle_delta") {
    quoteConsumeLiveFrame(group, message, receivedAt);
    return;
  }
  if (!QUOTE_STATE_MESSAGE_TYPES.has(message.type)) return;
  const sequence = quoteExactSequence(message.sequence, "QUOTE_WORKER_SEQUENCE_INVALID");
  if (message.type === "quote_snapshot") {
    if (group.state.ready || sequence !== 1) {
      throw quoteProtocolError(
        "QUOTE_WORKER_SEQUENCE_INVALID",
        "server quote snapshot must start a new sequence at 1",
      );
    }
    const serverRouteKeys = quoteServerRouteKeys(group, message.subscription);
    if (!quoteSameRouteSet(serverRouteKeys, group.fixed.routeKeys)) {
      throw quoteProtocolError(
        "QUOTE_WORKER_SERVER_SNAPSHOT_INVALID",
        "server snapshot routes do not match the fixed subscription",
      );
    }
    const cacheRevision = quoteCacheRevision(message);
    group.state.serverSubscription = message.subscription;
    group.state.rowsByIdentity = quoteRowsByIdentity(group, message.rows, { complete: true });
    group.state.rowsRevision = quoteExactRowsRevision(message.revision);
    group.state.cacheEpoch = cacheRevision.epoch;
    group.state.cacheGeneration = cacheRevision.generation;
    group.state.warnings = message.warnings
      && typeof message.warnings === "object"
      && !Array.isArray(message.warnings)
      ? message.warnings
      : {};
    group.state.degraded = message.degraded === true;
    if (Object.hasOwn(message, "option_targets")) {
      group.state.optionTargets = quoteValidatedIntervalItems(
        group,
        message.option_targets,
        "option target",
      );
      group.state.optionTargetsKnown = true;
    }
    if (Object.hasOwn(message, "price_alert_runtime")) {
      group.state.priceAlertRuntime = quoteValidatedIntervalItems(
        group,
        message.price_alert_runtime,
        "price alert runtime",
      );
      group.state.priceAlertRuntimeKnown = true;
    }
    if (Object.hasOwn(message, "fast_indicators")) {
      group.state.fastIndicators = quoteValidatedFastIndicatorScopes(
        group,
        message.fast_indicators,
      );
      group.state.fastIndicatorsKnown = true;
    }
    group.state.sequence = sequence;
    group.state.ready = true;
    return;
  }
  if (!group.state.ready || sequence !== group.state.sequence + 1) {
    throw quoteProtocolError(
      "QUOTE_WORKER_SEQUENCE_INVALID",
      `expected quote state sequence ${group.state.sequence + 1}, received ${sequence}`,
    );
  }
  if (message.interval !== group.fixed.interval) {
    throw quoteProtocolError("QUOTE_WORKER_INTERVAL_INVALID", "quote state interval changed");
  }
  const hasPriceAlertRuntime = Object.hasOwn(message, "price_alert_runtime");
  if (hasPriceAlertRuntime && message.type !== "quote_heartbeat") {
    throw quoteProtocolError(
      "QUOTE_WORKER_PRICE_ALERT_RUNTIME_INVALID",
      "price alert runtime is allowed only on quote snapshot or heartbeat envelopes",
    );
  }
  if (hasPriceAlertRuntime) {
    group.state.priceAlertRuntime = quoteValidatedIntervalItems(
      group,
      message.price_alert_runtime,
      "price alert runtime",
    );
    group.state.priceAlertRuntimeKnown = true;
  }
  if (message.type === "quote_delta") {
    const cacheRevision = quoteCacheRevision(message);
    if (
      cacheRevision.epoch !== group.state.cacheEpoch
      || cacheRevision.generation < group.state.cacheGeneration
    ) {
      throw quoteProtocolError(
        "QUOTE_WORKER_CACHE_REVISION_INVALID",
        "quote delta cache revision regressed or changed epoch",
      );
    }
    const dirtyRows = quoteRowsByIdentity(group, message.rows);
    for (const [key, row] of dirtyRows) group.state.rowsByIdentity.set(key, row);
    group.state.rowsRevision = quoteExactRowsRevision(message.revision);
    group.state.cacheGeneration = cacheRevision.generation;
  } else if (message.type === "option_targets_snapshot") {
    group.state.optionTargets = quoteValidatedIntervalItems(
      group,
      message.option_targets,
      "option target",
    );
    group.state.optionTargetsKnown = true;
  } else if (message.type === "fast_indicators_snapshot") {
    group.state.fastIndicators = quoteValidatedFastIndicatorScopes(
      group,
      message.scopes,
    );
    group.state.fastIndicatorsKnown = true;
  } else if (message.type === "quote_aux_status") {
    group.state.warnings = message.warnings
      && typeof message.warnings === "object"
      && !Array.isArray(message.warnings)
      ? message.warnings
      : {};
    group.state.degraded = message.degraded === true;
  }
  group.state.sequence = sequence;
}

function quoteBootstrapPayload(group) {
  if (!group.state.ready) return "";
  const rows = group.fixed.routeKeys.map(key => group.state.rowsByIdentity.get(key));
  if (rows.some(row => !row)) {
    throw quoteProtocolError(
      "QUOTE_WORKER_BOOTSTRAP_INVALID",
      "materialized quote state is missing a fixed route",
    );
  }
  const message = {
    type: "quote_snapshot",
    source: "quote:shared-worker",
    ts: new Date().toISOString(),
    sequence: group.state.sequence,
    revision: group.state.rowsRevision,
    cache_epoch: group.state.cacheEpoch,
    cache_generation: group.state.cacheGeneration,
    subscription: group.state.serverSubscription,
    rows,
    degraded: group.state.degraded,
    warnings: group.state.warnings,
    shared_transport_bootstrap: true,
  };
  if (group.state.optionTargetsKnown) message.option_targets = group.state.optionTargets;
  if (group.state.fastIndicatorsKnown) message.fast_indicators = group.state.fastIndicators;
  if (group.state.priceAlertRuntimeKnown) {
    message.price_alert_runtime = group.state.priceAlertRuntime;
  }
  return JSON.stringify(message);
}

function quoteLiveBootstrapPayload(group, now) {
  if (group.state.liveSequence <= 0) return "";
  quotePruneLiveBars(group, now);
  const bars = [];
  for (const key of group.fixed.routeKeys) {
    const materialized = group.state.liveBarsByIdentity.get(key);
    if (materialized) bars.push(materialized.bar);
  }
  return JSON.stringify({
    type: "live_candle_delta",
    source: "quote:shared-worker",
    ts: new Date(now).toISOString(),
    interval: group.fixed.interval,
    sequence: group.state.liveSequence,
    loss_after_sequence: group.state.liveLossAfterSequence,
    bars,
    shared_transport_bootstrap: true,
  });
}

function quoteSendBootstrap(group, port) {
  const receivedAt = Date.now();
  const data = quoteBootstrapPayload(group);
  if (data) {
    quotePost(port, quoteWorkerEnvelope(group, {
      type: "message",
      data,
      received_at: receivedAt,
      bootstrap: true,
    }));
  }
  const liveData = quoteLiveBootstrapPayload(group, receivedAt);
  if (liveData) {
    quotePost(port, quoteWorkerEnvelope(group, {
      type: "message",
      data: liveData,
      received_at: receivedAt,
      bootstrap: true,
    }));
  }
}

function quoteOpenSocket(group) {
  const socket = new WebSocket(group.wsUrl);
  group.socket = socket;
  socket.onopen = () => {
    if (quoteGroups.get(group.key) !== group || group.socket !== socket) return;
    quoteBroadcast(group, {
      type: "open",
      received_at: Date.now(),
    });
  };
  socket.onmessage = event => {
    if (quoteGroups.get(group.key) !== group || group.socket !== socket) return;
    if (typeof event.data !== "string") {
      quoteRestartGroup(
        group,
        quoteProtocolError("QUOTE_WORKER_FRAME_INVALID", "quote WebSocket frame must be text"),
      );
      return;
    }
    let message;
    const receivedAt = Date.now();
    try {
      message = JSON.parse(event.data);
      quoteConsumeServerMessage(group, message, receivedAt);
    } catch (error) {
      quoteRestartGroup(group, error);
      return;
    }
    quoteBroadcast(group, {
      type: "message",
      data: event.data,
      received_at: receivedAt,
      bootstrap: false,
    });
  };
  socket.onerror = () => {
    if (quoteGroups.get(group.key) !== group || group.socket !== socket) return;
    quoteBroadcast(group, {
      type: "error",
      code: "QUOTE_WORKER_SOCKET_ERROR",
      message: "shared quote WebSocket error",
      fatal: false,
    });
  };
  socket.onclose = event => {
    if (quoteGroups.get(group.key) !== group || group.socket !== socket) return;
    quoteTerminateGroup(group, {
      code: Number(event?.code || 1006),
      reason: String(event?.reason || "shared quote WebSocket closed"),
      notify: true,
      restart: false,
    });
  };
}

function quoteCreateGroup(key, wsUrl, fixed) {
  const group = {
    key,
    wsUrl,
    fixed,
    ports: new Map(),
    socket: null,
    idleTimer: 0,
    state: {
      ready: false,
      sequence: 0,
      rowsRevision: 0,
      cacheEpoch: "",
      cacheGeneration: 0,
      rowsByIdentity: new Map(),
      serverSubscription: null,
      optionTargets: [],
      optionTargetsKnown: false,
      fastIndicators: [],
      fastIndicatorsKnown: false,
      priceAlertRuntime: [],
      priceAlertRuntimeKnown: false,
      warnings: {},
      degraded: false,
      liveSequence: 0,
      liveLossAfterSequence: null,
      liveBarsByIdentity: new Map(),
    },
  };
  quoteGroups.set(key, group);
  quoteOpenSocket(group);
  return group;
}

function quoteSubscribePort(port, payload) {
  const portState = quotePortStates.get(port);
  if (!portState) {
    throw quoteProtocolError(
      "QUOTE_WORKER_PORT_CLOSED",
      "quote worker port is no longer active",
    );
  }
  const fixed = quoteFixedSubscription(payload.subscription);
  const key = quoteExactIdentity(payload.key, "key");
  if (key !== fixed.key) {
    throw quoteProtocolError(
      "QUOTE_WORKER_SUBSCRIPTION_KEY_INVALID",
      "quote transport key does not match the fixed subscription",
    );
  }
  const wsUrl = quoteValidatedSocketUrl(payload.ws_url, fixed);
  let group = quoteGroups.get(key);
  if (group && (
    group.wsUrl !== wsUrl
    || JSON.stringify(group.fixed.pairs) !== JSON.stringify(fixed.pairs)
    || group.fixed.interval !== fixed.interval
  )) {
    throw quoteProtocolError(
      "QUOTE_WORKER_GROUP_CONFLICT",
      "an existing quote group has a different fixed subscription",
    );
  }
  if (!group) group = quoteCreateGroup(key, wsUrl, fixed);
  if (group.idleTimer) clearTimeout(group.idleTimer);
  group.idleTimer = 0;
  const now = Date.now();
  group.ports.set(port, now);
  portState.subscriptions.add(key);
  if (group.socket?.readyState === WebSocket.OPEN) {
    quotePost(port, quoteWorkerEnvelope(group, {
      type: "open",
      received_at: now,
    }));
    quoteSendBootstrap(group, port);
  }
}

function quoteHandlePortMessage(port, event) {
  const payload = event?.data;
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    quotePortError(
      port,
      "",
      quoteProtocolError("QUOTE_WORKER_PORT_MESSAGE_INVALID", "port message must be an object"),
      { fatal: true },
    );
    quoteReleasePort(port);
    return;
  }
  const type = String(payload.type || "");
  const key = typeof payload.key === "string" ? payload.key : "";
  try {
    if (type === "subscribe") {
      quoteSubscribePort(port, payload);
      return;
    }
    if (type === "lease") {
      const exactKey = quoteExactIdentity(key, "key");
      const group = quoteGroups.get(exactKey);
      if (group?.ports.has(port)) {
        group.ports.set(port, Date.now());
      } else {
        quoteDeletePortSubscriptionState(port, exactKey);
      }
      return;
    }
    if (type === "close") {
      const exactKey = quoteExactIdentity(key, "key");
      const group = quoteGroups.get(exactKey);
      quoteReleasePortSubscription(port, exactKey, false);
      quoteAcknowledgePortClose(
        port,
        exactKey,
        group,
        payload.code,
        payload.reason,
      );
      return;
    }
    if (type === "restart") {
      const exactKey = quoteExactIdentity(key, "key");
      const group = quoteGroups.get(exactKey);
      if (!group || !group.ports.has(port)) {
        quoteDeletePortSubscriptionState(port, exactKey);
        return;
      }
      quoteRestartGroup(
        group,
        quoteProtocolError(
          "QUOTE_WORKER_CLIENT_PROTOCOL_CORRUPTION",
          String(payload.reason || "client requested quote protocol restart"),
        ),
      );
      return;
    }
    throw quoteProtocolError("QUOTE_WORKER_PORT_MESSAGE_INVALID", `unsupported port message ${type}`);
  } catch (error) {
    quotePortError(port, key, error, { fatal: true });
    quoteReleasePort(port);
  }
}

self.onconnect = event => {
  const port = event?.ports?.[0];
  if (!port || quotePortStates.has(port)) return;
  quotePortStates.set(port, { subscriptions: new Set() });
  port.onmessage = eventMessage => quoteHandlePortMessage(port, eventMessage);
  port.onmessageerror = () => quoteReleasePort(port);
  port.start();
};

setInterval(() => {
  const cutoff = Date.now() - QUOTE_PORT_LEASE_TIMEOUT_MS;
  for (const group of Array.from(quoteGroups.values())) {
    for (const [port, leasedAt] of Array.from(group.ports.entries())) {
      if (leasedAt < cutoff) quoteReleasePortSubscription(port, group.key, true);
    }
  }
}, QUOTE_PORT_LEASE_SWEEP_MS);
