"""Fixtures shared by the diagnosis tests.

`tiny_trained` is session-scoped: training a small model once (~seconds) and
reusing the artifact dir keeps the suite fast. It touches no DB, so it
composes safely with the function-scoped `db_session` from the root conftest.
"""

from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
import pytest

from app.models import Payment, PaymentEvent
from app.services.diagnosis.synthetic import SyntheticConfig, generate_dataset, generate_window
from app.services.diagnosis.training import save_artifacts, train_and_compare

T0 = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="session")
def tiny_trained(tmp_path_factory):
    """Train the 3-algo comparison on a tiny synthetic set; return
    (artifacts_dir, TrainingResult)."""
    df = generate_dataset(SyntheticConfig(windows_per_class=15, seed=7))
    result = train_and_compare(df, seed=7)
    artifacts_dir = tmp_path_factory.mktemp("diag_artifacts")
    save_artifacts(result, artifacts_dir, "synthetic-mini test fixture")
    return artifacts_dir, result


def seed_records(db_session, merchant, records: list[dict[str, Any]], window_start) -> None:
    """Write synthetic-generator records into the DB as Payment+PaymentEvent
    rows, placed at window_start + t_offset_s."""
    for rec in records:
        status = {"failed": "failed", "captured": "captured", "pending": "created"}[rec["outcome"]]
        payment = Payment(
            merchant_id=merchant.id,
            amount_paise=rec["amount_paise"],
            method=rec["method"],
            status=status,
            error_source=rec.get("error_source"),
            meta={"subscription_id": "sub_test"} if rec.get("is_subscription") else {},
        )
        db_session.add(payment)
        db_session.flush()
        event = PaymentEvent(
            payment_id=payment.id,
            event_type=f"payment.{status if status != 'created' else 'created'}",
            to_status=status,
            source="simulator",
            occurred_at=window_start + timedelta(seconds=rec.get("t_offset_s", 0.0)),
            payload={
                "bank": rec.get("bank"),
                "error_source": rec.get("error_source"),
                "error_step": rec.get("error_step"),
                "error_reason": rec.get("error_reason"),
                "latency_ms": rec.get("latency_ms"),
                "subscription_id": "sub_test" if rec.get("is_subscription") else None,
            },
        )
        db_session.add(event)
    db_session.commit()


@pytest.fixture()
def make_window(db_session, make_merchant):
    """Return a helper seeding (baseline, window) records for a cause."""

    def _seed(cause: str, seed: int = 123, window_start=T0):
        rng = np.random.default_rng(seed)
        cfg = SyntheticConfig()
        merchant = make_merchant()
        baseline, window = generate_window(cause, window_start, rng, cfg)
        seed_records(db_session, merchant, baseline, window_start - timedelta(hours=1))
        seed_records(db_session, merchant, window, window_start)
        return merchant

    return _seed
