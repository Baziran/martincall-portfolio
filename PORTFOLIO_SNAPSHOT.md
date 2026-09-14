# Portfolio source snapshot

This repository publishes the current source of Grigory Shmykov's personal MartinCall project.
It starts a separate public history; the private working repository and its earlier experiments are retained separately.

Prepared from private baseline `1b5abf060bffcbf65779cfc18cc8f34eb9dea6b7`, with portfolio documentation and credential-hygiene changes applied on 14 September 2026.
The 1.5.0 private release label in the project describes the personal product baseline.

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

The full runtime suite was not rerun for this source-publication preparation. The existing workflows are included so future changes can be checked in CI.

## Publication CI adjustments

Ruff is pinned to the verified 0.15.22 development-tool version so a fresh CI installation does not silently change the lint baseline. The mobile-toolbar browser fixture uses an explicit narrow width to test overflow independently of platform font metrics; the scroll and settings-visibility assertions remain enabled. Application behavior is unchanged.
