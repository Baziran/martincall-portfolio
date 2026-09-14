from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aef_terminal.ui.asset_services import (
    QUOTE_STREAM_WORKER_FILE,
    martincall_js_source_sync,
    martincall_quote_worker_build_id_sync,
    martincall_quote_worker_delivery_sync,
    martincall_quote_worker_source_sync,
)
from aef_terminal.ui.quote_stream_contract import quote_stream_row_contract_manifest
from aef_terminal.ui.routers.assets import AssetRouterDeps, create_asset_router
from tests.route_helpers import app_route_paths


def test_shared_quote_worker_is_cache_busted_and_served_as_an_immutable_asset() -> None:
    worker_source = martincall_quote_worker_source_sync()
    worker_delivery = martincall_quote_worker_delivery_sync()
    worker_build_id = martincall_quote_worker_build_id_sync()

    assert worker_source.startswith('"use strict";\nconst QUOTE_STREAM_ROW_CONTRACT_MANIFEST = ')
    assert worker_source.endswith(
        QUOTE_STREAM_WORKER_FILE.read_text(encoding="utf-8").removeprefix('"use strict";\n')
    )
    assert worker_delivery
    encoded_contract = json.dumps(
        quote_stream_row_contract_manifest(),
        sort_keys=True,
        separators=(",", ":"),
    )
    assert f"const manifest={encoded_contract};" in worker_source
    assert f"const manifest={encoded_contract};" in martincall_js_source_sync()
    assert "const QUOTE_ROW_REQUIRED_FIELDS" not in worker_source
    assert "QUOTE_ROW_CONTRACT.required_fields" in worker_source
    assert '"price_alert_runtime_snapshot"' not in worker_source
    assert 'Object.hasOwn(message, "price_alert_runtime")' in worker_source
    assert 'hasPriceAlertRuntime && message.type !== "quote_heartbeat"' in worker_source
    assert (
        "price alert runtime is allowed only on quote snapshot or heartbeat envelopes"
        in worker_source
    )
    assert "function quoteCacheRevision(message)" in worker_source
    assert "cacheRevision.epoch !== group.state.cacheEpoch" in worker_source
    assert "cacheRevision.generation < group.state.cacheGeneration" in worker_source
    assert "cache_epoch: group.state.cacheEpoch" in worker_source
    assert "cache_generation: group.state.cacheGeneration" in worker_source
    assert len(worker_build_id) == 10
    assert worker_build_id == hashlib.sha1(worker_delivery.encode("utf-8")).hexdigest()[:10]
    assert (
        f'window.MARTINCALL_QUOTE_WORKER_URL = "'
        f'/assets/quote-stream-worker.js?v={worker_build_id}";'
    ) in martincall_js_source_sync()

    async def html(_debug: bool) -> str:
        return "<html></html>"

    async def javascript() -> str:
        return "window.ready=true;"

    async def css() -> str:
        return "body{}"

    async def quote_worker() -> str:
        return worker_delivery

    app = FastAPI()
    app.include_router(
        create_asset_router(
            AssetRouterDeps(
                html=html,
                js=javascript,
                css=css,
                quote_worker=quote_worker,
            )
        )
    )
    assert "/debug-js" not in set(app_route_paths(app))
    response = TestClient(app).get(f"/assets/quote-stream-worker.js?v={worker_build_id}")

    assert response.status_code == 200
    assert response.text == worker_delivery
    assert response.headers["content-type"].startswith("application/javascript")
    assert "immutable" in response.headers["cache-control"]


def test_shared_quote_worker_rejects_coerced_sequence_and_revision_values(
    tmp_path: Path,
) -> None:
    worker_path = tmp_path / "quote-stream-shared-worker.js"
    worker_path.write_text(martincall_quote_worker_source_sync(), encoding="utf-8")
    script_path = tmp_path / "quote-stream-worker-exact-numbers.js"
    script_path.write_text(
        "\n".join(
            (
                'const assert = require("node:assert/strict");',
                'const fs = require("fs");',
                'const vm = require("vm");',
                "global.setInterval = () => 1;",
                "global.clearInterval = () => {};",
                "global.setTimeout = () => 1;",
                "global.clearTimeout = () => {};",
                "global.WebSocket = class {};",
                "global.self = { location: { protocol: 'http:', host: 'terminal.test', href: 'http://terminal.test/worker.js' }, onconnect: null };",
                'vm.runInThisContext(fs.readFileSync(process.argv[2], "utf8"), { filename: process.argv[2] });',
                "assert.equal(quoteExactSequence(1, 'SEQUENCE_INVALID'), 1);",
                "assert.equal(quoteExactRowsRevision(0), 0);",
                "assert.throws(() => quoteExactSequence('1', 'SEQUENCE_INVALID'), error => error?.code === 'SEQUENCE_INVALID');",
                "assert.throws(() => quoteExactRowsRevision('0'), error => error?.code === 'QUOTE_WORKER_ROWS_REVISION_INVALID');",
                "assert.throws(() => quoteExactSequence(1.5, 'SEQUENCE_INVALID'), error => error?.code === 'SEQUENCE_INVALID');",
                "const route = { instrument_id: 'coinbase|product|BTC-USD', route_fingerprint: 'coinbase|BTC-USD|generation-1' };",
                "const group = { fixed: { routeKeySet: new Set([quoteIdentityKey(route.instrument_id, route.route_fingerprint)]) }, state: { rowsByIdentity: new Map() } };",
                "assert.throws(() => quoteRowsByIdentity(group, [{ ...route, price: 100 }]), error => error?.code === 'QUOTE_WORKER_ROWS_INVALID' && error.message.includes('quote row is incomplete'));",
                "const typed = Object.fromEntries(QUOTE_ROW_CONTRACT.required_fields.map(field => [field, null]));",
                "Object.assign(typed, route, { key: 'BTC-USD', display: 'BTC-USD', provider_symbol: 'BTC-USD', price: 100, bid: 99, ask: 101, last: 100, quote_ts: '2026-07-16T14:45:00+00:00', price_source: 'last', quote_time_basis: 'provider_event', quote_provider_ts: '2026-07-16T14:45:00+00:00', quote_received_at: '2026-07-16T14:45:00+00:00', quote_status: 'live', quote_entitlement: 'live', quote_is_delayed: false, quote_is_stale: false, last_provider_ts: '2026-07-16T14:45:00+00:00', last_status: 'live', bid_ask_received_at: '2026-07-16T14:45:00+00:00', bid_ask_status: 'live', source: 'coinbase:quote-live', live_quote: true, warning: '', contract_rollover_due: false, contract_rollover_new: false });",
                "assert.equal(quoteRowsByIdentity(group, [typed]).get(quoteIdentityKey(route.instrument_id, route.route_fingerprint)).price, 100);",
                "assert.throws(() => quoteRowsByIdentity(group, [{ ...typed, price: '100' }]), error => error?.code === 'QUOTE_WORKER_ROWS_INVALID' && error.message.includes('fields are malformed'));",
                "assert.throws(() => quoteRowsByIdentity(group, [{ ...typed, contract_rollover_warning: { status: 'rollover_due', days_left: '2', expiry_date: '2026-09-18', contract_month: '202609', message: 'Roll soon' } }]), error => error?.code === 'QUOTE_WORKER_ROWS_INVALID' && error.message.includes('fields are malformed'));",
            )
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        ["node", str(script_path), str(worker_path)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_shared_quote_worker_multiplexes_exact_groups_and_bootstraps_late_ports(
    tmp_path: Path,
) -> None:
    worker_path = tmp_path / "quote-stream-shared-worker.js"
    worker_path.write_text(martincall_quote_worker_source_sync(), encoding="utf-8")
    script_path = tmp_path / "quote-stream-shared-worker-contract.js"
    script_path.write_text(
        r"""
const assert = require("assert");
const fs = require("fs");
const vm = require("vm");

let now = 1_000;
Date.now = () => now;
const intervals = [];
const timeouts = [];
global.setInterval = (callback, milliseconds) => {
  const row = { callback, milliseconds, active: true };
  intervals.push(row);
  return row;
};
global.clearInterval = row => { if (row) row.active = false; };
global.setTimeout = (callback, milliseconds) => {
  const row = { callback, milliseconds, active: true };
  timeouts.push(row);
  return row;
};
global.clearTimeout = row => { if (row) row.active = false; };
function flushTimeouts() {
  for (const row of [...timeouts]) {
    if (!row.active) continue;
    row.active = false;
    row.callback();
  }
}

class FakeWebSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;
  static instances = [];

  constructor(url) {
    this.url = url;
    this.readyState = FakeWebSocket.CONNECTING;
    this.closed = false;
    FakeWebSocket.instances.push(this);
  }

  open() {
    this.readyState = FakeWebSocket.OPEN;
    this.onopen?.({});
  }

  message(data) {
    this.onmessage?.({ data });
  }

  close(code = 1000, reason = "") {
    this.closed = true;
    this.closeCode = code;
    this.closeReason = reason;
    this.readyState = FakeWebSocket.CLOSED;
  }

  serverClose(code = 1006, reason = "server close") {
    this.readyState = FakeWebSocket.CLOSED;
    this.onclose?.({ code, reason });
  }
}
global.WebSocket = FakeWebSocket;

class FakePort {
  constructor(name) {
    this.name = name;
    this.posts = [];
    this.started = false;
  }

  start() { this.started = true; }
  postMessage(payload) { this.posts.push(payload); }
  send(payload) { this.onmessage?.({ data: payload }); }
}

global.self = {
  location: {
    protocol: "http:",
    host: "terminal.test",
    href: "http://terminal.test/assets/quote-stream-worker.js?v=test",
  },
  onconnect: null,
};

let source = fs.readFileSync(process.argv[2], "utf8");
const portStateDeclaration = "const quotePortStates = new WeakMap();";
assert(source.includes(portStateDeclaration));
assert(!source.includes("__quotePortStateHas"), "production worker must not expose debug state");
source = source.replace(
  portStateDeclaration,
  `${portStateDeclaration}\nglobalThis.__quotePortStateHas = port => quotePortStates.has(port);`,
);
vm.runInThisContext(source, { filename: process.argv[2] });
assert.strictEqual(intervals.length, 1);

function connectPort(name) {
  const port = new FakePort(name);
  self.onconnect({ ports: [port] });
  assert.strictEqual(port.started, true);
  assert.strictEqual(__quotePortStateHas(port), true);
  return port;
}

const routes = [
  { instrument_id: "coinbase|product|BTC-USD", route_fingerprint: "coinbase|BTC-USD|generation-1" },
  { instrument_id: "coinbase|product|ETH-USD", route_fingerprint: "coinbase|ETH-USD|generation-1" },
];
function quoteRow(route, price) {
  const symbol = route.instrument_id.includes("BTC") ? "BTC-USD" : "ETH-USD";
  const ts = "2026-07-16T14:45:00+00:00";
  return {
    ...route,
    key: symbol,
    display: symbol,
    provider_symbol: symbol,
    price,
    bid: price - 1,
    ask: price + 1,
    last: price,
    quote_close: null,
    quote_ts: ts,
    price_source: "last",
    quote_time_basis: "provider_event",
    quote_provider_ts: ts,
    quote_received_at: ts,
    quote_status: "live",
    quote_entitlement: "live",
    quote_is_delayed: false,
    quote_is_stale: false,
    last_provider_ts: ts,
    last_status: "live",
    bid_ask_received_at: ts,
    bid_ask_status: "live",
    change: null,
    change_pct: null,
    previous_session_close: null,
    change_base: null,
    source: "coinbase:quote-live",
    live_quote: true,
    warning: "",
    contract: null,
    local_symbol: null,
    contract_month: null,
    contract_rollover_due: false,
    contract_rollover_warning: null,
    contract_rollover_new: false,
    contract_rollover_new_message: null,
  };
}
const pairs = routes.map(route => [route.instrument_id, route.route_fingerprint]);
const subscription = { interval: "5m", routes };
const key = JSON.stringify([pairs, "5m"]);
function socketUrl(interval, routeRows) {
  const params = new URLSearchParams({
    routes: JSON.stringify(routeRows),
    interval,
  });
  return `ws://terminal.test/ws/quotes?${params.toString()}`;
}
const ws_url = socketUrl("5m", routes);
const subscribe = { type: "subscribe", key, ws_url, subscription };
const cacheEpoch = "00000000-0000-4000-8000-000000000001";

const first = connectPort("first");
first.send(subscribe);
assert.strictEqual(FakeWebSocket.instances.length, 1);
const firstSocket = FakeWebSocket.instances[0];
firstSocket.open();
assert.strictEqual(first.posts.at(-1).type, "open");
assert.strictEqual(first.posts.at(-1).shared_ports, 1);

const liveBeforeStateRaw = JSON.stringify({
  type: "live_candle_delta",
  interval: "5m",
  sequence: 1,
  loss_after_sequence: null,
  bars: [{
    ...routes[0],
    timeframe: "5m",
    preview_kind: "broker_trade",
    close: 99,
  }],
});
firstSocket.message(liveBeforeStateRaw);
assert.strictEqual(first.posts.at(-1).data, liveBeforeStateRaw);

const liveBeforeStateLate = connectPort("live-before-state-late");
liveBeforeStateLate.send(subscribe);
assert.strictEqual(FakeWebSocket.instances.length, 1);
assert.strictEqual(liveBeforeStateLate.posts[0].type, "open");
assert.strictEqual(liveBeforeStateLate.posts[1].type, "message");
assert.strictEqual(liveBeforeStateLate.posts[1].bootstrap, true);
const liveBeforeStateBootstrap = JSON.parse(liveBeforeStateLate.posts[1].data);
assert.strictEqual(liveBeforeStateBootstrap.type, "live_candle_delta");
assert.strictEqual(liveBeforeStateBootstrap.shared_transport_bootstrap, true);
assert.strictEqual(liveBeforeStateBootstrap.sequence, 1);
assert.deepStrictEqual(liveBeforeStateBootstrap.bars, [{
  ...routes[0],
  timeframe: "5m",
  preview_kind: "broker_trade",
  close: 99,
}]);
liveBeforeStateLate.send({
  type: "close",
  key,
  code: 1000,
  reason: "live-before-state bootstrap checked",
});
assert.strictEqual(liveBeforeStateLate.posts.at(-1).close_ack, true);
assert.strictEqual(__quotePortStateHas(liveBeforeStateLate), false);

const serverSubscription = {
  interval: "5m",
  routes: routes.map((route, index) => ({
    ...route,
    provider: "coinbase",
    provider_symbol: index ? "ETH-USD" : "BTC-USD",
  })),
};
const snapshot = {
  type: "quote_snapshot",
  source: "quote:stream",
  sequence: 1,
  revision: 10,
  cache_epoch: cacheEpoch,
  cache_generation: 10,
  subscription: serverSubscription,
  rows: [
    quoteRow(routes[0], 100),
    quoteRow(routes[1], 200),
  ],
};
const snapshotRaw = JSON.stringify(snapshot);
firstSocket.message(snapshotRaw);
assert.strictEqual(first.posts.at(-1).type, "message");
assert.strictEqual(first.posts.at(-1).data, snapshotRaw);

firstSocket.message(JSON.stringify({
  type: "quote_delta",
  interval: "5m",
  sequence: 2,
  revision: 11,
  cache_epoch: cacheEpoch,
  cache_generation: 11,
  rows: [quoteRow(routes[0], 101)],
}));
const targets = [{
  id: "target-1",
  ...routes[1],
  timeframe: "5m",
  point: { price: 210 },
}];
const priceAlertRuntime = [{
  id: "alert-1",
  ...routes[0],
  timeframe: "5m",
  armed: true,
  fired: false,
}];
firstSocket.message(JSON.stringify({
  type: "option_targets_snapshot",
  interval: "5m",
  sequence: 3,
  revision: 4,
  option_targets: targets,
}));
firstSocket.message(JSON.stringify({
  type: "quote_heartbeat",
  interval: "5m",
  sequence: 4,
  revision: 5,
  price_alert_runtime: priceAlertRuntime,
}));
firstSocket.message(JSON.stringify({
  type: "quote_aux_status",
  interval: "5m",
  sequence: 5,
  degraded: true,
  warnings: { quote_rows: "provider delayed" },
}));
firstSocket.message(JSON.stringify({
  type: "quote_heartbeat",
  interval: "5m",
  sequence: 6,
  revision: 11,
}));
firstSocket.message(JSON.stringify({
  type: "quote_delta",
  interval: "5m",
  sequence: 7,
  revision: 11,
  cache_epoch: cacheEpoch,
  cache_generation: 12,
  rows: [],
}));
const liveRaw = JSON.stringify({
  type: "live_candle_delta",
  interval: "5m",
  sequence: 7,
  loss_after_sequence: 4,
  bars: [
    {
      ...routes[0],
      timeframe: "5m",
      preview_kind: "broker_trade",
      gateway_ts: new Date(now - 10_001).toISOString(),
      close: 101,
    },
    {
      ...routes[1],
      timeframe: "5m",
      preview_kind: "broker_trade",
      close: 201,
    },
  ],
});
firstSocket.message(liveRaw);

const late = connectPort("late");
late.send(subscribe);
assert.strictEqual(FakeWebSocket.instances.length, 1, "same exact key must reuse one WebSocket");
assert.strictEqual(late.posts[0].type, "open");
assert.strictEqual(late.posts[0].shared_ports, 2);
assert.strictEqual(late.posts[1].type, "message");
assert.strictEqual(late.posts[1].bootstrap, true);
const bootstrap = JSON.parse(late.posts[1].data);
assert.strictEqual(bootstrap.type, "quote_snapshot");
assert.strictEqual(bootstrap.shared_transport_bootstrap, true);
assert.strictEqual(bootstrap.sequence, 7);
assert.strictEqual(bootstrap.revision, 11);
assert.strictEqual(bootstrap.cache_epoch, cacheEpoch);
assert.strictEqual(bootstrap.cache_generation, 12);
assert.deepStrictEqual(bootstrap.subscription, serverSubscription);
assert.strictEqual(bootstrap.rows.find(row => row.instrument_id === routes[0].instrument_id).price, 101);
assert.deepStrictEqual(bootstrap.option_targets, targets);
assert.deepStrictEqual(bootstrap.price_alert_runtime, priceAlertRuntime);
assert.strictEqual(bootstrap.degraded, true);
assert.strictEqual(bootstrap.warnings.quote_rows, "provider delayed");
assert.strictEqual(late.posts[2].type, "message");
assert.strictEqual(late.posts[2].bootstrap, true);
const liveBootstrap = JSON.parse(late.posts[2].data);
assert.strictEqual(liveBootstrap.type, "live_candle_delta");
assert.strictEqual(liveBootstrap.shared_transport_bootstrap, true);
assert.strictEqual(liveBootstrap.sequence, 7);
assert.strictEqual(liveBootstrap.loss_after_sequence, 4);
assert.strictEqual(liveBootstrap.interval, "5m");
assert.deepStrictEqual(liveBootstrap.bars, [{
  ...routes[1],
  timeframe: "5m",
  preview_kind: "broker_trade",
  close: 201,
}]);

const nextLiveRaw = JSON.stringify({
  type: "live_candle_delta",
  interval: "5m",
  sequence: 8,
  loss_after_sequence: null,
  bars: [{
    ...routes[0],
    timeframe: "5m",
    preview_kind: "broker_trade",
    gateway_ts: new Date(now).toISOString(),
    close: 102,
  }],
});
firstSocket.message(nextLiveRaw);
assert.strictEqual(late.posts[3].data, nextLiveRaw, "future raw live frame must follow bootstrap FIFO");

const nextRaw = JSON.stringify({
  type: "quote_heartbeat",
  interval: "5m",
  sequence: 8,
  revision: 11,
});
firstSocket.message(nextRaw);
assert.strictEqual(late.posts[4].type, "message");
assert.strictEqual(late.posts[4].data, nextRaw, "subsequent raw frame must follow bootstrap FIFO");
assert.strictEqual(JSON.parse(late.posts[4].data).sequence, bootstrap.sequence + 1);

now += 11_001;
const expiredLate = connectPort("expired-late");
expiredLate.send(subscribe);
assert.strictEqual(expiredLate.posts[0].type, "open");
assert.strictEqual(JSON.parse(expiredLate.posts[1].data).type, "quote_snapshot");
const expiredLiveBootstrap = JSON.parse(expiredLate.posts[2].data);
assert.strictEqual(expiredLiveBootstrap.type, "live_candle_delta");
assert.strictEqual(expiredLiveBootstrap.sequence, 8);
assert.strictEqual(expiredLiveBootstrap.loss_after_sequence, null);
assert.deepStrictEqual(expiredLiveBootstrap.bars, []);
expiredLate.send({ type: "close", key, code: 1000, reason: "expired bootstrap checked" });
assert.strictEqual(expiredLate.posts.at(-1).close_ack, true);
assert.strictEqual(__quotePortStateHas(expiredLate), false);

const invalidSequence = JSON.stringify({
  type: "quote_heartbeat",
  interval: "5m",
  sequence: 10,
  price_alert_runtime: priceAlertRuntime,
});
firstSocket.message(invalidSequence);
assert.strictEqual(firstSocket.closed, true);
assert(first.posts.some(row => row.type === "close" && row.restart === true));
assert(late.posts.some(row => row.type === "close" && row.restart === true));
assert.strictEqual(__quotePortStateHas(first), false, "group restart must release the first port state");
assert.strictEqual(__quotePortStateHas(late), false, "group restart must release every member state");

const invalidIdentityOwner = connectPort("invalid-identity-owner");
invalidIdentityOwner.send(subscribe);
assert.strictEqual(FakeWebSocket.instances.length, 2);
const invalidIdentitySocket = FakeWebSocket.instances[1];
invalidIdentitySocket.message(snapshotRaw);
invalidIdentitySocket.message(JSON.stringify({
  type: "quote_heartbeat",
  interval: "5m",
  sequence: 2,
  price_alert_runtime: [{
    ...priceAlertRuntime[0],
    route_fingerprint: "coinbase|BTC-USD|wrong-generation",
  }],
}));
assert.strictEqual(invalidIdentitySocket.closed, true);
assert(invalidIdentityOwner.posts.some(row => (
  row.type === "error"
  && row.code === "QUOTE_WORKER_PRICE_ALERT_RUNTIME_INVALID"
  && row.fatal === true
)));
assert.strictEqual(__quotePortStateHas(invalidIdentityOwner), false);

const invalidIntervalOwner = connectPort("invalid-interval-owner");
invalidIntervalOwner.send(subscribe);
assert.strictEqual(FakeWebSocket.instances.length, 3);
const invalidIntervalSocket = FakeWebSocket.instances[2];
invalidIntervalSocket.message(snapshotRaw);
invalidIntervalSocket.message(JSON.stringify({
  type: "quote_heartbeat",
  interval: "5m",
  sequence: 2,
  price_alert_runtime: [{ ...priceAlertRuntime[0], timeframe: "1m" }],
}));
assert.strictEqual(invalidIntervalSocket.closed, true);
assert(invalidIntervalOwner.posts.some(row => (
  row.type === "error"
  && row.code === "QUOTE_WORKER_PRICE_ALERT_RUNTIME_INVALID"
  && row.fatal === true
)));
assert.strictEqual(__quotePortStateHas(invalidIntervalOwner), false);

const emptyRuntimeIdOwner = connectPort("empty-runtime-id-owner");
emptyRuntimeIdOwner.send(subscribe);
assert.strictEqual(FakeWebSocket.instances.length, 4);
const emptyRuntimeIdSocket = FakeWebSocket.instances[3];
emptyRuntimeIdSocket.message(snapshotRaw);
emptyRuntimeIdSocket.message(JSON.stringify({
  type: "quote_heartbeat",
  interval: "5m",
  sequence: 2,
  price_alert_runtime: [{ ...priceAlertRuntime[0], id: "" }],
}));
assert.strictEqual(emptyRuntimeIdSocket.closed, true);
assert(emptyRuntimeIdOwner.posts.some(row => (
  row.type === "error"
  && row.code === "QUOTE_WORKER_PRICE_ALERT_RUNTIME_INVALID"
  && row.fatal === true
)));
assert.strictEqual(__quotePortStateHas(emptyRuntimeIdOwner), false);

const duplicateRuntimeIdOwner = connectPort("duplicate-runtime-id-owner");
duplicateRuntimeIdOwner.send(subscribe);
assert.strictEqual(FakeWebSocket.instances.length, 5);
const duplicateRuntimeIdSocket = FakeWebSocket.instances[4];
duplicateRuntimeIdSocket.message(snapshotRaw);
duplicateRuntimeIdSocket.message(JSON.stringify({
  type: "quote_heartbeat",
  interval: "5m",
  sequence: 2,
  price_alert_runtime: [
    priceAlertRuntime[0],
    { ...priceAlertRuntime[0], ...routes[1] },
  ],
}));
assert.strictEqual(duplicateRuntimeIdSocket.closed, true);
assert(duplicateRuntimeIdOwner.posts.some(row => (
  row.type === "error"
  && row.code === "QUOTE_WORKER_PRICE_ALERT_RUNTIME_INVALID"
  && row.fatal === true
)));
assert.strictEqual(__quotePortStateHas(duplicateRuntimeIdOwner), false);

const replacementOwner = connectPort("replacement-owner");
replacementOwner.send(subscribe);
assert.strictEqual(FakeWebSocket.instances.length, 6);
const replacementSocket = FakeWebSocket.instances[5];
const isolated = connectPort("isolated");
isolated.send(subscribe);
assert.strictEqual(FakeWebSocket.instances.length, 6);
isolated.send({ type: "close", key, code: 1000, reason: "isolated close" });
const isolatedAck = isolated.posts.at(-1);
assert.strictEqual(isolatedAck.type, "close");
assert.strictEqual(isolatedAck.close_ack, true);
assert.strictEqual(isolatedAck.code, 1000);
assert.strictEqual(isolatedAck.reason, "isolated close");
assert.strictEqual(isolatedAck.shared_ports, 1);
assert.strictEqual(__quotePortStateHas(isolated), false, "acknowledged close must release port state");
assert.strictEqual(replacementSocket.closed, false, "closing one port must not close another port's socket");

replacementOwner.send({ type: "restart", key, reason: "client exact-contract failure" });
assert.strictEqual(replacementSocket.closed, true);
assert(replacementOwner.posts.some(row => row.type === "close" && row.restart === true));
assert.strictEqual(__quotePortStateHas(replacementOwner), false);

const leasedOwner = connectPort("leased-owner");
leasedOwner.send(subscribe);
assert.strictEqual(FakeWebSocket.instances.length, 7);
const leasedSocket = FakeWebSocket.instances[6];

const bad = connectPort("bad");
bad.send({
  ...subscribe,
  ws_url: socketUrl("5m", [routes[0]]),
});
assert.strictEqual(FakeWebSocket.instances.length, 7);
assert(bad.posts.some(row => row.type === "error" && row.fatal === true));
assert.strictEqual(__quotePortStateHas(bad), false, "fatal port failure must release port state");

const oneMinuteSubscription = { interval: "1m", routes };
const oneMinuteKey = JSON.stringify([pairs, "1m"]);
const expiringOwner = connectPort("expiring-owner");
expiringOwner.send({
  type: "subscribe",
  key: oneMinuteKey,
  ws_url: socketUrl("1m", routes),
  subscription: oneMinuteSubscription,
});
assert.strictEqual(FakeWebSocket.instances.length, 8, "a different interval must own a different WebSocket");
const expiringSocket = FakeWebSocket.instances[7];

now += 70_000;
leasedOwner.send({ type: "lease", key });
intervals[0].callback();
assert(expiringOwner.posts.some(row => row.type === "close" && row.lease_expired === true));
assert.strictEqual(__quotePortStateHas(expiringOwner), false, "lease expiry must release port state");
assert.strictEqual(__quotePortStateHas(leasedOwner), true, "renewed lease must retain active state");
flushTimeouts();
assert.strictEqual(expiringSocket.closed, true, "last expired lease must close its idle group");
assert.strictEqual(leasedSocket.closed, false, "a renewed exact lease must remain isolated and open");

leasedOwner.send({ type: "close", key, code: 1000, reason: "last port close" });
const finalAck = leasedOwner.posts.at(-1);
assert.strictEqual(finalAck.close_ack, true);
assert.strictEqual(finalAck.reason, "last port close");
assert.strictEqual(__quotePortStateHas(leasedOwner), false);
flushTimeouts();
assert.strictEqual(leasedSocket.closed, true);

const serverClosedOwner = connectPort("server-closed-owner");
serverClosedOwner.send(subscribe);
assert.strictEqual(FakeWebSocket.instances.length, 9);
const serverClosedSocket = FakeWebSocket.instances[8];
serverClosedSocket.serverClose(1001, "upstream maintenance");
assert(serverClosedOwner.posts.some(row => row.type === "close" && row.code === 1001));
assert.strictEqual(__quotePortStateHas(serverClosedOwner), false, "server close must release port state");
""".strip()
        + "\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        ["node", str(script_path), str(worker_path)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
