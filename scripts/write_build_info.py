#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import tomllib
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "src" / "aef_terminal" / "_build_info.json"


def main() -> int:
    git_sha = str(os.environ.get("BUILD_GIT_SHA") or "").strip()
    if not git_sha or git_sha == "unknown":
        raise RuntimeError("BUILD_GIT_SHA must identify the source revision")
    built_at = str(os.environ.get("BUILD_DATE", "") or "").strip()
    if not built_at:
        built_at = datetime.now(tz=UTC).replace(microsecond=0).isoformat()
    version = str(os.environ.get("BUILD_VERSION", "") or "").strip()
    if not version:
        with (ROOT / "pyproject.toml").open("rb") as handle:
            version = str(tomllib.load(handle)["project"]["version"]).strip()
    if not version:
        raise RuntimeError("Project version must not be empty")
    payload = {"version": version, "git_sha": git_sha, "built_at": built_at}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        f"MartinCall image build: version={version} built_at={built_at} git={git_sha}", flush=True
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
