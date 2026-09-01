"""In-process worker tier — the monolith's scheduler (docs/worker.md).

Public surface:

    from app.services.worker import Worker, WorkerSupervisor, start_worker

    worker = Worker(session_factory, gateway)          # synchronous engine
    report = worker.tick()                             # one pass over all units

    supervisor = await start_worker(                   # lifespan wiring
        settings, session_factory=SessionLocal, gateway=get_gateway(settings)
    )
    await supervisor.stop()

The app lifespan (app.main) starts the supervisor only when
WORKER_ENABLED=true; tests drive `Worker.tick` directly with an injected
clock. See ADR 0011 (P2) and docs/worker.md for the design contract.
"""

from app.services.worker.senders import (
    LoggingNotificationSender,
    NotificationDeliveryError,
    RazorpayNotesNotificationSender,
    default_sender,
)
from app.services.worker.supervisor import (
    WorkerSupervisor,
    build_worker,
    current,
    set_current,
    start_worker,
)
from app.services.worker.worker import (
    NOTIFICATION_MAX_ATTEMPTS,
    NOTIFICATION_RETRY_BASE_SECONDS,
    TickReport,
    Worker,
    WORKER_ACTOR,
)

__all__ = [
    "LoggingNotificationSender",
    "NOTIFICATION_MAX_ATTEMPTS",
    "NOTIFICATION_RETRY_BASE_SECONDS",
    "NotificationDeliveryError",
    "RazorpayNotesNotificationSender",
    "TickReport",
    "WORKER_ACTOR",
    "Worker",
    "WorkerSupervisor",
    "build_worker",
    "current",
    "default_sender",
    "set_current",
    "start_worker",
]
