from __future__ import annotations

import asyncio
import json
import logging
import os
import struct
import subprocess
import sys
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from time import perf_counter
from typing import Any

from aef_terminal.features.provider_session import provider_session_intervals
from aef_terminal.runtime.metrics import increment_metric, observe_metric
from aef_terminal.ui.services.market_analysis_compaction import (
    compact_analysis_snapshot_for_cache,
)
from aef_terminal.ui.services.market_snapshot_enrichment import enrich_market_analysis_snapshot


MARKET_ANALYSIS_PROCESS_TIMEOUT_SECONDS = float(
    os.getenv("AEF_MARKET_ANALYSIS_PROCESS_TIMEOUT_SECONDS", "45.0")
)
_LOGGER = logging.getLogger(__name__)
_PROCESS_RESULT_HEADER = struct.Struct("!QQ")
_PROCESS_REQUEST_HEADER = struct.Struct("!Q")
_PROCESS_ENVELOPE_HEADER = struct.Struct("!BQ")
_PROCESS_ENVELOPE_OK = 0
_PROCESS_ENVELOPE_ERROR = 1
_PROCESS_READY = b"AEF-MARKET-ANALYSIS/1\n"
_PROCESS_START_TIMEOUT_SECONDS = 10.0


class _MarketAnalysisWorkerJobError(RuntimeError):
    pass


class MarketAnalysisWorkerUnavailable(RuntimeError):
    error_code = "MARKET_ANALYSIS_WORKER_UNAVAILABLE"
    retryable = False

    def __init__(self, message: str, *, outcome: str = "unavailable") -> None:
        super().__init__(message)
        self.outcome = outcome


@dataclass(frozen=True)
class MarketAnalysisProcessResult:
    snapshot_bytes: bytes
    research_capture_bytes: bytes | None = None


class MarketAnalysisRequestController:
    """Loop-owned cancellation settlement for one persistent-worker request."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[Any] | None = None
        self._cancel_requested = False
        self._finished = asyncio.Event()

    def _require_loop(self) -> asyncio.AbstractEventLoop:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise RuntimeError("market analysis request controller crossed event loops")
        return loop

    def _claim_current_task(self) -> None:
        self._require_loop()
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("market analysis request requires an asyncio task owner")
        if self._task is not None and self._task is not task:
            raise RuntimeError("market analysis request controller already has an owner")
        self._task = task
        # Clear the marker before the logical request enters the persistent
        # worker. Cancellation waiters must observe its physical settlement.
        self._finished.clear()

    def _cancellation_requested(self) -> bool:
        self._require_loop()
        return self._cancel_requested

    def _mark_finished(self) -> None:
        self._require_loop()
        self._finished.set()

    def request_cancellation(self) -> bool:
        """Fence publication and report whether physical work must settle."""

        self._require_loop()
        self._cancel_requested = True
        return self._task is not None

    async def wait_finished(self) -> None:
        self._require_loop()
        await self._finished.wait()


_MARKET_ANALYSIS_REQUEST_CONTROLLER: ContextVar[MarketAnalysisRequestController | None] = (
    ContextVar(
        "market_analysis_request_controller",
        default=None,
    )
)


@contextmanager
def bind_market_analysis_request_controller(
    controller: MarketAnalysisRequestController,
) -> Iterator[None]:
    """Bind the request-settlement controller inherited by the child task."""

    if not isinstance(controller, MarketAnalysisRequestController):
        raise TypeError("market analysis request controller has invalid type")
    token = _MARKET_ANALYSIS_REQUEST_CONTROLLER.set(controller)
    try:
        yield
    finally:
        _MARKET_ANALYSIS_REQUEST_CONTROLLER.reset(token)


def _market_analysis_research_capture(
    snapshot: dict[str, Any],
    *,
    instrument: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    meta = snapshot.get("meta")
    indicators = snapshot.get("indicators")
    if not isinstance(meta, dict) or not isinstance(indicators, dict):
        return None
    captured_indicators: dict[str, Any] = {}
    channel = indicators.get("channel_master")
    channel_observations = (
        channel.get("research_observations") if isinstance(channel, dict) else None
    )
    if isinstance(channel_observations, list) and channel_observations:
        captured_channel_observations: list[dict[str, Any]] = []
        for observation in channel_observations:
            if not isinstance(observation, dict):
                continue
            captured = dict(observation)
            decision_session = _provider_decision_session(
                observation,
                instrument,
            )
            if decision_session is not None:
                captured["decision_session"] = decision_session
            captured_channel_observations.append(captured)
    else:
        captured_channel_observations = []
    if captured_channel_observations:
        captured_indicators["channel_master"] = {
            "research_observations": captured_channel_observations,
        }
    option_reversal = indicators.get("option_reversal")
    option_observation = (
        option_reversal.get("research_observation") if isinstance(option_reversal, dict) else None
    )
    if isinstance(option_observation, dict):
        captured_indicators["option_reversal"] = {
            "research_observation": option_observation,
        }
    if not captured_indicators:
        return None
    identity_fields = (
        "instrument_id",
        "route_fingerprint",
        "provider",
        "provider_contract_id",
        "provider_symbol",
        "timeframe",
    )
    return {
        "meta": {field: meta[field] for field in identity_fields if field in meta},
        "indicators": captured_indicators,
    }


def _provider_decision_session(
    observation: dict[str, Any],
    instrument: dict[str, Any] | None,
) -> dict[str, Any] | None:
    session = instrument.get("session") if isinstance(instrument, dict) else None
    if not isinstance(session, dict):
        return None
    micro_bar = observation.get("micro_bar")
    raw_session_ts = (
        micro_bar.get("ts") if isinstance(micro_bar, dict) else observation.get("decision_ts")
    )
    if not isinstance(raw_session_ts, str) or not raw_session_ts:
        return None
    try:
        session_ts = datetime.fromisoformat(raw_session_ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if session_ts.tzinfo is None or session_ts.utcoffset() is None:
        return None
    calendar = str(session.get("calendar") or "unknown")
    timezone = str(session.get("timezone") or "unknown")
    if calendar.strip().lower() == "continuous_24_7":
        return {
            "contract": "provider-session-decision-v1",
            "state": "known",
            "source": "provider_schedule",
            "calendar": calendar,
            "timezone": timezone,
            "session_key": session_ts.astimezone(UTC).date().isoformat(),
            "segment": "continuous",
        }
    trading = [
        interval
        for interval in provider_session_intervals(
            instrument,
            "trading_intervals",
        )
        if interval.contains(session_ts)
    ]
    liquid = [
        interval
        for interval in provider_session_intervals(
            instrument,
            "liquid_intervals",
        )
        if interval.contains(session_ts)
    ]
    session_keys = {
        interval.session_key for interval in trading if interval.session_key is not None
    }
    if not trading or len(session_keys) != 1:
        return {
            "contract": "provider-session-decision-v1",
            "state": "unknown",
            "source": "provider_schedule",
            "calendar": calendar,
            "timezone": timezone,
            "reason_code": (
                "decision_outside_provider_trading_intervals"
                if not trading
                else "provider_session_intervals_ambiguous"
            ),
        }
    return {
        "contract": "provider-session-decision-v1",
        "state": "known",
        "source": "provider_schedule",
        "calendar": calendar,
        "timezone": timezone,
        "session_key": next(iter(session_keys)),
        "segment": "liquid" if liquid else "extended",
    }


def build_market_analysis_process_result(
    key: str,
    payload: dict[str, Any],
) -> MarketAnalysisProcessResult:
    from aef_terminal.config import AppConfig
    from aef_terminal.storage.postgres import (
        CanonicalWriterCapability,
        PostgresStore,
    )
    from aef_terminal.engine.snapshot.builder import build_market_snapshot_from_db

    parent_canonical_generation = payload.get("parent_canonical_generation")
    if (
        isinstance(parent_canonical_generation, bool)
        or not isinstance(parent_canonical_generation, int)
        or parent_canonical_generation < 0
    ):
        raise ValueError("parent_canonical_generation must be a non-negative integer")
    raw_context_generations = payload.get("confirmed_bar_context_generations", {})
    if not isinstance(raw_context_generations, dict):
        raise ValueError("confirmed_bar_context_generations must be an object")
    context_generations: dict[str, int] = {}
    for timeframe, generation in sorted(raw_context_generations.items()):
        if (
            not isinstance(timeframe, str)
            or not timeframe
            or timeframe != timeframe.strip()
            or isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation < 0
        ):
            raise ValueError("confirmed bar context generation is invalid")
        context_generations[timeframe] = generation

    config = AppConfig()
    store = (
        PostgresStore(
            config.database_url,
            schema_owner=False,
            canonical_writer_capability=CanonicalWriterCapability.READ_ONLY,
        )
        if config.database_url
        else None
    )
    try:
        signal_range = payload["signal_range"]
        analysis_as_of_utc = datetime.now(UTC)
        snapshot = build_market_snapshot_from_db(
            source=payload["source"],
            interval=payload["interval"],
            # The chart checkpoint already owns the viewport projection and its
            # provider-session axis. This subprocess produces only the analysis
            # projection, so rebuilding chart-range context here duplicates
            # work discarded by compact_analysis_snapshot_for_cache().
            range_=signal_range,
            signal_range_=signal_range,
            show_visuals=payload["show_visuals"],
            include_chart_projection=False,
            indicator_params=payload["indicator_params"],
            gex_context_active=payload.get("gex_context_active", False),
            gex_capture_mode=payload.get("gex_capture_mode", "request"),
            include_telemetry=bool(payload.get("include_telemetry", False)),
            instrument=payload.get("instrument")
            if isinstance(payload.get("instrument"), dict)
            else None,
            store=store,
            analysis_as_of_utc=analysis_as_of_utc,
        )
        snapshot.setdefault("meta", {})
        snapshot["meta"]["analysis_key"] = key
        snapshot["meta"]["analysis_status"] = "ready"
        snapshot["meta"]["analysis_parent_canonical_revision"] = parent_canonical_generation
        snapshot["meta"]["analysis_confirmed_bar_context_revisions"] = context_generations
        enrich_market_analysis_snapshot(snapshot)
        research_capture = _market_analysis_research_capture(
            snapshot,
            instrument=(
                payload.get("instrument") if isinstance(payload.get("instrument"), dict) else None
            ),
        )
        snapshot = compact_analysis_snapshot_for_cache(snapshot)
        return MarketAnalysisProcessResult(
            snapshot_bytes=json.dumps(
                snapshot,
                separators=(",", ":"),
            ).encode("utf-8"),
            research_capture_bytes=(
                json.dumps(
                    research_capture,
                    separators=(",", ":"),
                ).encode("utf-8")
                if research_capture is not None
                else None
            ),
        )
    finally:
        if store is not None:
            store.close()


def _encode_market_analysis_process_result(
    result: MarketAnalysisProcessResult,
) -> bytes:
    research_capture = result.research_capture_bytes or b""
    return b"".join(
        (
            _PROCESS_RESULT_HEADER.pack(
                len(result.snapshot_bytes),
                len(research_capture),
            ),
            result.snapshot_bytes,
            research_capture,
        )
    )


def _decode_market_analysis_process_result(
    value: bytes,
) -> MarketAnalysisProcessResult:
    if len(value) < _PROCESS_RESULT_HEADER.size:
        raise ValueError("market analysis process result is truncated")
    snapshot_size, research_size = _PROCESS_RESULT_HEADER.unpack_from(value)
    expected_size = _PROCESS_RESULT_HEADER.size + snapshot_size + research_size
    if len(value) != expected_size:
        raise ValueError("market analysis process result has invalid framing")
    snapshot_start = _PROCESS_RESULT_HEADER.size
    research_start = snapshot_start + snapshot_size
    return MarketAnalysisProcessResult(
        snapshot_bytes=value[snapshot_start:research_start],
        research_capture_bytes=(value[research_start:] if research_size else None),
    )


@dataclass
class _AnalysisProcessWorker:
    process: asyncio.subprocess.Process
    stderr_task: asyncio.Task[None] | None = None
    stderr_tail: str = ""
    busy: bool = False


class MarketAnalysisProcessRuntime:
    """Own a bounded, prestarted pool of read-only analysis processes."""

    def __init__(self, *, worker_count: int | None = None) -> None:
        if worker_count is None:
            worker_count = int(os.getenv("AEF_MARKET_ANALYSIS_WORKERS", "1"))
        if type(worker_count) is not int or not 1 <= worker_count <= 8:
            raise ValueError("AEF_MARKET_ANALYSIS_WORKERS must be an integer from 1 to 8")
        self.worker_count = worker_count
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock: asyncio.Lock | None = None
        self._available: asyncio.Condition | None = None
        self._workers: list[_AnalysisProcessWorker] = []
        self._operations: set[asyncio.Task[MarketAnalysisProcessResult]] = set()
        self._started = False
        self._failed = False
        self._stopped = False

    def _require_loop(self) -> tuple[asyncio.AbstractEventLoop, asyncio.Lock]:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
            self._lock = asyncio.Lock()
            self._available = asyncio.Condition()
        elif self._loop is not loop:
            raise RuntimeError("MARKET_ANALYSIS_PROCESS_RUNTIME_LOOP_MISMATCH")
        if self._lock is None:
            raise RuntimeError("MARKET_ANALYSIS_PROCESS_RUNTIME_LOCK_REQUIRED")
        return loop, self._lock

    async def start(self) -> None:
        loop, lock = self._require_loop()
        async with lock:
            if (
                self._failed
                or self._stopped
                or any(worker.process.returncode is not None for worker in self._workers)
            ):
                raise MarketAnalysisWorkerUnavailable(
                    "market analysis workers require an application restart"
                )
            if self._started:
                return
            try:
                for index in range(self.worker_count):
                    process = await asyncio.create_subprocess_exec(
                        sys.executable,
                        "-m",
                        "aef_terminal.ui.services.market_analysis_process",
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                    worker = _AnalysisProcessWorker(process)
                    self._workers.append(worker)
                    if process.stdin is None or process.stdout is None or process.stderr is None:
                        raise RuntimeError("MARKET_ANALYSIS_PROCESS_PIPES_REQUIRED")
                    worker.stderr_task = loop.create_task(
                        self._drain_stderr(worker),
                        name=f"market-analysis-process-stderr-{index}",
                    )
                    ready = await asyncio.wait_for(
                        process.stdout.readexactly(len(_PROCESS_READY)),
                        timeout=_PROCESS_START_TIMEOUT_SECONDS,
                    )
                    if ready != _PROCESS_READY:
                        raise RuntimeError("MARKET_ANALYSIS_PROCESS_READY_INVALID")
            except BaseException:
                self._failed = True
                await asyncio.gather(*(self._terminate(worker) for worker in self._workers))
                raise
            self._started = True

    async def execute(
        self,
        request: bytes,
        *,
        timeout_seconds: float,
    ) -> MarketAnalysisProcessResult:
        loop, _lock = self._require_loop()
        operation = loop.create_task(
            self._execute_admitted(request, timeout_seconds=timeout_seconds),
            name="market-analysis-process-exchange",
        )
        self._operations.add(operation)
        try:
            return await asyncio.shield(operation)
        except asyncio.CancelledError as cancellation:
            # Repeated cancellation must not abandon a physical exchange or let
            # its worker accept another frame before the response is consumed.
            while not operation.done():
                try:
                    await asyncio.shield(operation)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            try:
                operation.result()
            except BaseException as exc:
                cancellation.add_note(f"analysis worker settlement failed: {exc}")
            raise cancellation
        finally:
            self._operations.discard(operation)

    def _require_available_runtime(self) -> None:
        if any(worker.process.returncode is not None for worker in self._workers):
            self._failed = True
        if self._failed or self._stopped or not self._started:
            raise MarketAnalysisWorkerUnavailable(
                "market analysis workers are unavailable until application restart"
            )

    async def _execute_admitted(
        self,
        request: bytes,
        *,
        timeout_seconds: float,
    ) -> MarketAnalysisProcessResult:
        self._require_loop()
        assert self._available is not None
        queued_at = perf_counter()
        async with self._available:
            while True:
                self._require_available_runtime()
                worker = next((item for item in self._workers if not item.busy), None)
                if worker is not None:
                    worker.busy = True
                    break
                await self._available.wait()
        observe_metric("market_analysis_queue_seconds", perf_counter() - queued_at)
        started = perf_counter()
        try:
            try:
                result = await asyncio.wait_for(
                    self._exchange(worker.process, request),
                    timeout=max(float(timeout_seconds), 1.0),
                )
            except TimeoutError as exc:
                await self._fail_worker(worker)
                raise MarketAnalysisWorkerUnavailable(
                    f"market analysis worker timed out after {timeout_seconds:.1f}s; "
                    "application restart required",
                    outcome="timeout",
                ) from exc
            except asyncio.CancelledError:
                raise
            except _MarketAnalysisWorkerJobError:
                raise
            except Exception as exc:
                await self._fail_worker(worker)
                detail = worker.stderr_tail[-2000:].strip()
                raise MarketAnalysisWorkerUnavailable(
                    detail or "market analysis worker failed; application restart required"
                ) from exc
            # A sibling's fatal error also fences this result. No partial pool
            # recovery or silent replacement beside provider threads is allowed.
            if self._failed:
                raise MarketAnalysisWorkerUnavailable(
                    "market analysis worker pool failed; application restart required"
                )
            return result
        finally:
            observe_metric("market_analysis_execution_seconds", perf_counter() - started)
            async with self._available:
                worker.busy = False
                self._available.notify_all()

    async def _exchange(
        self,
        process: asyncio.subprocess.Process,
        request: bytes,
    ) -> MarketAnalysisProcessResult:
        if process.stdin is None or process.stdout is None:
            raise RuntimeError("MARKET_ANALYSIS_PROCESS_PIPES_REQUIRED")
        process.stdin.write(_PROCESS_REQUEST_HEADER.pack(len(request)) + request)
        await process.stdin.drain()
        header = await process.stdout.readexactly(_PROCESS_ENVELOPE_HEADER.size)
        status, payload_size = _PROCESS_ENVELOPE_HEADER.unpack(header)
        payload = await process.stdout.readexactly(payload_size)
        if status == _PROCESS_ENVELOPE_OK:
            return _decode_market_analysis_process_result(payload)
        if status == _PROCESS_ENVELOPE_ERROR:
            raise _MarketAnalysisWorkerJobError(payload.decode("utf-8", errors="replace")[-4000:])
        raise RuntimeError("MARKET_ANALYSIS_PROCESS_ENVELOPE_INVALID")

    async def _drain_stderr(self, worker: _AnalysisProcessWorker) -> None:
        process = worker.process
        if process.stderr is None:
            return
        while line := await process.stderr.readline():
            decoded = line.decode("utf-8", errors="replace")
            worker.stderr_tail = (worker.stderr_tail + decoded)[-4000:]
            _LOGGER.warning("market analysis worker %s stderr: %s", process.pid, decoded.rstrip())

    async def _fail_worker(self, worker: _AnalysisProcessWorker) -> None:
        self._failed = True
        assert self._available is not None
        async with self._available:
            self._available.notify_all()
        await self._terminate(worker)

    async def _terminate(self, worker: _AnalysisProcessWorker) -> None:
        process = worker.process
        if process.returncode is None:
            process.kill()
        await process.wait()
        if worker.stderr_task is not None:
            await asyncio.gather(worker.stderr_task, return_exceptions=True)

    async def shutdown(self) -> None:
        if self._loop is None:
            return
        _loop, lock = self._require_loop()
        async with lock:
            self._stopped = True
            assert self._available is not None
            async with self._available:
                self._available.notify_all()
            await asyncio.gather(*tuple(self._operations), return_exceptions=True)
            for worker in self._workers:
                process = worker.process
                if process.stdin is not None:
                    process.stdin.close()
                    try:
                        await process.stdin.wait_closed()
                    except BrokenPipeError, ConnectionResetError:
                        pass
                try:
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except TimeoutError:
                    await self._terminate(worker)
                if worker.stderr_task is not None:
                    await asyncio.gather(worker.stderr_task, return_exceptions=True)
            self._workers.clear()

    def diagnostics(self) -> dict[str, Any]:
        if self._failed:
            status = "failed"
        elif self._stopped:
            status = "stopped"
        elif not self._started:
            status = "not_started"
        elif any(worker.process.returncode is not None for worker in self._workers):
            status = "exited"
        else:
            status = "running"
        return {
            "status": status,
            "worker_count": self.worker_count,
            "busy_workers": sum(worker.busy for worker in self._workers),
            "workers": [
                {
                    "pid": worker.process.pid,
                    "returncode": worker.process.returncode,
                    "busy": worker.busy,
                }
                for worker in self._workers
            ],
            "restart_required": self._failed or self._stopped or status == "exited",
        }


_PROCESS_RUNTIME = MarketAnalysisProcessRuntime()


async def start_market_analysis_process_runtime() -> None:
    await _PROCESS_RUNTIME.start()


async def shutdown_market_analysis_process_runtime() -> None:
    await _PROCESS_RUNTIME.shutdown()


def market_analysis_process_runtime_diagnostics() -> dict[str, Any]:
    return _PROCESS_RUNTIME.diagnostics()


def market_analysis_process_worker_count() -> int:
    return _PROCESS_RUNTIME.worker_count


async def run_market_analysis_process(
    key: str,
    payload: dict[str, Any],
    *,
    timeout_seconds: float = MARKET_ANALYSIS_PROCESS_TIMEOUT_SECONDS,
) -> MarketAnalysisProcessResult:
    started = perf_counter()
    outcome = "error"
    metric_labels = {
        "source": str(payload.get("source") or "unknown"),
        "interval": str(payload.get("interval") or "unknown"),
        "gex_active": str(payload.get("gex_context_active") is True).lower(),
    }
    controller = _MARKET_ANALYSIS_REQUEST_CONTROLLER.get()
    if controller is not None:
        controller._claim_current_task()
    request = json.dumps(
        {"key": key, "payload": payload},
        separators=(",", ":"),
    ).encode("utf-8")
    try:
        if controller is not None and controller._cancellation_requested():
            raise asyncio.CancelledError()
        result = await _PROCESS_RUNTIME.execute(
            request,
            timeout_seconds=timeout_seconds,
        )
        outcome = "ok"
        return result
    except asyncio.CancelledError:
        outcome = "cancelled"
        raise
    except MarketAnalysisWorkerUnavailable as exc:
        outcome = exc.outcome
        raise
    finally:
        increment_metric(
            "market_analysis_process_total",
            status=outcome,
            **metric_labels,
        )
        observe_metric(
            "market_analysis_process_seconds",
            perf_counter() - started,
            status=outcome,
            **metric_labels,
        )
        if controller is not None:
            controller._mark_finished()


def _main() -> int:
    sys.stdout.buffer.write(_PROCESS_READY)
    sys.stdout.buffer.flush()
    while header := sys.stdin.buffer.read(_PROCESS_REQUEST_HEADER.size):
        if len(header) != _PROCESS_REQUEST_HEADER.size:
            raise ValueError("market analysis request header is truncated")
        (request_size,) = _PROCESS_REQUEST_HEADER.unpack(header)
        request_bytes = sys.stdin.buffer.read(request_size)
        if len(request_bytes) != request_size:
            raise ValueError("market analysis request is truncated")
        try:
            request = json.loads(request_bytes.decode("utf-8"))
            result = build_market_analysis_process_result(
                str(request["key"]),
                request["payload"],
            )
            response = _encode_market_analysis_process_result(result)
            status = _PROCESS_ENVELOPE_OK
        except Exception:
            response = traceback.format_exc().encode("utf-8", errors="replace")
            status = _PROCESS_ENVELOPE_ERROR
        sys.stdout.buffer.write(_PROCESS_ENVELOPE_HEADER.pack(status, len(response)))
        sys.stdout.buffer.write(response)
        sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
