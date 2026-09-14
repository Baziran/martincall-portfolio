# UI Error Contract

MartinCall API and browser UI share a small structured error shape so clients can show stable messages without parsing free text.

## Server payload

Action and read endpoints return JSON with:

```json
{
  "ok": false,
  "message": "human-readable summary",
  "error": {
    "code": "TICK_INSTRUMENT_ID_REQUIRED",
    "category": "tick",
    "retryable": false,
    "message": "instrument_id is required"
  }
}
```

- `ok` — request success flag.
- `message` — optional top-level copy (present on action errors).
- `error.code` — stable machine id for UI logic and tests.
- `error.category` — domain (`tick`, `market`, `analysis`, `storage`, …).
- `error.retryable` — whether the client may retry safely.
- `error.message` — user-facing detail.

Helpers live in `aef_terminal.ui.routers.error_payloads`:

- `build_error_payload` — base shape
- `build_action_error_response` — adds top-level `message`
- `build_stream_status_payload` — websocket status frames

HTTP failures (for example chart-only `503`) use the same `error` object in the JSON body.
If persisted watchlist storage is unavailable, `/api/market` fails closed with HTTP `503` and
`MARKET_WATCHLIST_UNAVAILABLE`; it never reconstructs an instrument from request text or a static
catalog.

## Browser helpers

Defined in `src/aef_terminal/ui/assets/js/15-fetch-json.js`:

| Helper | Use |
|--------|-----|
| `apiErrorMessage(response, fallback)` | JSON body with `error` or `message` |
| `requestErrorMessage(error, fallback)` | Thrown `Error`, including `error.payload` |
| `requestErrorCode(error)` | Stable code from a normalized thrown HTTP payload |
| `fetchJson(url, options)` | Parses JSON; on HTTP error attaches body to `Error.payload` |

Covered UI paths (incremental): storage sync, tick/live streams, paper trading and command
collisions, market load/analysis poll and route selection, watchlist CRUD/version conflicts, Discord
feed/gate/ingest, system health, sleep/TWS/Telegram/IBKR reconnect settings, GEX refresh,
option-target repricing, boot failures, GEX scheduler / option caps settings.

Expected domain failures reach routers as typed exceptions with stable `.code` fields. Router code
may map those codes to status/category/retryability, but must not inspect exception text to discover
route identity, optimistic-concurrency conflicts, command replay collisions, or service state.
Malformed route selections return non-retryable `422`; an exact route-generation mismatch returns
retryable `409 ROUTE_SELECTION_MISMATCH` with typed expected/actual routes. Browser market and
screener consumers reload the qualified watchlist once and retry without parsing the message.

### `fetchJson` HTTP errors

When the server returns a non-2xx response with a JSON body, `fetchJson` throws an `Error` whose:

- `message` comes from `apiErrorMessage(payload)`
- `payload` is the parsed JSON (for `requestErrorMessage` downstream)

FastAPI HTTP errors often wrap the structured body as `{ "detail": { "ok": false, "error": ... } }`.
`normalizeErrorPayload()` unwraps that shape before message extraction.

Plain-text HTTP bodies still produce a generic `HTTP <status>: …` message without `payload`.

## Client usage

Prefer structured helpers over `error.message` alone:

```javascript
try {
  const payload = await fetchJson("/api/ticks/live", { method: "POST", body: … });
  if (!payload?.ok) throw new Error(apiErrorMessage(payload, "tick live failed"));
} catch (error) {
  console.warn("tick live failed", requestErrorMessage(error, "tick live failed"));
}
```

For in-band `ok: false` responses (no HTTP error), call `apiErrorMessage` on the payload directly.

## CI smoke

`tests/test_api_smoke.py` exercises `/api/health`, chart-only `/api/market`, `/api/ticks/context`,
`/api/paper/trades`, `/api/paper/orders`, `/api/paper/events`, and `/api/system`,
`/api/storage`, `/api/gex`, `/api/gex/scheduler`, `/api/instruments`, and `/api/data-providers`
through FastAPI `TestClient` (no external server). The `API Smoke` GitHub workflow runs these tests on push/PR;
the post-deployment live curl smoke is an explicit operator step through `make smoke-api` under the
[runtime and readiness contract](ARCHITECTURE.md#runtime-and-toolchain).
