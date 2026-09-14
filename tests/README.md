# Tests

Run the standard suite from the repository root with `make test`. Use test names and targeted
search before opening a large module, for example:

```bash
rg -n '^def test_.*gex' tests
PYTHONPATH=src ../../venv/bin/python -m pytest tests/test_gex_context.py -k history -q
```

## Validation targets

- `make lint` runs the repository-wide Ruff lint and canonical formatting gates.
- `make test` is the canonical full pytest collection. PostgreSQL-backed tests join that same pass
  when `AEF_DATABASE_URL` points to the test database; otherwise they skip explicitly. The browser
  raster regression also requires the Playwright Chromium binary.
- `make test-postgres` is a focused gate for the PostgreSQL instrument and paper-transaction
  contracts, not the complete storage or schema suite.
- `tests/test_canonical_writer_lease.py` is the no-Docker fake-session gate for leader/fence SQL,
  app cancellation and drain ordering, readiness loss, and exclusive operator-script guards.
- `make test-ui-gates` is the curated server/UI architecture gate defined by `UI_GATE_TESTS` in the
  `Makefile`. It does not replace browser-asset tests or the full suite.
- `make smoke-api` checks a separately running, ready terminal with at least one persisted exact-route
  watchlist instrument. It is an explicit operator post-deployment step, not the in-process API smoke
  test, and agents do not start or rebuild its containers.

Use focused tests for the affected owner and contract after a coherent batch. Also run `make test`
for source deletion, a public or cross-owner contract change, or release completion. Storage work
that requires PostgreSQL is not fully validated by a pass in which those tests skip; report the
missing environment instead of treating a narrower target as equivalent.

Documentation may itself be machine-checked. When changing a map, index, executable command, or
operator contract, search for tests that consume that document and run the owning focused check.

Shared test helpers are explicit imports:

- `tests/provider_payloads.py` builds provider-qualified instruments and session facts.
- `tests/route_helpers.py` inspects application routes without duplicating router traversal.

There is intentionally no root `conftest.py`. Mutable-state resets and autouse fixtures stay in the
test module that owns that state. Add a scoped `conftest.py` only when the same fixture is genuinely
shared by multiple test modules in one domain.

`make test-postgres` requires `AEF_DATABASE_URL` and fails immediately when it is absent.
