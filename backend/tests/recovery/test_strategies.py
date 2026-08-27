"""Strategy generator tests: candidates, ranking, recommendation, eligibility."""

import app.models as models
from app.ports import ActionType
from app.services.recovery import DELAY_SECONDS, StrategyGenerator


def _by_constraints(rows):
    """Split retry_payment rows into (immediate, delayed)."""
    immediate = [r for r in rows if r.action_type is ActionType.RETRY_PAYMENT and not r.constraints]
    delayed = [r for r in rows if r.action_type is ActionType.RETRY_PAYMENT and r.constraints]
    return immediate, delayed


class TestCandidateSet:
    def test_six_candidates_with_delayed_retry_variant(
        self, db_session, make_opportunity, failed_payment
    ):
        opp = make_opportunity(payment=failed_payment())
        rows = StrategyGenerator(db_session).generate(opp)

        assert len(rows) == 6
        types = {r.action_type for r in rows}
        assert types == {
            ActionType.RETRY_PAYMENT,
            ActionType.CREATE_PAYMENT_LINK,
            ActionType.NOTIFY_CUSTOMER,
            ActionType.ESCALATE_HUMAN,
            ActionType.NO_ACTION,
        }
        _, delayed = _by_constraints(rows)
        assert len(delayed) == 1
        assert delayed[0].constraints == {"delay_seconds": DELAY_SECONDS}

    def test_expected_recovery_uses_revenue_priors(
        self, db_session, make_opportunity, failed_payment
    ):
        payment = failed_payment(amount_paise=100_000)
        opp = make_opportunity(payment=payment, amount_paise=100_000)
        rows = StrategyGenerator(db_session).generate(opp)
        by_type = {}
        for r in rows:
            by_type.setdefault(r.action_type, r)

        # timeout recoverability 0.70; retry effectiveness 0.50 -> 35_000
        assert by_type[ActionType.RETRY_PAYMENT].expected_recovery_paise == 35_000
        # link effectiveness 0.30 -> 21_000
        assert by_type[ActionType.CREATE_PAYMENT_LINK].expected_recovery_paise == 21_000
        # no_action never inflates the plan
        assert by_type[ActionType.NO_ACTION].expected_recovery_paise == 0

    def test_recommendation_is_highest_eligible_expected_recovery(
        self, db_session, make_opportunity, failed_payment
    ):
        opp = make_opportunity(payment=failed_payment())
        rows = StrategyGenerator(db_session).generate(opp)
        recommended = next(r for r in rows if r.selected)

        assert recommended.action_type is ActionType.RETRY_PAYMENT
        assert recommended.constraints == {}  # immediate beats delayed on tie
        assert recommended.rank == 0
        # every eligible candidate ranks above every ineligible one
        ordered = sorted(rows, key=lambda r: r.rank)
        flags = [r.eligibility for r in ordered]
        assert flags == sorted(flags, reverse=True)


class TestConfidence:
    def test_diagnosis_confidence_drives_auto_execute_band(
        self, db_session, make_opportunity, make_diagnosis, failed_payment
    ):
        opp = make_opportunity(payment=failed_payment())
        incident = db_session.get(models.Incident, opp.incident_id)
        make_diagnosis(incident, confidence=0.95)
        rows = StrategyGenerator(db_session).generate(opp)
        retry = next(
            r for r in rows if r.action_type is ActionType.RETRY_PAYMENT and not r.constraints
        )
        # 0.95 evidence x 0.98 timeout fit
        assert retry.confidence == round(0.95 * 0.98, 4)
        assert retry.confidence >= 0.85  # auto-execute band

    def test_without_diagnosis_confidence_stays_in_approval_band(
        self, db_session, make_opportunity, failed_payment
    ):
        opp = make_opportunity(payment=failed_payment())
        rows = StrategyGenerator(db_session).generate(opp)
        retry = next(
            r for r in rows if r.action_type is ActionType.RETRY_PAYMENT and not r.constraints
        )
        assert retry.confidence == round(0.80 * 0.98, 4)
        assert retry.confidence < 0.85


class TestEligibility:
    def test_opted_out_customer_disables_notify(
        self, db_session, make_opportunity, make_customer, failed_payment
    ):
        customer = make_customer(opted_out=True)
        opp = make_opportunity(payment=failed_payment(customer_id=customer.id))
        rows = StrategyGenerator(db_session).generate(opp)
        notify = next(r for r in rows if r.action_type is ActionType.NOTIFY_CUSTOMER)

        assert notify.eligibility is False
        assert "opted out" in notify.reason

    def test_hard_decline_disables_retry(
        self, db_session, make_opportunity, make_payment
    ):
        payment = make_payment(
            status="failed",
            error_code="BAD_REQUEST_ERROR",
            error_description="card_number_invalid",
            error_source="bank",
        )
        opp = make_opportunity(payment=payment)
        rows = StrategyGenerator(db_session).generate(opp)
        immediate, delayed = _by_constraints(rows)

        assert immediate[0].eligibility is False
        assert delayed[0].eligibility is False
        # the recommendation falls back to a non-retry action
        recommended = next(r for r in rows if r.selected)
        assert recommended.action_type is not ActionType.RETRY_PAYMENT

    def test_tiny_amount_disables_payment_link(
        self, db_session, make_opportunity, failed_payment
    ):
        opp = make_opportunity(payment=failed_payment(amount_paise=50), amount_paise=50)
        rows = StrategyGenerator(db_session).generate(opp)
        link = next(r for r in rows if r.action_type is ActionType.CREATE_PAYMENT_LINK)
        assert link.eligibility is False

    def test_dropped_checkout_recommends_payment_link(
        self, db_session, make_opportunity
    ):
        opp = make_opportunity(
            payment=None, opportunity_type="dropped_checkout", amount_paise=80_000
        )
        rows = StrategyGenerator(db_session).generate(opp)
        recommended = next(r for r in rows if r.selected)

        assert recommended.action_type is ActionType.CREATE_PAYMENT_LINK
        retry = [r for r in rows if r.action_type is ActionType.RETRY_PAYMENT]
        assert all(not r.eligibility for r in retry)


class TestIdempotency:
    def test_regeneration_returns_the_persisted_rows(
        self, db_session, make_opportunity, failed_payment
    ):
        opp = make_opportunity(payment=failed_payment())
        planner = StrategyGenerator(db_session)
        first = planner.generate(opp)
        db_session.commit()
        second = planner.generate(opp)

        assert [r.id for r in second] == [r.id for r in first]
        assert (
            db_session.query(models.RecoveryStrategy)
            .filter_by(opportunity_id=opp.id)
            .count()
            == 6
        )

    def test_opportunity_summary_backfilled_from_recommendation(
        self, db_session, make_opportunity, failed_payment
    ):
        opp = make_opportunity(payment=failed_payment())
        rows = StrategyGenerator(db_session).generate(opp)
        recommended = next(r for r in rows if r.selected)

        db_session.refresh(opp)
        assert opp.expected_recovery_paise == recommended.expected_recovery_paise
        assert opp.confidence == recommended.confidence
        assert opp.risk == recommended.risk
