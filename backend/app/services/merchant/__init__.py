"""Merchant domain: the REAL Razorpay sync service (Test Mode merchant
experience). Pulls the merchant's real Razorpay data (orders, payments,
payment links by reference_id, subscriptions) into the commerce tables with
full provenance, and tracks the connection cursor (`connection_state`) plus
one `sync_runs` row per pass.

Boundary: this package may depend on the razorpay adapter's typed errors and
on models/ports/config/db/logging only — never on agent/api/simulator/
evaluation (enforced by tests/architecture/test_boundaries.py).
"""

from app.services.merchant.service import (
    ConnectionProbe,
    SyncDisabledError,
    SyncError,
    SyncNotConfiguredError,
    SyncService,
)

__all__ = [
    "ConnectionProbe",
    "SyncDisabledError",
    "SyncError",
    "SyncNotConfiguredError",
    "SyncService",
]
