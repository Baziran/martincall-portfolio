# AEF Trading Terminal — Agent Rules v4

## Copyright and automated reuse

Read [LICENSE.md](LICENSE.md) before processing or reusing material. This is proprietary portfolio source shared for recruitment and professional evaluation only. AI-assisted evaluation is permitted under the license conditions; a shared link does not authorize code reuse, modification, redistribution, training or dataset collection. Do not copy source into other projects or reusable corpora without the author's written permission. Owner-authorized maintenance remains permitted. LICENSE.md is authoritative; this paragraph is a reminder for automated tools, not a separate license.

## PURPOSE AND AUTHORITY

MartinCall is a real-time trading system. Correct market state, deterministic event order,
and realtime safety take priority over a small diff or preserving a legacy mechanism.

The authoritative documents have separate jobs:

* `AGENTS.md` defines agent workflow, reading gates, and critical stop-lines.
* `docs/PROJECT_IDEOLOGY.md` defines stable product principles and ownership philosophy.
* `docs/ARCHITECTURE.md` describes the current system, contracts, and canonical owners.
* Canonical code/tests and repository documents explicitly assigned a role by architecture define
  narrower details only within that role. `docs/TODO.md` records future work; it is neither current
  architecture nor task authorization.

Historical reports, handoffs, old test expectations, and git history are evidence only. If the
authoritative sources conflict, do not guess. Report the conflict, follow the source that owns the
topic, and update a stale document only within the authorized task scope; otherwise stop for a
product decision.

## TASK-SCOPED READING

Before a product-scope decision, read `docs/PROJECT_IDEOLOGY.md`. Before analyzing, designing, or
changing a domain contract or code, inspect the worktree, then use
`docs/ARCHITECTURE.md#reference-map` to select only the relevant sections. Use headings and
repository search instead of loading the entire document. Broad cross-cutting changes may require
several rows. A reference to a detailed document does not weaken the stop-lines below.

## REPOSITORY AND DATA BOUNDARY

* Keep the MartinCall repository limited to current product source, tests, build/configuration
  files, authoritative documentation, and reusable operator/development tools required to run or
  verify the current system.
* `src/aef_terminal/research/` is product source for canonical research capture. Its datasets,
  journals, reports, trained artifacts, and other outputs belong below `../data/`, never beside
  the source.
* Put one-off research or experiment code, prototypes, notebooks, saved diffs, retired documents,
  captures, datasets, generated reports, model experiments, logs, backups, and runtime output in
  the external locations defined by `data/README.md`. Caches and bytecode are disposable and never
  belong in the repository. Do not create an in-repository archive; git history is the archive for
  retired code and documentation.
* `../data/` is storage and external evidence, not default agent context or architecture
  authority. Do not scan or read it proactively. Open only a named artifact required by the current
  task, an explicitly requested research/reproduction workflow, or diagnosis of a specific runtime
  output. Start from repository code, tests, and task-scoped authoritative documentation.

## EXECUTION AND RECOVERY

* Treat an authorized task list as one queue and continue until it is complete. Independent reads,
  searches, and checks may run in parallel; preserve deterministic mutation order.
* Do not pause between safe in-scope subtasks. Stop only for missing authority, a destructive or
  externally mutating action that requires approval, a material product choice, or an architectural
  incompatibility.
  A material product choice exists only when the task and topic-owning authority leave multiple
  incompatible product semantics, public or durable contracts, ownership boundaries, or failure
  policies. Implementing an already authorized contract is not material merely because it changes
  core code or crosses files.
* Preserve user changes in a dirty worktree. On path, import, or runtime failure, keep completed work,
  fix the immediate environment issue, and continue from the current state.
* If a required stop leaves a coherent batch incomplete, do not claim delivery or automatically
  revert edits of unknown origin. Report the completed and incomplete producer, consumer, contract,
  and test edges, the blocker, and the last validation state.
* Core modules are not a protected legacy zone. Change them when the root cause or stated invariant
  requires it, while preserving realtime safety and deterministic ordering.

## ROOT CAUSE, OWNERSHIP, AND REUSE

Before a non-trivial code edit:

1. Identify the root cause, affected file/function, and invariant or product outcome being improved.
2. Search by responsibility, domain nouns/verbs, payload fields, call sites, and imports; names alone
   do not establish distinct responsibilities.
3. Identify the canonical owner and briefly report the candidates inspected and whether the change
   will reuse, extend, replace, or create behavior.
4. Reuse or extend the owner. Create a shared/domain abstraction or new source of truth only when the
   audit proves none exists.
5. Update every affected producer, consumer, public contract, and test; remove the replaced duplicate
   or legacy path in the same batch.

Fix a wrong producer or root contract before its consumers. Prefer typed fields to parsing display
text. Do not add text-based rescue paths, local wrappers, compatibility readers, parallel fallbacks,
or cosmetic cleanup without a concrete correctness or product benefit.

This is a new private product: do not preserve a wrong mechanism solely for backward compatibility.
If a provider or API requires weakening core invariants or building a large compensating state
machine, stop and explain the mismatch; propose a provider or design that fits the product model.

For uncertain provider/market semantics, use primary documentation. For unresolved trading UX,
compare proven terminals such as TradingView. Do not browse merely to justify an obvious local
contract.

## DEPENDENCIES AND BUILD INPUTS

* Reuse the standard library and declared dependencies before adding another package. Adding or
  removing a direct third-party dependency, or changing its source or version policy, is a material
  build and supply-chain choice unless the authorized task explicitly includes it.
* In an authorized runtime-dependency batch, update every affected manifest and production lock
  together without refreshing unrelated pins; keep development-only dependencies out of the
  production lock. Preserve required source URLs, hashes, markers, and runtime extras, report the
  runtime, licensing, security, and operational effects, and run the dependency/build contract tests
  plus the relevant suite. If the repository does not define a reproducible clean standard-GIL
  Python 3.14 lock procedure for that change, stop for a build/release decision instead of
  hand-editing only a direct pin or freezing the shared development environment.

## REALTIME AND MARKET-DATA STOP-LINES

* Never block tick ingestion or websocket/event-loop delivery, perform synchronous storage
  initialization/reads or schema work there, reorder events, or introduce hidden mutable shared
  state or races. Keep streaming handlers lightweight.
* Only exchange/provider-confirmed closed trade bars admitted by the exact provenance contract are
  authoritative OHLCV for storage, analysis, and execution. Quotes, heuristics, projections,
  interpolation, and synthetic fills are typed provisional display/state aids only.
* Virtual `3m` charts derive only from the canonical `1m` provider series. Never request, persist,
  repair, receipt, or commit a parallel `3m` series; canonical write boundaries must reject it.
  Confirm one elapsed bucket only from its three exact contiguous provider-confirmed `1m` children.
  An incomplete elapsed triplet remains absent without a placeholder. Keep the current target bucket
  provisional and publish its in-memory live projection before storage or schedule work.
* Flat OHLC or provider-reported zero volume does not invalidate an admitted closed bar. Reject or
  repair it only from a stronger typed provider fact; consumer heuristics must not create a repair
  loop against a deterministic provider response.
* Historical provider mirroring belongs to bounded provider-range evidence, not a locally
  reconstructed exchange calendar. A durable coverage receipt may close one fully elapsed exact UTC
  `[from, to)` range only after the adapter returns typed terminal `complete`, every page is
  exhausted, every returned item passes the provider and bar admission contracts, and the
  provider-specific terminal, pagination, max-span, and applicable limit contracts classify the
  response as untruncated and unsaturated. This is agreement with the provider's returned bounded
  snapshot under the product's provider-mirror tolerance, not philosophical proof that another feed
  or the exchange would expose a different bar. Common code never guesses terminal state from count.
  Empty complete responses are valid coverage. Timeout, transport, authentication, entitlement,
  malformed, partial, saturated, ambiguous, finality-unknown, or storage failures never create
  coverage.
* Only an adapter that explicitly owns bounded historical snapshot authority may reconcile canonical
  range membership. Under the provider-mirror tolerance its complete response atomically upserts
  every returned bar, removes previously stored bars in the same exact scope and range that the
  provider omitted, verifies exact ordered readback, and commits the coverage receipt in one
  transaction. A provider without that authority may still commit admitted returned bars, but it
  never deletes historical bars or creates absence-authoritative coverage.
* A coverage receipt stores no OHLCV and does not assert that an omitted timestamp was a trading
  slot. Commit only returned provider-confirmed closed trade bars. Omitted timestamps remain absent
  from storage, analysis, extrema, and execution input. Receipts prove exact-range absence and
  completeness for audit, reconciliation, and bounded repair; they are never global admission for
  generic indicators, candidates, decisions, paper trading, or execution. Generic calculation uses
  the ordered admitted confirmed-bar series even when coverage is unknown or partial. A consumer may
  fail closed only when its declared actual minimum input is unavailable. An active/forming tail stays
  pending until provider-declared finality. Interpolation and fill-forward remain typed display aids
  only.
* Receipt absence alone is not a suspected bar gap and must not start an unbounded viewport fetch
  loop. A concrete typed gap, invalidated provider timestamp, or explicit provider-refresh demand may
  enqueue bounded repair and must expose provider, code, exact range, finite admission state, and a
  bounded terminal outcome for that exact intent generation.
  Current provider data/session quality gates new entry timing; it does not stop confirmed-bar
  calculation. Reduce-only exits and manual position closure never depend on analysis or historical
  coverage.
* Provider schedules own current and prospectively captured session facts used for market status,
  execution gating, and session-dependent calculations. Unknown session facts block only operations
  that require them. For one small internal discontinuity (at most 24 bars and one day), already
  materialized verified provider schedule coverage ignores closed slots and requests missing open
  slots. Automatic small gaps outside that verified coverage and ambiguous medium session gaps remain
  unknown; only an explicit refresh, invalidation, or stale-tail demand may fall back directly to
  bounded provider history. A seven-day-or-larger internal history hole is a direct bounded provider
  mirror demand. Schedule rows never create bars or coverage, and no local exchange-calendar state
  machine may compensate for them. Drawing geometry never depends on schedule reconstruction.
* `HistoryRepairIntent` carries one provider-bounded chunk plus the immutable exact route-qualified
  target envelope and request/admission identity. A real bar mutation, deletion, or first-time
  coverage expansion (including an empty provider mirror receipt) advances the cursor automatically
  toward the older target. A replay/no-change, transport, authentication, malformed, saturated,
  partial, or finality-unknown outcome stops it and awaits a fresh typed demand. Continuation remains
  scoped to the canonical chart-event generation and never resets the envelope's absolute budget.
  Exact receipt replay is no mutation and emits no event; a later confirmed provider bar inside a
  covered range commits normally. Realtime handlers only enqueue bounded work and never perform
  provider or storage I/O.
* Durable drawing anchors identify confirmed bars by timestamp. Runtime geometry derives logical
  positions from the ordered confirmed series; future-only anchors use an explicit relative offset
  from a confirmed anchor. Schedule-ranked slots are not durable drawing, channel, crosshair, or
  cross-window authority. Magnet snapping admits only authoritative confirmed bars.

## DOMAIN STOP-LINES

### Instrument identity and providers

* One watchlist row is one provider-qualified instrument. `instrument_id` is the only durable lookup,
  relationship, membership, job, and API selection key.
* Provider identifiers are opaque. Normalize only search queries, never selected identities. Do not
  resolve, compare, recover, or route by aliases, display names, `instrument_key`, or symbol text.
* `provider_symbol` is typed routing metadata after `instrument_id` selection. Generic `data_symbol`
  and `InstrumentRoute.symbol` fields are forbidden. Adapters consume exact contracts; they do not
  guess or remap them.
* Derive `route_fingerprint` only after `instrument_id` lookup. It is a realtime/cache generation
  token, route assertion, and additional route-generation storage dimension beside the required
  `instrument_id`, never a resolver, durable lookup, or fallback identity. Consume adapter-produced
  `session_contract_id`; never reconstruct it.
* Providers own current-futures resolution and session/calendar facts. Unknown metadata stays unknown
  and blocks operations that require it. Watchlist prices come only from provider quote rows for the
  exact route, never chart state. Name-based indicator templates may choose calculation parameters
  only; they never select data or execution identity.
* A futures-root current-contract rollover carries durable drawings in the same fenced lifecycle
  transaction. The route owner supplies typed previous/next fingerprints and adapter session IDs;
  storage never reconstructs them. Drawing replacement takes the same lifecycle lock and rechecks
  the current contract. Missing transition facts, a stale save, or unexpected target-scope state
  fails closed and rolls back; concrete-futures instruments are not rebound.

### Storage and settings

* Runtime accepts canonical schema v18 only: create it only in a clean application schema or verify
  the exact `(18,)` contract. Older, drifted, or incompatible storage blocks startup; runtime never
  migrates, repairs, or reinterprets it.
* Active `ProviderManifest.db_providers` values own runtime provider routing and the exact schema
  v18 storage-provider enum. Adding or removing a storage value changes the schema contract and
  therefore requires a new schema version and converter; retired providers are not retained in the
  current contract.
* One PostgreSQL database admits one canonical writer process. The application and every supported
  offline writer acquire the same dedicated-session leader lease before schema access, provider I/O,
  route-authority mutation, or background writers. Each canonical transaction takes the shared
  transaction fence and then reverifies the leader lease before mutation; a replacement leader must
  pass the exclusive fence barrier. Lease loss latches writes and readiness closed until restart.
  Production stores require the lease by default; read-only processes are explicit non-schema owners,
  and only tests may opt into the explicit bypass.
* A schema change requires a new version and one operator-run exact previous-to-target converter.
  Stop all application and TimescaleDB writers, create and test-restore a whole-database backup under
  matching versions. The converter must preserve all current product data, fail closed on an
  unexpected source contract, transform data and advance the version marker in one transaction, and
  validate the exact target before commit. Delete it after validated cutover; retain no startup
  migration, compatibility path, or converter chain.
* `src/aef_terminal/settings_contract.py` solely owns the durable `client`, `server`, and
  `watchlist_presentation` scopes, keys, and value shapes.
* Each loaded page owns a fresh UUIDv4 writer and synchronously allocates and persists its per-key
  `(changed_at_ms, writer_id, sequence)` order before optimistic publication. PostgreSQL compares the
  tuple lexicographically; retries reuse it, exact replay creates no mutation or revision, and
  rejection requires a strictly newer authoritative tuple. The browser reconciles every rejection
  and never reports it as saved; Web Locks are not part of the contract.
* `settings_state.revision` advances once for every real mutation or deletion, never for replay, and
  is carried by snapshots, acknowledgements, and typed broadcasts. Stale revisions cannot delete
  state; equal-revision rows and presence must match exactly before browser state changes. PostgreSQL
  is authoritative; high-water tickets and BroadcastChannel messages are transient.
* Watchlist presentation is one server-owned revision per exact `instrument_id` shared by all tabs.
  Coalesce trend work by canonical route set and time bucket; receivers do not start duplicate work.

### Option contracts

* Option discovery is a provider capability independent of GEX. Preview exact provider-qualified
  `conId`, expiry, `expiry_at`, strike, right, exchange, trading class, multiplier, and currency
  without opening market data. GEX completeness, entitlement, or decision authority never gates
  contract selection. Preview values are typed and visibly theoretical; they must not claim live
  price, liquidity, entitlement, or broker Greeks.
* Saving one exact contract may open its persistent quote subscription; deleting the point releases
  that exact consumer. Future-point theta uses the contract's exact `expiry_at` and point timestamp;
  current calibration never rewinds. Option Board may extend the universe, but never replaces exact
  identity or makes GEX an Option Point dependency.
* Paper orders, positions, and fills for an option carry one immutable physical contract scope:
  canonical provider, `OPT|FOP`, exact provider contract ID and `conId`, and exchange. Expiry and
  `expiry_at`, strike, right, trading class, multiplier, and currency remain mandatory typed facts
  and must match on replay. Advisory locks, idempotency, netting, reduce-only transitions, reads,
  and indexes use that physical scope; an underlying route or timeframe never merges two option
  contracts. This durable paper contract does not authorize live broker execution.

### Indicators and UI output

* Indicator producers emit typed domain facts and execution intent. Shared output/rendering modules
  own screen text, sections, emphasis, and colors. Consumers never parse `tooltip`, `message`,
  `reason`, or other display strings as data; fallback text is advisory only.
* Extend typed payload/schema and the shared renderer before adding indicator-local formatting; test
  the producer contract and renderer behavior together.
* `depends_on` declares installed pipeline-order dependencies; `required_dependencies` is its subset
  that must be Calc-enabled; `optional_context` may be absent or Calc-disabled while the consumer
  remains standalone-capable. `control_specs.py` owns the control type and `_control` factory; each
  indicator module declares controls in `IndicatorSpec.controls`.
* Derive every dependency link from the generated manifest; do not collapse multiple links or copy
  current pairs into UI allowlists. A non-empty `depends_on` or `optional_context` produces LINK only
  when every declared link is registered and Calc-enabled, otherwise SOLO. The badge belongs left of
  Calc in settings, never in runtime rows.
* Indicator ordering belongs in settings; Manager owns status and Calc. Keep runtime titles
  right-aligned.
* Trade Setup momentum naming is fixed: BO means long breakout and BD short breakdown; signal names
  are `setup_momentum_breakout` and `setup_momentum_breakdown`.

## VALIDATION, RUNTIME, AND DELIVERY

* Do not test after every small edit. After a coherent implementation batch, use `tests/README.md` to
  run the focused checks for every affected contract. Source deletion, a public or cross-owner
  contract change, and a release-completion batch additionally require one full `make test` pass;
  PostgreSQL-dependent validation requires `AEF_DATABASE_URL`. Run `make lint` after a coherent
  Python batch. If a pass fails, collect all failures, fix them together, and run one new pass.
* Prose-only documentation changes normally require no runtime suite. Machine-checked maps or
  indexes, executable commands, and operator/runbook contracts require their focused validation.
  Report every check run and every environment-dependent check not run.
* Do not rebuild or restart Docker containers automatically. The user owns container runtime checks.
* Supported local Python is standard-GIL CPython 3.14.x only. After changing interpreters, run
  `make check-python`. From the repository root use `PYTHONPATH=src ../../venv/bin/python ...`;
  use `make test` or `PYTHONPATH=src ../../venv/bin/python -m pytest ...` for pytest; in the app
  container use `PYTHONPATH=src python ...`.
* Use repository-relative paths in shell commands and repository content. Never embed a
  machine-specific absolute path in project files.
* At delivery, list modified files, summarize the coherent change, and report tests/checks run or
  intentionally not run.
