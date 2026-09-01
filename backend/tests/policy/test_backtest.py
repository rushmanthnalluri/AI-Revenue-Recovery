"""Policy backtesting proofs — POST /api/v1/policy/backtest replays stored
policy decisions against the CURRENT policy document (docs/policy.md §7).

Covered: no flips when the policy is unchanged, flips (both directions) when
the policy version differs, empty history -> clean empty report, environment
scoping through the linked recovery action, window/limit filters, endpoint
determinism + read-only guarantee, and the mutating-route API key contract.
"""

from datetime import timedelta

import sqlalchemy as sa

import app.api.v1.policy as policy_api
import app.models as models
from app.db import utcnow
from app.ports import ActionType, PolicyOutcome
from app.schemas.policy import PolicyBacktestRequest
from app.services.policy.backtest import run_policy_backtest
from app.services.policy.config import (
    ActionsConfig,
    AutoExecuteConfig,
    PolicyConfig,
    RateLimitsConfig,
    RequireHumanApprovalConfig,
    StoppingRuleConfig,
)

API_KEY = {"X-API-Key": "dev-key"}

OLD_VERSION = "0.0-test+sha256.000000000000"


def _decision(db, **kw) -> models.PolicyDecisionRecord:
    """Persist a policy_decisions row as an older policy version recorded it
    (default: an ALLOWED ₹100.00 retry). ``context`` mirrors the original
    ActionContext, as engine._persist stores it."""
    base = dict(
        action_id=None,
        action_type="retry_payment",
        amount_paise=10_000,  # INR 100.00 — within the default ceiling
        currency="INR",
        confidence=0.95,
        outcome=PolicyOutcome.ALLOWED,
        reasons=["all auto-execute criteria met"],
        rules_matched=["auto_execute.ok"],
        policy_version=OLD_VERSION,
        actor="agent:strategist",
        context={
            "action_type": "retry_payment",
            "amount_paise": 10_000,
            "confidence": 0.95,
            "actor": "agent:strategist",
            "currency": "INR",
        },
        decided_at=utcnow(),
    )
    if "context" not in kw:
        for key in ("action_type", "amount_paise", "confidence", "actor", "currency"):
            if key in kw:
                base["context"][key] = kw[key]
    base.update(kw)
    record = models.PolicyDecisionRecord(**base)
    db.add(record)
    db.commit()
    return record


def _config(
    version: str,
    auto_execute_kw: dict | None = None,
    require_human_approval_kw: dict | None = None,
) -> PolicyConfig:
    """A current-policy stand-in built programmatically (never by editing the
    pinned policies/default.yaml)."""
    config = PolicyConfig(
        version=version,
        actions=ActionsConfig(allowlist=[a.value for a in ActionType]),
        never_auto_execute=["refund", "irreversible_action", "customer_opted_out"],
        auto_execute=AutoExecuteConfig(**(auto_execute_kw or {})),
        require_human_approval=RequireHumanApprovalConfig(**(require_human_approval_kw or {})),
        stopping_rule=StoppingRuleConfig(),
        rate_limits=RateLimitsConfig(),
    )
    config.policy_version = f"{version}+sha256.{'1' * 12}"
    return config


def _tight_config() -> PolicyConfig:
    """Ceiling lowered to ₹50.00: a recorded ₹100 ALLOWED replays as gated."""
    return _config("test-tight", auto_execute_kw={"max_amount_inr": 50})


def _loose_config() -> PolicyConfig:
    """Confidence floor lowered to 0.50: a recorded 0.80 REQUIRES_APPROVAL
    replays as ALLOWED (auto_execute + require_human_approval move together —
    the engine takes the stricter of the two)."""
    return _config(
        "test-loose",
        auto_execute_kw={"min_confidence": 0.5},
        require_human_approval_kw={"confidence_below": 0.5},
    )


class TestReplayUnchanged:
    def test_no_flips_when_policy_unchanged(self, db_session, policy_config):
        _decision(db_session)  # ALLOWED retry
        _decision(
            db_session,
            action_type="refund",
            outcome=PolicyOutcome.BLOCKED,
            rules_matched=["allowlist", "never_auto_execute.refund"],
            reasons=["refund can never be authorized by the policy gate"],
        )
        report = run_policy_backtest(db_session, PolicyBacktestRequest(), config=policy_config)
        assert report.decisions_scanned == 2
        assert report.flip_count == 0
        assert report.unchanged_count == 2
        assert report.flips == []
        assert report.transitions == []
        assert report.outcomes_original == {
            "ALLOWED": 1,
            "BLOCKED": 1,
            "REQUIRES_APPROVAL": 0,
        }
        assert report.outcomes_replayed == report.outcomes_original
        assert report.rule_hits.get("auto_execute.ok") == 1
        assert report.rule_hits.get("never_auto_execute.refund") == 1
        assert report.policy_version == policy_config.policy_version
        assert report.original_policy_versions == {OLD_VERSION: 2}

    def test_sparse_context_falls_back_to_normalized_columns(
        self, db_session, policy_config
    ):
        _decision(db_session, context={})
        report = run_policy_backtest(db_session, PolicyBacktestRequest(), config=policy_config)
        assert report.decisions_scanned == 1
        assert report.flip_count == 0
        assert report.outcomes_replayed["ALLOWED"] == 1


class TestFlipsWhenPolicyDiffers:
    def test_tightened_policy_flips_allowed_to_approval(self, db_session):
        record = _decision(db_session)  # ALLOWED, 10_000 paise, OLD_VERSION
        report = run_policy_backtest(db_session, PolicyBacktestRequest(), config=_tight_config())
        assert report.flip_count == 1
        flip = report.flips[0]
        assert flip.decision_id == record.id
        assert flip.original_outcome is PolicyOutcome.ALLOWED
        assert flip.replayed_outcome is PolicyOutcome.REQUIRES_APPROVAL
        assert "approval.amount" in flip.replayed_rules
        assert flip.original_rules == ["auto_execute.ok"]
        assert flip.original_policy_version == OLD_VERSION
        assert report.policy_version.startswith("test-tight+sha256.")
        assert report.original_policy_versions == {OLD_VERSION: 1}
        assert len(report.transitions) == 1
        impact = report.transitions[0]
        assert impact.from_outcome is PolicyOutcome.ALLOWED
        assert impact.to_outcome is PolicyOutcome.REQUIRES_APPROVAL
        assert impact.count == 1
        assert impact.amount_paise == 10_000
        assert report.outcomes_replayed["REQUIRES_APPROVAL"] == 1

    def test_loosened_policy_flips_approval_to_allowed(self, db_session):
        _decision(
            db_session,
            confidence=0.80,
            outcome=PolicyOutcome.REQUIRES_APPROVAL,
            rules_matched=["approval.confidence"],
            reasons=["confidence 0.8000 is below the auto-execute floor of 0.85"],
        )
        report = run_policy_backtest(db_session, PolicyBacktestRequest(), config=_loose_config())
        assert report.flip_count == 1
        flip = report.flips[0]
        assert flip.original_outcome is PolicyOutcome.REQUIRES_APPROVAL
        assert flip.replayed_outcome is PolicyOutcome.ALLOWED
        impact = report.transitions[0]
        assert impact.from_outcome is PolicyOutcome.REQUIRES_APPROVAL
        assert impact.to_outcome is PolicyOutcome.ALLOWED
        assert impact.count == 1
        assert impact.amount_paise == 10_000

    def test_hard_blocks_survive_any_threshold_change(self, db_session):
        _decision(
            db_session,
            action_type="refund",
            outcome=PolicyOutcome.BLOCKED,
            rules_matched=["allowlist", "never_auto_execute.refund"],
            reasons=["refund can never be authorized by the policy gate"],
        )
        for config in (_tight_config(), _loose_config()):
            report = run_policy_backtest(db_session, PolicyBacktestRequest(), config=config)
            assert report.flip_count == 0
            assert report.outcomes_replayed["BLOCKED"] == 1


class TestEnvironmentScoping:
    """policy_decisions has no environment column; the scope is derived
    through the soft action_id reference to recovery_actions — the two data
    environments never mix."""

    def test_environment_filter_scopes_via_linked_action(
        self, db_session, policy_config, make_recovery_action
    ):
        research_action = make_recovery_action()  # EnvironmentMixin default
        real_action = make_recovery_action()
        real_action.environment = "real_test"
        db_session.commit()
        _decision(db_session, action_id=research_action.id)  # ALLOWED
        _decision(
            db_session,
            action_id=real_action.id,
            action_type="refund",
            outcome=PolicyOutcome.BLOCKED,
            rules_matched=["allowlist", "never_auto_execute.refund"],
            reasons=["refund can never be authorized by the policy gate"],
        )
        _decision(db_session)  # unlinked preview-mode decision: no provenance

        research = run_policy_backtest(
            db_session, PolicyBacktestRequest(environment="research"), config=policy_config
        )
        assert research.decisions_scanned == 1
        assert research.outcomes_original["ALLOWED"] == 1

        real = run_policy_backtest(
            db_session, PolicyBacktestRequest(environment="real_test"), config=policy_config
        )
        assert real.decisions_scanned == 1
        assert real.outcomes_original["BLOCKED"] == 1

        # Unfiltered: every decision replays, including the unlinked one.
        both = run_policy_backtest(db_session, PolicyBacktestRequest(), config=policy_config)
        assert both.decisions_scanned == 3


class TestWindowAndLimit:
    def test_since_until_bound_decided_at(self, db_session, policy_config):
        now = utcnow()
        _decision(db_session, decided_at=now - timedelta(days=10))
        _decision(db_session, decided_at=now - timedelta(days=1))

        recent_only = run_policy_backtest(
            db_session,
            PolicyBacktestRequest(since=now - timedelta(days=5)),
            config=policy_config,
        )
        assert recent_only.decisions_scanned == 1
        assert recent_only.since == now - timedelta(days=5)

        old_only = run_policy_backtest(
            db_session,
            PolicyBacktestRequest(until=now - timedelta(days=5)),
            config=policy_config,
        )
        assert old_only.decisions_scanned == 1

    def test_limit_truncates_oldest_first(self, db_session, policy_config):
        now = utcnow()
        oldest = _decision(db_session, decided_at=now - timedelta(days=2))
        _decision(db_session, decided_at=now - timedelta(days=1), confidence=0.80,
                  outcome=PolicyOutcome.REQUIRES_APPROVAL)
        report = run_policy_backtest(
            db_session, PolicyBacktestRequest(limit=1), config=policy_config
        )
        assert report.decisions_scanned == 1
        assert report.original_policy_versions == {oldest.policy_version: 1}
        assert report.outcomes_original["ALLOWED"] == 1


class TestEndpoint:
    def test_empty_history_clean_empty_report(self, client, db_session, policy_config):
        resp = client.post("/api/v1/policy/backtest", json={}, headers=API_KEY)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "completed"
        assert body["decisions_scanned"] == 0
        assert body["flip_count"] == 0
        assert body["unchanged_count"] == 0
        assert body["flips"] == []
        assert body["transitions"] == []
        assert body["outcomes_replayed"] == {
            "ALLOWED": 0,
            "BLOCKED": 0,
            "REQUIRES_APPROVAL": 0,
        }
        assert body["rule_hits"] == {}
        assert body["policy_version"] == policy_config.policy_version
        assert body["detail"] == "no policy decisions matched the filters"
        # The run itself joins the audit trail, stamped "all" when unfiltered.
        entry = db_session.scalar(
            sa.select(models.AuditLog).where(models.AuditLog.action == "policy.backtest")
        )
        assert entry is not None
        assert entry.entity_type == "policy_backtest_run"
        assert entry.entity_id == body["run_id"]
        assert entry.environment == "all"
        assert entry.details["decisions_scanned"] == 0

    def test_endpoint_reports_flips_against_current_policy(
        self, client, db_session, monkeypatch
    ):
        _decision(db_session)
        monkeypatch.setattr(policy_api, "load_policy_config", lambda: _tight_config())
        resp = client.post("/api/v1/policy/backtest", json={}, headers=API_KEY)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["policy_version"].startswith("test-tight+sha256.")
        assert body["flip_count"] == 1
        flip = body["flips"][0]
        assert flip["original_outcome"] == "ALLOWED"
        assert flip["replayed_outcome"] == "REQUIRES_APPROVAL"
        assert flip["original_policy_version"] == OLD_VERSION
        assert "approval.amount" in flip["replayed_rules"]
        assert body["transitions"] == [
            {
                "from_outcome": "ALLOWED",
                "to_outcome": "REQUIRES_APPROVAL",
                "count": 1,
                "amount_paise": 10_000,
            }
        ]
        entry = db_session.scalar(
            sa.select(models.AuditLog).where(models.AuditLog.action == "policy.backtest")
        )
        assert entry.details["flip_count"] == 1

    def test_environment_filter_echoed_and_audited(self, client, db_session):
        resp = client.post(
            "/api/v1/policy/backtest", json={"environment": "research"}, headers=API_KEY
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["environment"] == "research"
        entry = db_session.scalar(
            sa.select(models.AuditLog).where(models.AuditLog.action == "policy.backtest")
        )
        assert entry.environment == "research"

    def test_backtest_persists_no_decisions(self, client, db_session):
        _decision(db_session)
        count = sa.select(sa.func.count()).select_from(models.PolicyDecisionRecord)
        before = db_session.scalar(count)
        resp = client.post("/api/v1/policy/backtest", json={}, headers=API_KEY)
        assert resp.status_code == 200, resp.text
        assert db_session.scalar(count) == before

    def test_deterministic_across_runs(self, client, db_session):
        _decision(db_session)
        first = client.post("/api/v1/policy/backtest", json={}, headers=API_KEY).json()
        second = client.post("/api/v1/policy/backtest", json={}, headers=API_KEY).json()
        for volatile in ("run_id", "started_at", "finished_at"):
            first.pop(volatile)
            second.pop(volatile)
        assert first == second


class TestEndpointAuth:
    """POST /api/v1/policy/backtest is a mutating route: the API-key
    middleware guards it (it is NOT in the demo/detection exemption)."""

    def test_missing_key_rejected(self, client):
        resp = client.post("/api/v1/policy/backtest", json={})
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "unauthorized"

    def test_wrong_key_rejected(self, client):
        resp = client.post(
            "/api/v1/policy/backtest", json={}, headers={"X-API-Key": "wrong-key"}
        )
        assert resp.status_code == 401
