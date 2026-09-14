from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from typing import Any

from aef_terminal.data.instrument_identity import require_exact_identity_text
from aef_terminal.domain import domain_frozen_value, domain_wire_value
from aef_terminal.paper_contract import (
    require_paper_contract_identity,
    require_paper_position_contract_identity,
)
from aef_terminal.runtime.async_tasks import run_physical_thread_call
from aef_terminal.runtime.metrics import increment_metric, set_metric


class PaperJournalRuntime:
    """Process-owned active paper-journal projection.

    PostgreSQL remains the durable authority. The projection loads all pending
    orders and open positions once, then reloads only after a committed journal
    mutation advances its generation.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._refresh_lock = threading.Lock()
        self._generation = 0
        self._loaded_generation = -1
        self._pending_orders: tuple[Mapping[str, Any], ...] = ()
        self._open_positions: tuple[Mapping[str, Any], ...] = ()

    @staticmethod
    def _freeze_rows(
        rows: Sequence[Mapping[str, Any]],
        *,
        entity: str,
        status: str,
    ) -> tuple[Mapping[str, Any], ...]:
        frozen_rows: list[Mapping[str, Any]] = []
        ids: set[str] = set()
        for row in rows:
            payload = dict(row)
            row_id = require_exact_identity_text(
                payload.get("id"),
                field=f"PAPER_{entity}_ID",
            )
            require_exact_identity_text(
                payload.get("instrument_id"),
                field=f"PAPER_{entity}_INSTRUMENT_ID",
            )
            require_exact_identity_text(
                payload.get("route_fingerprint"),
                field=f"PAPER_{entity}_ROUTE_FINGERPRINT",
            )
            if entity == "POSITION":
                require_paper_position_contract_identity(payload)
            else:
                require_paper_contract_identity(payload)
            if payload.get("status") != status:
                raise ValueError(f"PAPER_{entity}_RUNTIME_STATUS_INVALID: {row_id}")
            if row_id in ids:
                raise ValueError(f"PAPER_{entity}_RUNTIME_ID_DUPLICATE: {row_id}")
            frozen = domain_frozen_value(payload, field_name=f"paper_{entity.lower()}_runtime")
            if not isinstance(frozen, Mapping):
                raise TypeError(f"paper {entity.lower()} runtime must be an object")
            ids.add(row_id)
            frozen_rows.append(frozen)
        return tuple(frozen_rows)

    @staticmethod
    def _wire_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        wired: list[dict[str, Any]] = []
        for row in rows:
            payload = domain_wire_value(row, field_name="paper_journal_runtime")
            if not isinstance(payload, dict):
                raise TypeError("paper journal runtime must contain objects")
            wired.append(payload)
        return wired

    def mark_committed(self) -> int:
        """Invalidate the active projection after one real committed mutation."""

        with self._lock:
            self._generation += 1
            generation = self._generation
        set_metric("paper_journal_runtime_generation", generation)
        increment_metric("paper_journal_runtime_invalidations_total")
        return generation

    def _refresh_generation(self) -> int | None:
        with self._lock:
            if self._loaded_generation == self._generation:
                return None
            return self._generation

    def publish_snapshot(
        self,
        pending_orders: Sequence[Mapping[str, Any]],
        open_positions: Sequence[Mapping[str, Any]],
        *,
        generation: int,
    ) -> bool:
        frozen_orders = self._freeze_rows(
            pending_orders,
            entity="ORDER",
            status="pending",
        )
        frozen_positions = self._freeze_rows(
            open_positions,
            entity="POSITION",
            status="open",
        )
        with self._lock:
            if generation > self._generation:
                raise RuntimeError("PAPER_JOURNAL_RUNTIME_GENERATION_FUTURE")
            if generation != self._generation:
                increment_metric("paper_journal_runtime_stale_refreshes_total")
                return False
            self._pending_orders = frozen_orders
            self._open_positions = frozen_positions
            self._loaded_generation = generation
        set_metric("paper_journal_runtime_pending_orders", len(frozen_orders))
        set_metric("paper_journal_runtime_open_positions", len(frozen_positions))
        increment_metric("paper_journal_runtime_refreshes_total")
        return True

    def _refresh_snapshot_owned(self, store: Any) -> None:
        with self._refresh_lock:
            while (generation := self._refresh_generation()) is not None:
                pending_orders = store.read_paper_orders(
                    status="pending",
                    complete_pending=True,
                )
                open_positions = store.read_paper_positions(
                    status="open",
                    complete_open=True,
                )
                if self.publish_snapshot(
                    pending_orders,
                    open_positions,
                    generation=generation,
                ):
                    return

    async def active_snapshot(
        self, store: Any
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if self._refresh_generation() is not None:
            await run_physical_thread_call(self._refresh_snapshot_owned, store)
        with self._lock:
            pending = self._wire_rows(self._pending_orders)
            positions = self._wire_rows(self._open_positions)
        return pending, positions


paper_journal_runtime = PaperJournalRuntime()
