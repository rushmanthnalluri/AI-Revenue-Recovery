"""Opportunity builder tests: incident blast radius -> opportunity rows."""

from datetime import timedelta

import pytest

import app.models as models
from app.db import utcnow
from app.ports import RecoveryStatus
from app.services.recovery import OpportunityBuilder


class TestFailedPayments:
    def test_creates_one_opportunity_per_failed_payment_in_window(
        self, db_session, windowed_incident, make_payment
    ):
        incident = windowed_incident()
        p1 = make_payment(status="failed")
        p2 = make_payment(status="failed")
        make_payment(status="captured", captured=True)  # not a recovery target
        stale = make_payment(status="failed")
        stale.created_at = utcnow() - timedelta(days=2)  # outside the window
        db_session.commit()

        result = OpportunityBuilder(db_session).build_for_incident(incident.id)

        assert {o.payment_id for o in result.created} == {p1.id, p2.id}
        assert all(o.opportunity_type == "failed_payment_retry" for o in result.created)
        assert all(o.status is RecoveryStatus.PROPOSED for o in result.created)
        assert all(o.incident_id == incident.id for o in result.created)

    def test_amount_and_customer_are_carried_from_the_payment(
        self, db_session, windowed_incident, make_customer, make_payment
    ):
        incident = windowed_incident()
        customer = make_customer()
        payment = make_payment(status="failed", amount_paise=42_500, customer_id=customer.id)

        result = OpportunityBuilder(db_session).build_for_incident(incident.id)

        (opp,) = result.created
        assert opp.amount_paise == 42_500
        assert opp.currency == "INR"
        assert opp.customer_id == customer.id


class TestAbandonedCheckouts:
    def test_order_with_no_payments_becomes_dropped_checkout(
        self, db_session, windowed_incident, make_order
    ):
        incident = windowed_incident()
        order = make_order(status="created", amount_paise=75_000)

        result = OpportunityBuilder(db_session).build_for_incident(incident.id)

        (opp,) = result.created
        assert opp.opportunity_type == "dropped_checkout"
        assert opp.payment_id is None
        assert opp.amount_paise == 75_000
        assert opp.meta["order_id"] == order.id

    def test_order_with_a_failed_payment_is_not_double_counted(
        self, db_session, windowed_incident, make_order, make_payment
    ):
        incident = windowed_incident()
        order = make_order(status="created")
        payment = make_payment(status="failed", order_id=order.id)

        result = OpportunityBuilder(db_session).build_for_incident(incident.id)

        types = sorted(o.opportunity_type for o in result.created)
        assert types == ["failed_payment_retry"]
        assert result.created[0].payment_id == payment.id

    def test_paid_order_yields_no_opportunity(
        self, db_session, windowed_incident, make_order
    ):
        incident = windowed_incident()
        make_order(status="paid")

        result = OpportunityBuilder(db_session).build_for_incident(incident.id)
        assert result.created == []


class TestIdempotency:
    def test_rerun_creates_nothing_and_reports_existing(
        self, db_session, windowed_incident, make_payment, make_order
    ):
        incident = windowed_incident()
        make_payment(status="failed")
        make_order(status="created")
        builder = OpportunityBuilder(db_session)

        first = builder.build_for_incident(incident.id)
        db_session.commit()
        second = builder.build_for_incident(incident.id)

        assert len(first.created) == 2
        assert second.created == []
        assert len(second.existing) == 2
        total = db_session.query(models.RecoveryOpportunity).count()
        assert total == 2

    def test_rerun_after_new_failures_adds_only_the_delta(
        self, db_session, windowed_incident, make_payment
    ):
        incident = windowed_incident()
        make_payment(status="failed")
        builder = OpportunityBuilder(db_session)
        builder.build_for_incident(incident.id)
        db_session.commit()

        late = make_payment(status="failed")
        result = builder.build_for_incident(incident.id)

        assert [o.payment_id for o in result.created] == [late.id]
        assert len(result.existing) == 1

    def test_every_created_opportunity_is_audited(
        self, db_session, windowed_incident, make_payment
    ):
        incident = windowed_incident()
        make_payment(status="failed")

        result = OpportunityBuilder(db_session).build_for_incident(
            incident.id, actor="agent:strategist"
        )

        rows = (
            db_session.query(models.AuditLog)
            .filter_by(entity_type="recovery_opportunity", entity_id=result.created[0].id)
            .all()
        )
        assert [r.action for r in rows] == ["recovery.opportunity_created"]
        assert rows[0].actor == "agent:strategist"


class TestGuards:
    def test_unknown_incident_raises(self, db_session):
        with pytest.raises(ValueError, match="incident not found"):
            OpportunityBuilder(db_session).build_for_incident("inc_missing")
