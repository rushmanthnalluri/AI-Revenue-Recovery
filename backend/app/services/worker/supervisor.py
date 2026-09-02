"""Asyncio supervisor for the in-process worker (docs/worker.md).

`WorkerSupervisor` paces the synchronous `Worker.tick` inside the app's
event loop (via `asyncio.to_thread`, so a slow tick never blocks request
handling) and records liveness for the `/api/v1/system/health` worker check.

The module-level `current()` registry is how the health endpoint (a plain
sync view, no request-scoped state) sees the running supervisor. The app
lifespan sets it on start and clears it on stop; tests may substitute a
stand-in via `set_current`.
"""

import asyncio
from datetime import datetime

from app.config import Settings
from app.db import utcnow
from app.logging import get_logger
from app.ports import PaymentGateway
from app.services.worker.senders import default_sender
from app.services.worker.worker import Worker

logger = get_logger(__name__)

_current: "WorkerSupervisor | None" = None


def current() -> "WorkerSupervisor | None":
    """The running supervisor, or None when the worker is stopped/disabled."""
    return _current


def set_current(supervisor: "WorkerSupervisor | None") -> None:
    """Registry seam used by the lifespan (and tests)."""
    global _current
    _current = supervisor


class WorkerSupervisor:
    """Owns the worker's asyncio task and its liveness stamps."""

    def __init__(self, worker: Worker, *, tick_seconds: float = 30.0) -> None:
        self._worker = worker
        self._tick_seconds = float(tick_seconds)
        self._task: asyncio.Task | None = None
        self._stop: asyncio.Event | None = None
        self.last_tick_at: datetime | None = None
        self.last_error: str | None = None
        self.tick_count: int = 0

    async def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("worker supervisor already started")
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="pulserecover-worker")
        set_current(self)
        logger.info(
            "worker started",
            extra={"tick_seconds": self._tick_seconds},
        )

    async def _run(self) -> None:
        assert self._stop is not None
        while not self._stop.is_set():
            # Stamped when the tick BEGINS: liveness means the loop is alive;
            # a failing tick body lands in last_error, not in a stale stamp.
            self.last_tick_at = utcnow()
            try:
                await asyncio.to_thread(self._worker.tick)
                self.last_error = None
            except Exception as exc:  # a bad tick must never kill the loop
                self.last_error = f"{type(exc).__name__}: {exc}"
                logger.exception("worker tick failed")
            self.tick_count += 1
            try:
                # Prompt shutdown: wakes immediately when stop() sets the event.
                await asyncio.wait_for(self._stop.wait(), timeout=self._tick_seconds)
            except asyncio.TimeoutError:
                pass

    async def stop(self, *, timeout: float = 10.0) -> None:
        if self._stop is not None:
            self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=timeout)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            self._task = None
        if current() is self:
            set_current(None)
        logger.info("worker stopped", extra={"ticks": self.tick_count})


def build_worker(
    settings: Settings,
    *,
    session_factory,
    gateway: PaymentGateway,
) -> Worker:
    """Wire the deployment Worker from settings (sender, cadences)."""
    return Worker(
        session_factory,
        gateway,
        sender=default_sender(settings),
        reconcile_seconds=settings.WORKER_RECONCILE_SECONDS,
        detection_seconds=settings.WORKER_DETECTION_SECONDS,
    )


async def start_worker(
    settings: Settings,
    *,
    session_factory,
    gateway: PaymentGateway,
) -> WorkerSupervisor:
    """Build, start, and register the worker; returns the live supervisor."""
    supervisor = WorkerSupervisor(
        build_worker(settings, session_factory=session_factory, gateway=gateway),
        tick_seconds=settings.WORKER_TICK_SECONDS,
    )
    await supervisor.start()
    return supervisor


__all__ = [
    "WorkerSupervisor",
    "build_worker",
    "current",
    "set_current",
    "start_worker",
]
