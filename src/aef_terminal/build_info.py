from __future__ import annotations

import json
import subprocess
from functools import lru_cache
from pathlib import Path

_BUILD_INFO_PATH = Path(__file__).with_name("_build_info.json")
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


@lru_cache(maxsize=1)
def build_info() -> dict[str, str]:
    if _BUILD_INFO_PATH.is_file():
        try:
            data = json.loads(_BUILD_INFO_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                version = str(data.get("version") or "unknown").strip() or "unknown"
                git_sha = str(data.get("git_sha") or version).strip() or version
                built_at = str(data.get("built_at") or "").strip()
                return {"version": version, "git_sha": git_sha, "built_at": built_at}
        except Exception:
            pass
    git_sha = _git_sha_short()
    version = f"dev-{git_sha}" if git_sha else "dev"
    return {"version": version, "git_sha": git_sha or "unknown", "built_at": ""}


def _git_sha_short() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except Exception:
        return ""


def git_worktree_info() -> dict[str, str | bool]:
    """Return exact local source provenance for offline research artifacts."""

    try:
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=_PROJECT_ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=normal"],
            cwd=_PROJECT_ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except OSError, subprocess.SubprocessError:
        return {"git_sha": "unknown", "git_dirty": True}
    if len(revision) != 40 or any(character not in "0123456789abcdef" for character in revision):
        return {"git_sha": "unknown", "git_dirty": True}
    return {"git_sha": revision, "git_dirty": bool(status.strip())}
