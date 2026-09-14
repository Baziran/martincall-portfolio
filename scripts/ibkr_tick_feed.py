from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from collections.abc import Callable
from typing import Any

from aef_terminal.config import AppConfig
from aef_terminal.data.ibkr_tick_feed import IbkrTickFeed
from aef_terminal.runtime.async_tasks import settle_physical_task
from aef_terminal.storage.postgres import CanonicalWriterLeaseUnavailable, PostgresStore


async def _settled_task(
    task: asyncio.Future[Any],
    *,
    completed: Callable[[], None] | None = None,
) -> Any:
    outcome = await settle_physical_task(task)
    if outcome.error is not None:
        raise outcome.error
    if outcome.task_cancelled:
        if outcome.cancellation is not None:
            raise outcome.cancellation
        raise RuntimeError("IBKR_TICK_FEED_PHYSICAL_TASK_CANCELLED")
    if completed is not None:
        completed()
    if outcome.cancellation is not None:
        raise outcome.cancellation
    return outcome.result


async def _settled_thread_call(
    operation: Callable[..., Any],
    /,
    *args: Any,
    completed: Callable[[], None] | None = None,
    **kwargs: Any,
) -> Any:
    task = asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
    return await _settled_task(task, completed=completed)


async def main() -> int:
    parser = argparse.ArgumentParser(description="Run IBKR tick-by-tick collector.")
    parser.add_argument("--instrument-id", action="append", required=True)
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--client-id", type=int, default=None)
    parser.add_argument(
        "--no-bidask", action="store_true", help="Use AllLast only; delta falls back to tick-test."
    )
    parser.add_argument("--flush-interval", type=float, default=1.0)
    parser.add_argument("--max-batch", type=int, default=500)
    parser.add_argument(
        "--max-flush-batches",
        type=int,
        default=20,
        help="Maximum micro-batches drained per flush wake-up.",
    )
    parser.add_argument("--max-buffer", type=int, default=100_000)
    parser.add_argument(
        "--retention-hours",
        type=float,
        default=None,
        help="Raw tick retention before rollup+purge.",
    )
    parser.add_argument(
        "--purge-interval", type=float, default=3600.0, help="Seconds between rollup+purge runs."
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    instrument_ids = [str(item).strip() for item in args.instrument_id if str(item).strip()]
    if not instrument_ids:
        parser.error("at least one exact provider-qualified --instrument-id is required")
    config = AppConfig()
    store = PostgresStore(config.database_url)
    lease_acquired = False
    feed_started = False
    feed_drained = True

    def mark_lease_acquired() -> None:
        nonlocal lease_acquired
        lease_acquired = True

    def mark_feed_drained() -> None:
        nonlocal feed_drained
        feed_drained = True

    try:
        try:
            await _settled_thread_call(
                store.acquire_canonical_writer_lease,
                owner="ibkr-tick-feed",
                completed=mark_lease_acquired,
            )
        except CanonicalWriterLeaseUnavailable as exc:
            logging.getLogger(__name__).error("WRITER-LEASE-ERROR: %s", exc)
            return 2
        await _settled_thread_call(store.initialize)
        instruments = [store.lookup_instrument(instrument_id) for instrument_id in instrument_ids]
        missing = [
            instrument_id
            for instrument_id, instrument in zip(instrument_ids, instruments, strict=True)
            if instrument is None
        ]
        if missing:
            raise ValueError(f"provider-qualified instruments are required: {', '.join(missing)}")
        feed = IbkrTickFeed(
            instruments=[instrument for instrument in instruments if isinstance(instrument, dict)],
            store=store,
            host=args.host,
            port=args.port,
            client_id=args.client_id,
            use_bidask=not args.no_bidask,
            flush_interval=args.flush_interval,
            max_batch=args.max_batch,
            max_flush_batches=args.max_flush_batches,
            max_buffer=args.max_buffer,
            retention_hours=args.retention_hours,
            purge_interval=args.purge_interval,
        )
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop_event.set)

        task = asyncio.create_task(feed.run_forever())
        feed_started = True
        feed_drained = False
        stop_wait = asyncio.create_task(stop_event.wait())
        try:
            await asyncio.wait(
                {task, stop_wait},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            if not stop_wait.done():
                stop_wait.cancel()
            await asyncio.gather(stop_wait, return_exceptions=True)
            feed.request_stop()
            await _settled_task(task, completed=mark_feed_drained)
        return 0
    finally:
        if lease_acquired and feed_started and not feed_drained:
            logging.getLogger(__name__).critical(
                "Canonical writer lease retained because tick-feed drain did not complete"
            )
        else:
            try:
                if lease_acquired:
                    await _settled_thread_call(store.release_canonical_writer_lease)
            finally:
                await _settled_thread_call(store.close)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
