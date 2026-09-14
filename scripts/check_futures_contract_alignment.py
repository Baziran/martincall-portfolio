#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aef_terminal.data.ibkr.quotes import cached_quotes
from aef_terminal.data.instrument_identity import require_exact_identity_text
from aef_terminal.data.providers import read_recent_provider_bars, route_instrument
from aef_terminal.config import AppConfig
from aef_terminal.storage.postgres import CanonicalWriterCapability, PostgresStore


def parse_instrument_ids(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare provider-native IBKR continuous history vs the provider-current exact contract."
    )
    parser.add_argument(
        "--instrument-ids",
        type=parse_instrument_ids,
        required=True,
    )
    parser.add_argument("--interval", default="5m")
    parser.add_argument("--provider", default="ibkr")
    args = parser.parse_args()

    store = PostgresStore(
        AppConfig().database_url,
        schema_owner=False,
        canonical_writer_capability=CanonicalWriterCapability.READ_ONLY,
    )
    store.initialize()
    now = datetime.now(tz=UTC)
    print(f"checked_at={now.isoformat()}")
    for instrument_id in args.instrument_ids:
        instrument = store.lookup_instrument(instrument_id, watchlist_only=False)
        if not isinstance(instrument, dict):
            print(f"\n{instrument_id}")
            print(
                f"  ERROR INSTRUMENT_IDENTITY_REQUIRED provider={args.provider} instrument_id={instrument_id}"
            )
            continue
        route = route_instrument(instrument, expected_source=args.provider)
        symbol = route.instrument_key
        quote = cached_quotes([route]).quotes.get(route.fingerprint)
        if not isinstance(quote, dict):
            raise RuntimeError(
                "FUTURES_QUOTE_CONTRACT_REQUIRED "
                f"provider={route.provider} instrument_id={route.instrument_id}"
            )
        quote_month = require_exact_identity_text(
            quote.get("contract_month"),
            field="quote.contract_month",
        )
        quote_local = require_exact_identity_text(
            quote.get("local_symbol"),
            field="quote.local_symbol",
        )
        current_contract = store.read_current_futures_contract(
            provider=args.provider,
            instrument_id=route.instrument_id,
        )
        print(f"\n{symbol}")
        if current_contract:
            stored_local_symbol = require_exact_identity_text(
                current_contract.get("local_symbol"),
                field="current_contract.local_symbol",
            )
            stored_contract_id = require_exact_identity_text(
                current_contract.get("provider_contract_id"),
                field="current_contract.provider_contract_id",
            )
            resolved_at = require_exact_identity_text(
                current_contract.get("resolved_at"),
                field="current_contract.resolved_at",
            )
            print(
                f"  stored_current={stored_local_symbol} "
                f"provider_contract_id={stored_contract_id} "
                f"resolved_at={resolved_at}"
            )
        else:
            print(
                f"  ERROR FUTURES_CURRENT_CONTRACT_UNRESOLVED provider={args.provider} "
                f"instrument_id={route.instrument_id}"
            )
        print(f"  quote_contract={quote_local} month={quote_month}")
        contract_bars = store.read_latest_futures_contract_bar_boundaries(
            provider=route.provider,
            instrument_id=route.instrument_id,
            route_fingerprint=route.fingerprint,
            timeframe=args.interval,
            limit=8,
        )
        canonical_route = route.adapter.canonical_history_route(route.instrument)
        if canonical_route is None:
            raise RuntimeError("FUTURES_CANONICAL_HISTORY_ROUTE_REQUIRED")
        canonical_bars = read_recent_provider_bars(
            route,
            args.interval,
            store=store,
            limit=1,
        )
        if canonical_bars:
            latest = canonical_bars[-1]
            print(
                f"  canonical provider={route.provider} "
                f"series={canonical_route.series_type}/{canonical_route.roll_policy} "
                f"latest={latest.ts.isoformat()} close={float(latest.close)} "
                f"source={latest.source}"
            )
        else:
            print(
                f"  canonical provider={route.provider} "
                f"series={canonical_route.series_type}/{canonical_route.roll_policy} rows=0"
            )
        if contract_bars:
            print(
                "  raw_contracts="
                + ", ".join(f"{key}@{ts.isoformat()}" for key, ts in contract_bars)
            )
        else:
            print("  raw_contracts=none")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
