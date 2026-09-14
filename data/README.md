# Data boundary

The MartinCall repository does not store runtime data, research artifacts, experiment code, or
generated model output. Immutable weights required by the current product may be versioned only
inside their owning source package. This in-repository `data/` directory contains only this
boundary document.

## External data root

`AppConfig.data_root` resolves to the sibling `../data/` tree. Docker mounts the same tree at
`/data`.

```text
../data/
├── backtests/                       generated replay/backtest output
├── datasets/                        captured and curated training datasets
├── models/                          training runs and externally activated artifacts
├── backups/                         operator-selected database backups
├── codex-advisor/runtime/           host bridge PID and log
├── discord-signals/                 raw captures, reports, support files, runtime logs
├── research/                        current research output and evidence
├── MartinCall_docs/                 retired/historical repository documents
├── MartinCall_scripts/              one-off and retired research/experiment scripts
├── MartinCall_prototypes/           retired prototypes and notes
└── MartinCall_models_experiments/   non-current model experiments
```

Reusable capture code under `src/aef_terminal/research/` remains product source in the repository.
Its journals, datasets, reports, and model outputs belong in the external tree. Reusable current
operator/development tools remain under `scripts/`; one-off experimental tools do not.

Current prospective journals publish immutable raw records below
`../data/datasets/channel_interactions/raw/schema-v1/date=YYYY-MM-DD/` and
`../data/datasets/option_reversal/raw/schema-v1/date=YYYY-MM-DD/`. These locations are outputs, not
inputs that ordinary agent sessions should scan.

In Docker, `${MARTINCALL_DATA_ROOT:-../data}` is mounted at `/data`; remote deployments should set
`MARTINCALL_DATA_ROOT` to a stable host directory independent of a release checkout. The application
writes journals from the app container into that mount. Research files do not belong in the
TimescaleDB data volume even when both containers run on the same host.

`./martincall-server.sh research-export` creates a copyable archive and matching digest manifest
below `research/exports/`. The export is a bounded snapshot of the immutable files selected when the
command starts; new records may continue arriving for the next export. After copying both files to
another machine, verify them before analysis with:

```bash
PYTHONPATH=src ../../venv/bin/python scripts/export_research_capture.py \
  --verify-manifest ../data/research/imports/<export>.manifest.json
```

## Context rule

The external tree is storage and evidence, not architecture authority or default agent context. Do
not scan it during ordinary code discovery. Start with repository code, tests, and the task-scoped
[Architecture reference map](../docs/ARCHITECTURE.md#reference-map). Read an external file only when
the current task names that artifact, explicitly asks to reproduce or evaluate a study, or requires
diagnosis of a specific runtime output.

Historical documents and scripts below `../data/MartinCall_*` may be stale. They never override
`AGENTS.md`, `docs/PROJECT_IDEOLOGY.md`, `docs/ARCHITECTURE.md`, canonical code, or tests.

## Write and retention rules

- Runtime and tool output must use `AppConfig.data_root`; never write captures, CSVs, databases,
  reports, logs, backups, model runs, or caches into the repository.
- A packaged model asset is source-controlled only when current runtime behavior requires that exact
  promoted weight file; training and evaluation still happen in the external tree.
- Keep only artifacts required by a current runtime/model, needed to reproduce a retained result,
  or selected as the current verified rollback point.
- Regenerable caches, rejected experiments, superseded reports, loose exports, and transient logs
  may be removed after confirming that no current workflow names or uses them.
- Do not hide accidental in-repository data with a broad ignore rule; move it to the appropriate
  external directory and keep the repository boundary visible.
