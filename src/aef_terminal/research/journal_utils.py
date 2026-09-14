from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any, Literal

from aef_terminal.config import AppConfig


def dataset_root(*, data_root: Path | None, relative_root: Path) -> Path:
    configured_root = AppConfig().data_root if data_root is None else data_root
    if not isinstance(configured_root, Path):
        raise TypeError("data_root must be a pathlib.Path")
    return configured_root / relative_root


def canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )


def fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def publish_create_once(
    path: Path,
    data: bytes,
) -> Literal["created", "identical", "conflict"]:
    """Atomically publish one immutable file without replacing a winner."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            return "identical" if path.read_bytes() == data else "conflict"
        fsync_directory(path.parent)
        return "created"
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
