from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from aef_terminal.settings_contract import (
    OPTION_TARGET_CAPS_SETTING_KEY,
    require_option_target_caps,
)


@dataclass(frozen=True)
class OptionTargetRuntimeDeps:
    store_factory: Callable[[], Any]


class OptionTargetRuntime:
    def __init__(self) -> None:
        self._deps: OptionTargetRuntimeDeps | None = None
        self._lock = threading.RLock()
        self._caps: dict[str, float] | None = None
        self._settings_revision: int | None = None

    def configure(self, deps: OptionTargetRuntimeDeps) -> None:
        if not isinstance(deps, OptionTargetRuntimeDeps):
            raise TypeError("OptionTargetRuntimeDeps is required")
        with self._lock:
            self._deps = deps
            self._caps = None
            self._settings_revision = None

    @staticmethod
    def _require_revision(value: Any) -> int:
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or not 0 <= value <= 9_007_199_254_740_991
        ):
            raise ValueError("OPTION_TARGET_SETTINGS_REVISION_INVALID")
        return value

    def _publish_committed_locked(
        self,
        raw_caps: Any,
        *,
        settings_revision: int,
    ) -> dict[str, float]:
        revision = self._require_revision(settings_revision)
        caps = require_option_target_caps(raw_caps or {})
        if self._settings_revision is not None and revision < self._settings_revision:
            if self._caps is None:
                raise RuntimeError("OPTION_TARGET_SETTINGS_RUNTIME_UNAVAILABLE")
            return dict(self._caps)
        if self._settings_revision == revision and self._caps != caps:
            raise RuntimeError("OPTION_TARGET_SETTINGS_REVISION_MISMATCH")
        self._caps = dict(caps)
        self._settings_revision = revision
        return dict(caps)

    def caps_settings(self) -> dict[str, float]:
        with self._lock:
            if self._caps is not None:
                return dict(self._caps)
            store = self._store()
            raw, present, settings_revision = store.read_setting_snapshot(
                "server",
                OPTION_TARGET_CAPS_SETTING_KEY,
            )
            return self._publish_committed_locked(
                raw if present else None,
                settings_revision=settings_revision,
            )

    def save_caps_settings(self, caps: dict[str, float]) -> dict[str, float]:
        exact_caps = require_option_target_caps(caps)
        with self._lock:
            store = self._store()
            settings_revision = store.upsert_setting(
                "server",
                OPTION_TARGET_CAPS_SETTING_KEY,
                exact_caps,
            )
            return self._publish_committed_locked(
                exact_caps,
                settings_revision=settings_revision,
            )

    def publish_committed_snapshot(
        self,
        raw_caps: Any,
        present: bool,
        settings_revision: int,
    ) -> dict[str, float]:
        if not isinstance(present, bool):
            raise TypeError("OPTION_TARGET_SETTINGS_PRESENCE_INVALID")
        with self._lock:
            return self._publish_committed_locked(
                raw_caps if present else None,
                settings_revision=settings_revision,
            )

    def _store(self) -> Any:
        if self._deps is None:
            raise RuntimeError("OPTION_TARGET_RUNTIME_DEPS_REQUIRED")
        store = self._deps.store_factory()
        if store is None:
            raise RuntimeError("OPTION_TARGET_SETTINGS_STORAGE_REQUIRED")
        return store


_RUNTIME = OptionTargetRuntime()


def configure_option_target_runtime_deps(deps: OptionTargetRuntimeDeps) -> None:
    _RUNTIME.configure(deps)


def option_target_caps_settings() -> dict[str, float]:
    return _RUNTIME.caps_settings()


def save_option_target_caps_settings(caps: dict[str, float]) -> dict[str, float]:
    return _RUNTIME.save_caps_settings(caps)


def reconcile_option_target_caps_settings(
    raw_caps: Any,
    present: bool,
    settings_revision: int,
) -> dict[str, float]:
    return _RUNTIME.publish_committed_snapshot(
        raw_caps,
        present,
        settings_revision,
    )
