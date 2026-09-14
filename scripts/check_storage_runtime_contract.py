#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aef_terminal.config import AppConfig
from aef_terminal.storage.postgres import CanonicalWriterCapability, PostgresStore


def main() -> int:
    database_url = AppConfig().database_url
    if not database_url:
        print("AEF_DATABASE_URL is required", file=sys.stderr)
        return 2
    store = PostgresStore(
        database_url,
        schema_owner=False,
        canonical_writer_capability=CanonicalWriterCapability.READ_ONLY,
    )
    try:
        store.verify_storage_runtime_contract()
    except Exception as exc:
        print(f"STORAGE-CONTRACT-ERROR: {exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        store.close()
    print("storage runtime contract: valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
