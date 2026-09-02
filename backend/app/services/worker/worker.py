"""In-process worker: the monolith's scheduler tier (docs/worker.md).

One `Worker` drives four due-driven units of work per tick:

1. **Delayed retries** — recovery_actions parked in SCHEDULED by the
   executor (strategy `constraints.delay_seconds`) are fired through the
   executor's normal `execute()` path once due: same policy re-gate at fire
   time, same find-or-create/row-lock guards, same audit trail. Exactly one
   gateway mutation per action, ever — firing leaves SCHEDULED, so a later
   tick never re-fires.
2. **Notification outbox** — PENDING notification_outbox rows past their
   `due_at` are delivered via the NotificationSender port. A failed attempt
   is retried with linear backoff up to NOTIFICATION_MAX_ATTEMPTS, then the
   row is FAILED — surfaced, never silently dropped.
3. **Scheduled reconciliation** — the ADR 0011 sweep
   (`run_reconciliation`, reused as-is) runs on the configured cadence
   (WORKER_RECONCILE_SECONDS, default 15 min), and always once on the first
   tick after startup.
4. **Scheduled detection** — one detection pass over the REAL_TEST
   environment (the real merchant's payment_events stream, fed by sync and
   webhooks) through the same `run_detection` entry point the API uses,
   on the configured cadence (WORKER_DETECTION_SECONDS, default 5 min) and
   always once on the first tick after startup. The research environment
   stays on-demand (demo/eval surfaces) — the worker never scores it.

This class is deliberately synchronous and asyncio-free; `WorkerSupervisor`
(app.services.worker.supervisor) paces `tick()` inside the app's event loop.
Transaction boundary: each unit commits per repaired row (one bad row never
undoes earlier work in the same tick, mirroring the sweep's documented
per-unit commits); the units never share a transaction.

Single-process by design (the deployment is a one-node monolith): one worker
instance paces itself, and the executor's opportunity row lock remains the
cross-writer guard on Postgres.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.db import utcnow
from app.logging import get_logger
from app.models import NotificationOutbox, RecoveryAction
from app.models.base import ENVIRONMENT_REAL_TEST
from app.ports import NotificationSender, NotificationStatus, PaymentGateway, RecoveryStatus
from app.schemas.detection import DetectionRunRequest
from app.services.detection import DetectionRunResult, run_detection
from app.services.policy import audit
from app.services.recovery.executor import RecoveryExecutor
from app.services.recovery.reconcile import ReconcileReport, run_reconciliation
from app.services.worker.senders import LoggingNotificationSender

logger = get_logger(__name__)

WORKER_ACTOR = "system:worker"
NOTIFICATION_MAX_ATTEMPTS = 3
NOTIFICATION_RETRY_BASE_SECONDS = 60.0


def run_real_test_detection(db: Session, *, actor: str) -> DetectionRunResult:
    """Default detection unit: one pass over the REAL_TEST environment only.

    Reuses the API's exact entry point (`run_detection`, the same function
    `app.api.v1.detection` calls) and mirrors its post-run audit row, with
    `trigger: "worker"` marking the scheduled origin. Research stays
    on-demand (demo/eval surfaces) — this unit never scores it.
    `run_detection` commits the incidents it persists (non-dry runs); the
    commit here covers the audit row.
    """
    result = run_detection(db, DetectionRunRequest(environment=ENVIRONMENT_REAL_TEST))
    entry = audit.record(
        db,
        actor=actor,
        action="detection.run",
        entity_type="detection_run",
        entity_id=result.run_id,
        details={
            "environment": ENVIRONMENT_REAL_TEST,
            "trigger": "worker",
            "anomalies_detected": result.anomalies_detected,
            "anomalies_filtered": result.anomalies_filtered,
            "incidents_created": result.incidents_created,
            "incidents_updated": result.incidents_updated,
        },
    )
    entry.environment = ENVIRONMENT_REAL_TEST
    db.commit()
    return result


@dataclass
class TickReport:
    """What one tick did; returned for tests and the supervisor's logs."""

    ticked_at: datetime
    scheduled_seen: int = 0
    actions_fired: int = 0
    notifications_sent: int = 0
    notifications_retried: int = 0
    notifications_failed: int = 0
    reconciled: bool = False
    reconcile_report: ReconcileReport | None = None
    detected: bool = False
    detection_result: DetectionRunResult | None = None
    errors: list[str] = field(default_factory=list)


class Worker:
    """The synchronous unit-of-work engine behind the supervisor's tick loop.

    `session_factory` produces short-lived sessions (one per unit per tick).
    `gateway` is the deployment's PaymentGateway (research actions route to
    it; real_test actions route through the executor's real-gateway seam).
    `clock` is injectable so due/cadence logic is deterministic under test.
    `reconcile_fn` defaults to the real ADR 0011 sweep; tests inject a spy.
    `detection_fn` defaults to `run_real_test_detection` (one real_test pass
    through the API's `run_detection`); tests inject a spy.
    """

    def __init__(
        self,
        session_factory: Callable[[], Session],
        gateway: PaymentGateway,
        *,
        sender: NotificationSender | None = None,
        reconcile_seconds: float = 900.0,
        detection_seconds: float = 300.0,
        clock: Callable[[], datetime] | None = None,
        actor: str = WORKER_ACTOR,
        reconcile_fn: Callable[..., ReconcileReport] | None = None,
        detection_fn: Callable[..., DetectionRunResult] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._gateway = gateway
        self._sender = sender or LoggingNotificationSender()
        self._reconcile_seconds = float(reconcile_seconds)
        self._detection_seconds = float(detection_seconds)
        self._clock = clock or utcnow
        self._actor = actor
        self._reconcile_fn = reconcile_fn or run_reconciliation
        self._detection_fn = detection_fn or run_real_test_detection
        self._last_reconcile_at: datetime | None = None
        self._last_detection_at: datetime | None = None

    # ------------------------------------------------------------------
    # one tick
    # ------------------------------------------------------------------

    def tick(self) -> TickReport:
        """Run one pass over the four units. Units are failure-isolated: an
        exception in one is recorded in the report and never skips the others
        (a failing unit retries on the next tick)."""
        now = self._clock()
        report = TickReport(ticked_at=now)
        try:
            self._fire_due_actions(now, report)
        except Exception as exc:  # the unit must not kill the loop
            logger.exception("worker: scheduled-retry unit failed")
            report.errors.append(f"scheduled_retries: {type(exc).__name__}: {exc}")
        try:
            self._deliver_due_notifications(now, report)
        except Exception as exc:
            logger.exception("worker: notification unit failed")
            report.errors.append(f"notifications: {type(exc).__name__}: {exc}")
        if self._reconcile_due(now):
            try:
                report.reconcile_report = self._run_reconcile()
                report.reconciled = True
                # Advance only on success: a failing sweep retries next tick.
                self._last_reconcile_at = now
            except Exception as exc:
                logger.exception("worker: reconcile unit failed")
                report.errors.append(f"reconcile: {type(exc).__name__}: {exc}")
        if self._detection_due(now):
            try:
                report.detection_result = self._run_detection()
                report.detected = True
                # Advance only on success: a failing pass retries next tick.
                self._last_detection_at = now
            except Exception as exc:
                logger.exception("worker: detection unit failed")
                report.errors.append(f"detection: {type(exc).__name__}: {exc}")
        logger.info(
            "worker tick",
            extra={
                "scheduled_seen": report.scheduled_seen,
                "actions_fired": report.actions_fired,
                "notifications_sent": report.notifications_sent,
                "reconciled": report.reconciled,
                "detected": report.detected,
                "errors": report.errors,
            },
        )
        return report

    # ------------------------------------------------------------------
    # unit 1: delayed retries
    # ------------------------------------------------------------------

    def _fire_due_actions(self, now: datetime, report: TickReport) -> None:
        db = self._session_factory()
        try:
            executor = RecoveryExecutor(db, self._gateway, clock=self._clock)
            parked = list(
                db.scalars(
                    sa.select(RecoveryAction)
                    .where(RecoveryAction.status == RecoveryStatus.SCHEDULED)
                    .order_by(RecoveryAction.created_at, RecoveryAction.id)
                )
            )
            report.scheduled_seen = len(parked)
            for action in parked:
                if not executor.scheduled_due(action, now=now):
                    continue
                try:
                    executor.execute(action.opportunity_id, actor=self._actor)
                    db.commit()  # per-row durability: the fire must stick
                    report.actions_fired += 1
                except Exception as exc:  # one bad action never aborts the unit
                    db.rollback()
                    logger.warning(
                        "worker: scheduled retry fire failed; retried next tick",
                        extra={"action_id": action.id, "error": f"{type(exc).__name__}: {exc}"},
                    )
                    report.errors.append(
                        f"scheduled_retries[{action.id}]: {type(exc).__name__}: {exc}"
                    )
        finally:
            db.close()

    # ------------------------------------------------------------------
    # unit 2: notification outbox
    # ------------------------------------------------------------------

    def _deliver_due_notifications(self, now: datetime, report: TickReport) -> None:
        db = self._session_factory()
        try:
            rows = list(
                db.scalars(
                    sa.select(NotificationOutbox)
                    .where(
                        NotificationOutbox.status == NotificationStatus.PENDING,
                        NotificationOutbox.due_at <= now,
                    )
                    .order_by(NotificationOutbox.created_at, NotificationOutbox.id)
                )
            )
            for row in rows:
                try:
                    self._deliver_one(db, row, now, report)
                    db.commit()  # per-row durability
                except Exception as exc:
                    db.rollback()
                    logger.warning(
                        "worker: notification delivery bookkeeping failed",
                        extra={"outbox_id": row.id, "error": f"{type(exc).__name__}: {exc}"},
                    )
                    report.errors.append(
                        f"notifications[{row.id}]: {type(exc).__name__}: {exc}"
                    )
        finally:
            db.close()

    def _deliver_one(
        self, db: Session, row: NotificationOutbox, now: datetime, report: TickReport
    ) -> None:
        row.attempts += 1
        try:
            receipt = self._sender.send(
                customer=(row.payload or {}).get("customer"),
                channel=row.channel,
                payload=row.payload or {},
            )
        except Exception as exc:
            row.last_error = f"{type(exc).__name__}: {exc}"
            if row.attempts >= NOTIFICATION_MAX_ATTEMPTS:
                row.status = NotificationStatus.FAILED
                self._audit_notification(
                    db, row, "notification.failed", now,
                    {"attempts": row.attempts, "last_error": row.last_error},
                )
                report.notifications_failed += 1
            else:
                backoff = NOTIFICATION_RETRY_BASE_SECONDS * row.attempts
                row.due_at = now + timedelta(seconds=backoff)
                self._audit_notification(
                    db, row, "notification.retry_scheduled", now,
                    {
                        "attempts": row.attempts,
                        "last_error": row.last_error,
                        "next_due_at": row.due_at.isoformat(),
                    },
                )
                report.notifications_retried += 1
            return
        row.status = NotificationStatus.SENT
        row.sent_at = now
        row.last_error = None
        row.delivered_via = str(
            (receipt or {}).get("via") or getattr(self._sender, "name", "unknown")
        )
        self._audit_notification(
            db, row, "notification.sent", now,
            {"attempts": row.attempts, "delivered_via": row.delivered_via},
        )
        report.notifications_sent += 1

    def _audit_notification(
        self,
        db: Session,
        row: NotificationOutbox,
        action: str,
        now: datetime,
        details: dict[str, Any],
    ) -> None:
        entry = audit.record(
            db,
            actor=self._actor,
            action=action,
            entity_type="notification_outbox",
            entity_id=row.id,
            details={"channel": row.channel, **details},
        )
        entry.environment = row.environment

    # ------------------------------------------------------------------
    # unit 3: scheduled reconciliation (ADR 0011 sweep, reused as-is)
    # ------------------------------------------------------------------

    def _reconcile_due(self, now: datetime) -> bool:
        # Always reconcile on the first tick after startup — drift accumulates
        # while the process is down — then on the configured cadence.
        if self._last_reconcile_at is None:
            return True
        return (now - self._last_reconcile_at).total_seconds() >= self._reconcile_seconds

    def _run_reconcile(self) -> ReconcileReport:
        db = self._session_factory()
        try:
            # The sweep owns its transaction boundary (documented per-unit
            # commits in app.services.recovery.reconcile).
            return self._reconcile_fn(db, self._gateway, actor=self._actor)
        finally:
            db.close()

    # ------------------------------------------------------------------
    # unit 4: scheduled detection (real_test only; research stays on-demand)
    # ------------------------------------------------------------------

    def _detection_due(self, now: datetime) -> bool:
        # Always detect on the first tick after startup — the real
        # environment's event stream accrues while the process is down —
        # then on the configured cadence.
        if self._last_detection_at is None:
            return True
        return (now - self._last_detection_at).total_seconds() >= self._detection_seconds

    def _run_detection(self) -> DetectionRunResult:
        db = self._session_factory()
        try:
            # run_detection owns its transaction boundary (it commits the
            # incidents it persists on non-dry runs), like the sweep above.
            return self._detection_fn(db, actor=self._actor)
        finally:
            db.close()


__all__ = [
    "NOTIFICATION_MAX_ATTEMPTS",
    "NOTIFICATION_RETRY_BASE_SECONDS",
    "TickReport",
    "Worker",
    "WORKER_ACTOR",
    "run_real_test_detection",
]
