# Portfolio source snapshot

This repository publishes the current source of Grigory Shmykov's personal MartinCall project.
It starts a separate public history; the private working repository and its earlier experiments are retained separately.

Prepared from private baseline `1b5abf060bffcbf65779cfc18cc8f34eb9dea6b7`, with portfolio documentation and credential-hygiene changes applied on 14 September 2026.
The 1.5.0 private release label in the project describes the personal product baseline.

## Why the public history is short

The private source baseline above contains **1,355 commits**. This repository starts a new public history from that baseline; subsequent public commits record publication and maintenance changes. It is a dated portfolio artifact, not a continuously synchronized mirror of the private working tree.

The earlier private history includes local configuration and experimental material that are outside the publication scope. A separate current-source snapshot keeps those materials out of the public repository. This preparation did not rotate working credentials and does not claim a publicly exposed credential incident was resolved. Private history is not offered for unrestricted distribution.

## Evidence available in this snapshot

The [portfolio walkthrough](docs/PORTFOLIO.md) connects responsibilities to project artifacts. The [task list](docs/TODO.md) shows outstanding work and acceptance criteria; the [test guide](tests/README.md) explains the checks. After the initial publication adjustments, [Full Test Suite](https://github.com/ShmykovGrigory/martincall-portfolio/actions/runs/34830242451), [API Smoke](https://github.com/ShmykovGrigory/martincall-portfolio/actions/runs/34830242478) and [UI Architecture Gates](https://github.com/ShmykovGrigory/martincall-portfolio/actions/runs/34830242770) passed for revision `fdfa7e7ace988e1eebb0f417315f8905d477993f`. Later documentation-only revisions do not imply new runtime validation.

## Included

Current product source, tests, build configuration, reusable scripts, configuration templates and documentation.

## Excluded

Private Git history, local `.env` files, real tokens, runtime databases, backups and historical experiment exports.
Configuration templates and disposable CI test credentials are examples, not credentials for the working installation.

## Verification of this preparation

- Build/configuration tests: 14 passed.
- Repository Ruff lint and format checks passed.
- Exact comparison against 10 locally configured secret values found no matches in the 763 selected source files.
- No application or database restart was performed. No trading-performance claim is made.

The full runtime suite was not rerun during the initial local source-publication preparation. The subsequent public CI runs are recorded above; the workflows remain included for future checks.

## Publication CI adjustments

Ruff is pinned to the verified 0.15.22 development-tool version so a fresh CI installation does not silently change the lint baseline. The mobile-toolbar browser fixture uses an explicit narrow width to test overflow independently of platform font metrics; the scroll and settings-visibility assertions remain enabled. Application behavior is unchanged.
