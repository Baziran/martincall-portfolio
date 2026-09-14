# Documentation Index

Read [PROJECT_IDEOLOGY.md](PROJECT_IDEOLOGY.md) only for product-scope or priority decisions. For
technical work, start at the [Architecture reference map](ARCHITECTURE.md#reference-map) and read
only the sections relevant to the task.

| Document | Purpose |
|----------|---------|
| [PROJECT_IDEOLOGY.md](PROJECT_IDEOLOGY.md) | Stable product principles, ownership philosophy, and quality bar |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Task-scoped current technical contracts, owners, pipelines, and failure boundaries |
| [TODO.md](TODO.md) | Single live executable backlog |
| [ui-error-contract.md](ui-error-contract.md) | API error payload shape and browser helpers |

Historical and redundant references intentionally moved below the sibling `../data/` root are
external evidence only. They are not repository authority, are not required in another clone, and
must not be scanned as default task context.

## Related repo docs

| Path | Purpose |
|------|---------|
| [../README.md](../README.md) | Quick start, project layout, run commands |
| [../AGENTS.md](../AGENTS.md) | Agent execution rules |
| [../data/README.md](../data/README.md) | Sibling `../data/` vs in-repo stub |
| [../scripts/README.md](../scripts/README.md) | CLI script inventory |

## Maintenance rules

1. Stable product doctrine and ownership philosophy -> `PROJECT_IDEOLOGY.md`.
2. Current technical state machines, owners, and implementation design -> `ARCHITECTURE.md`.
3. Private constants, helpers, and mutable enumerations -> canonical code and tests; architecture
   records only public cross-boundary limits.
4. Backtest CSVs, research captures, generated reports, runtime logs, and backups stay below the
   sibling `../data/` root and never inside the repository.
5. Do not commit agent handoffs, completed audit reports, or duplicated TODO histories as
   architecture documents; git history and issues carry that evidence.
6. Treat external historical/research documents as evidence only. They do not override
   `AGENTS.md`, `PROJECT_IDEOLOGY.md`, `ARCHITECTURE.md`, or current tests.
7. Update this index when adding or retiring docs.
