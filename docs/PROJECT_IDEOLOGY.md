# Project Ideology

MartinCall is a local trading decision terminal. It should feel like an audited execution
workstation, not a generic web dashboard.

This document is the stable product compass. Detailed technical contracts belong in
[ARCHITECTURE.md](ARCHITECTURE.md); exact implementation belongs in canonical code and tests.
Operator commands belong in the root and scripts READMEs.

## Product target

The target is a compact, deterministic, explainable, operator-grade terminal:

- trusted chart and market state first;
- explicit scenario and trade-setup state second;
- alerts, paper, and live execution only after the decision path is auditable;
- replay and backtest through the same contracts as live.

TradingView is the reference for chart ergonomics, information density, and interaction speed.
MartinCall may remain narrower when that improves trust, auditability, or integration with the
operator's execution workflow.

## Stable product principles

1. **Truth before convenience.** Provider-confirmed facts become authoritative only after the exact
   domain admission contract accepts them. Provisional, stale, incomplete, or unknown state remains
   visibly distinct and cannot silently authorize a decision or execution.
2. **Provider identity is part of the data.** One instrument remains bound to one
   provider-qualified contract. Providers own qualification, lifecycle, and market-session facts;
   the terminal does not guess or silently cross providers.
3. **Realtime behavior is deterministic.** Market ingestion and delivery remain non-blocking,
   event order is explicit, and authority-relevant provenance is explicit at every boundary.
4. **One decision path serves every mode.** Live, replay, backtest, paper, and execution share
   calculation and decision logic. They may differ in effects, never in hidden signal semantics.
5. **Execution comes last.** The engine owns decisions and intent; journals own resulting execution
   state. Overlays, display text, and advisory analytics are not authorities.
6. **Modules exchange typed domain facts.** Shared presentation owners turn those facts into text
   and visuals. Display strings never become business data.
7. **Each responsibility has one canonical owner.** A wrong mechanism is replaced instead of being
   surrounded by aliases, compatibility layers, duplicate registries, or parallel fallback paths.
8. **Durable state changes through explicit, auditable transitions.** Runtime fails closed on an
   incompatible durable contract instead of silently repairing, migrating, or reinterpreting it.

## Module ownership

| Area | Owns | Must not own |
|---|---|---|
| `data` | provider qualification, routes, market facts, history, repair, and provider option acquisition | decisions, presentation, or Option Point intent |
| `settings_contract` | durable settings vocabulary and value contracts | persistence, transport, or compatibility conversion |
| `storage` | canonical persistence and journals | signal semantics or runtime migration |
| `features` / `ml` | deterministic facts and explainable models | stateful decisions or UI formatting |
| `runtime` | shared lifecycle, event, quality, commit, and calculation primitives | provider selection or a second domain authority |
| `indicators` / `signals` | typed analysis, candidates, and plans | screen prose or execution effects |
| `engine` | scenario state, decisions, and execution intent | provider I/O, transport, or rendering |
| `ui` | application composition, transport, controls, and shared presentation | duplicate provider or decision logic |
| `ui/assets` | interaction, geometry, and rendering of typed state | market, signal, or execution truth |
| `ui/paper` / `alerts` | journal transitions, typed alert evaluation, and delivery | alternate decision logic |
| `backtest` / `research` | replay, evaluation, and evidence | alternate live contracts or runtime authority |

## Quality bar

A change should materially improve at least one of these outcomes:

- trading correctness;
- realtime reliability;
- signal or decision explainability;
- latency or bounded resource use;
- ownership clarity;
- contract and failure-path coverage.

Cosmetic churn, duplicate authorities, compatibility-only paths, speculative infrastructure, and
display-string rescue logic do not meet this bar.
