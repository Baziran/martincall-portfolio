# MartinCall / AEF Trading Terminal

**From a rough MVP to a working market-research system with explicit architecture, operating rules and acceptance checks.**

By **Grigory Shmykov** — project initiator and product owner, responsible for technical direction and acceptance. Over six months, I led its evolution from a quickly assembled MVP to a working personal system with clear architectural responsibilities and stricter quality rules: requirements, solution review, implementation cycles, performance and interface refinement, operational feedback and acceptance. It complements my professional background in manufacturing management and enterprise IT, with particular relevance to **technical project management, systems integration and IT implementation leadership**.

The application connects market-data providers, analysis, replay, alerts and paper trading in one local workspace. **My contribution:** original ideas and product direction, requirements, priorities, architectural review, testing of behavior and final acceptance of changes. **AI contribution:** substantial code generation and implementation assistance using Gemini and Codex.

[Role and evidence walkthrough](docs/PORTFOLIO.md) · [Development history and snapshot](PORTFOLIO_SNAPSHOT.md) · [Current task list and acceptance criteria](docs/TODO.md)

## Six months of continuous product development

- Defined boundaries between confirmed market data, incomplete information, signals and execution intent.
- Challenged AI proposals that added unnecessary branches, duplicate logic or workarounds outside the intended architecture; required corrections at the responsible module.
- Drove repeated review and correction cycles across features, integration, performance and interface design. See the [development process and one traced example](docs/PORTFOLIO.md#continuous-development-and-acceptance).
- Reviewed outcomes against requirements and required rework until the behavior met acceptance expectations. The [product principles](docs/PROJECT_IDEOLOGY.md) and [ownership map](docs/ARCHITECTURE.md#ownership-map) document the intended boundaries.

My enterprise team experience includes coordinating factory departments and external integrators. In MartinCall, I directed AI-assisted implementation and retained responsibility for product decisions and acceptance.

## What is available to assess

This is a **dated source snapshot of a privately developed project**, prepared from a baseline with **1,355 commits**. Its short public history records publication updates, not the original development sequence. Earlier private history and local configuration are excluded; see [snapshot provenance](PORTFOLIO_SNAPSHOT.md).

The source includes provider adapters, persistent state, a browser interface, tests and CI workflows. Current scope ends at research, replay, alerts, paper trading and typed execution intent. Live broker order placement, commercial adoption, measured trading performance and high-load production operation are outside this portfolio’s demonstrated scope. A guided demonstration can focus on one instrument and its data-to-decision path.

Current source baseline: **MartinCall 1.5.0 private**, storage schema v18. The version identifies the source contract; this is not a hosted public trading service.

## Copyright and reuse

**Copyright (c) 2026 Grigory Shmykov. All rights reserved.** Shared for recruitment and professional evaluation only, including AI-assisted review under the license conditions. Copying, running, modification, redistribution and reuse require prior written permission, subject to the limited evaluation and platform/legal exceptions in [LICENSE.md](LICENSE.md). No permission is granted for model training. This is not an open-source project.

## Technical overview

The project is intentionally modular. Each block has a narrow responsibility and can be
replaced without rewriting the whole terminal:

- `data`: provider adapters, normalized bars/ticks/quotes, backfill, and repair.
- `features`: candle, volume, speed, and context feature extraction.
- `signals`: candidate generators such as SFP, Wave 5, and Wolfe.
- `ml`: built-in NumPy probability/regime models.
- `engine`: analysis orchestration, signal lifecycle, scenario decisions, and trade setup truth.
- `indicators`: runtime indicator registry, contracts, modules, and UI metadata.
- `backtest`: labels, replay, fills, and walk-forward validation.
- `storage`: PostgreSQL/TimescaleDB persistence.
- `ui`: FastAPI application/transport services plus the vanilla-JS renderer under `ui/assets/`.

## Product doctrine

MartinCall is not a generic charting clone. It is a local decision terminal: fast enough
for live operation, deterministic enough for replay/backtest, and explicit enough to audit
why a signal exists.

Use [PROJECT_IDEOLOGY.md](docs/PROJECT_IDEOLOGY.md) for stable product principles. For technical
work, select only the relevant sections from the
[Architecture reference map](docs/ARCHITECTURE.md#reference-map). Agent workflow and critical
stop-lines live in [AGENTS.md](AGENTS.md); this README does not duplicate those contracts.

## Project layout

```text
MartinCall/
├── src/aef_terminal/     # Python package (engine, indicators, ui, storage, …)
├── tests/                # pytest suite (`make test`)
├── scripts/              # Reusable operator/development tools (see scripts/README.md)
├── docs/                 # Current doctrine, architecture, contracts, and backlog
├── data/                 # Tracked boundary README; no datasets or runtime output
├── secrets/              # Runtime env files (not committed)
├── docker-compose.yml    # TimescaleDB + app; mounts ../data:/data
├── Makefile              # Python guard, test/lint, smoke, Docker, run
└── martincall-server.sh  # Docker lifecycle helper (start/stop/sleep/wake)

../data/                  # Runtime/research/experiment/archive artifacts (NOT in git)
```

Reusable product research capture code remains under `src/aef_terminal/research/`. Immutable model
weights required at runtime live with their owning indicator package and are installed with the
application. Training runs, one-off experiments, prototypes, notebooks, captures, reports,
datasets, logs, backups, caches, and backtest outputs live under `../data/`. The in-repo `data/`
folder contains only its boundary document; see [data/README.md](data/README.md).

The sibling data tree is not default documentation or agent context. Inspect only a named artifact
needed for the current task, reproduction, or runtime diagnosis.

Test navigation, shared helpers, and fixture conventions are indexed in
[tests/README.md](tests/README.md).

## Run

### Local Python

MartinCall uses standard-GIL CPython 3.14.x; the free-threaded `3.14t` build is not part of
the runtime contract. `.python-version` pins the repository to the 3.14 release family, and
the canonical local interpreter is `../../venv/bin/python`.

Create or recreate that environment from the repository root before the first local run:

```bash
python3.14 -m venv --clear ../../venv
../../venv/bin/python -m pip install --upgrade pip
../../venv/bin/python -m pip install -e ".[dev,ml]"
```

The `ml` extra supports the offline Channel Master training pipeline under
`ml/channel_interaction_training.py`. Runtime Channel Master continues to publish deterministic
parent-timeframe facts, but its one-minute research shadow currently fails closed with a typed
unavailable reason and publishes no research rows until the product owns an exact mapping between
parent confirmed-bar indices and confirmed one-minute bars.

For a host-Python run, start TimescaleDB, configure its URL, and launch FastAPI with the canonical
interpreter:

```bash
make check-python
export MARTINCALL_POSTGRES_PASSWORD='local-only-change-me'
docker compose up -d martincall-timescaledb
export AEF_DATABASE_URL="postgresql://martincall:${MARTINCALL_POSTGRES_PASSWORD}@127.0.0.1:5432/martincall"
make run
```

The password above is an explicit local-development example. Direct `docker compose` commands
require `MARTINCALL_POSTGRES_PASSWORD` from the environment or a local `.env`; copy
`.env.example` and replace its placeholder with a strong random value. `martincall-server.sh`
validates the same Compose environment before every container command and exits when the required
password is absent; it never discovers or invents a credential.

### Docker

```bash
./martincall-server.sh start
# or
make docker-up
```

Schema v18 supports exactly one MartinCall application process per PostgreSQL database. Keep the
canonical single-process Docker/`make run` launch; multiple Uvicorn workers, application replicas,
or another canonical-history writer against the same database are unsupported. Startup enforces
this with a PostgreSQL session-level canonical-writer lease acquired before schema verification and
fails while an offline writer owns it. Canonical transactions are fenced against leader replacement;
a lost lease latches readiness to not-ready and requires process restart. Shutdown releases the lease
only after physical runtime writers drain successfully.

The database service preloads bounded `pg_stat_statements` tracking and enables I/O timing for
operator profiling. Changes to those PostgreSQL command settings take effect only after an explicit
operator-owned database restart; application startup never restarts the database.

The standalone IBKR tick collector and current-futures refresh scripts are exclusive offline
writers, not companions to a running application. Stop the application,
containers, and every TimescaleDB writer before using them, then restart the normal single-process
runtime afterward; see [scripts/README.md](scripts/README.md).

Use the guarded collector entrypoint after stopping the application. It refuses to start while the
Compose app service is running and launches only the `ticks` profile plus its database dependency:

```bash
make tick-feed
```

The normal app and collector have separate `runtime` and `ticks` profiles. Their guarded entrypoints
refuse to start while the other service is running; do not enable both in one Compose invocation.
The shared canonical-writer lease remains the final fail-closed boundary.

For an operator-controlled upgrade or rollback, follow the canonical
[storage lifecycle](docs/ARCHITECTURE.md#storage-lifecycle) and the commands in this README.
Before stopping the current application for an upgrade, point `AEF_DATABASE_URL` at its database
and run `make storage-preflight` from the candidate checkout. This read-only gate applies the
candidate code's exact schema, durable settings, and drawing-payload verifier without bootstrapping
or mutation.

Do not start the current runtime against a non-v18 MartinCall database. The operator-controlled
runtime has no converter or migration path. A drifted, incompatible, unversioned, or unexpected
database is not a normal conversion candidate; restore a known-good backup or make a separate
reviewed operator recovery decision.

### Health checks

```bash
make health
make readiness
make smoke-api
make test
```

### Autonomous prospective research capture

The app can retain a research-only ES analysis demand on a remote Docker host without an open
browser tab. Configure exact durable identities in the deployment `.env`:

```dotenv
MARTINCALL_DATA_ROOT=/opt/martincall/shared/data
AEF_RESEARCH_CAPTURE_ENABLED=true
AEF_RESEARCH_CAPTURE_INSTRUMENT_IDS=["ibkr|future_root|ES|CME|USD|ES"]
AEF_RESEARCH_CAPTURE_TIMEFRAME=5m
AEF_RESEARCH_CAPTURE_RECONCILE_SECONDS=15
```

The owner retains the canonical parent stream plus Option Reversal's declared confirmed `1m`
context, runs full research analysis once per minute, and never performs paper effects. Inspect and
export it on the Docker host without stopping the app:

```bash
./martincall-server.sh research-status
./martincall-server.sh research-export
```

Raw journals and exports remain in the mounted external data root. Option Reversal observations are
captured whenever an exact saved Option Point has valid live option and underlying samples. Channel
Master remains fail-closed for new one-minute research rows until its exact parent-index-to-1m
mapping has a canonical owner; autonomous demand does not bypass that market-data contract.

## URLs

- Dashboard: http://127.0.0.1:8000/
- Liveness: http://127.0.0.1:8000/api/health
- Readiness (verified storage): http://127.0.0.1:8000/api/ready
- Market snapshot: http://127.0.0.1:8000/api/market

The terminal uses explicit provider-bound instruments in normal operation:

```text
/api/market?instrument_id=ibkr%7Ccontract%7C12345&expected_route_fingerprint=ibkr%7Ccontract%7C12345&interval=5m&range=1d
```

Both values come from the selected row returned by `/api/instruments`; names and display symbols
are never accepted as identity. The backend loads the row only by `instrument_id`, verifies the
current `route_fingerprint`, and calls that row's provider adapter. A missing or mismatched identity
is an error, not a request to try aliases or another provider.

Enabled market-data providers are IBKR, Coinbase, and T-Invest. The live UI must not mix bars
across providers; if the configured provider is unavailable or inconsistent, the chart shows a
data-quality warning instead of silently filling from another source.

The current watchlist is stored in PostgreSQL `instruments`, not in browser storage or a JSON
setting. Ordinary instruments are added from provider search results by provider contract id;
futures retain a canonical root while the provider resolves and persists the active concrete
contract separately. Reordering uses the versioned `watchlist_state` contract.

Watchlist row presentation (`classic` or `trend`) is also global server state, keyed by exact
`instrument_id` in the `watchlist_presentation` settings scope. Every workspace/tab renders the
same acknowledged revision; a browser workspace, local storage, or symbol map is never its owner.

## Documentation

| Doc | Purpose |
|-----|---------|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Living system design |
| [docs/PROJECT_IDEOLOGY.md](docs/PROJECT_IDEOLOGY.md) | Product and engineering doctrine |
| [docs/README.md](docs/README.md) | Documentation index |
| [AGENTS.md](AGENTS.md) | Agent execution rules |
| [scripts/README.md](scripts/README.md) | CLI script inventory |
