"""SyncService — pull the merchant's REAL Razorpay data into PulseRecover.

Scope (docs/razorpay-integration.md §I, all facts verified 2026-08-28):
- windowed list pulls: `GET /v1/orders`, `GET /v1/payments`,
  `GET /v1/subscriptions` with `from`/`to` (unix) + `count`<=100 + `skip`;
- payment links by targeted `reference_id` only (the list endpoint documents
  no pagination, §K) — reference ids come from our own real_test recovery
  actions (`gateway_request_id` IS the link's `reference_id`, §F);
- a connection probe: `GET /v1/payments?count=1`.

Durability contract:
- every synced row is upserted on `(source_type, external_id)` with full
  provenance (`source_system='razorpay'`, `external_id`=Razorpay id,
  `ingested_at`=first-seen); re-syncs update in place — ZERO duplicates;
- entities failing validation are QUARANTINED (skipped + recorded under
  `entity_counts.errors`), never silently coerced, never crashing the run;
- one `sync_runs` row per pass (running -> completed|failed) and the
  `connection_state` singleton are updated in the same flush; the caller
  (API layer) owns the commit;
- `sync_enabled=false` (Disconnect) refuses the run before any network I/O.

Secret hygiene: the key secret is used only for HTTP Basic auth and is never
logged or returned; only the masked key id is exposed.
"""

import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import utcnow
from app.logging import get_logger
from app.models import (
    CONNECTION_STATE_SINGLETON_ID,
    ConnectionState,
    Merchant,
    Order,
    Payment,
    RecoveryAction,
    Subscription,
    SyncRun,
)
from app.models.base import (
    ENVIRONMENT_REAL_TEST,
    RAZORPAY_SOURCE_SYSTEM,
    SOURCE_TYPE_RAZORPAY_LIVE,
    SOURCE_TYPE_RAZORPAY_TEST,
)
from app.ports import ActionType
from app.services.merchant.client import MAX_PAGE_SIZE, RazorpayReadClient
from app.services.merchant.normalize import (
    EntityValidationError,
    normalize_order,
    normalize_payment,
    normalize_payment_link,
    normalize_subscription,
)
from app.services.razorpay.errors import (
    GatewayAuthenticationError,
    GatewayClientError,
    GatewayError,
    GatewayTransientError,
)

logger = get_logger("app.services.merchant.service")

#: Default look-back for a sync pass. The Orders API refuses direct fetches
#: beyond 180 days (docs/razorpay-integration.md §H), so that is the hard cap.
DEFAULT_WINDOW_DAYS = 30
MAX_WINDOW_DAYS = 180

#: Bound on quarantine entries embedded in `entity_counts.errors` — a
#: pathological payload flood must not bloat the sync_runs JSON column.
_MAX_RECORDED_ERRORS = 50


class SyncError(Exception):
    """Base for sync refusals/failures surfaced to the API layer."""


class SyncNotConfiguredError(SyncError):
    """Real keys missing, SIMULATION_MODE on, or an unrecognized key format."""


class SyncDisabledError(SyncError):
    """`connection_state.sync_enabled` is false (merchant disconnected)."""


@dataclass(frozen=True)
class ConnectionProbe:
    """Result of the connection probe. `connection_error` is a stable typed
    code: None | 'authentication_failed' | 'unreachable' | 'gateway_error'."""

    configured: bool
    connected: bool
    environment: str | None  # 'test' | 'live' | None
    key_id_masked: str | None
    connection_error: str | None


def environment_for_key(key_id: str) -> str | None:
    """'test' for rzp_test_*, 'live' for rzp_live_*, else None (unknown)."""
    if key_id.startswith("rzp_test_"):
        return "test"
    if key_id.startswith("rzp_live_"):
        return "live"
    return None


def mask_key_id(key_id: str) -> str | None:
    """Display form of the key id, e.g. `rzp_test_••••ab12`. The SECRET is
    never involved — only the public key id, and only its edges."""
    if not key_id:
        return None
    for prefix in ("rzp_test_", "rzp_live_"):
        if key_id.startswith(prefix):
            suffix = key_id[-4:] if len(key_id) > len(prefix) else ""
            return f"{prefix}••••{suffix}"
    return f"••••{key_id[-4:]}" if len(key_id) > 4 else "••••"


def _source_type_for_key(key_id: str) -> str | None:
    env = environment_for_key(key_id)
    if env == "test":
        return SOURCE_TYPE_RAZORPAY_TEST
    if env == "live":
        return SOURCE_TYPE_RAZORPAY_LIVE
    return None


class SyncService:
    """Connection probe + full sync for the merchant's real Razorpay account."""

    def __init__(
        self,
        *,
        key_id: str,
        key_secret: str,
        base_url: str = "https://api.razorpay.com/v1",
        simulation_mode: bool = False,
        page_size: int = MAX_PAGE_SIZE,
        max_pages: int = 100,
        transport: Any = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._key_id = key_id or ""
        # Mirrors factory.get_real_gateway: SIMULATION_MODE refuses to touch
        # the network even when keys are present — the real connection is
        # honestly "not configured" for a simulator deployment.
        self._configured = bool(key_id and key_secret) and not simulation_mode
        self._page_size = min(max(1, page_size), MAX_PAGE_SIZE)
        self._max_pages = max(1, max_pages)
        self._client = (
            RazorpayReadClient(
                key_id=key_id,
                key_secret=key_secret,
                base_url=base_url,
                transport=transport,
                sleep=sleep,
            )
            if self._configured
            else None
        )

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        transport: Any = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> "SyncService":
        return cls(
            key_id=settings.RAZORPAY_KEY_ID,
            key_secret=settings.RAZORPAY_KEY_SECRET,
            base_url=settings.RAZORPAY_BASE_URL,
            simulation_mode=settings.SIMULATION_MODE,
            transport=transport,
            sleep=sleep,
        )

    # ------------------------------------------------------------------
    # connection probe
    # ------------------------------------------------------------------

    def probe_connection(self) -> ConnectionProbe:
        """Keys present? Authenticated `GET /v1/payments?count=1` succeed?"""
        if not self._configured or self._client is None:
            return ConnectionProbe(False, False, None, None, None)
        environment = environment_for_key(self._key_id)
        masked = mask_key_id(self._key_id)
        try:
            self._client.probe()
        except GatewayAuthenticationError:
            return ConnectionProbe(True, False, environment, masked, "authentication_failed")
        except GatewayTransientError:
            return ConnectionProbe(True, False, environment, masked, "unreachable")
        except GatewayError:
            return ConnectionProbe(True, False, environment, masked, "gateway_error")
        return ConnectionProbe(True, True, environment, masked, None)

    # ------------------------------------------------------------------
    # connection state
    # ------------------------------------------------------------------

    def get_connection_state(self, db: Session) -> ConnectionState:
        """The singleton cursor row, created on first use (flush only)."""
        state = db.get(ConnectionState, CONNECTION_STATE_SINGLETON_ID)
        if state is None:
            state = ConnectionState(id=CONNECTION_STATE_SINGLETON_ID)
            db.add(state)
            db.flush()
        return state

    def set_sync_enabled(self, db: Session, enabled: bool) -> ConnectionState:
        """Disconnect/Reconnect: flip `sync_enabled` (flush only; the API
        layer writes the audit row and commits)."""
        state = self.get_connection_state(db)
        state.sync_enabled = enabled
        db.flush()
        return state

    # ------------------------------------------------------------------
    # full sync
    # ------------------------------------------------------------------

    def run_sync(
        self,
        db: Session,
        *,
        actor: str,
        request_id: str | None = None,
        window_days: int = DEFAULT_WINDOW_DAYS,
    ) -> SyncRun:
        """One synchronous sync pass. Returns the completed `sync_runs` row.

        Raises SyncNotConfiguredError / SyncDisabledError BEFORE any network
        or database writes. A definitive per-endpoint refusal (4xx, e.g. the
        Subscriptions product not enabled on the account) degrades per entity:
        the skip is quarantined in `entity_counts.errors` and the rest of the
        catalog still syncs; the run is `failed` only when EVERY entity pull
        is refused. Ambiguous gateway failures mid-run (5xx/timeout) are
        caught, recorded on the run row (status='failed'), and returned — the
        row is the honest durable record; partial upserts already flushed are
        kept (the next run reconciles them idempotently).
        """
        if not self._configured or self._client is None:
            raise SyncNotConfiguredError(
                "real Razorpay connection is not configured "
                "(SIMULATION_MODE on or RAZORPAY_KEY_ID/RAZORPAY_KEY_SECRET missing)"
            )
        source_type = _source_type_for_key(self._key_id)
        if source_type is None:
            raise SyncNotConfiguredError(
                "unrecognized Razorpay key format (expected rzp_test_* or rzp_live_*)"
            )
        state = self.get_connection_state(db)
        if not state.sync_enabled:
            raise SyncDisabledError(
                "sync is disabled for this connection (POST /api/v1/merchant/sync/enable to reconnect)"
            )

        window_days = min(max(1, window_days), MAX_WINDOW_DAYS)
        run = SyncRun(status="running", actor=actor, request_id=request_id, entity_counts={})
        db.add(run)
        db.flush()

        counts: dict[str, Any] = {
            "orders": {"created": 0, "updated": 0},
            "payments": {"created": 0, "updated": 0},
            "payment_links": {"fetched": 0},
            "subscriptions": {"created": 0, "updated": 0},
            "errors": [],
        }
        try:
            # Auth canary: one cheap authenticated GET. A 4xx here means the
            # KEYS themselves are refused (nothing can sync) -> fail the run.
            # Once the keys are proven, a later per-endpoint 4xx is genuinely
            # endpoint/product-specific and degrades per entity below.
            self._client.probe()
            merchant = self._ensure_merchant(db, source_type)
            now = utcnow()
            to_ts = int(now.timestamp())
            from_ts = int((now - timedelta(days=window_days)).timestamp())
            pulls = (
                ("orders", "order", lambda: self._sync_orders(db, merchant, source_type, from_ts, to_ts, counts)),
                ("payments", "payment", lambda: self._sync_payments(db, merchant, source_type, from_ts, to_ts, counts)),
                ("payment_links", "payment_link", lambda: self._sync_payment_links(db, merchant, source_type, counts)),
                ("subscriptions", "subscription", lambda: self._sync_subscriptions(db, merchant, source_type, from_ts, to_ts, counts)),
            )
            refused = 0
            for path, kind, pull in pulls:
                try:
                    pull()
                except GatewayClientError as exc:
                    # Definitive per-endpoint refusal (4xx) — e.g. the
                    # Subscriptions product not enabled on this account, which
                    # Razorpay answers with a 401 on GET /v1/subscriptions
                    # while every other endpoint authenticates fine. Degrade
                    # per entity: record the skip and keep syncing the rest of
                    # the catalog. Ambiguous/transient failures (5xx, timeout)
                    # still fail the run honestly.
                    refused += 1
                    self._quarantine(
                        counts,
                        kind,
                        {"id": None},
                        SyncError(
                            f"endpoint skipped: {type(exc).__name__}: {exc.message} "
                            f"(GET /v1/{path} refused — is the product enabled on this Razorpay account?)"
                        ),
                    )
            if refused == len(pulls):
                run.status = "failed"
                run.error = (
                    "every entity pull was refused by the gateway "
                    "(check RAZORPAY_KEY_ID/RAZORPAY_KEY_SECRET and the enabled products)"
                )
                logger.warning(
                    "merchant sync failed", extra={"run_id": run.id, "error": run.error}
                )
            else:
                run.status = "completed"
        except GatewayError as exc:
            run.status = "failed"
            run.error = f"{type(exc).__name__}: {exc.message}"[:500]
            logger.warning(
                "merchant sync failed", extra={"run_id": run.id, "error": run.error}
            )
        run.finished_at = utcnow()
        run.entity_counts = counts
        state.last_sync_at = run.finished_at
        state.last_sync_status = run.status
        db.flush()
        logger.info(
            "merchant sync finished",
            extra={
                "run_id": run.id,
                "status": run.status,
                "counts": {k: v for k, v in counts.items() if k != "errors"},
                "errors": len(counts["errors"]),
            },
        )
        return run

    # ------------------------------------------------------------------
    # entity pulls
    # ------------------------------------------------------------------

    def _paged(self, path: str, from_ts: int, to_ts: int) -> Iterator[dict[str, Any]]:
        """Yield raw entities across count/skip pages (stop on a short page)."""
        skip = 0
        for _ in range(self._max_pages):
            items = self._client.get_collection_page(
                path, {"from": from_ts, "to": to_ts, "count": self._page_size, "skip": skip}
            )
            yield from items
            if len(items) < self._page_size:
                return
            skip += len(items)
        logger.warning(
            "sync pagination cap reached; window is incomplete",
            extra={"path": path, "pages": self._max_pages},
        )

    def _sync_orders(
        self, db: Session, merchant: Merchant, source_type: str,
        from_ts: int, to_ts: int, counts: dict[str, Any],
    ) -> None:
        for raw in self._paged("orders", from_ts, to_ts):
            try:
                gateway_id, fields = normalize_order(raw)
            except EntityValidationError as exc:
                self._quarantine(counts, "order", raw, exc)
                continue
            fields["merchant_id"] = merchant.id
            _, created = self._upsert(db, Order, source_type, gateway_id, fields)
            counts["orders"]["created" if created else "updated"] += 1

    def _sync_payments(
        self, db: Session, merchant: Merchant, source_type: str,
        from_ts: int, to_ts: int, counts: dict[str, Any],
    ) -> None:
        for raw in self._paged("payments", from_ts, to_ts):
            self._ingest_payment(db, merchant, source_type, raw, counts)

    def _ingest_payment(
        self, db: Session, merchant: Merchant, source_type: str,
        raw: dict[str, Any], counts: dict[str, Any],
    ) -> None:
        try:
            gateway_id, fields = normalize_payment(raw)
        except EntityValidationError as exc:
            self._quarantine(counts, "payment", raw, exc)
            return
        fields["merchant_id"] = merchant.id
        gateway_order_id = fields.pop("_gateway_order_id", None)
        if gateway_order_id:
            fields["order_id"] = self._local_order_id(db, source_type, gateway_order_id)
        _, created = self._upsert(db, Payment, source_type, gateway_id, fields)
        counts["payments"]["created" if created else "updated"] += 1

    def _sync_payment_links(
        self, db: Session, merchant: Merchant, source_type: str, counts: dict[str, Any]
    ) -> None:
        """Targeted reconciliation of OUR outbound links by reference_id.

        `GET /v1/payment_links` documents no pagination (§K), so we fetch only
        links we created: real_test recovery actions whose gateway_request_id
        was sent as the link's `reference_id` (§F). Links have no local table;
        they are validated + counted, and their post-capture `payments[]`
        sub-entities are ingested as real payments.
        """
        reference_ids = db.scalars(
            sa.select(RecoveryAction.gateway_request_id)
            .where(
                RecoveryAction.action_type == ActionType.CREATE_PAYMENT_LINK,
                RecoveryAction.environment == ENVIRONMENT_REAL_TEST,
                RecoveryAction.gateway_request_id.is_not(None),
            )
            .distinct()
        ).all()
        for reference_id in sorted(r for r in reference_ids if r):
            try:
                items = self._client.get_collection_page(
                    "payment_links", {"reference_id": reference_id}
                )
            except GatewayError as exc:
                self._quarantine(
                    counts, "payment_link", {"id": None, "reference_id": reference_id}, exc
                )
                continue
            for raw in items:
                try:
                    _, embedded = normalize_payment_link(raw)
                except EntityValidationError as exc:
                    self._quarantine(counts, "payment_link", raw, exc)
                    continue
                counts["payment_links"]["fetched"] += 1
                for payment_raw in embedded:
                    self._ingest_payment(db, merchant, source_type, payment_raw, counts)

    def _sync_subscriptions(
        self, db: Session, merchant: Merchant, source_type: str,
        from_ts: int, to_ts: int, counts: dict[str, Any],
    ) -> None:
        for raw in self._paged("subscriptions", from_ts, to_ts):
            try:
                gateway_id, fields = normalize_subscription(raw)
            except EntityValidationError as exc:
                self._quarantine(counts, "subscription", raw, exc)
                continue
            fields["merchant_id"] = merchant.id
            _, created = self._upsert(db, Subscription, source_type, gateway_id, fields)
            counts["subscriptions"]["created" if created else "updated"] += 1

    # ------------------------------------------------------------------
    # persistence helpers
    # ------------------------------------------------------------------

    def _ensure_merchant(self, db: Session, source_type: str) -> Merchant:
        """Find-or-create the local Merchant anchor for this real connection
        (one per source_type; first by created_at wins)."""
        merchant = db.scalar(
            sa.select(Merchant)
            .where(Merchant.source_type == source_type)
            .order_by(Merchant.created_at)
            .limit(1)
        )
        if merchant is None:
            label = "Test Mode" if source_type == SOURCE_TYPE_RAZORPAY_TEST else "Live"
            merchant = Merchant(
                name=f"Razorpay {label} Merchant",
                source_type=source_type,
                source_system=RAZORPAY_SOURCE_SYSTEM,
                meta={"created_by": "merchant_sync"},
            )
            db.add(merchant)
            db.flush()
        return merchant

    @staticmethod
    def _local_order_id(db: Session, source_type: str, gateway_order_id: str) -> str | None:
        return db.scalar(
            sa.select(Order.id).where(
                Order.source_type == source_type,
                Order.external_id == gateway_order_id,
            )
        )

    def _upsert(
        self,
        db: Session,
        model: type[Order] | type[Payment] | type[Subscription],
        source_type: str,
        external_id: str,
        fields: dict[str, Any],
    ) -> tuple[Any, bool]:
        """Insert or update keyed on (source_type, external_id) — the UNIQUE
        contract that makes re-syncs idempotent. `ingested_at` (first seen)
        and `created_at` (gateway timestamp) are immutable after insert.
        Returns (row, created)."""
        existing = db.scalar(
            sa.select(model).where(
                model.source_type == source_type,
                model.external_id == external_id,
            )
        )
        if existing is None:
            row = model(
                source_type=source_type,
                source_system=RAZORPAY_SOURCE_SYSTEM,
                external_id=external_id,
                ingested_at=utcnow(),
                **fields,
            )
            db.add(row)
            db.flush()
            return row, True
        for key, value in fields.items():
            if key in ("merchant_id", "created_at"):
                continue  # immutable after first ingest
            setattr(existing, key, value)
        db.flush()
        return existing, False

    @staticmethod
    def _quarantine(
        counts: dict[str, Any], entity_kind: str, raw: dict[str, Any], exc: Exception
    ) -> None:
        """Skip a bad/unreachable entity and record it — never crash the run."""
        errors = counts["errors"]
        if len(errors) < _MAX_RECORDED_ERRORS:
            errors.append(
                {
                    "entity": entity_kind,
                    "id": raw.get("id") or raw.get("reference_id"),
                    "reason": str(exc)[:200],
                }
            )
        else:
            counts["errors_truncated"] = counts.get("errors_truncated", 0) + 1
        logger.warning(
            "sync quarantined entity",
            extra={"entity": entity_kind, "entity_id": raw.get("id"), "reason": str(exc)[:200]},
        )


__all__ = [
    "ConnectionProbe",
    "SyncDisabledError",
    "SyncError",
    "SyncNotConfiguredError",
    "SyncService",
    "environment_for_key",
    "mask_key_id",
    "DEFAULT_WINDOW_DAYS",
    "MAX_WINDOW_DAYS",
]
