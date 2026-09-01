"""System domain: audit trail and inbound webhook event log."""

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, Session, mapped_column

from app import ids
from app.db import Base, TZDateTime, utcnow
from app.models.base import EnvironmentMixin, TimestampMixin


def _canonical_ts(value: datetime) -> str:
    """UTC-normalized ISO form for hashing (mirrors TZDateTime's bind
    normalization so insert-time and verify-time digests agree)."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def compute_entry_hash(row: "AuditLog") -> str:
    """sha256 over the row's canonical fields: id, ts, actor, action, entity,
    details, previous_hash. Serialization is deterministic (sorted keys,
    compact separators) so app.services.audit.verify recomputes the identical
    digest from a freshly loaded row."""
    payload = {
        "id": row.id,
        "ts": _canonical_ts(row.created_at),
        "actor": row.actor,
        "action": row.action,
        "entity_type": row.entity_type,
        "entity_id": row.entity_id,
        "details": row.details or {},
        "previous_hash": row.previous_hash,
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class AuditLog(EnvironmentMixin, Base):
    """Append-only, hash-chained audit trail. No updated_at — rows are immutable.

    Tamper-evidence: the session-level `before_flush` hook below stamps every
    new row with `previous_hash` (the chain head's `entry_hash`) and
    `entry_hash` (sha256 over the canonical fields). Every writer —
    `services.policy.audit.record`, the direct ORM constructions in
    webhook/executor paths, test seeds — chains transparently, with no
    per-writer code. Rows predating the chain keep NULL hashes and verify as
    legacy-valid.

    Single-node assumption: the hook finds the chain head with a flush-time
    query inside the same transaction. That is exact for the single-writer
    SQLite deployment (and a single-node Postgres); concurrent writers on
    separate connections would need an external anchor (docs/security-testing.md).
    """

    __tablename__ = "audit_logs"
    __table_args__ = (
        sa.Index("ix_audit_entity", "entity_type", "entity_id"),
    )

    id: Mapped[str] = mapped_column(sa.String(64), primary_key=True, default=ids.audit_id)
    created_at: Mapped[datetime] = mapped_column(TZDateTime(), nullable=False, index=True)
    entity_type: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    entity_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    actor: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    action: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(sa.JSON, default=dict, nullable=False)
    request_id: Mapped[str | None] = mapped_column(sa.String(64))
    # Hash chain — NULL on pre-chain legacy rows; see compute_entry_hash and
    # app.services.audit.verify.verify_chain.
    previous_hash: Mapped[str | None] = mapped_column(sa.String(64))
    entry_hash: Mapped[str | None] = mapped_column(sa.String(64))


@sa.event.listens_for(Session, "before_flush")
def _stamp_hash_chain(session, flush_context, instances) -> None:  # noqa: ANN001, ANN202
    """Chain every new audit row to its predecessor — transparently.

    Session-level `before_flush` (not mapper `before_insert`): SQLAlchemy 2
    batches same-flush INSERTs (executemany), so a per-row insert hook cannot
    see sibling rows of the same flush. Here the whole pending batch is
    chained at once, sorted by (created_at, id) — exactly the order
    verify_chain replays — starting from the latest chained row already
    flushed in this transaction.

    Rows flushed earlier in the transaction are visible to the head query;
    the chain therefore follows insertion order as long as writers stamp
    per-row `utcnow()` (all in-app writers do). The hash fields are always
    (re)stamped here so the digest format has exactly one writer. Direct Core
    `insert()` bypasses the ORM entirely — such rows stay NULL-hashed and are
    flagged if they appear after chain genesis.
    """
    new_rows = [obj for obj in session.new if isinstance(obj, AuditLog)]
    if not new_rows:
        return
    for row in new_rows:
        if row.id is None:
            row.id = ids.audit_id()
        if row.created_at is None:
            row.created_at = utcnow()
        if row.details is None:
            row.details = {}
    prev_hash = session.scalar(
        sa.select(AuditLog.entry_hash)
        .where(AuditLog.entry_hash.is_not(None))
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(1)
        .execution_options(autoflush=False)
    )
    for row in sorted(new_rows, key=lambda r: (r.created_at, r.id)):
        row.previous_hash = prev_hash
        row.entry_hash = compute_entry_hash(row)
        prev_hash = row.entry_hash


class WebhookEvent(TimestampMixin, Base):
    """Raw inbound webhook events. gateway_event_id is UNIQUE — that constraint
    is the dedup mechanism (Razorpay retries deliver the same event id)."""

    __tablename__ = "webhook_events"

    id: Mapped[str] = mapped_column(sa.String(64), primary_key=True, default=ids.webhook_event_id)
    gateway_event_id: Mapped[str] = mapped_column(sa.String(128), unique=True, nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(sa.String(128), nullable=False, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(sa.JSON, default=dict, nullable=False)
    signature_valid: Mapped[bool] = mapped_column(sa.Boolean, default=False, nullable=False)
    processed: Mapped[bool] = mapped_column(sa.Boolean, default=False, nullable=False, index=True)
    processed_at: Mapped[datetime | None] = mapped_column(TZDateTime())
    error: Mapped[str | None] = mapped_column(sa.Text)
    # razorpay | simulator
    source: Mapped[str] = mapped_column(sa.String(32), default="razorpay", nullable=False)
    received_at: Mapped[datetime] = mapped_column(TZDateTime(), nullable=False, index=True)
