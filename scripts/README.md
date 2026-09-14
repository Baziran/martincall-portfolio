# Scripts

This directory contains only reusable operator and development tools required by the current
product. One-off research, experiments, prototypes, notebooks, and retired utilities belong below
the external data root documented by the [data boundary](../data/README.md), not in the repository.

External files are evidence or artifacts, not default context. Do not scan the external tree to
discover current behavior; inspect a named file only when a task requires it.

## Running Python tools

From the repository root:

```bash
make check-python
PYTHONPATH=src ../../venv/bin/python scripts/<cli-name>.py --help
```

The help command applies to CLI tools. `write_build_info.py` is a Docker-build helper and writes the
ignored package metadata file; it has no interactive help contract.

The supported runtime is standard-GIL CPython 3.14.x. Tools consume the canonical application
contracts and must not create compatibility schemas, duplicate settings vocabularies, or alternate
provider-routing paths. Storage and operator invariants are in the
[Architecture reference](../docs/ARCHITECTURE.md#reference-map).

## Current inventory

| Script | Purpose |
|---|---|
| `check_futures_contract_alignment.py` | Compare provider-native continuous history with the exact current futures contract |
| `check_storage_runtime_contract.py` | Verify an existing database against the current code without mutation or schema ownership |
| `codex_advisor_bridge.py` | Run the authenticated, read-only host Codex bridge used by AI Third Opinion |
| `export_research_capture.py` | Snapshot or verify immutable prospective research journal exports |
| `ibkr_tick_feed.py` | Run the exact-instrument IBKR tick-by-tick collector |
| `refresh_watchlist_current_contracts.py` | Refresh provider-owned concrete futures contracts for persisted watchlist roots |
| `run_perf_budget.py` | Run the browser performance-budget harness against a local terminal |
| `scaffold_indicator.py` | Create a new indicator package scaffold |
| `smoke_api.sh` | Check liveness, readiness, and one exact-route chart snapshot |
| `write_build_info.py` | Docker-build-only writer for generated version metadata |

## Exclusive offline database writers

These commands are alternatives to the running MartinCall application, never companion processes:

| Script | Exclusive writer scope |
|---|---|
| `ibkr_tick_feed.py` | Standalone tick ingestion; use only instead of the application's in-process TickLive collector |
| `refresh_watchlist_current_contracts.py` | Provider-owned current-futures route authority |

Before running any of them, stop the MartinCall application and containers plus every other process
that writes the same TimescaleDB database. Keep all writers stopped for the entire command. Each
script acquires the database's canonical-writer lease before schema access or provider I/O, fails
immediately when the application or another writer owns it, and holds the lease until its provider
work and physical storage writers drain. It then releases the lease and closes storage. After the
command finishes, restart the normal single-process application or containers; startup must
reacquire the lease before schema verification, route refresh, or background writers begin.

For standalone tick ingestion, `make tick-feed` is the canonical guarded entrypoint. It verifies
that the Compose app service is not running and uses a one-shot `docker compose run --rm` collector;
the collector has no automatic restart policy.

The two `check_*` scripts are explicitly read-only and non-schema owners. They neither bootstrap
storage nor acquire the writer lease. Run `make storage-preflight` with the candidate code before
stopping the current application for an upgrade; a failure requires an explicit operator cutover
or recovery decision before deployment.

## Data boundary

Runtime output, captures, datasets, reports, model experiments, logs, backups, and generated
performance or backtest results go through `AppConfig.data_root` below `../data/`. Scripts must
not write those artifacts into the repository. See [data/README.md](../data/README.md) only when the
task concerns a specific data location or artifact.
