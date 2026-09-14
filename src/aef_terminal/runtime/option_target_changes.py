from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass


_lock = threading.Lock()
_revision = 0


def option_targets_revision() -> int:
    """Invalidation token for the single canonical writer process, not stored data."""
    with _lock:
        return _revision


@dataclass
class OptionTargetMutation:
    changed: bool = False


@contextmanager
def option_target_mutation() -> Iterator[OptionTargetMutation]:
    """Wrap the connection/transaction so only a successful real commit invalidates reads."""
    mutation = OptionTargetMutation()
    yield mutation
    if mutation.changed:
        global _revision
        with _lock:
            _revision += 1
