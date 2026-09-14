from __future__ import annotations

import argparse
import hashlib
import json
import os
import tarfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aef_terminal.config import AppConfig
from aef_terminal.research.journal_utils import fsync_directory


_DATASET_ROOTS = {
    "channel_interactions": Path("datasets/channel_interactions/raw/schema-v1"),
    "option_reversal": Path("datasets/option_reversal/raw/schema-v1"),
}


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def export_research_capture(
    *,
    data_root: Path,
    output_dir: Path | None = None,
    created_at: datetime | None = None,
) -> dict[str, Any]:
    if not isinstance(data_root, Path):
        raise TypeError("data_root must be a pathlib.Path")
    root = data_root.resolve()
    destination = (
        output_dir.resolve()
        if output_dir is not None
        else (root / "research" / "exports").resolve()
    )
    if not _is_within(destination, root):
        raise ValueError("research export output must remain below data_root")

    selected: list[tuple[str, Path]] = []
    counts: dict[str, int] = {}
    for dataset, relative_root in _DATASET_ROOTS.items():
        dataset_root = root / relative_root
        paths = sorted(dataset_root.rglob("*.json")) if dataset_root.exists() else []
        admitted: list[Path] = []
        for path in paths:
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"research export admits regular files only: {path}")
            resolved = path.resolve()
            if not _is_within(resolved, dataset_root.resolve()):
                raise ValueError(f"research export path escaped its dataset root: {path}")
            admitted.append(resolved)
            selected.append((dataset, resolved))
        counts[dataset] = len(admitted)
    if not selected:
        raise RuntimeError("no prospective research journal records are available to export")

    timestamp = (created_at or datetime.now(tz=UTC)).astimezone(UTC)
    stamp = timestamp.strftime("%Y%m%dT%H%M%S%fZ")
    archive_name = f"martincall-research-{stamp}.tar.gz"
    manifest_name = f"martincall-research-{stamp}.manifest.json"
    destination.mkdir(parents=True, exist_ok=True)
    archive_path = destination / archive_name
    manifest_path = destination / manifest_name
    if archive_path.exists() or manifest_path.exists():
        raise FileExistsError("research export timestamp already exists")

    temp_archive = destination / f".{archive_name}.tmp-{os.getpid()}-{uuid.uuid4().hex}"
    try:
        with tarfile.open(temp_archive, mode="w:gz") as archive:
            for _dataset, path in selected:
                archive.add(
                    path,
                    arcname=path.relative_to(root).as_posix(),
                    recursive=False,
                )
        temp_archive.chmod(0o644)
        os.replace(temp_archive, archive_path)
        fsync_directory(destination)
    finally:
        try:
            temp_archive.unlink()
        except FileNotFoundError:
            pass

    manifest = {
        "schema_version": 1,
        "created_at": timestamp.isoformat(),
        "archive": archive_name,
        "archive_sha256": _sha256(archive_path),
        "archive_bytes": archive_path.stat().st_size,
        "record_counts": counts,
        "records_total": len(selected),
        "source_roots": {
            dataset: relative_root.as_posix() for dataset, relative_root in _DATASET_ROOTS.items()
        },
    }
    manifest_bytes = (
        json.dumps(
            manifest,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )
    temp_manifest = destination / f".{manifest_name}.tmp-{os.getpid()}-{uuid.uuid4().hex}"
    try:
        descriptor = os.open(
            temp_manifest,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o644,
        )
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(manifest_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_manifest, manifest_path)
        archive_path.chmod(0o644)
        manifest_path.chmod(0o644)
        fsync_directory(destination)
    finally:
        try:
            temp_manifest.unlink()
        except FileNotFoundError:
            pass
    return {
        **manifest,
        "archive_path": str(archive_path),
        "manifest_path": str(manifest_path),
    }


def verify_research_export(manifest_path: Path) -> dict[str, Any]:
    if not isinstance(manifest_path, Path):
        raise TypeError("manifest_path must be a pathlib.Path")
    manifest_file = manifest_path.resolve()
    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("research export manifest is unreadable") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ValueError("research export manifest schema is unsupported")
    archive_name = manifest.get("archive")
    if (
        not isinstance(archive_name, str)
        or not archive_name
        or Path(archive_name).name != archive_name
    ):
        raise ValueError("research export archive name must be one local basename")
    archive_path = manifest_file.parent / archive_name
    expected_bytes = manifest.get("archive_bytes")
    if (
        isinstance(expected_bytes, bool)
        or not isinstance(expected_bytes, int)
        or expected_bytes < 0
        or archive_path.stat().st_size != expected_bytes
    ):
        raise ValueError("research export archive size mismatch")
    expected_sha256 = manifest.get("archive_sha256")
    if (
        not isinstance(expected_sha256, str)
        or len(expected_sha256) != 64
        or _sha256(archive_path) != expected_sha256
    ):
        raise ValueError("research export archive digest mismatch")

    counts = {dataset: 0 for dataset in _DATASET_ROOTS}
    with tarfile.open(archive_path, mode="r:gz") as archive:
        for member in archive.getmembers():
            member_path = Path(member.name)
            if member.name.startswith("/") or ".." in member_path.parts or not member.isfile():
                raise ValueError("research export contains an unsafe archive member")
            matched = False
            for dataset, relative_root in _DATASET_ROOTS.items():
                if _is_within(member_path, relative_root):
                    counts[dataset] += 1
                    matched = True
                    break
            if not matched:
                raise ValueError("research export contains an unknown dataset member")
    if manifest.get("record_counts") != counts or manifest.get("records_total") != sum(
        counts.values()
    ):
        raise ValueError("research export manifest record counts mismatch")
    return {
        "ok": True,
        "manifest_path": str(manifest_file),
        "archive_path": str(archive_path),
        "archive_sha256": expected_sha256,
        "record_counts": counts,
        "records_total": sum(counts.values()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create an immutable snapshot archive of prospective research journals.",
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        default=None,
        help="External MartinCall data root (defaults to AppConfig.data_root).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Output directory below data_root (defaults to research/exports).",
    )
    parser.add_argument(
        "--verify-manifest",
        type=Path,
        default=None,
        help="Verify one copied export manifest and its sibling archive instead of creating one.",
    )
    args = parser.parse_args()
    if args.verify_manifest is not None:
        print(
            json.dumps(
                verify_research_export(args.verify_manifest),
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    data_root = args.data_root or AppConfig().data_root
    result = export_research_capture(
        data_root=data_root,
        output_dir=args.output_dir,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
