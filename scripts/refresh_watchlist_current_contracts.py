from __future__ import annotations

import json
import sys
import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aef_terminal.config import AppConfig
from aef_terminal.data.providers import (
    bind_provider_future_root,
    provider_catalog,
    search_provider_instruments,
)
from aef_terminal.storage.postgres import CanonicalWriterLeaseUnavailable, PostgresStore
from aef_terminal.ui.reference_actions import (
    ReferenceActionDeps,
    refresh_watchlist_current_contracts,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Refresh current concrete futures contracts for persisted watchlist roots."
    )
    parser.add_argument(
        "--provider",
        action="append",
        default=[],
        help="Limit refresh to a provider key, for example: --provider ibkr",
    )
    args = parser.parse_args()
    config = AppConfig()
    if not config.database_url:
        print("AEF_DATABASE_URL is required", file=sys.stderr)
        return 2
    store = PostgresStore(config.database_url)
    lease_acquired = False
    try:
        store.acquire_canonical_writer_lease(owner="refresh-watchlist-current-contracts")
        lease_acquired = True
        store.initialize()
        result = refresh_watchlist_current_contracts(
            ReferenceActionDeps(
                reconcile_client_settings=lambda *_args: None,
                reconcile_gex_scheduler_settings=lambda *_args: {},
                reconcile_option_target_caps_settings=lambda *_args: {},
                data_provider_catalog=provider_catalog,
                search_provider_instruments=search_provider_instruments,
                bind_provider_instrument=lambda _provider, _contract_id: None,
                bind_provider_future_root=bind_provider_future_root,
                store_factory=lambda: store,
                refresh_quote_routes=lambda *_args, **_kwargs: None,
            ),
            providers=set(args.provider),
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if result.get("ok") else 1
    except CanonicalWriterLeaseUnavailable as exc:
        print(f"WRITER-LEASE-ERROR: {exc}", file=sys.stderr, flush=True)
        return 2
    finally:
        try:
            if lease_acquired:
                store.release_canonical_writer_lease()
        finally:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
