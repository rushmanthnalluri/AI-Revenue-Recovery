"""Attack vector: malformed and adversarial inputs on the public API.

Wrong types for every public POST body (422, safe envelope, no 500),
pagination abuse, SQL-injection-shaped filter values (proves the query layer
is parameterized), unicode/emoji ids, null bytes, and NaN/Infinity confidence
reaching the policy gate through the agent tool path.
"""

from __future__ import annotations

import pytest

import app.models as models
from app.ports import RecoveryStatus
from app.services.agent.tools import AgentTools, ToolNotAllowed

API_KEY = {"X-API-Key": "dev-key"}


class TestWrongTypesForPublicPostBodies:
    """Every public POST body is pydantic-validated: wrong types -> 422 with
    the safe envelope, never a 500 leaking internals."""

    def test_recovery_bodies_reject_wrong_types(self, client):
        opp = "opp_nonexistent"
        cases = [
            ("/api/v1/recovery/opportunities/build", {"incident_id": 123}),
            ("/api/v1/recovery/opportunities/build", {"incident_id": ["inc_x"]}),
            ("/api/v1/recovery/opportunities/build", {"incident_id": None}),
            ("/api/v1/recovery/opportunities/build", "not-an-object"),
            (f"/api/v1/recovery/{opp}/execute", {"strategy_id": 42}),
            (f"/api/v1/recovery/{opp}/execute", {"actor": {"name": "x"}}),
            (f"/api/v1/recovery/{opp}/approve", {"note": [1, 2]}),
            (f"/api/v1/recovery/{opp}/reject", {"reason": None}),
            (f"/api/v1/recovery/{opp}/escalate", {}),
            (f"/api/v1/recovery/{opp}/cancel", {"reason": 3.14}),
            ("/api/v1/recovery/reconcile", {"actor": True}),
            ("/api/v1/incidents/inc_x/investigate", {"force_refresh": "yes-please"}),
        ]
        for path, body in cases:
            r = client.post(path, json=body, headers=API_KEY)
            assert r.status_code == 422, f"{path} {body!r} -> {r.status_code}"
            err = r.json()["error"]
            assert err["code"] == "validation_error"
            # The safe envelope never echoes internals beyond a static message.
            assert err["message"] == "Request validation failed."

    def test_garbage_content_type_and_body(self, client):
        r = client.post(
            "/api/v1/recovery/opportunities/build",
            content=b"\x89PNG\r\n garbage",
            headers={**API_KEY, "Content-Type": "application/json"},
        )
        assert r.status_code in (400, 422)  # unparseable body, safe envelope
        assert "internal" not in r.text.lower()


class TestPaginationAbuse:
    @pytest.mark.parametrize(
        "param", ["page=0", "page=-1", "page_size=0", "page_size=-5", "page_size=1000000000"]
    )
    def test_out_of_range_pagination_is_422(self, client, param):
        assert client.get(f"/api/v1/recovery/opportunities?{param}").status_code == 422
        assert client.get(f"/api/v1/audit?{param}").status_code == 422

    def test_huge_page_number_is_safe_empty_page(self, client):
        r = client.get("/api/v1/recovery/opportunities?page=1000000000&page_size=20")
        assert r.status_code == 200
        assert r.json()["items"] == []

    def test_non_numeric_pagination_is_422(self, client):
        assert client.get("/api/v1/audit?page=abc").status_code == 422


class TestSqlInjectionShapedInputs:
    """Filter params flow through SQLAlchemy bound parameters; prove the
    classic shapes neither error nor bypass filters, and tables survive."""

    PAYLOADS = [
        "' OR '1'='1",
        "1; DROP TABLE payments;--",
        "' UNION SELECT * FROM audit_logs--",
        "%' OR 1=1 --",
        "x' AND (SELECT 1 FROM (SELECT SLEEP(5))x)--",
    ]

    def test_opportunity_filters_are_parameterized(self, client, make_opportunity):
        make_opportunity()  # one legitimate row that must NOT match the payloads
        for payload in self.PAYLOADS:
            r = client.get(f"/api/v1/recovery/opportunities?customer_id={payload}")
            assert r.status_code == 200
            assert r.json()["items"] == [], f"payload {payload!r} bypassed the filter"
        # The table still exists and the row is intact.
        r = client.get("/api/v1/recovery/opportunities")
        assert r.status_code == 200 and r.json()["total"] == 1

    def test_audit_filters_are_parameterized(self, client):
        for payload in self.PAYLOADS:
            r = client.get(f"/api/v1/audit?entity_type={payload}&entity_id={payload}")
            assert r.status_code == 200
            assert r.json()["items"] == []


class TestUnicodeAndHostileIds:
    def test_emoji_and_unicode_ids_404_not_500(self, client):
        for weird in ("🔥💳", "opp_%20%20", "opp_\u202ereverse", "के लिए"):
            r = client.get(f"/api/v1/recovery/{weird}")
            assert r.status_code in (404, 422), f"{weird!r} -> {r.status_code}"
            r = client.post(
                f"/api/v1/recovery/{weird}/execute", json={"actor": "a"}, headers=API_KEY
            )
            assert r.status_code in (404, 422), f"execute {weird!r} -> {r.status_code}"

    def test_null_byte_and_sql_in_incident_id_body(self, client):
        r = client.post(
            "/api/v1/recovery/opportunities/build",
            json={"incident_id": "inc_\x00'; DROP TABLE incidents;--"},
            headers=API_KEY,
        )
        assert r.status_code == 404  # unknown incident, cleanly
        assert client.get("/api/v1/incidents").status_code == 200  # table intact


class TestExtremeNumericInputs:
    """NaN/Infinity confidence can arrive via LLM tool-call JSON (Python's
    json accepts the constants); the policy gate must fail closed. Huge
    amounts must take the approval lane, never auto-execute."""

    def test_nan_and_inf_confidence_fail_closed_via_agent_tool(
        self, db_session, make_incident, make_payment
    ):
        """Mutation tools refuse non-finite confidence BEFORE any row exists
        (NaN used to crash the INSERT with an IntegrityError — the gate never
        even saw it). The dry-run propose path still passes NaN to the gate,
        which BLOCKs it as malformed.confidence."""
        from app.services.agent.tools import ToolError

        incident = make_incident()
        for bad in (float("nan"), float("inf"), float("-inf")):
            payment = make_payment(status="failed")
            tools = AgentTools(db_session, incident_id=incident.id)
            with pytest.raises(ToolError):
                tools.call(
                    "request_recovery_execution",
                    {
                        "action_type": "retry_payment",
                        "payment_id": payment.id,
                        "confidence": bad,
                    },
                )
            # The dry-run surface proves the gate itself fails closed too.
            preview = tools.call(
                "propose_recovery_strategy",
                {
                    "action_type": "retry_payment",
                    "payment_id": payment.id,
                    "confidence": bad,
                },
            )
            assert preview.data["policy"]["outcome"] == "BLOCKED"
            assert "malformed.confidence" in preview.data["policy"]["rules_matched"]
        assert (
            db_session.query(models.RecoveryAction)
            .filter_by(incident_id=incident.id)
            .count()
            == 0
        )

    def test_int64_max_amount_takes_approval_lane_via_agent_tool(
        self, db_session, make_incident, make_payment
    ):
        incident = make_incident()
        payment = make_payment(amount_paise=2**63 - 1, status="failed")
        tools = AgentTools(db_session, incident_id=incident.id)
        result = tools.call(
            "request_payment_link", {"payment_id": payment.id, "confidence": 0.99}
        )
        assert result.data["policy"]["outcome"] == "REQUIRES_APPROVAL"
        assert result.data["executed"] is False
        action = db_session.get(models.RecoveryAction, result.data["action_id"])
        assert action.status is RecoveryStatus.PENDING_APPROVAL

    def test_non_numeric_confidence_raises_before_any_row(
        self, db_session, make_incident, make_payment
    ):
        """float({...}) is a TypeError raised BEFORE the action row is built —
        the LLM loop catches TypeError and feeds it back as a tool error."""
        incident = make_incident()
        payment = make_payment(status="failed")
        tools = AgentTools(db_session, incident_id=incident.id)
        with pytest.raises(TypeError):
            tools.call(
                "request_recovery_execution",
                {
                    "action_type": "retry_payment",
                    "payment_id": payment.id,
                    "confidence": {"value": 0.9},
                },
            )
        assert (
            db_session.query(models.RecoveryAction)
            .filter_by(incident_id=incident.id)
            .count()
            == 0
        )


class TestNonWhitelistedToolNames:
    def test_arbitrary_tool_names_refused(self, db_session, make_incident):
        tools = AgentTools(db_session, incident_id=make_incident().id)
        for name in (
            "execute_shell",
            "__import__",
            "eval",
            "request_recovery_execution; DROP TABLE",
            "request_refund",
            "",
        ):
            with pytest.raises(ToolNotAllowed):
                tools.call(name, {})
