# TODO

Rules for adding work here:

1. Add only executable, product-relevant tasks with a clear owner and acceptance criteria.
2. Do not add historical audit notes, completed handoffs, speculative ideas, or broad cleanup wishes.
3. Do not use this file to preserve legacy or compatibility work. If a contract is wrong, fix the
   root contract and delete the obsolete path.
4. Remove an item in the same commit that completes it.

## Causal VSA MTF climax

Owner: `features/vsa_classify.py`, `engine/vsa_context.py`, chart commit/subscription
coordination, snapshot MTF reads, analysis cache/versioning, and execution generation.

Goal: validate and, only after validation, publish rolling confirmed-5m climax WATCH state with
confirmed post-parent M1 release/push transitions through the always-on engine-owned VSA context.

- [ ] Replace the superseded Phase 0 methodology with a versioned Phase 0.5 study using true
  rolling OHLCV, exact contiguous provider slots in one session, a prior-session M1 slot baseline,
  exact range-authoritative provider session intervals, common complete 5m/M1 slot coverage,
  wall-clock horizons/cooldowns, costs, deduplication, and out-of-sample or walk-forward results.
- [ ] If Phase 0.5 passes, add one shared internal confirmed-M1 lease for active 5m routes, an
  engine-only bounded post-parent observation lane, durable-commit wakeups, and M1-aware analysis
  and execution generations. Ordinary indicator MTF context must remain parent-trimmed.
- [ ] Add deterministic WATCH → CONFIRMED/EXPIRED VSA state and typed availability/output under
  top-level `snapshot.vsa_volume`; pending or failed M1 verification blocks confirmation and no
  closed-5m fallback is allowed.

Acceptance criteria:

1. Prefix replay and live durable-commit replay produce identical state, event order, timestamps,
   and entry prices; no post-`as_of` M1 bar can influence an earlier snapshot.
2. A watch uses exactly three contiguous confirmed 5m provider slots in one provider session, and
   confirmation admits only a confirmed M1 bar whose start is at or after the trigger and whose
   close is within TTL.
3. Missing, pending, failed, cross-session, stale-route, or incomplete M1 context is typed and
   fail-closed. M1 work never blocks tick ingestion and never appears on the 5m chart axis.
4. Analysis cache identity and paper/execution generation advance on accepted M1 context changes,
   while coalesced leases prevent duplicate provider subscriptions.
5. Research, state-machine, availability, cache-generation, route-rollover, and end-to-end tests
   pass before any MTF climax action or alert is enabled.

## Startup package removal with durable settings

Owner: `indicators/module_contract.py`, `indicators/module_discovery.py`,
`settings_contract.py`, the settings repository, and explicit operator tooling.

Goal: make removal of a startup-discovered package deterministic after that package has written
canonical durable settings. Runtime startup must remain strict: it must neither ignore orphaned
settings nor reinterpret them as another package's state.

- [ ] Define and implement one explicit operator-run uninstall/settings operation for an exact
  installed package. It must identify only settings owned by that package, stop application
  writers and package resources, remove those rows transactionally, validate the resulting exact
  current contract, and then allow the package directory to be removed. If the accepted durable
  schema itself changes, follow the canonical version/converter/backup contract instead of adding
  a runtime cleanup or compatibility path.

Acceptance criteria:

1. A package with no durable rows can still be removed by deleting its directory and restarting;
   no shared-core import, static DOM row, route, service task, or asset remains active.
2. A package with durable rows can be removed only through the explicit bounded operator workflow;
   runtime startup never deletes, ignores, converts, or accepts unknown rows.
3. The operation is idempotent for its exact declared package/settings scope, fails closed for an
   unknown owner or unexpected row, and cannot delete another package's settings.
4. Canonical schema v18 contains no retired GEX/VSA Calc tombstones or inert indicator-order IDs;
   future package removal must likewise leave no compatibility rows behind.
5. Settings, package-absence, resource-shutdown, and full-suite tests cover both clean removal and
   removal after durable settings exist.

## Live broker execution protection

Owner: a future dedicated broker-execution adapter/session, the existing engine execution-intent
contract, and the durable order/position/fill journal. This is not part of history repair or market
data acquisition.

Goal: add live broker execution only as an explicit opt-in phase. Protection must live at the broker:
an accepted entry is accompanied by broker-side stop and target orders using bracket/OCA semantics,
so closing a position does not depend on terminal uptime, chart rendering, indicators, history
coverage, or a local monitoring loop.

- [ ] Start with IBKR paper accounts, one account, one dedicated execution client/session, exact
  concrete tradable contracts, and MartinCall-owned orders only. Keep the current application
  incapable of live order placement until the explicit execution contract and operator enablement
  exist.
- [ ] Persist one immutable local command/correlation before submission, stage the entry and its
  attached broker-side stop/target atomically through broker-supported bracket/OCA sequencing, and
  accept state changes only from broker callbacks and execution reports.
- [ ] Reconcile broker positions, open orders, and executions on startup, reconnect, or ambiguous
  outcome. Manual and protective exits must be independent of history, indicators, session-entry
  gates, and chart state.
- [ ] Enforce close-only behavior against the broker's exact position for brokers without a generic
  retail `reduceOnly` field; a missing/rejected protective order is a hard visible fault and triggers
  the documented emergency-close policy.

Acceptance criteria:

1. No entry can be reported open merely because order submission returned; broker callbacks and
   fills own the durable transition.
2. Every filled entry has accepted broker-side protection or enters an explicit emergency state;
   terminal shutdown or market-data loss cannot silently remove that protection.
3. Duplicate submission, reconnect, partial/ambiguous outcome, OCA sibling cancellation, and manual
   close are deterministic and idempotent for the exact account and physical contract.
4. Live execution remains disabled by default and is first validated end to end against an IBKR
   paper account before any production enablement.
