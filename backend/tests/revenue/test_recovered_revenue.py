"""recovered_revenue: the measured, verification-proven dashboard number."""

from datetime import datetime, timedelta, timezone

from app.ports import ActionType, RecoveryStatus
from app.services.revenue import RevenueService

REF = datetime(2026, 8, 20, 12, 0, 0, tzinfo=timezone.utc)
DEFAULT_WINDOW = timedelta(hours=1)

WIN_END = REF + DEFAULT_WINDOW


def _mk_action(make_action, make_opportunity, incident, **kw):
    opp = kw.pop("opportunity", None) or make_opportunity(incident_id=incident.id)
    return make_action(opportunity=opp, incident_id=incident.id, **kw)


def test_only_verified_recovered_actions_count(
    db_session, make_incident, make_action, make_opportunity
):
    incident = make_incident()
    in_win = REF + timedelta(minutes=10)
    _mk_action(
        make_action, make_opportunity, incident,
        status=RecoveryStatus.RECOVERED, amount_paise=300_000, verified_at=in_win,
        action_type=ActionType.RETRY_PAYMENT,
    )
    _mk_action(
        make_action, make_opportunity, incident,
        status=RecoveryStatus.RECOVERED, amount_paise=150_000, verified_at=in_win,
        action_type=ActionType.CREATE_PAYMENT_LINK,
    )
    # Excluded: right status, wrong window.
    _mk_action(
        make_action, make_opportunity, incident,
        status=RecoveryStatus.RECOVERED, amount_paise=999_000,
        verified_at=REF - timedelta(days=2),
    )
    # Excluded: unverified / failed / in-flight outcomes.
    _mk_action(make_action, make_opportunity, incident,
               status=RecoveryStatus.FAILED, amount_paise=50_000, completed_at=in_win)
    _mk_action(make_action, make_opportunity, incident,
               status=RecoveryStatus.UNKNOWN, amount_paise=70_000, completed_at=in_win)
    _mk_action(make_action, make_opportunity, incident,
               status=RecoveryStatus.EXECUTING, amount_paise=80_000)

    report = RevenueService(db_session).recovered_revenue(REF, WIN_END)

    assert report.total_recovered_paise == 450_000
    assert report.recovered_actions_count == 2
    # UNKNOWN is surfaced, never folded into the total.
    assert report.unknown_actions_count == 1
    assert report.by_incident == {incident.id: 450_000}
    assert report.by_action_type == {"retry_payment": 300_000, "create_payment_link": 150_000}


def test_incident_filter(db_session, make_incident, make_action, make_opportunity):
    inc_a = make_incident()
    inc_b = make_incident()
    in_win = REF + timedelta(minutes=10)
    _mk_action(make_action, make_opportunity, inc_a,
               status=RecoveryStatus.RECOVERED, amount_paise=100_000, verified_at=in_win)
    _mk_action(make_action, make_opportunity, inc_b,
               status=RecoveryStatus.RECOVERED, amount_paise=200_000, verified_at=in_win)

    svc = RevenueService(db_session)
    only_a = svc.recovered_revenue(REF, WIN_END, incident_id=inc_a.id)
    assert only_a.total_recovered_paise == 100_000
    both = svc.recovered_revenue(REF, WIN_END)
    assert both.total_recovered_paise == 300_000


def test_completed_at_used_when_verified_at_missing(
    db_session, make_incident, make_action, make_opportunity
):
    incident = make_incident()
    _mk_action(
        make_action, make_opportunity, incident,
        status=RecoveryStatus.RECOVERED, amount_paise=42_000, verified_at=None,
        completed_at=REF + timedelta(minutes=5),
    )
    report = RevenueService(db_session).recovered_revenue(REF, WIN_END)
    assert report.total_recovered_paise == 42_000


def test_empty_window_is_zero(db_session):
    report = RevenueService(db_session).recovered_revenue(REF, WIN_END)
    assert report.total_recovered_paise == 0
    assert report.recovered_actions_count == 0
    assert report.unknown_actions_count == 0
    assert report.by_incident == {}
