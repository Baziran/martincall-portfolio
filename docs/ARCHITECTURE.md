# Architecture

MartinCall is an event-driven local trading decision terminal for research, alerts, and paper
trading. Market data is provider-bound: IBKR, Coinbase, and T-Invest are separate sources of
truth, never interchangeable fallbacks. The engine emits typed execution intent; live broker order
placement is a product target, not part of the current runtime.

This document is the technical reference for the current system. It describes owners, public
contracts, state transitions, and failure boundaries. Private implementation constants, helper
names, and changeable enumerations remain code- and test-owned; public cross-boundary limits are
stated here only when they are architectural. Current operator commands live in the root and scripts
READMEs.

Read only the section relevant to the task. Historical reports, completed handoffs, research notes,
and git history are evidence, not current architecture.

## Reference map

| Change area | Read |
|---|---|
| Runtime, release, readiness | [Runtime and toolchain](#runtime-and-toolchain), [Storage and settings](#storage-and-settings) |
| Provider integration or routing | [Instrument identity and watchlist](#instrument-identity-and-watchlist), [Data layer (provider-bound)](#data-layer-provider-bound) |
| Bars, sessions, gaps, history, repair | [Instrument identity and watchlist](#instrument-identity-and-watchlist), [Market OHLCV](#market-ohlcv), [Realtime streams](#realtime-streams), [Data layer (provider-bound)](#data-layer-provider-bound) |
| Quotes, watchlist trends, ticks | [Quotes and execution snapshots](#quotes-and-execution-snapshots), [Realtime streams](#realtime-streams), [Tick ingestion](#tick-ingestion) |
| Economic-event calendar | [Economic-event calendar](#economic-event-calendar), [UI](#ui), [Storage and settings](#storage-and-settings) |
| Schema or settings | [Storage and settings](#storage-and-settings); operator commands are in the root and scripts READMEs |
| GEX, options, Option Points | [Quotes and execution snapshots](#quotes-and-execution-snapshots), [Data layer (provider-bound)](#data-layer-provider-bound), [Options and GEX](#options-and-gex) |
| Indicators, signals, Trade Setup | [Signals and decisions](#signals-and-decisions), [Engine and indicators](#engine-and-indicators) |
| Research capture or model datasets | [Engine and indicators](#engine-and-indicators), [Backtest](#backtest), [external data layout](../data/README.md) |
| Browser, WebSockets, alerts | [UI](#ui), [Realtime streams](#realtime-streams) |
| Paper, replay, backtest | [Signals and decisions](#signals-and-decisions), [Paper trading](#paper-trading), [Backtest](#backtest) |

## Current system

| Area | Current contract |
|---|---|
| Runtime | Standard-GIL CPython 3.14; FastAPI application; process-owned async lanes |
| Providers | IBKR, Coinbase, and T-Invest through typed provider adapters |
| Storage | PostgreSQL/TimescaleDB; canonical application schema v18 |
| Market data | Provider-qualified bars, quotes, ticks, schedules, and explicit provisional state |
| Economic events | Read-only US macro and Federal Reserve schedule from official public sources |
| Engine | Indicator pipeline → typed candidates → `ScenarioDecision` → Trade Setup |
| UI | Server-owned domain state; vanilla JavaScript/canvas presentation over HTTP/WebSocket |
| Execution | Typed execution intent, alerts, and paper journal; no current live broker-order path |
| Tests | `make test`; PostgreSQL contract tests activate when `AEF_DATABASE_URL` is available |

Historical completeness uses durable exact-range receipts. Confirmed bars stay DB-first and remain
eligible for generic calculation while exact coverage is verified separately.

### System flow

```text
provider-qualified instrument
  -> selected ProviderAdapter
  -> typed bars / quotes / ticks / sessions / option facts
  -> canonical PostgreSQL state + process-owned realtime projections
  -> deterministic features and indicator pipeline
  -> ScenarioDecision + Trade Setup
  -> UI / alerts / paper journal / replay and backtest
```

The Python `ui` package is the application and transport layer. Browser code under `ui/assets/`
renders typed state and owns interaction geometry; it is not a second market-data or decision
engine.

Process-owned background loops are supervised as long-lived owners. Any unplanned terminal
outcome, including unexpected cancellation or a return without an exception, consumes the same
bounded restart budget. Controlled shutdown marks the owner stopping before cancellation and does
not expose completion until already-started thread, provider, and storage work has physically
settled; a replacement loop therefore never overlaps work left by its predecessor.

### Repository layout

```text
src/aef_terminal/
  data/           provider contracts, adapters, history, quotes, ticks, options, GEX
  runtime/        shared lifecycle, commit, event, quality, and calculation primitives
  features/       deterministic market and chart facts
  indicators/     module contracts, discovery, registry, and indicator packages
  signals/        candidate and trade-plan primitives
  engine/         analysis, decisions, Trade Setup, execution intent
  storage/        PostgreSQL repositories and canonical schema
  ui/             FastAPI composition, services, paper, browser assets
  alerts/         Telegram and server alert delivery
  backtest/       replay and evaluation through live contracts
  ml/             explainable model helpers and model boundaries
  research/       product source for research capture, never decision authority

tests/            contract and integration tests
scripts/          reusable operator and development tools
docs/             stable doctrine, current architecture/contracts, and executable backlog
../data/          runtime, research, experiment, and archive artifacts; never repository source
```

The repository working tree contains only files needed to build, test, run, operate, or document
the current product. Reusable capture logic under `src/aef_terminal/research/` is product source;
its generated journals, datasets, reports, and model runs live below `../data/`. One-off
experiments, prototypes, notebooks, saved diffs, caches, and retired material live in the external
locations described by [data/README.md](../data/README.md) or in git history.

`../data/` is not part of normal architecture discovery. Agents and developers start from this
reference map, repository code, and tests; they inspect only a specifically named external artifact
when a task requires research reproduction, artifact analysis, or runtime-output diagnosis.

## Runtime and toolchain

`pyproject.toml`, `.python-version`, Docker, and CI target standard-GIL CPython 3.14. Free-threaded
Python and other minor families are outside the runtime contract. `make check-python` is the local
interpreter gate.

The IBKR client is `ib_async`. Synchronous first-import boundaries call the canonical event-loop
guard before importing broker types; coroutine-only paths reuse their running loop. Broker objects
remain inside the manager-owned loop/session that created them and never cross a synchronous or
owner-loop boundary.

IBKR work is separated into process-owned chart, quote, history, and option/GEX lanes. Different
lanes may use different Gateway client IDs because their pacing and lifetime differ, but REST or
WebSocket requests never create competing runtime clients. A timeout or cancellation must not
abandon a physical database or broker operation and start an unowned duplicate.

Application composition arms owners before producers start. Shutdown closes admission first, then
lets already-started physical work settle under its real PostgreSQL or broker deadline. Restarting
an owner is explicit; shutdown does not create a parallel recovery path.

Application composition also resolves build provenance once before runtime producers start and
injects that immutable snapshot into system status. Runtime status requests never execute Git or
start a provenance subprocess after provider transports are active.

`/health` reports process liveness. `/ready` verifies the lightweight storage and canonical-writer
lease contract and returns HTTP 503 until that contract is available. Estimated row counts and
other catalog diagnostics belong to `/api/system`, never readiness. Docker probes `/ready` with the
runtime image's compiled HTTP client at a bounded one-minute cadence; it does not cold-start Python
for health checks. Operator commands are indexed by the root README; storage transition and rollback
invariants are defined under [Storage lifecycle](#storage-lifecycle).

Compose separates the canonical application and standalone tick collector into the mutually
exclusive `runtime` and `ticks` profiles. `martincall-server.sh start` enables only `runtime`;
each guarded entrypoint verifies that the other Compose service is stopped, and `make tick-feed`
runs the no-restart collector as a one-shot `ticks` profile command. The shared PostgreSQL writer
lease is still the final fail-closed boundary for processes started outside that operator path.

## Design rules

1. One canonical calculation and decision path serves live, replay, backtest, paper, and execution.
2. Provider adapters preserve exact provider identity and emit typed facts; they do not contain
   trading decisions.
3. Feature and model code is deterministic for a frozen input and exposes its reasoning.
4. Signal modules produce typed candidates, not orders. Scenario and Trade Setup owners decide.
5. Execution is downstream of explicit data quality, decision authority, and journal transitions.
6. Streaming paths are lightweight and never initialize/query storage synchronously.
7. Domain and calculation code does not depend on UI. A package-local composition adapter may
   import a generic UI service host only to mount a declared service/router contribution.
8. Indicator manifests own default/pipeline/runtime metadata; durable user ordering belongs to
   settings. Neither duplicates the other.
9. Browser code owns presentation geometry and interaction, never provider selection, market
   confirmation, repair, indicator math, signal promotion, or execution truth.
10. Missing or incompatible facts fail closed. A second source, alias, or display-string fallback
    is not recovery.

### Ownership map

| Contract | Canonical owner |
|---|---|
| Provider manifests, routes, capabilities | `data/provider_contract.py`, `data/providers.py` |
| Broker sessions and IBKR lanes | `data/ibkr/` |
| IBKR option qualification and exact series selection | `data/ibkr/option_acquisition.py`; typed contracts and expiry facts in `data/ibkr/option_contracts.py` |
| Bar, lifecycle, and provenance types | `domain.py` |
| History quality | `engine/data_quality.py` |
| Repair intent type, planning, and orchestration | `data/provider_contract.py`, `engine/history_repair.py`, `data/history_repair.py` |
| Exact history-coverage contract and commit | `data/history_coverage.py`, `storage/repos/history_commits.py`, `storage/repos/history_coverage.py` |
| Logical confirmed-bar projection contract and SQL | `runtime/bar_projection.py`, `storage/bar_projection.py` |
| Per-operation storage deadlines | `runtime/storage_deadlines.py`; PostgreSQL application lives in `storage/repos/connection.py` |
| Canonical chart commits and generations | `runtime/chart_commits.py`, `runtime/chart_events.py`, storage bar repositories |
| Provider schedule materialization, current state, and session axes | `data/provider_sessions.py`, `storage/session_intervals.py`, `storage/repos/trading_hours.py` |
| PostgreSQL connections and the canonical-writer process lease | `storage/repos/connection.py` |
| Watchlist transaction lock and optimistic ordering conflict | `watchlist_contract.py` |
| Durable drawing-point contract and runtime normalization | `drawing_contract.py`, `ui/drawing_services.py`, `ui/assets/js/10-core-state.js` |
| Durable settings vocabulary | `settings_contract.py` |
| Active paper-journal runtime projection | `paper_journal_runtime.py`; durable transactions in `storage/repos/paper.py` |
| Indicator type and manifest contracts | `indicators/module_contract.py`, `indicators/registry.py` |
| Indicator discovery and quarantine | `indicators/module_discovery.py`, `indicators/module_validation.py` |
| Decision loop and Trade Setup | `engine/decision.py`, `engine/trade_setup.py` |
| Indicator provider-session resets and windows | `features/provider_session.py` |
| Quote-stream row wire contract | `ui/quote_stream_contract.py` |
| Economic-event facts and source adapters | `data/economic_calendar.py`; request cache in `ui/economic_calendar_runtime.py` |
| Application composition and transport | `ui/` |
| Browser rendering and interaction | `ui/assets/` |

### Instrument identity and watchlist

One watchlist row represents one provider-qualified instrument contract.

```text
provider search result
  -> exact provider-owned bind/qualification
  -> persisted instruments row
  -> lookup qualified row by instrument_id
  -> route_instrument(row)
  -> immutable InstrumentRoute
  -> route.adapter.<operation>(route.instrument, ...)
```

`instrument_id` is the only durable selection, relationship, membership, job, alert, drawing,
journal, and API lookup key. `instrument_key`, display text, symbols, local symbols, and provider
symbol text are presentation or transport fields, not identity proofs. A missing ID is an explicit
failure; no name recovers it.

Provider contract identifiers are opaque. Search may normalize the user's query, but selection,
persistence, comparison, and routing retain exact provider-returned fields. Consumers do not trim,
uppercase, rewrite punctuation, or accept aliases.

`provider_symbol` is exact transport metadata after selection by `instrument_id`. The legacy
`data_symbol` field and a generic `InstrumentRoute.symbol` field are forbidden. A name-based
indicator template may select calculation parameters only; it never selects a provider, route,
stream, session, storage row, quote, or execution object.

`route_fingerprint` is derived only after an `instrument_id` lookup returns a qualified route. It
asserts the current provider-route generation and scopes caches, events, and route-sensitive rows.
It never resolves a name, selects a durable instrument, or substitutes for `instrument_id`.

The adapter publishes `session_contract_id` as exact active-route metadata. For ordinary contracts
it is the selected provider contract; for a futures root it is the provider-confirmed current
contract. Consumers use this field directly and never reconstruct it from names or fingerprints.

`InstrumentRoute.price_increment` is the projection of the exact provider execution fact selected
by that route: `contract_identity.min_tick` for an ordinary contract and
`contract_identity.current_contract.min_tick` for a futures root. A missing fact remains typed
unavailable; an instrument name or analysis profile never supplies or recovers it.

Each adapter declares one `ProviderManifest`. Registration verifies that every advertised runtime
capability implements its narrow protocol. Unsupported optional behavior is absent rather than a
`NotImplementedError` or silent no-op. `data.providers` is the composition root: it registers and
routes adapters but owns no generic history/quote/session/GEX facade or symbol dispatch.

The watchlist is server-authoritative. Membership and order are incremental transactional
operations protected by `watchlist_state.version`; replace-all JSON and browser-owned membership
are not contracts. One PostgreSQL snapshot returns membership, order, presentation, current futures
route, and persisted session materialization without contacting providers.
Selecting one provider-qualified instrument is one fenced lifecycle transaction: it persists the
instrument row, the provider-confirmed initial current contract for a futures root, membership and
order, and the resulting watchlist version together. Any failure rolls the whole selection back;
the UI never composes those durable writes or reports an incompatible partial store as unconfigured.
The selection receipt carries only that committed version; the UI accepts the following canonical
snapshot only at the same or a newer version and never publishes a writer-local partial row.
Re-selecting a retired futures root derives a typed previous/next route transition from the
persisted route owner and provider-bound next instrument; the lifecycle transaction rechecks it and
carries drawings before publishing membership.
Schema v18 can contain only active manifest providers. Watchlist materialization therefore rejects
an unexpected provider row and publishes no partial reference or quote-route snapshot; it has no
skip-unavailable compatibility reader.
Ordering conflicts cross storage/UI boundaries only as typed `WatchlistOrderError`; error codes and
payload fields say `instrument_id`, never symbol. The current request field is `expected_version`;
there is no legacy `version` alias.

If watchlist storage is unavailable, `/api/market` returns typed retryable
`MARKET_WATCHLIST_UNAVAILABLE`; it does not reconstruct a route. The browser accepts only the
versioned `{items, watchlist_version}` envelope.

Watchlist presentation is one server-owned `{display_mode, revision}` row per exact
`instrument_id`. Removing a member transactionally removes its presentation and canonical settings
references. Trend series are a separate persisted-quote read model, coalesced by exact route set and
time bucket; they never register quote demand or fall back to chart state.

## Canonical pipelines

Every product fact follows one canonical route. Another path is valid only for a different domain
object, not as a fallback representation of the same object.

### Market OHLCV

```text
qualified route
  -> selected adapter history/live contract
  -> validated Bar + immutable BarProvenance
  -> provisional realtime projection or canonical commit lane
  -> route-qualified canonical storage
  -> snapshot / indicator / engine consumers
```

#### Bar contract and lifecycle

Every `Bar` has a timezone-aware UTC timestamp, finite OHLCV, an exact boolean `closed` value, a
consistent canonical `BarState`, non-empty timeframe/source metadata,
`low <= open/close <= high`, and non-negative volume. Storage persists validated values without
clamping. Price is not globally required to be positive because valid futures can trade at zero or
below.

A provider bar additionally carries immutable provenance: provider, `instrument_id`,
`route_fingerprint`, request type, provider contract ID/type, and data type. Advisory `source` text
is never parsed as identity or admission evidence. A derived/test bar without typed provenance
cannot enter canonical storage.

The lifecycle is:

```text
forming -> awaiting_provider_confirmation -> confirmed
```

Only exchange/provider-confirmed closed `TRADES` bars admitted by the exact provenance contract
become authoritative OHLCV. IBKR `MIDPOINT`, quote-derived candles, projections, and live previews
remain typed provisional context. For Coinbase, a later provider bucket or explicit
provider-final fact proves the latest close; elapsed wall-clock time alone does not.
All canonical provider bars have minute-precision timestamps. Adapters retain ownership of the
wider provider/session grid, including valid session-opening partial bars, but a sub-minute
timestamp is malformed and cannot enter any canonical bar table.

A broker-confirmed close may render immediately as `commit_pending`, but storage, analysis,
execution, and alerts remain blocked until its canonical commit. Flat OHLC and provider-reported
zero volume do not invalidate an admitted trade bar without a stronger typed provider fact.

Canonical storage is separated by instrument class:

- ordinary instruments: `bars(instrument_id, route_fingerprint, timeframe, ts)`;
- provider-native continuous futures: `futures_continuous_bars`;
- explicitly addressed concrete futures: `futures_contract_bars`.

A higher-timeframe bar becomes confirmed only after its interval closes and every child actually
consumed by its provider aggregation contract is provider-confirmed. An absent child remains absent;
it never creates a synthetic parent. Ordinary `security_context` is causally trimmed to the closed
parent. A declared `confirmed_bar_context` may add an ordered provider-confirmed lower-timeframe
tail. Its exact-range coverage remains an audit diagnostic, while missing bars or pending finality
can make only the dependent context unavailable. Derived parent previews remain `closed=false`;
they are not stored and cannot promote candidates.

`3m` is one virtual chart/analysis timeframe with canonical source `1m`. Adapters never receive a
`3m` request and storage has no `3m` rows: DB history, repair, coverage, canonical generation, and
commit ownership remain on the exact provider-qualified `1m` series, and all three canonical bar
writers reject `3m` before SQL. The read boundary confirms one elapsed epoch-aligned bucket only from
its exact three contiguous provider-confirmed children. An incomplete elapsed triplet remains absent
without a placeholder; the current bucket may be a typed forming projection from the children already
returned. A later real `1m` commit advances the shared source generation and deterministically revises
the affected `3m` bucket. Drawing anchors remain confirmed timestamps; their logical projection groups
the same persisted `1m` source in SQL and verifies every exact minute child, not only aggregate
count/min/max, without loading months of OHLCV or creating another storage authority.

#### Sessions, historical coverage, and chart geometry

Provider schedule snapshots are the durable authority for the session facts they actually cover.
`data/provider_sessions.py` is the shared data-layer owner for exact schedule state, bounded session
axes, and refresh invalidation; storage does not import the engine. Materialized intervals and the
latest trading-hours row are derived read models only. They contain explicit provider-declared
intervals and exact source coverage. Storage admits that coverage through the provider-neutral
`provider_declared_intervals` snapshot contract, never by matching provider-specific transport
metadata. A schedule single-flight retains lifecycle ownership through provider fetch, durable
upsert, cache invalidation, and refresh acknowledgement; caller cancellation is exposed only after
that chain settles. Raw trading-hours strings and date-only closure rows are not runtime inputs. Current and
prospectively captured facts drive market status, execution gating, session reset/VWAP, and other
calculations that explicitly require session certainty. Unknown or incomplete state remains typed
unknown for those consumers; browser code never embeds an exchange calendar.

Automatic chart repair treats every continuous-session discontinuity as a gap. For a scheduled
instrument, a sub-seven-day discontinuity consults already materialized provider schedule coverage
inside the bounded newest schedule scan: verified closed slots are ignored and each contiguous
verified expected-open range is requested, while unknown, truncated, or out-of-window schedule
coverage leaves that range unknown. This classifies a discontinuity that spans an overnight closure
by its provider-confirmed open slots instead of its total wall-clock duration, without turning daily
maintenance breaks, nights, or weekends into repair demands. A seven-day-or-larger internal history
hole is a direct provider-mirror envelope regardless of historical schedule availability. Normal
chart tails are not gap facts. Empty history and a broad missing prefix are explicit DB-history
demands; explicit stale tails, invalidated timestamps, and manual refreshes may fall back directly
to bounded provider history when schedule coverage is unavailable. Schedule evidence never creates
bars or a coverage receipt.

Historical provider mirroring has a separate authority. A successful bounded provider request may create
one durable route-qualified coverage receipt for an exact, fully elapsed UTC `[from, to)` range only
when the adapter returns typed terminal `complete`, all pages are exhausted, and its provider-specific
terminal, response metadata, max-span, and applicable limit contracts classify the response as
untruncated and unsaturated. This records agreement with the provider's returned bounded snapshot
under the product's provider-mirror tolerance; it does not claim cross-feed or exchange parity.
Common orchestration never infers terminal state from bar count. The receipt includes
provider source, instrument and route generation, timeframe, exact bounds, request/admission
contract version, response cardinality and limit context, and fetch time. Only complete receipts are
durable authority; partial and failed attempts remain operational diagnostics. An empty complete
response is valid. Authentication, entitlement, partial, saturated, malformed, timed-out,
finality-unknown, failed, or ambiguous responses create no coverage.

Adapters own bounded fetch semantics but do not import or schedule the process orchestrator.
`data/history_repair.py` validates the typed adapter request and owns admission/continuation; the
application boundary injects `runtime/chart_commits.py` as the only commit owner. This keeps fetch,
orchestration, and canonical commit responsibilities acyclic and independently testable.

Coverage evidence contains no OHLCV and makes no claim that an omitted timestamp was an expected
trading slot. Only returned provider-confirmed closed trade bars enter canonical storage. Omitted
timestamps stay absent from analysis, channel extrema, alerts, and execution input. The receipt
union proves exact-range absence/completeness for audit, exact-snapshot reconciliation, and repair
planning; it is not generic calculation, candidate, decision, paper, or execution permission.
Changing the request/admission contract version invalidates old absence evidence, not the admitted
confirmed bars themselves. An active or forming tail remains pending until provider-declared
finality, and a later confirmed bar inside a covered range commits normally.

Bounded historical snapshot authority is an explicit adapter capability, separate from the ability
to request repair. For such a provider, one terminal successful response defines the provider's returned
timestamp set for its exact route/timeframe/range: canonical commit upserts those bars, removes older
in-range bars absent from that response, verifies ordered timestamps and OHLCV by readback, and
commits the receipt in one transaction. This proves agreement with that provider snapshot only; it
does not invent missing trading slots or claim cross-feed/exchange parity. Providers without this
capability may contribute admitted returned bars but cannot delete bars or create
absence-authoritative coverage.

A provider live/forming bar may appear as a typed provisional preview and is replaced as a whole by
the same-timestamp confirmed trade bar. Missing/repairing ranges remain typed status and overlay
facts. When consecutive chart rows carry authoritative provider-backed `bar_slot` values, the
browser preserves their slot discontinuity as an explicitly labelled empty screen span; verified
closed intervals have already been excluded by the provider schedule axis. The empty span contains
no bar, OHLCV, volume, indicator input, snapping target, or confirmed-series logical position.
New-entry admission uses current provider tail/session quality; it does not wait for historical
receipt coverage.

`ProviderBarSlotSequence` is the sole in-process contract for a chart or confirmed-context slot
axis. The full storage snapshot produces it with explicit `schedule_state`; snapshot assembly keeps
that exact typed value through analysis and wire serialization. A plain integer list, a missing
contract field, a non-authoritative context entry, or a sequence not exactly aligned with its bars
is a contract error, never an implicitly verified axis. When no typed axis exists, serialized bars
may receive local display coordinates only with `bar_slot_authoritative=false` and
`bar_slot_schedule_state=unknown`.

Durable drawing anchors identify confirmed bars by timestamp. Each snapshot deterministically maps
those timestamps onto the ordered confirmed-bar series used by the chart. Channels and drawing
geometry use that logical confirmed-bar distance; adding a newly confirmed bar recomputes the
mapping from the durable timestamps rather than preserving a guessed calendar slot. A future-only
point is stored as an explicit positive bar offset from a confirmed anchor. Long-lived drawings keep
their confirmed anchors and remain valid as the series grows; their age is not a validity bound.
Magnet snapping selects only confirmed authoritative bars. Schedule-ranked `bar_slot` and a
provider-calendar `future_axis` are not durable geometry or cross-window authority. An
authoritative `bar_slot` may determine transient candle screen spacing and expose missing provider
open slots, while drawing price slope and durable distance continue to use only the ordered
confirmed series.

The prospective chart axis is a transient time-only projection from the latest confirmed bar.
Within the bounded current/+7-day provider schedule it maps consecutive positive `bar_offset`
values to provider timestamps and omits closed intervals. It never serializes OHLCV, inserts a
placeholder candle, assigns authority to a live bar, or depends on the historical viewport's
schedule-ranked slots. An unavailable later schedule leaves only that timestamp projection unknown;
durable drawings continue to use their confirmed timestamp anchor and relative bar offset.

Drawing load validates every referenced confirmed timestamp against canonical storage and may attach
a non-durable `anchorProjection` containing the exact route/timeframe, canonical generation,
confirmed-series bounds and logical indices for off-window anchors. This projection avoids loading a
one-to-three-month bar window merely to render a current channel. The browser validates its scope and
generation, keeps server-owned anchor resolution separate from transient render readiness, and strips
both runtime facts before every save. Projection validation is an internal refresh concern, not a
channel lifecycle state. A normal canonical-generation mismatch leaves the channel active for
rendering, editing, hit-testing, and analysis while a bounded refresh runs. A refresh result applies
only when the durable drawing definition still has the exact signature captured when that check
started; an edit invalidates the stale result and schedules validation of the new definition.
Transport failure, timeout, or unresolved history is inconclusive and preserves the active channel.
Only a completed invalid result for the current definition removes its geometry and excludes it from
analysis.
Rendered line and channel paths sample logical prices at the actual screen x-coordinate of each
visible confirmed bar; a forming provider preview may retain display width but contributes no logical
distance. An unresolved or deleted anchor is never rescued by a schedule slot.
Legacy durable point fields such as `barSlot` and `future` are rejected at both browser and server
boundaries; runtime load/save never converts them into a relative anchor.

A provider-confirmed current-contract rollover for one futures-root `instrument_id` carries its
durable drawings forward atomically with the route transition. The route owner supplies typed old
and new fingerprints plus adapter-produced old/new session contract IDs; storage never parses or
reconstructs a fingerprint. Under the same provider/instrument lifecycle lock and canonical-writer
fence, storage rechecks the locked old current contract, rejects an absent or mismatched transition
and any unexpected pre-existing new-route drawing state, moves every old-route drawing payload to
the new fingerprint and provider contract ID across all timeframes, verifies readback, then publishes
the new current contract in that transaction. Drawing IDs, timestamps, geometry, and `updated_at`
are preserved. Drawing replacement takes the same lifecycle lock and rechecks the current futures
contract, so a stale old-route save either completes before rollover and is carried or fails after
rollover; it cannot strand a row. Concrete `FUT` instruments have distinct `instrument_id` values
and do not participate in this futures-root carry-forward. This uses existing schema-v18 rows and
requires no schema change.

#### DB-first snapshots and live delivery

Interactive chart reads are DB-first. The HTTP response returns the available canonical snapshot
without waiting for a provider. Candidate receipt dedupe and qualified bounded repair remain
separate background work. Normal tails and unknown session discontinuities do not become repair
demands. Unknown or incomplete coverage stays visible as an audit/repair diagnostic and does not
hide or globally block admitted confirmed history. Receipt absence by itself is not a suspected gap;
only a qualified typed gap, stale-tail/invalidation demand, broad missing prefix/empty history, or
explicit refresh starts provider work.

`ProviderAdapter.load_bars` and `async_load_bars` own one exact shared result contract:
`(list[Bar], str)`. The common provider boundary rejects non-`Bar` rows, a non-string warning,
source-timeframe disagreement, duplicate timestamps, and unordered rows before projection or cache
publication. Chart-history loads and pages retain immutable ordered `tuple[Bar, ...]` values under
their canonical generation. Gap and coverage consumers use those typed bars directly; an object
with a timestamp but missing `closed` is never interpreted as a confirmed bar, and malformed rows
are never skipped to manufacture a plausible partial series.

Canonical recent-bar reads use one equally strict storage contract. Every requested exact
`(instrument_id, source_timeframe)` scope is present in the returned mapping, including a genuine
empty series, and every value is an ordered `list[Bar]` containing only confirmed rows for that
timeframe. Limits are positive integers and are never coerced. Futures storage must return every
requested timeframe explicitly; a missing scope is an incompatible producer response, not an empty
history. Provider routing owns virtual-timeframe projection, then publishes the exact requested
scope mapping. The generation-fenced recent-bar cache, alert runtime, and screener consume that
mapping directly and never filter lookalikes, default a missing `closed` field, or rescue a missing
scope with an ad hoc empty list.

The absence of configured storage is the explicit `store is None` state. Once a store exists,
full and chart-only snapshots require its complete canonical bar, provider-slot, schedule,
coverage, and futures-roll read contract. A missing method is an incompatible runtime; it never
becomes an `unknown` schedule or silently removes roll events. One shared snapshot reader supplies
the same validated roll-event rows to full and chart-only rendering.

Chart windows are bounded and bucket-aligned before storage access. Oversized or unbounded requests
fail explicitly; rows are never silently tail-truncated because window identity, indicators, and
drawing geometry depend on complete scope.

Interactive backward navigation extends an already admitted chart through the DB-only
`/api/market/history-page` contract. One page is scoped by exact `instrument_id`,
`route_fingerprint`, timeframe, canonical generation, an exclusive `before_ts`, and a bounded
100–1000 row limit. Storage selects the immediately preceding rows with descending `LIMIT` and the
response restores ascending time order with typed `next_before_ts`, `has_more`, and
`canonical_generation`. A page never waits for or starts provider work, rebuilds analysis/guides,
or replaces the full market snapshot. Virtual `3m` pages read bounded canonical `1m` children and
apply the same exact-triplet projection as every other `3m` consumer.

One process-owned coordinator exists per exact
`(instrument_id, route_fingerprint, timeframe)`. Consumer viewport/range is a projection and does
not create another provider subscription. On native-stream registration, the consumer's exact
aware `since_ts` expands the bounded initial provider snapshot tail enough to bridge a recent
offline interval, capped by the shared recovery budget; it does not turn viewport depth into a
second broker-history request. When that exact client recovery cursor is older than the normal live
tail, the maintenance checkpoint additionally consumes it once as a bounded typed stale-tail repair
demand. That demand bypasses ambiguous-medium-gap inference and enters the same provider history
orchestrator outside the realtime lane. Provider-confirmed rows from either response enter the same
canonical commit lane. Its lanes are separate:

1. provider callback normalizes an immutable latest event;
2. provisional delivery publishes without waiting for SQL or analysis, carries the current parent
   canonical generation only as scope evidence, and remains lower priority than a canonical commit;
3. range-specific DB checkpoint and quality work runs outside the event loop; and
4. a serialized commit lane writes confirmed bars, verifies durability, then publishes the
   canonical event.

The chart stream also admits one typed presentation-only `secondary_candles` consumer profile for
the browser MTF Lens. It joins the same exact route/timeframe coordinator and physical provider
lease, but admits one fixed 300-bar profile so untrusted query values cannot multiply periodic
checkpoint work. Its checkpoint reads that bounded recent canonical tail through the
provider-qualified bar-read boundary. Each compact secondary recovery carries the complete ordered
timestamp membership of that bounded tail so the browser can reconcile canonical deletion and
aging without retransmitting unchanged OHLCV. The coordinator recomputes its requested live tail
from remaining consumers when one leaves. It neither expands a viewport history range nor starts
history repair, quality/coverage projection, future-axis work, analysis, or a paper-execution
snapshot. Coordinator frames carry the consumer role, exact identity, generation, and sequence so
rejection cannot consume transport order or mutate the primary chart. Early routing, disabled, and
sleep statuses for a valid secondary request carry its exact role and scope but are intentionally
unsequenced.
Chart and quote disconnect cleanup remains process-owned through its reconnect grace period and
physical producer stop. Cancellation of the WebSocket owner is exposed only after the registered
release task settles, so reconnect and shutdown cannot overlap old and replacement producers.

For virtual `3m`, the coordinator subscribes to that existing `1m` provider source. Every provider
update is aggregated in memory and published before any canonical commit or maintenance work;
confirmed source bars then enter only the `1m` commit lane. A canonical `1m` event invalidates the
matching `3m` bucket and starts a bounded DB checkpoint. No schedule, receipt, or storage read sits
between a provider live event and the first chart publication.

Lower-timeframe `confirmed_bar_context` requests reuse the same coordinator and physical provider
lease. They are activated only for Calc-enabled declared consumers and never create a second feeder,
browser socket, or paper snapshot. A newer matching canonical generation invalidates only the exact
wanted/cache scope. A changed server market version may retain the stable analysis key, but it
causally cancels the running older payload and only the current wanted version may publish;
superseded analysis cannot populate cache or authorize paper execution. A versioned analysis whose
inputs are bar-derived is event-driven and remains valid while its exact demand/version is current;
only declared live shared contexts retain polling, using the cadence of their canonical producer
(including the five-minute persisted GEX lane), never a universal analysis timer.

Every queued analysis payload captures the parent chart's exact canonical generation together with
the server market version and any declared lower-timeframe generations. The execution boundary
requires all captured generations before DB-consuming work and again after the persistent-worker
request settles; an active write retries after the bounded settle window, a changed lower-timeframe
context rearms its analysis-only payload, and a changed parent generation supersedes the stale market
version and requires a new chart registration. Research, paper admission, and cache publication
reuse the same fence.

An analysis snapshot carries both its parent canonical revision and the exact declared lower-context
revision map. Automatic paper entry validates that typed snapshot provenance against the queued
request, then acquires one process-owned publication guard for all of those canonical scopes before
checking the per-instrument enablement or touching the journal. The guard remains held through paper
order reservation and fill settlement. A canonical writer that meets an active authority-publication
guard retries before storage; an active writer or changed generation rejects the stale paper effect.
The guard is not a second generation or a durable authority, and non-authoritative research capture
does not hold it.

That exact chart-registration response may carry EMA/VWAP chart guides calculated for its canonical
bar generation before the heavier analysis result is ready. The browser admits those guides only
when route, timeframe, canonical revision, and required confirmed bar all match, projects them by
timestamp onto both the active chart and retained canonical live-tail axes, and renders them without
claiming heavy-analysis ownership. The analysis worker builds signal facts only: it neither
constructs a second chart projection nor returns `chart_guides`, and the browser discards any such
field before merging an analysis payload. `MarketAnalysisProcessRuntime` prestarts a bounded pool of
read-only analysis processes before scheduling provider background runtimes or opening provider
gRPC transports. `AEF_MARKET_ANALYSIS_WORKERS` selects 1..8 workers (default 1); the same owner supplies
the scheduler's concurrency limit. This does not multiply canonical database writers or provider
connections. Independent analysis identities may run concurrently, but each process consumes one
framed request at a time; routine analysis never creates a process after gRPC startup. Cancellation
physically settles the request before that worker can admit another, including repeated cancellation.
Shutdown closes admission before settling exchanges and processes. A timeout, protocol failure, or
worker exit publishes a non-retryable typed error, fences the whole pool until application restart,
and never starts a replacement process or a scheduler retry loop beside live provider threads.
`process_workers` diagnostics expose configured count, busy count and each PID/exit status; queue
wait and physical execution duration are measured separately. Other retryable analysis failures respect
the analysis refresh cadence rather than hot-looping. A forming provisional bar remains aligned on
those axes with null guide values until it becomes an admitted confirmed input.

Each analysis execution captures one timezone-aware `analysis_as_of_utc` at its outer boundary and
carries it unchanged through DB windowing, MTF admission, data quality, shared contexts, indicator
adapters, and the published snapshot metadata. Replay/backtest supply the signal-time clock; live
and fast lanes capture one clock per execution batch. Option Flow expiry/freshness and Option
Reversal quote admission consume only that input and never read wall time inside calculation. Direct
engine calls without an outer runtime derive a deterministic bar-end clock from their latest input.
HTTP market analysis, analysis-worker requests, and Telegram chart capture each supply one explicit
UTC clock at their outer live execution boundary and reuse it across generation retries.
Operational duration and publication timestamps remain diagnostics, not calculation authority.

Channel Master evaluates persisted channels on the parent timeframe's ordered confirmed-bar logical
indices. The analysis execution boundary owns drawing hydration: a queued analysis job reads and
normalizes anchors only when Channel Master actually runs, while a direct full-snapshot execution
reuses the same owner. Normalization binds each channel's transient projection to the exact route,
timeframe, and captured canonical generation. Chart-only responses never read drawings. A generation
change alone never blocks the channel: Channel Master rebases the last projection only when exact
timestamp overlap with the current confirmed series proves one consistent logical offset. A
structurally invalid projection or a definitive current-definition anchor failure excludes the
channel; an inconclusive refresh never revokes its prior validity. Schedule slots and coverage
receipts never substitute for or gate that evidence. A transient anchor-coverage payload
may remain visible for absence/audit diagnosis only. The typed output keeps the analyzed confirmed
bar timestamp separate from the decision-availability timestamp and carries one explicit relative
render offset from that confirmed anchor. Current arrows and channel labels use that causal render
anchor; they never center a current decision on an older visible portion of a long channel. The
separate one-minute interaction research shadow is fail-closed
until an exact parent-confirmed-index-to-confirmed-1m mapping has a canonical owner. It emits a typed
unavailable reason and no prospective research rows, while the deterministic parent-timeframe
channel remains active.

Autonomous prospective capture is one process-owned, research-only analysis demand configured by
exact durable `instrument_id`. It reuses the canonical chart coordinator for its retained parent
timeframe and every declared lower confirmed-bar context, refreshes at the bounded research cadence,
and submits only through the existing latest-wins journal worker. It never creates a parallel feed,
never depends on a browser lease, and its typed effect scope cannot evaluate or synchronize paper
trading. Route rollover replaces the exact internal lease through normal provider-qualified lookup.
Enabling this owner makes Option Reversal capture autonomous; it does not weaken Channel Master's
one-minute fail-closed mapping contract.

Browser analysis demand is an explicit per-page lease with a monotonic page-owned sequence. A
queued chart response attaches the page to one exact analysis key; visible pages renew it,
route/key replacement transfers it atomically, and `hidden`/`pagehide` releases its exact sequence
watermark immediately, including a registration whose chart request is still in flight. Late
registration or heartbeat at or below that watermark cannot restore demand, while a late release
from an older sequence cannot revoke the current lease. Price-driven refreshes of an already leased
analysis key update that exact demand directly; they do not reread chart history or rebuild a
chart-only snapshot. The server retains the canonical bar version, applies its bounded analysis
cadence, and the analysis job reads only its signal window when due; the chart checkpoint remains
the sole owner of viewport projection and full chart-axis work. Analysis consumes exact-range
coverage as an audit/repair diagnostic, never as generic admission. Generic indicators, candidates,
and decisions consume the exact ordered set of admitted provider-confirmed closed bars available in
their signal and declared context windows. Only a module missing its actual declared input becomes
typed unavailable; unrelated modules continue. Provider session and current-tail quality may block
a new entry after calculation while market-closed calculations still publish. Reduce-only exits are
independent of analysis and historical coverage. Provider adapters qualify the exact route
once per bounded request rather than once per expected slot. A missing or mismatched lease falls
back to a normal chart registration. The server expires abandoned leases after a bounded heartbeat
grace period and cancels work when the last consumer leaves. Channel Master declares no one-minute
confirmed-bar context and creates no retained research demand while its one-minute shadow is
fail-closed. Lower-timeframe demand is derived only from currently leased consumers that declare an
active context. Bursts of matching canonical-generation events
invalidate immediately but start only the latest replacement calculation after one short settle
window. The outer snapshot assembly performs JSON-native normalization once after all chart and
indicator projection is complete; direct engine-analysis callers retain the same finalized output
contract without an intermediate assembly pass. Synchronous and asynchronous snapshot entrypoints
share one post-prefetch assembly owner; they differ only in how required reads are scheduled. No
transport-only marker changes the domain snapshot shape.

Analysis cache compaction owns one exact `AnalysisBarWindow` derived from the complete ordered list
of serialized provider-confirmed bars before that list is removed from the compact snapshot. The
window supplies count, first/latest timestamp, latest close/volume, and a content hash over exact
bar facts, source, symbol, and timeframe. Market-version scheduling and durable paper idempotency
consume that same owner; neither filters malformed rows nor reconstructs the window from display
placeholders or object attributes. A compact analysis snapshot always carries the window hash,
count, and timestamps, including an explicit empty window. Paper execution requires the non-empty
typed metadata and never falls back to an embedded bar or runtime cache lookup.

`run_market_analysis_job` is the sole analysis-cache record producer. A ready record contains one
immutable JSON `bytes` snapshot and its exact byte count; response projection and diagnostics decode
that representation through the same strict reader. Mutable snapshot dictionaries, size-untracked
cache entries, and route/timeframe scans for a guessed latest analysis snapshot are not supported.

Per-indicator timings already belong to `engine.indicator_pipeline` and are exposed as
`meta.indicator_timings_ms` only for `/api/market?debug=true`. Normal analysis does not collect this
diagnostic payload; use one fresh debug request with the active Calc set before introducing another
profiler or classifying an indicator as heavy. Non-authoritative research journals run through one
bounded latest-wins worker at a lower cadence and never delay snapshot publication, signal choice,
paper synchronization, tick ingestion, or WebSocket delivery.

#### History repair

A concrete qualified internal gap, current-tail defect, invalidated provider timestamp, broad missing
prefix, empty-history demand, or explicit refresh may create one typed `HistoryRepairIntent`. The
intent carries one provider-bounded chunk, its immutable full exact route-qualified target envelope,
and request/admission identity. Unknown receipt coverage by itself is only an audit diagnostic and
creates no steady-state fetch loop. A normal chart load performs no unconditional receipt-union read;
when a candidate exists, one indexed candidate-envelope receipt lookup deduplicates already terminal
mirror ranges. Repair admission never initializes or queries storage from a WebSocket/event-loop path;
realtime paths only enqueue bounded work.

Chart history metadata separates `bar_repair` from `absence_verification`. The first reports whether
the current provider-bounded returned-bar fetch path is supported and its finite admission status,
code, and target range. The second reports whether the adapter may prove omitted membership and
reconcile an exact snapshot. A new canonical generation never inherits an older failed terminal
result. Mutating completion additionally reaches clients through the canonical
generation/checkpoint event.

Coinbase and T-Invest both own bounded provider-mirror snapshot authority: a terminal successful,
unsaturated response, including an empty response, may close its exact requested chunk, reconcile
omitted local rows, and commit a receipt. This is explicitly the product's tolerance of agreement with
that provider database, not a claim that the API supplies a philosophical proof of exchange history.
An IBKR `CONTFUT` root uses the distinct typed `rolling_end_now` returned-bar repair described below;
it never claims exact-range absence authority. A caller that explicitly requires an arbitrary
historically anchored `CONTFUT` snapshot still receives
`IBKR_HISTORY_FUTURE_ROOT_EXACT_RANGE_UNSUPPORTED`. Repair admission is not itself a terminal
provider outcome: the orchestrator retains a bounded intent/generation terminal outcome, and the
next matching chart or screener payload reports its status, code, chunk, full target, and remaining
range. The remaining range includes the current chunk until both provider completion and canonical
commit succeed; a failed or partial chunk never advances that boundary. An exhausted target is
serialized with null remaining bounds, not an empty interval. Actual rolling response bounds survive into the
terminal diagnostic. Committing admitted bars from a partial response preserves `incomplete`, its
error, and mutation counts; it cannot report the repair as successful or continue to older chunks.

The process-owned repair orchestrator provides exact deduplication, one active lane per provider,
foreground-before-background priority, bounded queues/envelopes, cooldown, and failure backoff. One
logical large demand starts at the newest adapter-bounded chunk and automatically continues older
after each real bar mutation, deletion, or first-time receipt expansion, including an empty mirror
receipt, until the target start is reached. A replay/no-change, transport, authentication, malformed,
saturated, explicit partial, or finality-unknown result stops continuation with its typed terminal
outcome. A queued intent revalidates its captured route and canonical generation before provider I/O.

After provider I/O, repair revalidates the captured route and generation again. Confirmed bars and
their exact coverage receipt commit under the same captured-generation transaction boundary; a race
rolls back both rather than publishing stale bars or coverage. For an exact-snapshot provider that
transaction also removes prior in-range bars absent from the response and verifies the complete
ordered response by readback. A complete empty snapshot may therefore delete stale in-range bars
before committing its receipt. Non-snapshot providers never reconcile absence.
Caller cancellation remains deferred through the canonical commit handle: the repair lane does not
report cancellation or release ownership while an admitted storage transaction can still finish.

Coverage continuation cursors belong to the canonical chart-event generation and immutable target
envelope; there is no separate coverage generation. Each advancing chunk carries forward the
resulting canonical generation and never resets the target's absolute budget. Exact replay without
union expansion is no mutation, emits no event, and stops that envelope until a fresh typed demand.
A stale plan returns typed `stale_snapshot` and likewise waits for a fresh demand.

History maintenance is watchlist-demand-driven and range-based. It refreshes futures lifecycle and
current session state independently. When typed repair demand exists, eligible receipts deduplicate
already mirrored candidate subranges and adjacent work stays provider-bounded; receipt absence alone
schedules nothing. Deep history remains an explicit foreground operation. Exact budgets and
provider-safe windows are code-owned and surfaced through health.

A successful IBKR self-heal reconnect creates one explicit, one-day-bounded recovery-tail demand for
each IBKR watchlist route on the actively demanded chart intervals. Verified provider schedule facts
shrink that envelope to elapsed expected-open slots after the latest confirmed bar; a verified empty
open-slot set schedules nothing, while unknown schedule coverage retains the single bounded explicit
refresh. Without an active chart interval, reconnect never invents a default history demand. Self-heal
reports success only when every required tail is typed not-needed, admitted/running/queued, or already
terminally committed/no-change; unavailable, rejected, throttled, saturated, malformed, and failed
tail outcomes keep the composite result visibly failed. This is a reconnect-generation demand, not
generic permission to classify normal closed tails as gaps.

#### Canonical commit and cache coherence

Canonical history-state writes publish `ChartBarsUpdatedEvent` only after a confirmed-bar mutation
or eligible coverage-union expansion commits. The shared per-route/timeframe generation is also the
confirmed-history cache coherence token; coverage has no second generation. Exact receipt replay is
acknowledged without mutation, generation advance, or event. Checkpoints carry the generation under
which they read and reload when a commit races them.
Coalesced chart-history loads and their cache hits wait asynchronously for the exact writer to settle
within one absolute request deadline. A retry retains that deadline and uses the generation returned
by the stable read. Checkpoint load and publication conflicts share one transient classification and defer further
work to the maintenance cadence or a new canonical event; storage failures remain visible degradation.

Every submitted logical batch owns one monotonic `revision_sequence` and one outer transaction.
Set-based upsert, exact snapshot-range reconciliation when authorized, and ordered readback complete
inside that transaction; internal bounded write stages are never independent commits. Identical rows
are acknowledged, a newer conflicting row is superseded, and missing outcomes remain pending. A
partial retry retains the same sequence; SQL row count alone is not durability evidence. Insert,
replacement, deletion, or eligible coverage-union expansion advances the shared generation once.

The supported deployment has exactly one MartinCall application process per PostgreSQL database;
its process-owned `ChartCommitRuntime` is the sole chart/history writer and revision-order authority.
Before schema bootstrap/verification, route refresh, or any background writer starts, the
application acquires one exclusive PostgreSQL session advisory lease on a dedicated, non-pooled
storage connection. Busy lease blocks startup; failed schema initialization releases an acquired
lease. Cancellation during physical lease acquisition, schema initialization, route refresh, runtime
startup, or runtime drain is deferred until that operation settles. Shutdown explicitly releases the
lease only after runtime drain succeeds; an undrained or failed stop retains the lease until storage or
process close.

Canonical and futures route-authority mutations use a two-key advisory protocol without changing
schema v18. The elected leader session holds exclusive lock `L`. Every canonical transaction first
takes transaction-scoped shared fence `F`, then reverifies `L` before its first mutation. A candidate
leader takes `L`, tries exclusive `F` as a barrier, and releases `F` immediately; an older active
transaction makes startup fail fast, while an older transaction that races after the barrier fails
its post-fence `L` verification and rolls back. Lease loss is latched for the process: later writes
fail closed, readiness becomes false even when diagnostics were cached, and operator restart is
required.

Production PostgreSQL stores require that lease by default. A read-only process must opt into the
explicit read-only capability and cannot own schema bootstrap; only tests may opt into the explicit
test bypass. The standalone IBKR tick collector and current-futures refresh are exclusive offline
writers: each acquires the same lease before schema access or provider I/O and
holds it until provider disconnect/runtime drain and physical storage completion, then releases it
and closes storage. An exact-contract PostgreSQL advisory transaction lock is additionally acquired
before snapshot upsert or reconciliation so one
contract transaction cannot interleave another. Multiple Uvicorn workers, application replicas, or
independent canonical-history writers are unsupported: schema v18 has no durable per-snapshot
high-water that could stop an older out-of-process reservation from resurrecting rows after a newer
absence reconciliation.

A logical timeout never abandons physical storage work. Chart commits, quote snapshots, and tick
writes admit a successor only after the current physical attempt settles. Slow consumers use
bounded queues, stream generation/sequence, canonical revision, and send acknowledgement; loss
forces a checkpoint rather than guessed state.

### Quotes and execution snapshots

```text
qualified watchlist routes
  -> immutable QuoteRouteSnapshot
  -> provider-owned stream or poll
  -> exact-route quote cache
  -> sequenced snapshot / delta / heartbeat
  -> screener, chart preview, alerts, paper, option consumers
```

The PostgreSQL watchlist snapshot publishes one immutable route model keyed by exact
`instrument_id`, with watchlist and route generations. Quote hot paths perform O(1) in-memory
lookups; they never initialize storage or query PostgreSQL per row.
Quote producers retain their selected instruments and qualified routes for that immutable snapshot
version, revalidating selection before processing a changed version. Trade-preview polls reuse those
routes without repeating qualification. An ordered price-alert event first checks its exact armed
registry scope, then reads only its instrument entry from the same snapshot without copying the
watchlist or requalifying unchanged identity.

IBKR quotes have one async subscription owner and one qualified cache. Coinbase and T-Invest use
their adapter-owned async polling runtimes. One runtime lives for one orchestrator lifetime and
settles before shutdown. The orchestrator is the sole last-quote cache for these pollers: adapters
return only the current provider attempt and never replay a second TTL snapshot. Coinbase keeps a
bounded set of blocking HTTP calls outside the event loop and physically settles them before
cancellation completes; it creates no nested per-poll executor. T-Invest keeps one SDK
`AsyncClient`/gRPC channel on the application event loop, admits only one bounded exact-UID
`GetLastPrices` RPC at a time, and reuses that transport after success. Timeout, cancellation, or
transport failure drains the RPC, closes the channel, and requires a fresh channel on the next
circuit-breaker-admitted attempt; no polling call creates a nested event loop.
Provider callbacks stamp factual receipt time at the observation boundary. Replaying an unchanged
cached response does not manufacture a newer provider timestamp; metadata-only changes do not
refresh price age.

The 300 ms publication loop retains qualified routes and subscription keys from that same immutable
route snapshot. IBKR's existing snapshot sequence is checked under its owner lock before copying:
an unchanged `QuoteSnapshotRead` carries no rows. The reader resets its cursor when its exact route
selection changes and acknowledges it only after publication succeeds. A changed read publishes only
changed exact-route rows; the retained provider snapshot also supplies the separate persistence pass.
`QuoteStreamRuntime` owns partial-row publication and the next categorical freshness deadline for
each cached row. Successful unchanged reads do not repeat envelopes or renew observation timestamps;
deadline expiry, clock regression, and warning changes still advance semantic cache revisions when
needed. Selected-price, last-trade, and bid/ask freshness remain independent. Route cutover prunes
both cached rows and their deadlines. Provider poll cadence, persistence cadence, the separate 50 ms
ordered trade-preview path, and native live-bar ingestion are unchanged.

Each process owns a quote `cache_epoch` and monotonic `cache_generation`. HTTP screener and
WebSocket frames carry that revision. A socket sends one full authoritative snapshot before any
delta, then full dirty-row deltas and compact heartbeats under one sequence. Identity, epoch, or
sequence violation requires full resync.

`ui/quote_stream_contract.py` owns the exact full-row field grammar and server projection. The asset
assembler generates the same immutable manifest into the page bundle and SharedWorker bundle;
neither JavaScript consumer carries a parallel field/status/entitlement registry or fallback.

Publishing a new route generation atomically prunes obsolete demand and cache state. A stale
in-flight generation cannot repopulate the cache or publish after that cutover.
Producer release closes quote demand only after every already-started screener, option-target,
price-alert, and row-projection task has physically settled; cancelling an asyncio waiter never
abandons its database thread or permits an overlapping successor.

Browser windows multiplex the exact canonical route set through one required same-origin
`SharedWorker` socket. The worker materializes current transport state for late joiners but owns no
persistence, routing, calculation, chart/GEX stream, or execution authority. Missing browser
capability, worker asset, or worker startup fails the quote transport closed with a typed visible
error; a page never opens a parallel direct quote socket.

The browser consumes watchlist quotes only through this sequenced WebSocket/screener projection.
There is no parallel per-instrument HTTP quote endpoint or transient demand owner.

Quote envelopes carry provider/receipt timestamps, source, entitlement, delayed/stale status, and
route identity. Stale, delayed, timestamp-unknown, route-ambiguous, or loss-affected quotes may
remain visual context but cannot authorize execution or server alerts.

The provider projection owns one selected numeric `price` and its exact `price_source`: `last`
matches the separate `last` fact and uses provider-event time, while `bid_ask_mid` matches the exact
non-crossed `bid`/`ask` midpoint and uses client-receipt time. There is no `trade_price` alias,
consumer-side source reselection, numeric-text coercion, or timestamp substitution. Provider trade
preview events likewise require the adapter-stamped `gateway_ts`; the trade-event clock never stands
in for receipt time.

GEX and Option Target read the same canonical underlying quote cache. They do not open another
underlying subscription. Trend rows read only persisted `quote_snapshots` through their separate
coalesced endpoint and never enter the quote WebSocket projection.

Dynamic EMA/VSA alerts and the Fast indicator lane share one bounded exact-route recent-confirmed-
bar projection. It reads PostgreSQL only on first demand, a larger bounded limit, or a canonical
bar-generation advance; generation fences reject a read that overlaps a writer. The alert monitor's
half-second quote evaluation owns no second per-tick bar cache and never rereads an unchanged
confirmed tail. VSA's verified provider-slot projection is retained under that same generation and
is resolved again only after the confirmed tail advances or expands. EMA feature context and VSA
calculation output are likewise coalesced by exact route, timeframe, canonical bar generation, and
typed indicator parameters; each quote performs only the alert-specific touch/event projection.

Bar-derived server alerts retain that projection's exact canonical generation through calculation
and acquire the same short authority-publication guard before changing alert state or its delivery
outbox. A generation advance or active writer discards the stale evaluation and waits for the next
tick. Alert dispatch is exact by typed kind: `vsa_fuel` and `ema233_touch` never fall through to the
generic `price` crossing path when their canonical context is unavailable or malformed.
The VSA alert adapter consumes only the VSA owner's exact-version, ordered `events` projection and
its canonical `ts`; `latest` is presentation state, not a second event source, and no `timestamp`
alias or malformed-result-to-empty conversion exists.
The evaluator commits one generation-CAS transition per observation: an ordinary state advance and
an optional fired state plus durable delivery intent are never split across transactions. Durable
`wasTouching` owns EMA edge admission; a transient price-cross baseline advances only after a fired
transition commits, so retrying the same ordered quote cannot consume a failed crossing.

One committed Option Point sample batch stages every affected Fast scope before changing shared
state, revalidates every exact source-bar generation, and publishes all scopes atomically. Each Fast
scope carries its exact `bar_timeframe` and `bar_canonical_generation` beside the Option Point
timeframe and scope revision. The quote producer rechecks that source generation on every bounded
projection poll. A generation advance, active writer, malformed scope, or failed Fast projection
publishes an empty authoritative Fast snapshot and removes the prior browser projection; it never
retains an advisory result calculated from an older confirmed-bar source while waiting for the next
sample.

`OptionTargetRuntime` owns the server-side premium-cap projection. It reads the typed server setting
once on first demand, returns isolated snapshots to the two-second reprice loop, and serializes each
operator update as one revision-fenced durable `persist -> publish` transition. Watchlist retirement
returns its typed post-mutation setting and revision from the same fenced transaction, then reconciles
the exact-key owner without a second storage window; a late older completion cannot restore a removed
instrument. The reprice consumer never polls the server settings table, and the options settings API
cannot write around the runtime owner.

`GexSchedulerSettingsRuntime` likewise owns the typed scheduler setting. A successful server-scope
read is retained for scheduler ticks and status requests; a failed read is not cached and each later
bounded tick may retry while the scheduler remains disabled. The update API serializes durable
persistence and revision-fenced runtime publication through this owner instead of writing around it.
Watchlist retirement reconciles this exact-key owner from that same typed transaction result.

Host-sleep detection updates its monitor state and persists the forensic gap record from a worker
thread. Resume handling never performs synchronous PostgreSQL work on the application event loop.

Interactive Telegram settings and the last backend-restart record also have one process-local
owner each. A successful first read is retained for system-status polling; a failed read stays
unavailable and may be retried. Telegram updates serialize durable persistence, runtime publication,
and the bot lifecycle transition, while a successful restart-record write publishes the exact
durable record. These owners, the GEX scheduler, option-target caps, and route-qualified dividend
metadata use only exact-key reads; there is no full-scope settings fallback. The 30-second browser
status heartbeat never scans the server settings scope.

Durable quote and GEX snapshots are bounded evidence, not an archive. Admission accepts only aware
timestamps from the trailing 48-hour window through five seconds ahead of the capture clock. An
hourly background owner drops fully expired quote hypertable chunks first, then deletes bounded
residual old/future quote rows and GEX rows. Realtime handlers only enqueue persistence; retention
never runs on the provider callback or WebSocket delivery path.

GEX projects the freshest exact underlying observation from that cache: a non-crossed Bid/Ask uses
its factual receipt time, while Last uses its provider event time. A missing, display-only, unknown-
entitlement, or already-stale observation blocks the request before option subscriptions open. If
the frozen request observation crosses its 35-second freshness bound during an otherwise complete collection,
the partial attempt completes that scheduler slot without repeating the option-chain pass; the
route's next staggered five-minute slot evaluates a new canonical quote snapshot.

The route-qualified GEX producer coalesces live calculation into one canonical five-second frame
and continuously keeps only that latest frame. Five-minute historical persistence remains a
separate cadence. Browser transport sends an initial full frame, then exact material changes at a
bounded publication cadence; acquisition-
only timestamps and sequence counters do not create a material revision. Unchanged periods use a
compact heartbeat, while status, authority recovery, and history revision messages remain immediate.

### Signals and decisions

```text
confirmed bars + typed optional context
  -> registered indicator calculations
  -> DomainFact + SignalCandidate
  -> choose_decision() in engine/decision.py
  -> build_trade_setup_card() in engine/trade_setup.py
  -> UI / alerts / paper / backtest / execution intent
```

`DomainFact`, `SignalCandidate`, `ScenarioDecision`, and action/Trade Setup contracts use canonical
codes and typed attributes. Numeric scores, levels, confidence, and prices are finite before ranking.
Inputs and nested fact payloads are copied/frozen at domain boundaries so later mutation cannot
change a decision. The shared domain-value traversal validates and freezes in one pass;
wire projection still returns detached mutable JSON containers. Already immutable nested
`DomainFact` values may share their frozen attributes, never caller-owned mutable containers.
Validation-only consumers use the same domain admission without constructing a discarded
`DomainFact` or wire copy. Validation alone does not transfer ownership or freeze mutable input;
consumers that retain facts still use the immutable domain boundary.

Every `SignalCandidate` carries typed `CandidateFinality`. Only `confirmed` is decision-authoritative;
`provisional`, `retained`, and `expired` candidates remain display/audit context and are rejected by
the central decision boundary even if a package promoter emits them. Display payload flags and event
collection membership never recover decision authority.

Consumers branch on typed `source`, `role`, `kind`, `direction`, setup, trigger, evidence, risk,
quality, and plan fields. Candidate names, tooltips, labels, and translated text are not domain
inputs. The shared renderer is the only owner of screen-formatted prose.

Trade Setup copies the weighted winner's `ScenarioDecision.source` into its required top-level
`signal_source`. Execution intent, paper attribution, and backtest grouping consume only that field;
`confluence` remains typed supporting evidence and never selects or recovers execution attribution.
Executable plan rounding, tick-denominated stop floors, and Trade Setup price normalization consume
the exact route `price_increment`. Name-based `InstrumentProfile.tick_size` remains an analysis
tolerance only. Missing exact price-increment authority blocks an otherwise actionable decision and
Trade Setup instead of substituting a symbol/profile constant.

An option-flow veto yields a neutral `WAIT` decision without executable levels. Candidate ranking
and veto metrics remain diagnostic only. Structural invalidation is carried separately from the
execution stop through candidate promotion, decision, and Trade Setup. Shared plan normalization
may anchor the stop to the signal bar, but never moves a supplied target to repair geometry or meet
minimum reward/risk; those failures make the plan non-executable. Only the scenario producer owns
target selection, including any explicit R-based policy.
Execution-plan rejection returns a `DomainFact` with a canonical code and numeric attributes;
the shared renderer owns its human-readable reason, and decision consumers inspect no prose.

`latest.signal` is the sole current indicator signal state. A plan may expose plan facts but never a
second signal copy. Lifecycle, candidate promotion, runtime cards, and sentiment consume that one
typed object; sentiment deduplicates an indicator and its promoted candidate by `SignalCandidate.source`.
An active lifecycle or sentiment contributor requires its finite numeric score from that typed
owner. Consumers never recover it from `latest.score`, numeric text, or a neutral default. Missing
or malformed contributor facts are excluded; a sentiment calculation with no admitted weighted
contributors has the explicit aggregate result `50/flat`.
Market-context sentiment reads only the producer's top-level `market_context`; table and latest-row
projections never recover absent or malformed context.
Sentiment contributors carry a canonical `ActionPhase`, a semantic role, and any typed context
phase. The shared browser renderer derives status labels; formatted action text and contributor
labels are not engine output.

The operator-selected signal calculation and chart-mark horizon is one of one, two, three, or seven
days, with two days as the default when the setting is absent. A present invalid value is rejected.
It is independent of the chart's loaded range: unavailable or short signal-range history is passed
to the declared indicator minimum-input contracts and is never replaced with chart-range bars.

### Realtime streams

```text
provider event / canonical event / DB notification
  -> lightweight exact-route owner
  -> typed sequenced payload
  -> browser projection
```

Realtime owners do not perform blocking repair, expensive analysis, schema work, or synchronous
storage initialization/reads. Provisional state remains separate from confirmed truth.

WebSocket contracts use explicit identity plus source/status/lifecycle fields. Full snapshots
establish authority; deltas require a known snapshot and monotonic sequence. Ring-buffer loss,
generation mismatch, malformed identity, or stale route causes resync instead of heuristic merge.
The browser validates the complete frame, including bar geometry and lifecycle, before advancing its
generation or sequence cursor; a rejected frame cannot consume transport order.

`chart_stream_messages` owns the server-side chart frame contract. It admits an exact list of real
serialized bars for the declared timeframe, exact non-negative integer stream/canonical revisions,
and a fully typed future-axis projection. It rejects placeholders, malformed rows, route-identity
disagreement, unordered future slots, and coercible generation values before queueing or network
send. The coordinator owns the sole per-timestamp realtime precedence rule, while its delivery
tracker records only a frame that has passed that producer contract and has been sent. Neither the
coordinator, queue compactor, WebSocket boundary, nor tracker filters malformed rows or substitutes
an empty list. There is no separate batch merge API or alternate precedence implementation.

Serialization uses bounded hot and bulk encoder lanes and UTF-8 text frames. Encode queueing and
network send share a deadline; a slow client is disconnected rather than holding a realtime lane.

## Storage and settings

### Storage lifecycle

The application accepts canonical schema v18 only. The schema owner may create it inside one
transaction only when the active application schema is clean. An existing schema must expose the
exact `(18,)` marker and pass the bounded current-contract verifier. Older, unversioned, multi-row,
drifted, or incompatible state blocks readiness; startup performs no DDL, migration, reinterpretation,
or repair.

`store is None` is the only absent-storage state. Every non-`None` runtime store implements the
complete canonical schema-v18 repository contract, including writer-lease status. Domain consumers
never capability-probe it: missing methods are an incompatible runtime, not empty drawings, absent
option facts, unknown provider schedule, unavailable history, or permission to bypass readiness.

The verifier checks the explicitly owned critical contract, not whole-catalog equivalence. A
failure requires operator action; it does not authorize startup repair.

Candidate code may run the same exact verifier through the read-only `storage-preflight` operator
gate before an upgrade stops the current application. The preflight is a non-schema owner, takes a
shared schema lock, validates durable settings and drawing payloads as well as schema structure, and
performs no bootstrap, repair, conversion, or mutation. Failure requires an explicit offline
cutover or recovery decision; it never activates a compatibility reader.

An intentional storage change increments the version and uses one separately reviewed converter
for the exact preceding version. Every application and TimescaleDB writer is stopped, a whole-
database backup is restored successfully under matching versions, and the converter validates its
source, preserves data, transforms and advances the marker atomically, then validates the target.
The converter is deleted after validated cutover. Runtime and permanent scripts never retain a
migration chain or compatibility reader.

Active `ProviderManifest.db_providers` values generate the schema v18 storage provider constraint.
Adding or removing one is a schema change.

### Core relations

| Relation | Contract |
|---|---|
| `storage_schema_versions` | exact current version marker |
| `instruments` | provider-qualified rows keyed by `instrument_id`; current watchlist membership/order |
| `watchlist_state` | singleton optimistic-concurrency version |
| `bars` | ordinary confirmed OHLCV by exact route/timeframe/timestamp |
| `futures_contracts` | provider-observed concrete/current futures lifecycle |
| `futures_contract_bars` | explicitly addressed concrete futures bars |
| `futures_continuous_bars` | provider-native stable-root continuous history |
| `futures_roll_events` | provider-observed route-qualified roll transitions |
| `trading_schedule_snapshots` | immutable provider session authority |
| `trading_session_intervals` | replaceable materialization from schedule snapshots |
| `trading_hours` | latest schedule read model, never historical authority |
| `quote_snapshots` | route-qualified durable quote history |
| `history_coverage` | idempotent complete exact-chunk receipts; authority is the eligible range union |
| `ticks` and rollups | route-qualified tick ingest and derived aggregates |
| `gex_snapshots` | request/live GEX history under exact source and route |
| `option_targets` | exact Option Point intent and market sample |
| `settings`, `settings_state` | closed settings scopes, ordering, and transaction revision |
| paper journal tables | contract-scoped orders, positions, fills, and typed trade events |
| `price_alerts`, `drawings` | retained armed/fired exact-route price alerts and durable drawing objects |
| `discord_signal_messages` | typed Discord advisory journal/tombstones |

Route-sensitive rows store both required `instrument_id` and the additional
`route_fingerprint` generation dimension. Provider keys are canonical manifest values; feed kind
belongs in typed event/source fields.

### Settings

`src/aef_terminal/settings_contract.py` is the sole definition owner for durable `client`, `server`,
and `watchlist_presentation` scopes, accepted keys, and value shapes. Repositories, APIs, schema
verification, and browser helpers enforce or narrow that contract; none owns a second key list,
converter, or compatibility reader. Its stable `# section:` markers provide task-scoped navigation
without splitting the canonical contract into competing owners.

Schema v18 scopes indicator settings only as global or exact instrument state. Instrument rows use
the canonical `aef:instrument:[instrument_id,mode]:indicator:key` grammar; symbol-addressed
`aef:SYMBOL:indicator:mode:key` rows are not current settings and block runtime verification. The
offline v17→v18 cutover requires an explicit operator-supplied symbol/mode-to-`instrument_id` map;
runtime never resolves, copies, aliases, or repairs those retired rows.

Each loaded page owns a fresh UUIDv4 writer. Before optimistic publication it synchronously
allocates and persists the per-key `(changed_at_ms, writer_id, sequence)` order. PostgreSQL accepts
only a lexicographically newer tuple and returns the authoritative row as `applied`, exact
`replayed`, or newer-state `rejected`. Retries reuse the tuple; replay creates no mutation or
revision; rejected optimistic state is reconciled and never called saved. Browser Web Locks are not
part of the contract.

`settings_state.revision` advances once per real transaction mutation/deletion and never for replay.
Snapshots, acknowledgements, and typed broadcasts carry it. Stale revisions cannot delete state;
at the same revision, previously learned row content and presence must match exactly before browser
state changes. PostgreSQL is authority; high-water tickets and BroadcastChannel messages are
transient coordination only.

`ui/assets/js/07-settings-reducer.js` owns pure mutation normalization, tuple comparison, and
reconciliation plans. `08-server-storage.js` owns writer allocation, optimistic queues, durable
persistence, BroadcastChannel transport, and application/session-storage side effects. Keep the
ordering and selection rules in the reducer and the effects in the storage owner.
`20-storage-transport.js` admits and fetches exact-scope snapshots; `20-settings-sync.js` owns boot
hydration, authoritative snapshot reconciliation, and projection into workspace, runtime, layout,
and visual state; `20-storage-sync.js` orchestrates route storage and shared drawing/alert state.

The asset assembler projects the exact browser key grammar from that owner; browser snapshot,
broadcast, and optimistic-write admission consume the generated manifest and never infer settings
membership from a namespace prefix. Browser and server consumers must use this one contract rather
than maintain a second ownership matrix or settings vocabulary. Instrument membership is checked
once at the repository's locked mutation boundary; API preflight validates structure only and never
performs a second watchlist read before or after commit. A missing durable operational setting may
use its declared environment default, but a failed server-scope read is not equivalent to absence:
runtime settings
report `unavailable` and disable the affected background or interactive producer until PostgreSQL
authority can be read again.

The application reads the complete client scope once after acquiring and verifying the canonical
writer lease, then publishes one process-local runtime projection before any background consumer
starts. Successful browser settings transactions publish their authoritative returned values and
canonical per-key order plus `settings_state.revision` into that projection; an older completion
cannot overwrite a newer runtime value. Direct server-owned client-setting writes publish the exact
committed value and reset browser ordering under that same revision fence. Watchlist retirement
returns one full authoritative client snapshot, including deleted keys and mutation orders, from its
atomic durable transaction and reconciles that exact result without a post-commit storage read.
Retry returns the current exact snapshot again if commit completed before publication. Background
consumers read only this non-blocking projection and
never poll PostgreSQL for client settings. An unavailable or uninitialized projection fails closed
rather than falling back to a fresh consumer-owned database read.

One low-frequency route-independent full settings snapshot closes the late-`pagehide` keepalive
acknowledgement window. The browser validates its revision and mutation watermark before reconciling
or broadcasting any state learned from it.

### Tick ingestion

Tick-context and volume-profile price buckets default to the selected route's exact
`price_increment`. An explicit wider `price_step` must be a finite positive exact multiple of that
increment. Missing route authority or a misaligned requested step fails with a typed response; the
query never falls back to an instrument profile or silently clamps the request.

`TickLiveConnectorService.set_enabled` is the single owner of the durable tick-live intent and its
physical subscription transition. Enable starts the exact route and then persists that intent,
rolling the feed back on persistence failure. Disable persists the off intent before stopping the
feed, so a failed write cannot leave a stale enabled intent that the restore loop would restart.
Caller cancellation is deferred until each compound transition and physical thread cleanup reaches
a terminal state. Application shutdown stops only the physical subscription and intentionally keeps
the persisted intent for the next process start. A shutdown acknowledgement that still reports a
live tick thread fails closed and prevents canonical writer-lease release.

Every provider trade packet is validated before entering the hot FIFO: aware UTC timestamp, finite
price and optional bid/ask, exact positive integer volume, typed aggressor delta, and immutable
conditions. Zero or negative futures prices remain valid; invalid facts are rejected rather than
clamped. One malformed packet is counted with a stable typed admission code and does not discard the
remaining callback batch.

The IBKR callback boundary converts its unordered pending-ticker set into one explicit stable order:
provider timestamp, exact `(instrument_id, route_fingerprint)`, packet kind (BidAsk before trade at
the same timestamp), subscription sequence, then packet sequence. The latest BidAsk state never
regresses in provider time, and a trade never consumes a future BidAsk observation. This makes delta
classification reproducible without blocking or performing storage work in the callback.

`TickBulkAggregator` owns one explicit-capacity FIFO and bounded background micro-batch flush.
Container behavior never silently evicts a tick. Cross-thread producers wake the owner loop through
`call_soon_threadsafe`; a lock does not make an `asyncio.Event` cross-thread safe. Shutdown stops
admission, drains every successful micro-batch without the realtime wake budget, and records any
unwritable remainder as typed route-qualified loss before releasing the writer.

Tick writers and retention calls require an explicit canonical provider; there is no default
provider. Each durable batch is one atomic PostgreSQL `COPY`. Only exact submitted cardinality is
success. A definitely aborted retryable write may return to the FIFO; an unknown commit outcome is
not retried because the append table intentionally has no deduplication key. Unknown outcomes publish typed
loss and block completeness. One retention transaction aggregates and deletes the oldest complete
route-qualified five-minute bucket; it never cuts the one-minute delta or five-minute profile
read boundary, and the incomplete cutoff bucket remains raw. Raw `ticks` remain a rowstore
hypertable: a columnstore/compression policy or compressed
tick chunk is contract drift because retention must never perform row-level deletion from compressed
data. The exact staged purge analyzes its temp batch before joining it back to the hypertable;
purge failure is typed operational degradation, not tick loss. Durable rollups own the retained
compact history. After the exact cutoff contains no raw rows, retention drops the now-empty,
fully expired Timescale chunks so table metadata and disk allocation do not grow indefinitely.

## Economic-event calendar

`data/economic_calendar.py` owns the global, read-only economic-event contract. It combines the
official FRED release calendar for a curated set of high- and medium-impact US macro releases, the
Federal Reserve monthly calendar for FOMC meetings, Beige Book, testimony, and speeches, and the
BEA machine-readable release schedule for GDP, Personal Income and Outlays, and US international
trade. Direct agency facts outrank an identical FRED aggregation event. `impact` is an explicit
MartinCall presentation policy, while exact UTC time, source timezone, title, category, provider
event identity, and source URL retain source provenance. The contract carries no consensus,
previous, or actual value when the free official sources do not supply one.

The endpoint is global rather than instrument-qualified. `ui/economic_calendar_runtime.py` runs
blocking source access only in the router's physical worker thread, coalesces concurrent requests,
and caches complete results longer than partial or unavailable results. Each source reports typed
`ok`, `partial`, or `unavailable` state; one source failure never becomes a false complete calendar.
Calendar facts are not stored in schema v18, do not create bars or sessions, and never gate
calculation, alerts, paper trading, or execution.

The browser requests the calendar only while the durable `aef:economicCalendar` setting is enabled.
It admits the exact versioned payload, maps an event only to a loaded confirmed bar containing its
timestamp or an explicit provider `future_axis` slot, clusters collocated events on the volume
interaction layer, and renders source/time/category details through the shared structured tooltip
owner. If neither axis contains the event, the browser draws nothing; it never reconstructs a
market calendar or invents a future slot.

## Data layer (provider-bound)

### Adapter contract

Enabled runtime providers are IBKR, Coinbase, and T-Invest. One persisted instrument selects
exactly one adapter. A provider gap is a typed quality problem; common code never tries another
provider.

An unexpected unsupported-provider row is storage-contract corruption: reference materialization
fails the complete watchlist read, and quote routing retains its previous immutable snapshot rather
than publishing the remaining rows.

The selected adapter owns search/bind, current futures resolution, confirmed history, live bars,
quotes, trading schedules, bounded repair fetch semantics, and advertised option/GEX capability.
`data/provider_history.py` is the internal canonical provider-qualified storage-read owner used by
adapters and the public data gateway; adapters never import the engine to recover stored history.
`data/providers.py` is the only public bar-read gateway: engine and UI consumers submit a qualified
route and one bounded range, recent-tail, or logical-projection intent. Only adapters and that
gateway may invoke the internal storage-read owner; SQL remains inside `storage/repos` and every
connection is admitted by the shared PostgreSQL pool.
Shared repair orchestration owns admission and continuation. Shared chart,
watchlist, engine, and UI services coordinate an already-qualified `InstrumentRoute`; they do not
inspect symbol spelling, import private provider implementations, or reconstruct provider metadata.
Instrument-scoped adapters must own an exact `liquid` or `trading` history-session scope at
registration. Session, storage-axis, and stream consumers call the adapter bucket contract directly;
they do not substitute a local trading scope or local timestamp grid when that contract is absent or
invalid.
The public async bar-read gateway may coalesce an identical in-flight request only within the exact
event loop and storage handle. Requests using different storage contexts never share a result.
Blocking provider HTTP owned by an async history or live-tail lane settles physically before caller
cancellation is exposed, so cancellation cannot admit an overlapping successor.

For historical requests the adapter also owns typed terminal completeness, pagination exhaustion,
provider limit/saturation interpretation, and finality. Common repair orchestration accepts those
facts or fails closed; it never infers a complete response from returned bar count or schedule slots.

Provider implementations have one public adapter boundary. Private Coinbase transports and parsers
remain under its adapter package. IBKR's manager is broker runtime ownership, not a
second provider facade. Hourly canonical storage uses `60m`; `1h` is not a parallel key.

T-Invest search and binding retain the selected opaque `instrument_uid` as the exact provider
contract ID. Its exchange-only `GetCandles` history excludes dealer-weekend candles and admits each
returned provider-final candle. Tail polling includes the active aligned bucket so a provider-marked
incomplete candle remains a typed in-memory chart preview until it becomes final. The request is
capped at one day, rejects a response exceeding the possible timeframe slots or provider safety
bound, and treats a successful response without forming candles as the complete provider mirror for
that bounded request. Because the API supplies no explicit response limit alongside the exchange
source selector, the public completion does not fabricate a `provider_limit`. A complete response,
including empty, owns a receipt and exact in-range reconciliation under the provider-mirror
tolerance. Transport, malformed, saturated, or forming-tail responses remain non-terminal and never
delete absence. T-Invest futures are concrete contracts, never locally inferred roots or continuous
series. Exact-route watchlist and chart-marker quotes come from bounded batched `GetLastPrices`
polling with `LAST_PRICE_EXCHANGE`; provider event time remains the quote-age authority and no chart
bar is used as a quote fallback. For session facts the adapter resolves the exact UID again, scopes
`TradingSchedules` with the exchange returned for that instrument, and admits only a complete
timezone-aware provider window of at most seven days. Exact regular-session intervals and their
provider-bounded closed complement are clipped to the requested UTC range before shared
materialization; no local exchange calendar or symbol inference fills a missing provider fact. This
bounded schedule is not a live halt/status feed. The adapter is read-only: it provides canonical
chart history, tail polling, exchange last-price polling, and current/prospective schedule facts, not
broker order placement.

### IBKR

IBKR manager lanes serialize broker operations under their owner-loop and pacing contracts. Raw
`ib_async` bars, tickers, contracts, and event objects do not escape the scope that created them.

The remote Gateway deployment owns one private, container-internal login-control lane. Gateway
startup creates one full-authentication attempt; a second-factor timeout leaves its supervisor
running but does not start another attempt. The System Health `New login` action disconnects the
application-owned IBKR lanes, requires explicit operator confirmation, and asks that supervisor for
one cold login after removing only IBC's `autorestart` token. It has no Docker socket, host command,
or control over any other service. Requests are serialized and rate-limited; the daily configured
IBC auto-restart and an ordinary supervisor/container start preserve IBC's session token and remain
separate from the explicit cold-login mechanism.

Historical futures roots use qualified `CONTFUT` directly and write provider-native continuous
storage. A provider-qualified concrete futures instrument uses its exact `FUT` for bounded history
and writes concrete-contract storage; an eligible complete request may own the exact reconciliation
described below. Live market data for a root uses the exact concrete `FUT` returned by
current-contract resolution. Any future broker-order path must use that same exact concrete
contract; `CONTFUT.conId` is not tradable. IBKR owns roll composition; local month generation,
liquidity roll selection, inferred contract mapping, and relabeling continuous bars as concrete bars
are forbidden.

IBKR requires continuous-history requests to end now. `data/ibkr/rolling_history.py` owns the
`rolling_end_now` request/result contract: it sends an empty `endDateTime`, chooses a bounded duration
from provider-now back through the target start, and receives the provider-qualified `CONTFUT`
identity plus actual returned bounds. It admits only confirmed closed `TRADES` bars inside the target
and commits them through the existing continuous-history owner. Empty, truncated, target-not-
contained, finality-unknown, malformed, and identity-mismatched responses are finite typed outcomes;
none creates a coverage receipt, deletes an omitted timestamp, substitutes the current concrete
`FUT`, or starts exact-range chunk continuation. Current contract state is stored separately and is
never selected or changed by the latest historical bar.

Duration sizing accounts for the partial current trading day included in IBKR's end-now `D`
responses: an elapsed-time ceiling in days alone can start later than the requested target. The
adapter reserves that partial day when converting to coarse provider duration units, within IBKR's
unit limits, and still validates actual response containment. This is a single bounded request,
not a schedule reconstruction or a retry with progressively larger ranges. Request duration,
request time, target bounds, returned bounds, admitted count, and terminal code are logged together.

Exact-range coverage is available only for ordinary qualified IBKR `TRADES` contracts whose
historical request has an explicit end, bounded step-size-safe duration, and provider terminal
`historicalDataEnd`. `CONTFUT` roots and non-TRADES routes cannot prove an arbitrary exact range;
rolling root repair therefore remains returned-bar-only, while an explicit exact-range request is
typed unsupported. Receipt eligibility is scoped to the exact request
identity, including `useRTH`, currency, configured and primary exchange (with the actual request
exchange selected by the qualified contract type), provider contract ID/type, and `TRADES` data
type; a different identity cannot reuse the receipt. Eligible ordinary requests own exact
provider-snapshot reconciliation for their admitted route and range; this is agreement with IBKR's
feed contract, not a claim that IBKR contains every exchange trade. Consequently a long-lived visual
drawing and algorithmic Channel Master on an IBKR `CONTFUT` chart use the same ordered admitted
confirmed series. Exact-range receipt support remains an absence/audit distinction and never blocks
generic Channel candidates; unresolved anchors and generation mismatches still fail closed.

Provider execution facts such as positive `minTick` remain attached to the exact current contract.
Missing facts are typed unavailable and repaired by the maintenance lifecycle owner, never replaced
by symbol constants.

### Coinbase

Coinbase product IDs are exact provider identifiers. Coinbase remains the live crypto route; an
IBKR instrument with similar display text is a different instrument. Bounded repair validates and
admits each returned target bucket. One request contains at most 299 target buckets against the
provider's 300-row response bound; a saturated, malformed, or out-of-range response fails. A
successful unsaturated response, including empty, is terminal for that exact bounded request and
owns provider-mirror receipt/reconciliation authority. It may therefore remove an older local row
omitted by the new response; this claims agreement with Coinbase's returned set only, not that
Coinbase exposes every exchange trade.

Provider bar-load APIs default to DB-only reads. An explicit `live_refresh=True` is a read/fetch
projection only: the Coinbase loader neither initializes storage nor writes bars or cache metadata.
Its confirmed live-tail bars enter canonical storage only through the process-owned
`ChartCommitRuntime`, the adapter commit contract, and the shared generation/event publication lane.

Coinbase latest-bar finality follows provider succession or an explicit final fact. The terminal
does not invent a close from elapsed wall-clock time.

### Futures lifecycle

A futures root is the durable provider-qualified identity. The selected adapter owns validation and
root-to-current-contract resolution. `route_fingerprint` changes when the concrete route generation
changes; root `instrument_id` remains stable.

Provider-native continuous history, concrete-contract history, current-contract lifecycle, and
observed roll events are different domain objects. None is a fallback builder for another. Schedule
reads use the contract ID carried by provider schedule evidence and return unknown when it is absent.

## Options and GEX

Option discovery is a provider capability, not a GEX capability. The provider qualifies exact
`conId`, expiry/`expiry_at`, strike, right, security type, exchange, trading class, multiplier, and
currency. Missing or conflicting facts reject a contract; local-symbol parsing and case/suffix
normalization are not identity recovery.

### GEX ownership and authority

Provider-independent calculation, payload, history, and Dynamics live in `data/gex/`. IBKR owns
qualification, option transport, ticker lifecycle, and cancellation under `data/ibkr/`. Common GEX
code receives typed acquisition/subscription facts, never an IB connection or raw contract.
`data/gex/config.py` carries provider-neutral policy and typed request metadata; a provider request
is opaque there. `data/ibkr/gex_request.py` alone adds IBKR transport settings. The provider runtime
composes those contracts once and reports typed status, so common scheduling and health code never
parses an IBKR manager dictionary or reconstructs connection fields.

Within the common GEX domain, `payload.py` builds the canonical payload and level projections;
`payload_contract.py` owns their typed level, volume, expiry, and compact-reader contracts.
`quality.py` owns exact Call/Put-pair admission, coverage diagnostics, and execution-input authority.
`live.py` owns session lifecycle, provider subscription coordination, and publication;
`live_analysis.py` owns pure option-volume and level-motion calculations.
`snapshot_validation.py` owns strict admission of persisted snapshots and compact history
projections; storage, history, and Dynamics consume that validator directly. Admission is staged by
capture envelope, level-derived summary, raw provider identity/expiry facts, exact contract pairs,
coverage diagnostics, and deterministic mathematical readback; these are one validator pipeline,
not alternate acceptance paths.
History admission validates every level and the full summary before applying a display level limit.
The history builder reuses the detached nested values from that admission within its own batch;
external mutable inputs and wire boundaries still require their own admission. The compact storage
reader bounds exact-route rows before materializing each stored JSONB once for projection, retaining
all raw-contract, exact-expiry, and comparison-scope checks.

GEX acquisition is serialized only by the provider-owned asynchronous option/GEX lane. Common code
does not expose a synchronous refresh producer and does not add a second lock around that lane.

Published GEX `strength` is rounded to its canonical four-decimal representation before
`power_class` is assigned, so producer classification and payload admission use the same exact
10/55/85-percent boundaries. Level geometry continues to use the full-precision ratio before its
own published rounding, preserving deterministic reconstruction of already persisted snapshots.

Within the IBKR adapter, `option_acquisition.py` owns underlying qualification, option-chain caches,
exact-series and expiry qualification, and global contract selection. `option_contracts.py` owns the
typed immutable contracts and expiry facts. `options.py` consumes that universe for Option Points and
Option Board; `gex.py` consumes it for broker market-data sampling and GEX subscription transport.

The acquisition universe is ragged, not a Cartesian expiry/strike grid. Current policy selects the
nearest 17 unique strikes under a maximum of 68 exact contracts and retains every provider-confirmed
Call/Put pair for the selected series. Qualification establishes the exact denominator. Missing
series are confirmed absence; missing market facts are not synthesized.

`comparison_scope` is built from the full provider-selected strike ladder and exact `conId`
universe before market-quality filtering. Usable `raw.contracts` are only an analysis subset and
cannot define comparison identity; missing quotes therefore cannot masquerade as concentration
migration.

Analysis admits only numerically complete exact Call/Put pairs. At least 75% of the qualified exact
pairs and at least one selected strike with non-zero GEX are required for a publishable analysis.
An explicitly degraded subset never weakens decision authority: every admitted observation still
needs matching identity, actual entitlement, freshness, model Greeks, exact OI evidence, and fresh
underlying spot. Below threshold or with an unknown denominator, the frame is partial,
non-persistable, and signal-blocking.

Actual market-data entitlement comes from each ticker's TWS callback, not the requested mode or a
library default. Frozen/delayed frames may be visible and persisted for history but are not current
decision authority. Null means unavailable; zero is factual only when explicitly reported.

Decision authority ends at the earliest exact provider expiry in the selected physical universe,
including qualified contracts omitted from the analysis subset. An expired or expiry-unknown frame
is display-only and signal-blocking. Strict 0DTE selection never falls forward to a later expiry.

Request and live capture are distinct authoritative sources (`gex:ibkr` and `gex:ibkr-live`) sharing
one canonical payload contract. A bad newer row invalidates older rows only in its own lane. The
chart history merges both lanes deterministically, while analysis compares only matching capture,
entitlement, and exact comparison scope. GEX Dynamics and every option-flow calculation consume the
operator-selected exact capture lane; a snapshot never combines current analysis from one mode with
option-flow context from the other. One exact instrument route has one active capture owner: a live
subscription and a request acquisition are mutually exclusive, while concurrent manual and
scheduler request demands join the same in-flight acquisition. Selecting live reserves the route
even while its provider session is temporarily unhealthy; switching modes stops the old owner before
starting the new one. Cancellation of any joined request remains deferred until the shared provider
acquisition, durable finalization, and capture-mode release reach a terminal outcome. The scheduler
also classifies and acknowledges that terminal result in its exact route slot before exposing caller
cancellation, preventing a completed request from being replayed by a restarted loop. The chart owns
its separate history projection after successful capture finalization: scheduler-only capture does
not build unused chart history, and joined manual consumers with identical projection parameters
share one history task using the acquisition owner's store. Manual delivery still waits for that
task, reports its failures, and checks
exact universe expiry again after projection; cancellation remains deferred through the projection.
Cached and unavailable request outcomes keep their existing advisory history/error policy. History
failure does not repeat a successfully finalized capture. The chart owns a bounded 48-hour display
window with `Last`, one-day, and two-day projections; live market
analysis reads only the latest two snapshots from its exact lane since midnight
`America/New_York`. That boundary also resets the option-activity baseline, so neither analysis
contract carries state across product days. When Option Flow is enabled for the same snapshot,
`SnapshotReadContext` reads that selected exact lane once over the union of its bounded lookback and
the current New York day; GEX Dynamics derives its latest pair and Option Flow derives its history
from those same validated rows rather than issuing parallel storage reads.

The selected `Last`/one-day/two-day projection is an exact-instrument indicator setting persisted
by the server settings contract. `Last` is the default when no preference exists, and independent
instrument tabs restore their own saved selection after reload.

`levels` is the only current strike-level contract; `option_volume_context` is the only current
volume/OI context. Deleted parallel shapes are not compatibility inputs. GEX Dynamics consumes the
immediate ordered predecessor in the same exact scope and publishes typed comparison context only.
It is not an indicator, candidate, sentiment input, paper authority, or execution signal.

The current request/live persistence and history horizon is bounded and code-owned. Historical
sampling preserves exact capture time and validity; it never stretches an observation across a
skipped snapshot or changed comparison scope.
Live-frame finalization retains lifecycle ownership through dividend-cache work, snapshot
persistence, and the provider-lane commit; caller cancellation is exposed only after that exact
frame reaches a terminal finalization outcome.

The GEX live owner also publishes a lock-protected, isolated copy of the latest complete raw frame
for bounded advisory service calculations. Reading that snapshot neither opens a subscription nor
performs provider or storage I/O, and the snapshot is cleared with its exact live session. It is
ephemeral display input only: it does not change the durable five-minute GEX persistence cadence or
grant decision authority. A standalone service indicator may close its own bounded minute buckets
from these copies while Calc and its live lane are active; removing or disabling that indicator
removes the projection without adding a pipeline or decision-context edge.
Option Drift runs storage admission, live-frame/minute-lane copies, calculation, and revision hashing
as one physically settled call on the existing background executor. Its minute-lane generation
rejects late work after service replacement or shutdown. Within one calculation adjacent intervals
reuse a contract index; no day-history cache, new polling loop, or capture authority is introduced.

### Option Points

An Option Point separates immutable user intent from mutable market sampling. Every operation is
scoped by `(instrument_id, route_fingerprint, target_id)`. The canonical payload contains
`payload.intent` with exact provider-qualified contract facts and `payload.market_sample` with
repricing facts; flattened duplicates and identity fallbacks are invalid.

`data/gex/option_target_contract.py` owns exact DTE classification, market-sample validation,
freshness, entitlement, and underlying-quote admission.
`data/gex/option_target_valuation.py` owns live option-quote admission and fair-value refresh.
`data/gex/option_target_selection.py` owns premium policy, eligible expiry/strike filtering, and
pure candidate ranking. `data/gex/option_target_universe.py` validates and theoretically
characterizes already provider-qualified exact contract rows without performing provider I/O.
`data/gex/option_targets.py` requests and admits the universe envelope, then orchestrates the
fixed-contract flow and final payload composition against those admitted, valued, and ranked facts.

Contract preview uses provider security-definition/detail qualification without opening market
data. Premium, Greeks, and fair value are typed theoretical estimates and visibly labelled as such;
GEX completeness, entitlement, or authority never gates selection.

Saving one exact contract opens or reuses its persistent option quote consumer. Deleting the point
releases that consumer and cancels the physical line only when no Board or other exact consumer owns
it. User intent updates and background repricing lock the same row but update only their respective
payload section, preventing stale full-row overwrite.

Storage owns the route-scoped maximum of three durable points. Create, deterministic oldest-row
eviction, and the returned eviction set occur inside one fenced transaction; the browser never
enforces the limit with a second delete request. A route-scoped transaction advisory lock serializes
concurrent creates before insert/count/eviction. The repricing owner reconciles provider quote
consumers against every canonical storage read, including the empty set, so a crash between commit
and cancellation cannot leave an orphaned `option-point:*` consumer.

Repository create/evict, intent update, market-sample update, and delete invalidate the process-owned
Option Point read revision only after their connection and transaction commit successfully. Replay,
no-change, and rollback do not advance it. This token carries no rows and is not durable authority;
it relies on the single canonical writer contract. Quote producers retain their exact-scope snapshot,
and the repricer retains its canonical read for its own lifecycle. They read PostgreSQL initially and
after invalidation, never to poll an unchanged empty table. A revision change during an in-flight
read discards that read and retries on the existing bounded cadence. Reconnect/startup performs a
fresh read; route selection changes invalidate scoped reads. Storage initialization and reads stay
outside the event loop, and cancellation settles physical work. Provider-consumer reconciliation and
the two-second price/freshness reevaluation continue even when the durable snapshot is unchanged.

Physical option-subscription identity includes exact `sec_type`, `conId`, and exchange; a
consumer with merely similar display fields does not share or release that line.

Live repricing requires fresh exact option and underlying observations with typed source,
entitlement, timestamp, and time basis. Only a valid non-crossed option Bid/Ask midpoint is admitted
to current Option Point calculation. Last remains a display diagnostic and never substitutes for
the BBO. Empty, stale, future-dated, or failed quotes clear current facts and keep dependent analysis
in typed wait state.

Future-point valuation uses the provider's exact `expiry_at` and
`max(current valuation time, target timestamp)`. Moving a point right applies theta decay; moving it
into the past never rewinds current calibration. The server-owned chart axis validates target time
and derives its canonical provider slot once when durable intent is created or changed; browser
display slots are never request authority. Pricing previews return that server-derived slot for the
exact route and timestamp. The repricer consumes the admitted exact point and never rebuilds an
immutable point's provider-session axis on its two-second market tick.
When the visible candle is still forming, current-point UI actions keep the clicked price but anchor
durable time to the latest provider-confirmed candle. Future timestamps still require an exact
server axis match; there is no browser slot fallback.
Repricing remains an Option Point-only lane. Only a materially changed, committed market sample
triggers the `fast` indicator lane; idle wall-clock ticks do no indicator work. Each storage read,
provider-consumer reconciliation, repricing pass, commit, and fast-lane publication is one
cancellation-deferred lifecycle operation. Shutdown therefore cannot abandon a provider-side
subscription transition or leave a durable new sample without its matching calculation event. The
fast lane reads one shared cached
provider-confirmed `1m` bar context and derives its ATR for the exact
`(instrument_id, route_fingerprint)`, evaluates each changed point with the same canonical Option
Reversal formula used by the primary pipeline, and cannot promote candidates, write research
observations, or authorize execution. The primary lane consumes that same declared confirmed `1m`
context even when the Option Point belongs to another chart timeframe. Exact live option BBO and
underlying quotes provide the intraminute price change; forming candle OHLC never enters the
calculation. Selection and scoring use one atomic sample from the same exact Option Point: its own
BBO, underlying quote, timestamps, and target price are never mixed with another contract. Missing
previous option/underlying samples remain absent rather than becoming numeric zeroes. A matching
`1m` bar generation or settings change invalidates the bounded result.

The route-scoped quote WebSocket carries durable inputs in `option_targets_snapshot` and publishes
the independent advisory result in `fast_indicators_snapshot`. Each result is accepted only while
its exact target input signature and per-scope revision match the current Option Point snapshot.
The producer holds publication while the durable target snapshot trails the fast calculation, then
publishes one aligned result; each full fast snapshot also removes projections for absent targets.
The browser retains the exact-scope primary Option Reversal result as the stable base and treats the
fast result only as an overlay. A missing, delayed, deleted, or reconnect-cleared fast projection
restores that base instead of removing the indicator table. The reducer reapplies a newer fast
projection after slower primary analysis merges, so a completed five-minute calculation cannot
overwrite current 2-second point dynamics.
The normal primary pass still provides Option Reversal context to declared downstream indicators;
enabling GEX retains the canonical five-minute GEX cadence and does not make GEX an Option Point
dependency.

### Option Board

Option Board is an on-demand route-scoped provider consumer, coalesced across visible browser clients.
It has no watchlist-wide or browser-polling path. A cold frame already carries exact route identity.
The last-consumer release remains owned through provider lease teardown even when its WebSocket is
cancelled. GEX stream release follows the same rule; neither owner can expose cancellation between
producer shutdown and provider cleanup. Blocking Option Board expiry-fact reads likewise settle
before producer cancellation becomes visible.

The Board may reuse an exact live-GEX ticker source or saved Option Point subscriptions; otherwise
it leases its own bounded provider option lines. Closing the last Board consumer releases only lines
owned by the Board. Availability and exact-contract selection never depend on GEX analytics.
The provider first centers that exact option universe from the canonical underlying quote cache. If
that exact route has no usable cached value, Option Board owns one bounded provider snapshot request
for the same qualified underlying, admits a provider current value or previous close as a typed
strike-centering reference, and releases that request immediately. The snapshot exposes the
reference acquisition, price source, entitlement, and `current | reference` state. A previous close
keeps contract discovery usable outside the underlying's trading session; it never becomes an option
premium, a live-price claim, or execution authority.

Board Bid/Ask/Mid/Last values are transient display facts and never seed the durable Option Point
market sample. Creating a point resolves the exact contract from the route-scoped snapshot revision,
persists intent only, and lets the canonical repricer own the first durable sample.
Futures-option routes preserve finite signed underlying and target prices, including zero. Option
premium, multiplier, and strike contracts remain positive. A non-positive futures underlying may
leave the current lognormal theoretical fair value typed stale/unavailable; it never invalidates a
fresh exact-contract option BBO or causes the signed provider fact to be clamped or discarded.

The workspace dock shell owns surface selection and layout only. Indicator Lens renders existing
indicator table overlays without starting calculation, provider, or storage work; Option Drive keeps
its independent execution state and lifecycle. Switching away from Drive uses its normal close path,
which disarms the gesture and releases the Drive Board surface before another dock surface opens.
The same shell hosts an optional GEX surface. Durable client presentation settings select the dock's
bounded width, left/right chart placement, and whether GEX uses the chart sidebar or dock. Moving GEX
reuses its existing controls and profile renderer, primary price projection, and admitted context;
the dock owns a separate canvas and tooltip coordinates, never a provider consumer or price scale.
Opening the GEX tab temporarily overrides its saved placement without a settings write. Closing the
tab or switching surfaces restores the saved placement; only the placement control changes it durably.
Closing or switching the dock does not change GEX calculation or capture cadence.

Option Drive is a workspace-docked paper-execution surface over that same coalesced Board producer,
never a second option universe or quote stream. Its 0DTE and 1DTE tabs request strict provider expiry modes;
the browser presents the nearest ATM plus six strikes on either side. Locking freezes the displayed
strike and exact-contract identities, while their quote cells and the selected `conId` continue to
receive current BBO. The premium ladder follows and recenters on that live selected-contract BBO in
both ready and ARMED states; the visible Bid and Ask also carry their current provider sizes. A stale
or missing BBO disables the ladder and disarms an active gesture rather than retaining tradeable old
prices.
Drive owns a memory-only trade-instrument selection from the server-authoritative watchlist. That
exact `instrument_id` and its current route fingerprint own the Board stream and paper-journal scope;
the chart may display a different instrument and never substitutes its symbol, route, or provider.
Drive requests the typed `option` journal scope for that exact underlying route, so it sees every
option contract position on the route without mixing ordinary-instrument paper activity. Contract
mutations still require one immutable exact physical option scope; the route-wide journal is a read
projection, never an execution identity.
Changing the chart preserves the trade instrument and stream but disarms an ARMED gesture. Removing
or invalidating the selected trade route fails Drive closed until the operator selects another exact
option-capable watchlist row; it never silently falls back to the chart instrument.
The server resolves that `conId` again from the current exact-route Board producer before creating a
paper command, so browser-supplied contract metadata never becomes identity authority.
The Board and Drive expose route, exact-contract, BBO, and session presentation facts beside the
entry controls. Drive's action state remains fail-closed unless its route-scoped snapshot, locked
exact contract, and fresh live BBO all pass the existing entry contract; the badges are a view of
those typed gates, not another admission mechanism.

## Engine and indicators

### Decision ownership

`engine/decision.py` owns deterministic scenario choice. `engine/trade_setup.py` owns Trade Setup
card construction and execution-ready intent. Paper, alerts, Telegram, and backtest consume these
contracts; they do not reconstruct decisions from overlays.

Authoritative decision inputs are frozen confirmed bars plus admitted typed context. Indicators may
also calculate explicitly provisional, watch-only previews; those cannot promote candidates, alter
lifecycle truth, or authorize execution. Features are deterministic and side-effect free for their
input. A preview must preserve the same historical state transition, but when the pipeline consumes
only its latest row and same-timestamp events it should not materialize historical series, events, or
overlays again. Models score explainable probability/expected-R facts but do not hide the reason or
bypass scenario state.

Wolfe projected paths and the current, unconfirmed P5 are repainting geometry with `provisional`
finality; only a confirmed five-pivot Wolfe event may promote a candidate. W5 retained lifecycle
events remain bounded chart history, and expired events remain audit/display history. Neither may be
re-promoted or replace the current `latest` signal authority.

### Indicator packages and registry

Indicators are startup-discovered packages under `indicators/modules/`. `IndicatorSpec` and package
extension types live in `indicators/module_contract.py`; `indicators/registry.py` builds the runtime
and UI manifest. `indicators/module_discovery.py` owns scanning and catalog diagnostics;
`indicators/module_validation.py` owns local and cross-package quarantine rules. Broken, duplicate,
cyclic, or contract-invalid packages are quarantined without preventing unrelated packages from
loading.

`indicators/refs.py` owns manifest reference resolution without importing the registry. Runtime
execution primitives never look back into discovery/registry state. A complex indicator keeps its
private parameter/domain contracts and rendering projection in separate package modules; its
calculation pipeline consumes those owners directly instead of resolving its own callback through
the global registry.

An indicator package may declare calculation callbacks, a pure parameter builder, candidate
promoter, renderer metadata, JS/CSS, sidebar contribution, router, and bounded service lifecycle.
Discovery happens at startup, never in tick ingestion. Calc OFF prevents calculation and external
resource activation; lifecycle owners release resources deterministically.

`build_indicator_runtime_params()` is the sole request-scoped producer of
`IndicatorRuntimeParams`. That typed contract owns Calc enablement, per-indicator parameters,
parameter-build errors, and strategy mode together; every execution spec and run context carries
the same instance with immutable owner maps and exact matching enablement/parameter ID sets.
Pipeline consumers never accept a lookalike object, infer missing `.enabled` as Calc ON, admit a
Calc-disabled spec, or discard missing parameter/error fields into defaults.

Each runtime indicator spec declares exactly one base calculation contract: standalone or
dependency-context-aware. The pipeline always executes that owner; it has no precomputed bypass or
silent preference between parallel callbacks. A preview likewise has at most one calculation path.

Immutable model weights required by one indicator ship below that indicator package and are
installed as package data. The package passes their exact path to reusable `ml` helpers; helpers do
not discover weights from the repository root. Training runs, candidates, and externally activated
models remain below `AppConfig.data_root` and never silently replace a packaged runtime asset.

`depends_on` declares installed pipeline-order dependencies. `required_dependencies` is its subset
that must be Calc-enabled. `optional_context` may be absent or disabled while the consumer runs
standalone. The generated manifest exposes every link; UI code does not collapse multiple links or
copy current pairs into allowlists. LINK requires every declared link registered and Calc-enabled;
otherwise settings shows SOLO. Runtime rows carry no link badge.

`control_specs.py` owns `IndicatorControlSpec` and the `_control` factory. Each package declares its
controls in `IndicatorSpec.controls`; that tuple is also their presentation order. Generated UI
preserves it verbatim, while non-package core rows preserve authored DOM order. Display labels,
control types, and element identifiers never rank or reorder controls. Registry values own default
manager/runtime order; durable operator ordering is a settings concern.

Package calculation/domain code does not import `engine`, `ui`, or `storage`. Service callbacks use
the neutral types in `indicators/service_contract.py`; the UI service host owns runtime mounting and
lifecycle. A composition root may join the two, but indicator service code does not import the UI
host or expose UI/engine state to calculation.

### Output and rendering

Indicators publish typed `series`, `events`, `latest`, candidates, lifecycle, and overlays under
their declared payload contract. Domain facts use canonical codes and typed attributes. Shared
output/rendering owners produce labels, pro/con text, sections, colors, and emphasis.
Normalization supplies omitted optional defaults but never discards malformed containers, list
items, or explicit enum values. A producer contract violation becomes that indicator's typed runtime
error and cannot masquerade as a healthy empty result; unrelated indicators continue to run.

An overlay that visually represents an indicator signal carries typed `signal_overlay: true`; the
compact runtime projection preserves that flag even when it drops the nested signal payload. Chart
signal density uses only this typed admission: Minimal keeps command-tier labels, Focus keeps the
latest signal overlay per source plus the current or active plan, and Research keeps the complete
signal and lifecycle history.

Tooltip/message/reason strings are advisory fallback only and are never parsed into domain state.
Package-specific overlays carry typed presentation fields or an explicitly registered custom
renderer; shared canvas code does not branch on package IDs.
Standard indicator tables may publish bounded typed `tooltip_sections` with a title and role-tagged
lines; the shared tooltip renderer validates their roles and emphasis tokens and owns their visual
section layout. A column section overrides the table-wide section for that cell.
Table producers never receive placement or opacity settings and never suppress their typed table
payload for presentation reasons. Manifest controls select one shared presentation mode: an upper or
lower horizontal strip, a vertical card in one of the four price-chart corners, the Indicator Lens
dock, or off. The shared renderer owns background-only opacity, deterministic runtime-order stacks,
and bounded chart occupancy. A table that cannot fit its selected stack is exposed in Indicator Lens
as typed chart overflow rather than covering more price geometry or moving to another corner.
Table placement excludes the visible in-chart GEX sidebar using its effective geometry; a docked
GEX profile reserves no in-chart width. Horizontal width as well as vertical stack height must fit
before a table can render on the chart.
Advisor handoff suppresses plan geometry only from typed overlay `role`; display labels never
classify an ordinary indicator line as entry, stop, target, or trail geometry.

### Shared contexts and service packages

`features/provider_session.py` is the pure interpreter for provider schedule facts and reset
semantics. Unknown/incomplete schedules produce typed unavailable context; no exchange or symbol
guess is allowed.

SMC owns structural geometry independently of session availability, but its opening range, initial
balance, previous-session confluence, and trend-day adjustment consume only that provider-session
interpreter. The first 30 and 60 elapsed minutes are measured from the exact provider interval open,
including overnight sessions; previous/current extrema group only by provider session key. Unknown
or out-of-schedule facts leave those adjustments typed unavailable while the standalone structure
calculation continues. UTC dates, exchange-profile guesses, first-N bars, and recent-N bars are not
session substitutes.

The display-only NY Range is distinct from that generic provider-session opening range.
`features/market_context.py` admits the named 09:30 New York opening only when the provider's
same-date liquid interval contains that timestamp, derives its box from the first 30 elapsed
minutes, and caps the visible session at 16:00 `America/New_York` or an earlier provider close.
Its typed chart-guide payload carries separate box-end and session-end timestamps. The browser maps
those bounds to pixels: the box covers the opening window, H/L/M segments end with the NY session,
the global NY Range toggle enables or disables the complete visual, and a separate global Lines
toggle controls only those H/L/M segments while leaving the opening box visible.

VSA is always-on terminal-owned analysis context in `engine/vsa_context.py`, not an optional
indicator or Calc gate. Its calculations read the exact required DB-backed analysis window. The
shared analysis cache retains only the latest twelve elapsed hours (at most 720 one-minute rows).
The browser presentation setting selects a two-, four-, six-, or twelve-hour response and render
tail without creating another analysis key, and defaults to six hours. Older VSA render rows are
neither rebuilt nor transported with history. Chart guides and shared market series are backend
facts projected onto the canonical chart axis; provisional gaps receive unavailable values and do
not enter calculations. Causal intraday volume seasonality groups bars by active elapsed minute in
the exact provider session; provider-declared closed gaps do not advance that coordinate. UTC clock
time, symbol/exchange calendars, and inferred opens are not substitutes.

Trade Setup Engine is an optional startup-discovered producer of the semantic execution-authority
contract. If the registered authority is missing, disabled, ambiguous, or failed, execution remains
not ready. The engine-owned Trade Center and journal contracts do not import the package.

Channel Master, Market Spotlight, Linda Theory, MartinCarlo, Tick Flow, Discord Signals, and other
packages own their private calculations, payloads, renderers, and lifecycle under the same manifest
contract. Their feature-specific thresholds and UI details are code/test-owned, not global
architecture.

Raw tick capability remains terminal core even when Tick Flow is absent. Discord Signals remains an
advisory package: its host companion uses official Discord Desktop RPC, filters configured identity
before terminal ingress, and commits typed message/tombstone state before projection. Discord
premiums never become provider market data. Exact-instrument Calc settings are the sole demand owner:
while the server is awake, any Calc-enabled target forces the companion Gate open without a second
browser lease. The package publishes typed Gate/companion state through its manifest-declared
settings status (`LIVE`, `WAIT`, `STALE`, `CONFIG`, or `ERROR`). External archive layout and
retention are described by [data/README.md](../data/README.md); external research documents are
evidence only.

## UI

### Application and browser boundary

FastAPI routers and services compose providers, storage, engine, alerts, and browser transport.
Static browser assets render typed payloads with layered canvas/object managers. The browser may
calculate pixel geometry, clipping, hit tests, animation, opacity, and label placement; it may not
calculate market/session truth, data repair, confirmation, indicators, scoring, promotion, alerts,
or execution. Use the task-scoped [browser asset map](../src/aef_terminal/ui/assets/README.md) to
select a file without scanning the complete browser source tree.

Every workspace persists active selection only as exact `instrumentId`. Boot loads settings before
route-bound controls, resolves that ID against the server watchlist, validates exact route storage,
then starts market/realtime work. A non-empty missing ID becomes typed
`INSTRUMENT_SELECTION_NOT_IN_WATCHLIST`; only an empty/absent selection may explicitly choose the
first watchlist row.

View range and presentation keys are scoped by workspace, exact `instrumentId`, and timeframe.
`route_fingerprint` is a runtime generation assertion, never a durable selection key. Unknown or
malformed canonical settings block boot/read rather than being renamed, filtered, or repaired.

Browser request ownership lives under `state.requests`; chart/quote transport ownership lives under
`state.transport`. There is no flat legacy facade. PostgreSQL snapshots and revisions outrank local
caches.

Opening the settings panel does not initiate a storage request. Automatic visible-page
system-health checks use only the lightweight storage status. Detailed PostgreSQL diagnostics run
only after an explicit operator request and may reuse the server-owned diagnostics cache; merely
viewing settings never authorizes a storage query.

The optional MTF Lens is one resizable candle-and-drawing picture-in-picture surface over the price
chart. Right-clicking a timeframe glyph opens or replaces its different-timeframe view for the same
exact instrument and route; it owns a separate socket, scope epoch, generation/sequence cursor, bar
projection, render scheduling, canvases, crosshair, drawing read projection, and cleanup lifecycle.
Its durable drawings come through a separate exact instrument/route/secondary-timeframe storage
request, not the chart stream, and render read-only without selection, handles, hit testing, editing,
or persistence from the Lens. This secondary storage lane is request- and epoch-fenced; its failure
does not mutate the primary chart's storage status or notices. The Lens never swaps the primary
`snapshot`, viewport, transport, or drawing state, aggregates one timeframe into another, or renders
volume, indicators, GEX, alerts, or paper state. Only its bounded frame size and corner anchor are
durable Lens presentation settings; open state and selected secondary timeframe are session state.

Browser history demand is one cursor scheduler for every timeframe. It prefetches a bounded
`countBack`-style page before the visible viewport reaches the retained left edge, is single-flight
per current chart scope, and retains at most one exact latest user intent. A real page prepend may
continue that same intent while its requested viewport and bounded left buffer remain unfilled; an
empty/replayed page, reversal, error, exhausted range, or scope change stops it and never creates a
retry loop. Settlement does not synthesize a new demand. Route or timeframe change invalidates the
request owner and aborts its fetch.
A page prepend preserves the commit-time timestamp and screen position. A drag still held beyond the
left edge may reveal only its bounded elastic pull; a completed, reversed, or superseded gesture may
populate the presentation cache but never moves the viewport. The visible-bar scale is capped by a
fixed source-row budget, and persisted values are normalized to that budget before the first render.
The viewport and drawing axis always retain the complete ordered source-bar slice. At subpixel
spacing the base price and volume canvases derive ephemeral screen-column render projections after
the exact timeframe projection: a column stays within one continuous visual-axis segment, preserves
first open, maximum high, minimum low, last close, summed volume, and the exact source indices of its
extrema, and never exposes canonical bar identity or enters analysis, storage, transport, snapping,
or execution. A forming provider candle remains a separate provisional projection. Drawing geometry
continues to use the full confirmed series, and dense magnet selection resolves an exact confirmed
source timestamp and price rather than anchoring to a render column.
Mouse-wheel and two-finger trackpad gestures zoom time around the pointer, Alt-wheel zooms price,
chart drag and arrow keys pan time, Page Up/Down pans one viewport, and End plus the visible Go Live
control returns to the live tail. These inputs share the same viewport math and cursor scheduler;
they do not select different history ranges or data-loading paths. The named price and volume canvas
regions are keyboard-focusable; while either chart owns focus, Left/Right and Home/End inspect the
same loaded bar axis through the shared crosshair and one accessible OHLCV status, while Escape
clears it. This presentation creates no parallel bar series or screen-reader-only market state.

Transient operator notices are typed owner-scoped presentation state. Each notice carries owner,
code, explicit presentation state, priority, sequence, and optional exact route scope. Producers
clear only their own notice; one shared renderer selects the visible notice. A shared scalar notice
or display-string prefix/regular-expression ownership is forbidden. A history-repair notice is
fenced to its requested range and clears from the typed terminal/not-needed repair lifecycle;
independent session or signal quality cannot keep a completed repair labelled pending.
Successful history-page confirmation expires after a short sequence-fenced interval; loading,
failure, and exhausted-range notices remain owned by their explicit lifecycle transitions.

### Browser transport and cross-window state

WebSocket projections validate exact identity, epoch/generation, and sequence before state changes.
Snapshots establish state; deltas patch only declared fields. A malformed or stale frame causes
resync and cannot become partial authority.

Browser liveness is an explicit presentation state, not an inference from socket existence. The UI
distinguishes waiting, connecting, syncing, live, stale, degraded, and retry states; `LIVE` requires
an accepted authoritative snapshot for the current generation. Exact-route quote rows expose stale,
delayed, frozen, error, and no-data status visibly as well as through accessible text. A current
forming candle is rendered as a provisional projection distinct from confirmed candle geometry.
Transport health is scoped to the active provider's advertised capabilities: an unsupported quote
lane cannot report connected or make the route critical, and an accepted chart heartbeat proves
transport liveness even when a polling provider has no new bar. Provider bar freshness remains a
separate typed quality fact and is never refreshed by a heartbeat.
`assets/js/06-labels.js` owns the shared presentation-state matrix and its loading, ready, attention,
stale, degraded, empty, disabled, and error categories. A visible-page heartbeat reevaluates
time-dependent quote, option-BBO, expiry, and transport presentation state without provider,
storage, analysis, or execution I/O; it patches only actual state transitions, so a silent feed
cannot leave a stale observation labelled live.

One typed `BroadcastChannel` dispatcher carries presentation-only cross-window messages. Crosshair
messages contain semantic timestamp and optional price and require exact workspace, instrument,
route, and timeframe. Any logical bar position is derived locally from that snapshot's ordered
confirmed series, never transmitted as durable authority. Watchlist presentation messages carry
exact `instrument_id` plus backend-acknowledged display revision. Receivers never write, republish,
change selection, or launch duplicate forced work.

Durable drawings, price alerts, and Option Points remain PostgreSQL-authoritative. Browser caches and
cross-window messages accelerate presentation but do not arbitrate conflicting state.

### Alerts and capture

Price-alert evaluation is server-owned. PostgreSQL is the durable authority and stores only exact-
route alerts that are enabled and either armed or fired. Disable and delete physically remove the
row; inactive rows and deletion tombstones are not product history. Fired rows, including a durable
pending or retrying delivery intent, remain stored until explicit deletion or rearm. Browser code
edits and renders price alerts but never evaluates crossings or marks delivery complete. Drawings
and channels are visual analysis objects and do not own alert runtime state.

One process-owned alert registry is hydrated from a validated PostgreSQL snapshot at startup. Any
malformed, deleted, disabled, or otherwise inert row violates the current schema-v18 runtime
contract and blocks startup; hydration never repairs or deletes it. Hydration is generation-fenced:
committed mutations arriving while the snapshot is loading are replayed over it before publication.
Every successful repository commit then upserts, replaces the exact scope, or removes that same
alert in the registry; exact replay advances no registry revision. Because one database admits one
canonical writer, evaluation, delivery scheduling, and WebSocket projection consume the registry
and never
poll the alert table. A restart performs the control reconciliation; there is no periodic full-row
scan in the healthy steady state. A degraded browser transport reports stale alert state and waits
for the same registry-backed WebSocket projection; it never falls back to the storage read model.
The durable definition includes one kind-matched typed `level_source`; fixed, dynamic EMA/VSA, and
GEX snapshot provenance are distinct exact shapes. Exact-route storage reads validate every row and
reject duplicate IDs or semantic definitions instead of choosing a preferred survivor.

Ordinary price crossings consume a bounded ordered event queue fed directly by semantic changes to
the fresh, execution-eligible exact-route canonical quote cache. Quote ingestion only appends an
immutable event when the route has an armed consumer and never performs storage work. The evaluator
processes each accepted cache publication in order; overflow is measured and fails closed by
discarding crossing baselines before resuming. Each process-owned crossing baseline is scoped by
exact alert, instrument, route, timeframe, and durable `rearmedAt` generation. A new generation
first observes its own fresh quote and never crosses from a price retained by an earlier alert
definition. The periodic registry consumer retires baselines for deleted, inactive, or superseded
generations. Consumers validate the complete registry projection and never manufacture a plausible
active subset by filtering malformed, inert, wrong-kind, or wrong-route rows. A missing, stale,
delayed, timestamp-unknown, or route-ambiguous quote fails closed; chart state and PostgreSQL bars
are not fallback price authority.
Confirmed-bar reads remain limited to alert kinds whose declared indicator context requires them.
Price-alert scopes do not resolve drawing geometry. Delivery wakes on a registry revision or the
exact next retry deadline and performs PostgreSQL compare-and-set transitions only for due durable
outbox work.

Telegram and browser actions use the same exact-route transition contract. Delivery is a bounded
outbox lane with durable generation, claim lease, retry, and compare-and-set completion, so network
I/O cannot block evaluation. Non-empty delivery batches consume the same immutable
`QuoteRouteSnapshot` as the alert runtime; each due alert looks up its exact instrument and checks
the stored route fingerprint before claiming delivery, without copying or requalifying the watchlist.
Rearm, disable, or delete advances generation and cancels stale pending
delivery. Once a delivery owner claims an attempt, caller cancellation remains deferred through the
physical send and its compare-and-set finalizer; shutdown cannot release the lane between those
side effects. Paper execution and option-quote release use the same physical-settlement primitive.
Browser alert changes call the same exact create/update/rearm/enable/disable/delete commands as
Telegram. There is no browser full-scope replacement, runtime merge callback, local deletion
tombstone, or compatibility writer. Delete-all is one explicit exact-scope storage transaction.
The interactive Telegram dashboard exposes one `Status` action that renders the overall
system, server resources, PostgreSQL state, and IBKR runtime from one system-status snapshot; it has
no parallel Server, System, Database, or IBKR menu commands.

Alert WebSocket projection checks the caller's revision under the registry lock before materializing
rows. A changed snapshot converts only the requested exact routes and timeframe; an unchanged read
has no rows, while deletion publishes an explicit empty snapshot. The producer resets its read cursor
on selection changes. Ordered quote evaluation, delivery events, and the 50 ms live trade path are
independent of this projection optimization.
Interactive Telegram quote display reads that exact route only from the canonical in-memory quote
cache. A cache miss stays visibly unavailable; durable `quote_snapshots` are trend evidence and are
never a current-price fallback.

Interactive Telegram polling, command executors, and the paper-notification worker are explicit
process-owned thread lifecycles. Startup performs their setting read and physical thread creation
outside the application event loop and settles that work before acknowledging readiness. Shutdown
stops new paper admissions, discards their non-durable queued notifications, settles any send or
command already in progress, and retains ownership of an undrained thread. The application event
loop performs their blocking joins through the physical thread boundary; an undrained
acknowledgement fails closed before writer-lease release.

Browser chart capture is an on-demand presentation lane with exact route/timeframe/request identity.
It does not acquire provider data or mutate canonical state. Timeout, disconnect, invalid image, or
route drift fails the capture with a typed visible error; Telegram never starts a parallel DB
snapshot calculation or renderer. A reconnect with the same page
`client_id` atomically replaces and closes the prior physical socket, so reload races cannot leave
orphan capture receivers on the event loop.

Expected route-selection and watchlist-version failures cross owner boundaries as typed exceptions
with stable codes. Malformed route payloads are non-retryable validation errors; a qualified-route
generation mismatch is retryable conflict and causes one exact watchlist reload before retry. Router
and browser code never classify these failures from message text.

Expected route-selection, watchlist-version, paper-command, and Discord-service failures cross owner
boundaries as typed exceptions with stable codes. Routers translate those codes to the shared
structured envelope and never recover domain identity or classify failures by parsing exception
text. Error payloads and browser helpers are defined in
[ui-error-contract.md](ui-error-contract.md).

### AI Third Opinion and Codex

AI Third Opinion is advisory only. It cannot create an order, replace confirmed data, or unblock a
quality-gated signal. Requests consume bounded confirmed context; the forming bar is separately
typed provisional.

The Codex provider uses the operator's ChatGPT-authenticated host CLI through a token-authenticated,
single-concurrency bridge. Credentials stay on the host; Docker receives only the local capability
token. Child execution uses an empty temporary directory, read-only sandbox, filtered API-key
environment, model allowlist, and hard timeout.

AI drawing suggestions are transient typed previews. Exact route/timeframe and confirmed bounds are
validated before the operator may accept one into the canonical drawing contract; suggestions never
persist themselves.

## Paper trading

Trade Center normalized `trade_setup` is the only paper-entry authority. Automatic entry also
requires the exact per-instrument canonical setting; missing or malformed state is disabled.
Minimum reward/risk and edge-gate policy come only from the validated durable settings contract;
each reserved order stores that immutable validated execution policy, including retries and generated
protective orders. Paper commands and HTTP payloads cannot override it. Unsupported research
variants are absent from runtime settings and UI rather than preserved as inert controls.

Manual placement, delayed monitoring, and automatic entry use one typed execution transition:
`pending | filled | rejected | price_unavailable`. An immutable command is reserved before the
transition. Exact route, analysis bar, analysis-window generation/hash, plan, and position
expectation make retries deterministic and stale commands reject atomically.
Pending-order update and cancellation commit their exact `paper_orders` mutation and corresponding
typed `paper_trade_events` record through one fenced PostgreSQL cursor and one transaction. The
process-owned projection is invalidated only after that compound commit. There is no standalone
event writer that can leave an order mutation without its audit record.
The manual chart-order surface exposes action, route, reference-quote freshness, and provider-session
facts adjacent to BUY/SELL. It refuses to arm or submit while the current chart snapshot does not
match the exact instrument route and timeframe. Reference quote and session badges do not invent a
new client-side price or session gate for a manual limit/stop; the server continues to own final
admission and exact-route revalidation.

Every paper order, position, and fill also carries a canonical `paper_contract`. Ordinary
instruments use an exact provider/provider-contract scope. An option scope uses provider,
`OPT|FOP`, provider contract ID, `conId`, and exchange as its physical key; expiry and `expiry_at`,
strike, right, trading class, multiplier, and currency are mandatory typed facts that must agree on
replay. Advisory locks, idempotency, position netting, reduce-only close, protective children,
reads, and indexes all include the contract scope. Two strikes or rights of one underlying and
timeframe therefore cannot share a paper position. The ordinary physical scope kind is
`instrument`.
Route-qualified journal reads select one explicit physical scope kind. The generic paper journal
defaults to `instrument`; Option Drive requests `option`. An exact `paper_contract` query and a
scope-kind projection are distinct contracts and cannot be combined.

Protective orders are reduce-only journal children. Partial reductions resize excess protection;
closed positions cancel it. An opposite GO may flip the net position only through the canonical
rank policy, and close/new residual protection is transactional.

Paper results are research metrics, not a simulated brokerage cash ledger. Ordinary instruments
report result in price points because their physical paper scope carries no authoritative contract
value or account currency. Option results multiply premium-point P&L by the immutable exact-contract
multiplier and report its typed currency. The terminal does not invent starting capital, cash
equity, commissions, margin, FX conversion, or buying power from those results.

Entry admission and risk-reducing exit are separate contracts. Analysis/history coverage, session,
and market-quality gates may block a new entry, but they never cancel or block an already-stored
manual, protective, or reduce-only exit intent. Such an exit uses the position's immutable
`instrument_id`, `route_fingerprint`, and `paper_contract`; a current-futures rollover or watchlist
change cannot rewrite that physical scope. Open positions detached from the current watchlist route
remain visible in the paper journal so the browser submits their stored scope rather than the
current chart scope.

A manual close reserves one idempotent reduce-only command before resolving price. Without a
current exact-route paper reference it remains pending and retryable; an old-route miss is reported
as `PAPER_EXIT_PRICE_UNAVAILABLE_OLD_ROUTE`. A later authoritative snapshot for that stored route
may fill it; a client- or operator-supplied exit price is never admitted. Protective triggers
inspect a confirmed bar's full high/low range. If one bar crosses both stop and take, the stop is
processed first as the deterministic conservative paper policy.

The paper fill model is deliberately a research approximation, not an exchange or broker simulator.
It uses one current exact-route provider-selected reference quote (fresh Last or Bid/Ask midpoint):
market fills at that reference, and limit/stop orders fill when it crosses their level. A confirmed
canonical bar may instead fill a touched limit/stop at its level. The model does not simulate queue
position, spread crossing, partial fills, latency, slippage, commissions, or broker rejection.
Chart-driven fills admit only exact-route `authoritative=true`, closed, confirmed canonical bars;
forming, gap, and commit-pending chart state cannot seed a fill. Markers come from the journal, not
raw indicator lifecycle. This contract remains Paper Trading only and does not call a broker order
API. It does not promise autonomous post-rollover protection for future live trading: that requires
provider-owned rehydration of the exact stored contract plus broker-native protective/OCO and
reduce-only orders. Local pending paper intents do not authorize or simulate that live guarantee.

Option Drive entry buttons are memory-only 60-second ARMED gestures, show their remaining time, and
create paper limit orders only. Its
premium ladder comes from the exact contract exchange's provider `marketRuleIds`/`reqMarketRule`
schedule; the server validates the same typed price-dependent bands. `minTick` is diagnostic only
and never replaces the complete schedule. Missing, malformed, or unavailable provider rules leave
the Board visible but fail limit entry closed without a local band fallback. Option paper execution
uses the midpoint of the current exact-contract live BBO as its reference. Manual `CLOSE MKT`
creates one idempotent reduce-only command without a client price; it fills at that paper mark when
available and otherwise remains priority pending. The underlying mark, analysis quality, and
historical coverage never substitute for a missing option premium reference.
For option execution, BBO freshness is the older of the latest exact IBKR BID-price and ASK-price
observations. Last, size, Greeks, and other ticker packets never advance either side's observation
clock. A future live close will submit a broker market order and consume only broker-reported order
and execution state; non-close live entries remain limit orders. The current runtime remains
paper-only and does not call the broker order API.
The process-owned paper-journal projection loads all pending orders and open positions once at
startup and reloads only after a real committed journal mutation. The paper/alert monitor consumes
that projection and publishes the exact prioritized option-contract demand without polling the
journal or waiting for option provider I/O. A separate process-owned refresh lane reconciles subscriptions and publishes
immutable quote snapshots; reduce-only demand precedes open-position marks and new entries. On
sleep or shutdown that lane settles already-started physical broker work before releasing its exact
consumers. Missing or failed refresh leaves option execution pending without delaying ordinary
alerts or non-option paper transitions.
Every paper journal, action, execution, and runtime-projection consumer requires the complete
canonical `PaperRepoMixin` read/write contract. A missing storage method is an incompatible runtime,
not an empty journal, absent position, `not found` result, or permission to attempt another entry.
Backtest edge artifacts are advisory journal annotations only. Filesystem discovery, artifact
recency, or a research rule never blocks or authorizes paper entry; changing execution admission
requires a versioned durable product policy rather than a generated report.
The alert monitor consumes the runtime's already validated active rows without a second rescue
filter: malformed identity or physical-contract state fails the iteration closed and is observable;
it is never silently left pending outside execution, rejection, or cancellation.

## Backtest

Replay under `backtest/` uses the same indicator, decision, Trade Setup, and lifecycle contracts as
live. External research workflows may evaluate alternatives, but they never become runtime authority
or a parallel signal implementation. Generated output belongs under `../data/backtests/`.
Every offline analysis and lower-timeframe execution series is admitted against the
selected instrument's exact provider, `instrument_id`, and `route_fingerprint` provenance before
replay. A typed provisional suffix remains unobservable; a confirmed bar after that suffix, missing
confirmed provenance, or mixed route fails closed. Canonical virtual timeframes remain eligible only
through their typed deterministic-aggregation provenance.
One confirmed bar becomes observable at its exact bucket end, so replay passes that close time as
the analysis clock and never evaluates the complete bar at its opening timestamp. The MVP reports
independent signal outcomes: entry begins with a later confirmed-bar touch, an ambiguous bar applies
stop before target, and an unresolved horizon exits at its last close. It does not claim a portfolio
equity curve or model overlapping-position capital, queue priority, partial fills, slippage,
commissions, financing, margin, or broker rejection; those excluded costs are typed in its result.

## Performance boundaries

The provider-to-screen hot path has no storage dependency: provider receipt → immutable exact-route
cache/event → sequenced dirty projection → next animation-frame render. Canonical persistence,
history repair, and heavy analysis run outside that path.

Current release gates measure genuine gateway-to-paint latency, WebSocket parse time, frame bytes,
animation cadence, long tasks, and exact-row DOM/canvas work. The canonical targets include 300 ms
live-candle p95, 400 ms quote p95, and 4 ms WebSocket parse p95 with required foreground samples.
Tests own the exact metric and sample definitions.

PostgreSQL starts with bounded `pg_stat_statements` query tracking and I/O timing enabled. This is
diagnostic authority for measured optimization only: it never changes market-data admission or
execution semantics, and activating preload changes requires an operator-owned database restart.

Browser quote work is proportional to dirty routes, not watchlist size. Chart history and object
layers have separate render generations so object-state changes do not repaint price history.
Typed render buffers and geometry caches are presentation caches only; they never become a second
market-data model.

Canonical bar materialization shares one immutable `BarProvenance` per exact provider/instrument/
route scope within a read or commit-readback batch. No provenance cache survives that batch, and
bar validation, write admission and transaction fences remain unchanged.

Time-dependent Option Board presentation records its nearest expiry/staleness transition deadline.
The one-second heartbeat performs no full-chain scan before that deadline; a new typed snapshot
recomputes the deadline while rendering.

A measured performance change replaces the affected versioned path end to end; it never creates a
speculative parallel authority or fallback.

The bounded analysis-context cache coalesces concurrent calculations for the same content key on
analysis workers. Building or waiting never holds its cache lock, so unrelated keys remain independent.
A clear fences pending publication; existing callers still receive the settled result or exception.

## Repository references

| Document | Authority |
|---|---|
| [PROJECT_IDEOLOGY.md](PROJECT_IDEOLOGY.md) | stable product principles and ownership philosophy |
| [ui-error-contract.md](ui-error-contract.md) | API/browser error envelope |
| [TODO.md](TODO.md) | current executable open work only |
| [README.md](README.md) | repository documentation index |
| [../README.md](../README.md) | operator quick start and runtime commands |
| [../scripts/README.md](../scripts/README.md) | current reusable operator/development tools |
| [../data/README.md](../data/README.md) | external data/artifact boundary and context rule |

When a technical contract changes, update its canonical code/tests and the relevant section here in
the same batch. Do not append implementation diary entries, completed work lists, speculative ideas,
pixel specifications, or private-helper narratives to this file.
