"""KYA-lite principal binding on the mutating recovery API.

The approver identity used to be a self-declared actor string only. Now every
mutating call also carries a Principal derived from the presented X-API-Key
(app.api.deps.get_principal): the executor receives the principal-attributed
actor string (pass-through for the shared demo cohort key, `actor@kya:<id>`
for non-default keys), and one additive `recovery.principal_bound` audit row
records the binding on every successful mutation.

These tests pin: the binding row's shape/environment stamp, the backwards
compatibility of the default cohort (approved_by unchanged), attribution for
non-default keys, and that refused calls bind nothing. Demo-grade identity —
NOT SSO; see docs/security-testing.md.
"""

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

import app.models as models
from app.api import deps
from app.api.deps import get_gateway_dependency
from app.config import settings
from app.db import get_db
from app.main import create_app
from app.ports import RecoveryStatus

API_KEY = {"X-API-Key": "dev-key"}
# Deterministic approval lane: above the ₹5,000 auto-execute ceiling.
APPROVAL_AMOUNT = 5_000_000


@pytest.fixture()
def api_client(db_session, sim_gateway):
    app = create_app()

    def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_gateway_dependency] = lambda: sim_gateway
    with TestClient(app) as c:
        yield c


def _binding_rows(db_session, entity_id=None):
    stmt = sa.select(models.AuditLog).where(
        models.AuditLog.action == "recovery.principal_bound"
    )
    if entity_id is not None:
        stmt = stmt.where(models.AuditLog.entity_id == entity_id)
    return list(db_session.scalars(stmt))


def _open_action(db_session, opportunity_id):
    return db_session.scalar(
        sa.select(models.RecoveryAction).where(
            models.RecoveryAction.opportunity_id == opportunity_id
        )
    )


class TestPrincipalUnits:
    """deps.Principal semantics: attribution, parsing, table lookup."""

    def test_default_cohort_passes_actor_through(self):
        principal = deps.Principal(deps.DEFAULT_PRINCIPAL)
        assert principal.authenticated
        assert principal.attributed_actor("human:ops") == "human:ops"

    def test_unauthenticated_passes_actor_through(self):
        principal = deps.Principal(deps.UNAUTHENTICATED_PRINCIPAL)
        assert not principal.authenticated
        assert principal.attributed_actor("human:ops") == "human:ops"

    def test_non_default_principal_attributes(self):
        principal = deps.Principal("ops-lead")
        assert principal.attributed_actor("human:ops") == "human:ops@kya:ops-lead"

    def test_principal_parsing_ignores_email_shaped_actors(self):
        # Existing actor values are email-shaped; a bare "@" must NOT parse
        # as a principal — only the explicit @kya: marker does.
        assert deps.Principal.principal_of_actor("human:ops@pulserecover.demo") is None
        assert deps.Principal.principal_of_actor("agent:strategist") is None
        assert deps.Principal.principal_of_actor("human:ops@kya:ops-lead") == "ops-lead"

    def test_dependency_maps_known_key_and_falls_back(self):
        assert deps.get_principal("dev-key").id == "demo-operator"
        assert deps.get_principal("not-a-known-key").id == "unauthenticated"
        assert deps.get_principal(None).id == "unauthenticated"


class TestPrincipalBindingAudit:
    def test_execute_records_binding_row(self, api_client, db_session, make_opportunity, failed_payment):
        opp = make_opportunity(payment=failed_payment())
        db_session.commit()

        resp = api_client.post(
            f"/api/v1/recovery/{opp.id}/execute",
            json={"actor": "human:console"},
            headers=API_KEY,
        )
        assert resp.status_code == 200
        action = _open_action(db_session, opp.id)
        (row,) = _binding_rows(db_session, entity_id=opp.id)
        assert row.entity_type == "recovery_opportunity"
        assert row.details["action_id"] == action.id
        assert row.actor == "human:console"  # default cohort: pass-through
        assert row.details["principal_id"] == "demo-operator"
        assert row.details["declared_actor"] == "human:console"
        assert row.details["endpoint"] == "execute"
        assert row.details["authenticated"] is True
        # environment stamped like every other recovery audit row
        assert row.environment == action.environment == "research"

    def test_approve_binds_and_preserves_approved_by(
        self, api_client, db_session, make_opportunity, failed_payment
    ):
        """COMPAT: the default cohort key must NOT rewrite approved_by —
        invariant tests outside this package pin the exact actor string."""
        opp = make_opportunity(amount_paise=APPROVAL_AMOUNT, payment=failed_payment())
        db_session.commit()
        r1 = api_client.post(
            f"/api/v1/recovery/{opp.id}/execute", json={"actor": "human:console"}, headers=API_KEY
        )
        assert r1.json()["status"] == "PENDING_APPROVAL"

        r2 = api_client.post(
            f"/api/v1/recovery/{opp.id}/approve",
            json={"actor": "human:ops", "note": "reviewed"},
            headers=API_KEY,
        )
        assert r2.status_code == 200
        action = _open_action(db_session, opp.id)
        assert action.approved_by == "human:ops"
        (row,) = [r for r in _binding_rows(db_session, entity_id=opp.id) if r.details["endpoint"] == "approve"]
        assert row.details["principal_id"] == "demo-operator"
        assert row.details["action_id"] == action.id
        assert row.actor == "human:ops"

    @pytest.mark.parametrize("verb,body", [
        ("reject", {"actor": "human:ops", "reason": "not worth it"}),
        ("escalate", {"actor": "human:ops", "reason": "needs a human"}),
        ("cancel", {"actor": "human:ops", "reason": "customer paid directly"}),
    ])
    def test_action_level_mutations_bind(
        self, verb, body, api_client, db_session, make_opportunity, make_proposed_action
    ):
        opp = make_opportunity()
        action = make_proposed_action(opp)
        db_session.commit()

        resp = api_client.post(
            f"/api/v1/recovery/{opp.id}/{verb}", json=body, headers=API_KEY
        )
        assert resp.status_code == 200, resp.text
        rows = _binding_rows(db_session, entity_id=opp.id)
        assert len(rows) == 1
        assert rows[0].entity_type == "recovery_opportunity"
        assert rows[0].details["action_id"] == action.id
        assert rows[0].details["endpoint"] == verb
        assert rows[0].details["principal_id"] == "demo-operator"

    def test_opportunity_level_mutation_binds_to_the_opportunity(
        self, api_client, db_session, make_opportunity
    ):
        """Reject/escalate/cancel on an opportunity with NO action bind the
        principal to the opportunity row (entity_type recovery_opportunity)."""
        opp = make_opportunity()
        db_session.commit()

        resp = api_client.post(
            f"/api/v1/recovery/{opp.id}/escalate",
            json={"actor": "human:ops", "reason": "handoff"},
            headers=API_KEY,
        )
        assert resp.status_code == 200
        (row,) = _binding_rows(db_session, entity_id=opp.id)
        assert row.entity_type == "recovery_opportunity"
        assert row.details["endpoint"] == "escalate"
        assert row.environment == "research"

    def test_real_test_environment_stamped_on_binding(
        self, api_client, db_session, make_opportunity
    ):
        """Environment scoping is never mixed: a real_test mutation's binding
        row is stamped real_test (escalate needs no gateway, so this is
        deterministic regardless of configured Razorpay keys)."""
        opp = make_opportunity(environment="real_test")
        db_session.commit()
        resp = api_client.post(
            f"/api/v1/recovery/{opp.id}/escalate",
            json={"actor": "human:ops", "reason": "handoff"},
            headers=API_KEY,
        )
        assert resp.status_code == 200, resp.text
        (row,) = _binding_rows(db_session, entity_id=opp.id)
        assert row.environment == "real_test"

    def test_refused_call_binds_nothing(self, api_client, db_session, make_opportunity):
        """A 409 (no action awaiting approval) rolls back: no binding row."""
        opp = make_opportunity()
        db_session.commit()
        resp = api_client.post(
            f"/api/v1/recovery/{opp.id}/approve", json={"actor": "human:ops"}, headers=API_KEY
        )
        assert resp.status_code == 409
        assert _binding_rows(db_session) == []


class TestNonDefaultKeyAttribution:
    """A deployment-issued per-operator key gets actor@kya:<principal>
    attribution end to end — executor rows, approved_by, and the binding."""

    def test_attributed_actor_reaches_executor_and_audit(
        self, api_client, db_session, make_opportunity, failed_payment, monkeypatch
    ):
        monkeypatch.setitem(deps.PRINCIPAL_BY_API_KEY, "kya-ops-key", "ops-lead")
        monkeypatch.setattr(settings, "API_KEY", "kya-ops-key")
        headers = {"X-API-Key": "kya-ops-key"}

        opp = make_opportunity(amount_paise=APPROVAL_AMOUNT, payment=failed_payment())
        db_session.commit()
        r1 = api_client.post(
            f"/api/v1/recovery/{opp.id}/execute", json={"actor": "human:ops"}, headers=headers
        )
        assert r1.status_code == 200, r1.text
        assert r1.json()["status"] == "PENDING_APPROVAL"
        action = _open_action(db_session, opp.id)
        assert action.actor == "human:ops@kya:ops-lead"  # proposer attributed

        r2 = api_client.post(
            f"/api/v1/recovery/{opp.id}/approve", json={"actor": "human:ops"}, headers=headers
        )
        assert r2.status_code == 200, r2.text
        db_session.refresh(action)
        assert action.approved_by == "human:ops@kya:ops-lead"  # approver attributed
        rows = _binding_rows(db_session, entity_id=opp.id)
        assert rows and all(r.details["principal_id"] == "ops-lead" for r in rows)
        # …and the proposer principal round-trips for the SoD check
        assert deps.Principal.principal_of_actor(action.actor) == "ops-lead"
