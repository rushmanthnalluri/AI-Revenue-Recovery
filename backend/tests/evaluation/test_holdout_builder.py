"""Holdout isolation at the opportunity-selection layer: the holdout-aware
builder must never create opportunities (the parents of recovery actions)
for excluded customers — for failed payments AND for abandoned orders."""

from datetime import timedelta

import app.models as models
from app.db import utcnow
from app.services.evaluation.holdout import HoldoutExcludingBuilder


def _customer(db, merchant, **kw):
    c = models.Customer(merchant_id=merchant.id, **kw)
    db.add(c)
    db.commit()
    return c


def _order(db, merchant, customer, **kw):
    o = models.Order(
        merchant_id=merchant.id,
        customer_id=customer.id,
        amount_paise=kw.pop("amount_paise", 75000),
        **kw,
    )
    db.add(o)
    db.commit()
    return o


def test_builder_never_builds_for_excluded_customers(
    db_session, make_merchant, make_payment, make_incident
):
    merchant = make_merchant()
    now = utcnow()
    incident = make_incident(
        window_start=now - timedelta(hours=2),
        window_end=now,
        detected_at=now,
    )
    held = _customer(db_session, merchant, name="held out")
    treated = _customer(db_session, merchant, name="treated")

    # Failed payments for both customers inside the incident window...
    make_payment(
        merchant, customer_id=held.id, created_at=now - timedelta(minutes=30)
    )
    make_payment(
        merchant, customer_id=treated.id, created_at=now - timedelta(minutes=30)
    )
    # ...and one abandoned checkout (created, never paid) for each.
    _order(db_session, merchant, held, created_at=now - timedelta(minutes=45))
    _order(db_session, merchant, treated, created_at=now - timedelta(minutes=45))

    builder = HoldoutExcludingBuilder(
        db_session, is_excluded=lambda cid: cid == held.id
    )
    built = builder.build_for_incident(incident.id, actor="system:test")

    assert len(built.created) == 2  # treated payment + treated order only
    assert {o.customer_id for o in built.created} == {treated.id}
    violation_count = sum(1 for o in built.created if o.customer_id == held.id)
    assert violation_count == 0


def test_builder_excludes_stuck_created_payments_for_holdout(
    db_session, make_merchant, make_payment, make_incident
):
    """The stuck-checkout source (payments stranded in `created`) must honor
    the holdout exactly like failed payments and abandoned orders — a held-out
    customer's stuck payment must never yield an opportunity (and therefore
    never an action)."""
    merchant = make_merchant()
    now = utcnow()
    incident = make_incident(
        window_start=now - timedelta(hours=2),
        window_end=now,
        detected_at=now,
    )
    held = _customer(db_session, merchant, name="held out")
    treated = _customer(db_session, merchant, name="treated")

    # One stuck-created checkout attempt per customer (older than the 30-min
    # stuck threshold, inside the incident window, no failed payment involved).
    for cust in (held, treated):
        make_payment(
            merchant,
            customer_id=cust.id,
            status="created",
            created_at=now - timedelta(minutes=45),
        )

    builder = HoldoutExcludingBuilder(
        db_session, is_excluded=lambda cid: cid == held.id
    )
    built = builder.build_for_incident(incident.id, actor="system:test")

    assert len(built.created) == 1
    assert built.created[0].customer_id == treated.id
    assert built.created[0].opportunity_type == "stuck_checkout_payment"
