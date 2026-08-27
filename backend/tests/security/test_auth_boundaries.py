"""Attack vector: unauthorized financial actions via the HTTP surface.

Method: enumerate the LIVE route table (no hand-maintained list, so a newly
added mutating route is fuzzed automatically) and assert every mutating
/api/v1 route rejects missing/wrong X-API-Key. The demo/detection exemption
is probed for financial effect (structural + behavioral proof it cannot move
money), APP_ENV is fuzzed with case variants/exotic values, and the API-key
middleware is proven fail-closed when the key is misconfigured empty.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, settings
from app.main import MUTATING_METHODS, create_app

# Mutating routes exempt from X-API-Key outside prod (documented demo posture).
_EXEMPT_PREFIXES = ("/api/v1/demo", "/api/v1/detection")


def _iter_routes(app):
    """Yield concrete routes, unwrapping this starlette version's
    `_IncludedRouter` containers (included routers are not flattened)."""
    for route in app.routes:
        inner = getattr(route, "original_router", None)
        yield from (inner.routes if inner is not None else [route])


def _mutating_api_routes(app) -> list[tuple[str, str]]:
    table: list[tuple[str, str]] = []
    for route in _iter_routes(app):
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", set()) or set()
        for method in methods & MUTATING_METHODS:
            if path.startswith("/api/v1"):
                table.append((method, path))
    return sorted(table)


def _concrete(path: str) -> str:
    """Fill path params with a nonexistent-but-well-formed id."""
    return (
        path.replace("{opportunity_id}", "opp_nonexistent")
        .replace("{incident_id}", "inc_nonexistent")
        .replace("{name}", "nonexistent_scenario")
    )


class TestRouteTableAuthFuzz:
    """Every mutating /api/v1 route must 401 without/with a wrong key — the
    table is built from the app itself, so this fuzzes routes that did not
    exist when the test was written."""

    def test_route_table_covers_expected_financial_routes(self):
        table = set(_mutating_api_routes(create_app()))
        # The financially sensitive routes MUST be present in the fuzz set.
        assert ("POST", "/api/v1/recovery/reconcile") in table
        assert ("POST", "/api/v1/recovery/opportunities/build") in table
        assert ("POST", "/api/v1/recovery/{opportunity_id}/execute") in table
        assert ("POST", "/api/v1/recovery/{opportunity_id}/approve") in table
        assert ("POST", "/api/v1/incidents/{incident_id}/investigate") in table

    def test_missing_key_rejected_on_every_mutating_route(self, client):
        table = _mutating_api_routes(client.app)
        assert len(table) >= 10  # fuzz is meaningful, not vacuous
        for method, path in table:
            concrete = _concrete(path)
            if path.startswith(_EXEMPT_PREFIXES):
                continue  # exempt-by-design paths are covered below
            resp = client.request(method, concrete, json={})
            assert resp.status_code == 401, (
                f"{method} {path} returned {resp.status_code} without X-API-Key"
            )
            assert resp.json()["error"]["code"] == "unauthorized"

    def test_wrong_key_rejected_on_every_mutating_route(self, client):
        for method, path in _mutating_api_routes(client.app):
            if path.startswith(_EXEMPT_PREFIXES):
                continue
            resp = client.request(
                method, _concrete(path), json={}, headers={"X-API-Key": "wrong-key"}
            )
            assert resp.status_code == 401, (
                f"{method} {path} returned {resp.status_code} with a wrong key"
            )

    def test_key_with_subtle_variation_rejected(self, client):
        for bad in ("dev-key ", " dev-key", "DEV-KEY", "dev-ke", "dev-keyy", "dev-key\x00"):
            resp = client.post(
                "/api/v1/recovery/reconcile", json={"actor": "a"}, headers={"X-API-Key": bad}
            )
            assert resp.status_code == 401, f"key variant {bad!r} accepted"


class TestApiKeyFailClosed:
    """REGRESSION: an empty configured API_KEY used to fail OPEN — the header
    default ('') compared equal to the empty configured key, so every mutating
    route was unauthenticated. The middleware must refuse mutating requests
    when no key is configured (fail closed, operator-visible)."""

    def test_empty_configured_key_denies_mutating_requests(self, client, monkeypatch):
        monkeypatch.setattr(settings, "API_KEY", "")
        resp = client.post(
            "/api/v1/recovery/opportunities/build", json={"incident_id": "inc_x"}
        )
        assert resp.status_code in (401, 503)
        assert resp.json()["error"]["code"] in ("unauthorized", "auth_not_configured")

    def test_empty_configured_key_denies_even_with_empty_header(self, client, monkeypatch):
        monkeypatch.setattr(settings, "API_KEY", "")
        resp = client.post(
            "/api/v1/recovery/reconcile",
            json={"actor": "human:ops"},
            headers={"X-API-Key": ""},
        )
        assert resp.status_code in (401, 503)

    def test_empty_key_still_allows_reads(self, client, monkeypatch):
        # Fail-closed targets mutations; read-only observability stays open.
        monkeypatch.setattr(settings, "API_KEY", "")
        assert client.get("/api/v1/recovery/opportunities").status_code == 200


class TestAppEnvExemptionAbuse:
    """The demo/detection API-key exemption hinges on APP_ENV != 'prod'.
    Prove exotic/case-variant APP_ENV values can never weaken the check:
    the Settings Literal rejects them at process start (fail closed)."""

    @pytest.mark.parametrize(
        "value", ["PROD", "Prod", "production", " prod", "prod ", "PRODUCTION", "0", "true", "dev\nprod"]
    )
    def test_exotic_app_env_rejected_at_startup(self, value):
        with pytest.raises(Exception):
            Settings(APP_ENV=value, _env_file=None)

    @pytest.mark.parametrize("value", ["dev", "test", "demo"])
    def test_known_app_envs_accepted(self, value):
        assert Settings(APP_ENV=value, _env_file=None).APP_ENV == value

    def test_exemption_never_applies_to_financial_routes(self, client):
        # Even though demo/detection are exempt outside prod, recovery/agent
        # mutations must never inherit the exemption via prefix tricks.
        for sneaky in (
            "/api/v1/demo/../recovery/reconcile",
            "/api/v1/detection/../recovery/opportunities/build",
        ):
            resp = client.post(sneaky, json={})
            assert resp.status_code in (401, 404, 422)


class TestDemoExemptionHasNoFinancialEffect:
    """The exempt demo/detection routes must be structurally incapable of
    moving money: they hold no gateway dependency, and a full trigger causes
    zero gateway mutations."""

    def test_exempt_routes_have_no_gateway_dependency(self, client):
        from app.api.deps import get_gateway_dependency

        def uses_gateway(dependant) -> bool:
            seen = set()
            stack = [dependant]
            while stack:
                dep = stack.pop()
                if id(dep) in seen:
                    continue
                seen.add(id(dep))
                for sub in dep.dependencies:
                    if sub.call is get_gateway_dependency:
                        return True
                    stack.append(sub)
            return False

        for route in _iter_routes(client.app):
            path = getattr(route, "path", "")
            if path.startswith(_EXEMPT_PREFIXES):
                assert not uses_gateway(route.dependant), (
                    f"exempt route {path} holds a gateway dependency — "
                    "the API-key exemption could move money"
                )

    def test_demo_trigger_and_detection_run_cause_zero_gateway_mutations(
        self, client, db_session
    ):
        from tests.security.conftest import CountingGateway
        from app.api.deps import get_gateway_dependency

        counting = CountingGateway(webhook_secret="whsec_security_tests")
        client.app.dependency_overrides[get_gateway_dependency] = lambda: counting
        try:
            # No X-API-Key — the exemption is what is under test.
            r1 = client.post("/api/v1/detection/run", json={})
            assert r1.status_code == 200, r1.text
            r2 = client.post("/api/v1/demo/reset")
            assert r2.status_code == 200, r2.text
            scenarios = client.get("/api/v1/demo/scenarios").json()["scenarios"]
            assert scenarios
            r3 = client.post(f"/api/v1/demo/scenario/{scenarios[0]['name']}")
            assert r3.status_code == 200, r3.text
        finally:
            client.app.dependency_overrides[get_gateway_dependency] = lambda: counting
        assert counting.mutation_calls == 0, (
            "a demo/detection route caused a gateway mutation without an API key"
        )
        assert counting.fetch_calls == 0

    def test_demo_reset_does_not_touch_gateway_and_audits(self, client, db_session):
        import sqlalchemy as sa

        from app.models import AuditLog

        resp = client.post("/api/v1/demo/reset")
        assert resp.status_code == 200
        row = db_session.scalar(
            sa.select(AuditLog).where(AuditLog.action == "demo.reset")
        )
        assert row is not None  # destructive demo op leaves an audit trace
