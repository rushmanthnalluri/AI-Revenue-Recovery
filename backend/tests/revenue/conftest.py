"""Fixtures for revenue-engine tests: deterministic synthetic populations.

All populations are planted with exact rates (e.g. precisely 360 of 400
captured), never sampled randomly, so tests are stable and labelled
synthetic/preliminary by construction. Timestamps are tz-aware UTC.
"""

from datetime import datetime, timedelta
from typing import Sequence

import pytest

import app.models as models
from app.db import utcnow
from app.ports import ActionType


@pytest.fixture()
def make_customer(db_session, make_merchant):
    def _make(merchant=None, **kw) -> models.Customer:
        merchant = merchant or make_merchant()
        c = models.Customer(merchant_id=merchant.id, **kw)
        db_session.add(c)
        db_session.commit()
        return c

    return _make


@pytest.fixture()
def plant_payments(db_session, make_merchant):
    """Plant `n` payments with an exact captured count.

    The first `captured` payments are captured, the rest failed; failure
    reasons cycle through `failure_reasons`. Deterministic — no RNG.
    """

    def _plant(
        *,
        n: int,
        start: datetime,
        captured: int | None = None,
        captured_rate: float | None = None,
        amount_paise: int = 100_000,
        method: str = "upi",
        step_seconds: int = 1,
        failure_reasons: str | Sequence[str] = "payment_timed_out",
        customer_id: str | None = None,
        merchant=None,
    ) -> list[models.Payment]:
        merchant = merchant or make_merchant()
        if captured is None:
            rate = 1.0 if captured_rate is None else captured_rate
            captured = int(round(n * rate))
        reasons = (
            [failure_reasons] if isinstance(failure_reasons, str) else list(failure_reasons)
        )
        payments: list[models.Payment] = []
        for i in range(n):
            ok = i < captured
            p = models.Payment(
                merchant_id=merchant.id,
                customer_id=customer_id,
                amount_paise=amount_paise,
                method=method,
                status="captured" if ok else "failed",
                captured=ok,
                created_at=start + timedelta(seconds=i * step_seconds),
            )
            if not ok:
                reason = reasons[(i - captured) % len(reasons)]
                p.error_code = "BAD_REQUEST_ERROR"
                p.error_source = "gateway"
                p.meta = {"error_reason": reason}
            db_session.add(p)
            payments.append(p)
        db_session.commit()
        return payments

    return _plant


@pytest.fixture()
def make_opportunity(db_session):
    def _make(**kw) -> models.RecoveryOpportunity:
        opp = models.RecoveryOpportunity(
            amount_paise=kw.pop("amount_paise", 100_000),
            opportunity_type=kw.pop("opportunity_type", "failed_payment_retry"),
            **kw,
        )
        db_session.add(opp)
        db_session.commit()
        return opp

    return _make


@pytest.fixture()
def make_action(db_session, make_opportunity):
    def _make(opportunity=None, **kw) -> models.RecoveryAction:
        opportunity = opportunity or make_opportunity()
        a = models.RecoveryAction(
            opportunity_id=opportunity.id,
            amount_paise=kw.pop("amount_paise", opportunity.amount_paise),
            action_type=kw.pop("action_type", ActionType.RETRY_PAYMENT),
            proposed_at=kw.pop("proposed_at", utcnow()),
            **kw,
        )
        db_session.add(a)
        db_session.commit()
        return a

    return _make
