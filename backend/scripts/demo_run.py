"""PulseRecover demo runner - five deterministic, resettable demo scenarios.

Run from backend/:

    python scripts/demo_run.py --scenario A --db scripts/.demo_A.db
    python scripts/demo_run.py --scenario all --db scripts/.demo.db

Every scenario:
1. RESETS the scratch sqlite DB at --db (deleted + recreated; never touches
   the app DATABASE_URL),
2. SEEDS it with the simulator (`app.simulator.engine.run_simulation`) using a
   fixed seed AND a fixed end_date, so the dataset is byte-reproducible,
3. DRIVES the real FastAPI pipeline in-process via TestClient against the real
   routers: POST /api/v1/detection/run -> POST /api/v1/incidents/{id}/investigate
   -> POST /api/v1/recovery/opportunities/build -> GET .../plan -> POST
   execute/approve -> signed simulator webhooks -> audit trail,
4. PRINTS a step-by-step narrative with the ACTUAL numbers the run produced.
   Nothing below forces an output: every printed number is read back from the
   database or an API response.

Scenarios:
    A  major gateway degradation, full closed loop (detect -> ... -> verified
       recovered revenue), tuned to a ~11-point success-rate drop with Rs 8L+
       at risk
    B  safe autonomous recovery (< Rs 5000, confidence >= 0.85 -> auto-execute
       -> webhook-verified RECOVERED)
    C  human approval lane (> Rs 5000 -> PENDING_APPROVAL -> approve -> execute
       -> verify)
    D  gateway timeout on execution -> UNKNOWN, no blind retry, recovery
       paused, resolved truthfully by re-query once the gateway shows the money
    E  unsafe AI recommendation (forced refund proposal) -> POLICY BLOCKED,
       zero gateway calls

The parallel-owned /api/v1/demo router is deliberately NOT used; seeding goes
through the simulator engine directly. Incident and audit reads go straight
to the DB of record (the same tables the /api/v1/incidents and /api/v1/audit
list endpoints serve) so the script asserts on stored truth, not a read API.
"""

import argparse
import json
import sys
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/

import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

import app.db as app_db
from app.config import settings
from app.db import Base
from app.main import create_app
from app.models import (
    AuditLog,
    Payment,
    RecoveryOpportunity,
    RecoveryStrategy,
    SimulatorGroundTruth,
)
from app.ports import ActionType, RecoveryStatus
from app.api.deps import get_gateway_dependency
from app.services.razorpay.simulated import SimulatedPaymentGateway
from app.services.revenue.classify import FailureClass, classify_failure
from app.simulator.config import IncidentKind, IncidentSpec, SimulatorConfig
from app.simulator.engine import run_simulation

# Fixed simulation anchor: all demo datasets cover [DEMO_END - days, DEMO_END).
# Pinning end_date (instead of "today") is what makes every printed number
# reproducible on any machine, on any day.
DEMO_END = datetime(2026, 8, 16, tzinfo=timezone.utc)

# The demo incident always sits on the last simulated day at 14:00 IST.
INCIDENT_START_IST = 14.0
IST_TO_UTC = 5.5

Say = Callable[[str], None]


def _clean(text: str) -> str:
    """Keep the narrative printable on a cp1252 Windows console, even when it
    quotes app-generated text containing smart punctuation."""
    return (
        text.replace("\u2014", "-")
        .replace("\u2013", "-")
        .replace("\u2192", "->")
        .encode("ascii", "replace")
        .decode("ascii")
    )


# ---------------------------------------------------------------------------
# formatting helpers
# ---------------------------------------------------------------------------


def rs(paise: int | None) -> str:
    """Integer paise -> 'Rs 9,41,483' (Indian digit grouping)."""
    if paise is None:
        return "Rs -"
    rupees = int(paise) // 100
    s = str(rupees)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts + [tail])
    return f"Rs {s}"


def pct(x: float | None) -> str:
    return "-" if x is None else f"{x * 100:.1f}%"


def _jsonable(value: Any) -> Any:
    if isinstance(value, (datetime,)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# demo gateway: simulator twin + outage switch + mutation counter
# ---------------------------------------------------------------------------


class DemoGateway(SimulatedPaymentGateway):
    """SimulatedPaymentGateway with (a) a runtime outage switch for the
    timeout scenario and (b) a counter proving how many mutating calls the
    system attempted (scenario E asserts this stays at zero)."""

    def __init__(self, **kw: Any) -> None:
        kw.setdefault("success_rate", 1.0)
        super().__init__(**kw)
        self.mutation_attempts = 0

    def set_outage(self, on: bool) -> None:
        if on:
            self._incident["outage"] = True
        else:
            self._incident.pop("outage", None)

    def create_order(self, **kw: Any) -> dict[str, Any]:
        self.mutation_attempts += 1
        return super().create_order(**kw)

    def create_payment_link(self, **kw: Any) -> dict[str, Any]:
        self.mutation_attempts += 1
        return super().create_payment_link(**kw)

    def create_subscription(self, **kw: Any) -> dict[str, Any]:
        self.mutation_attempts += 1
        return super().create_subscription(**kw)


# ---------------------------------------------------------------------------
# harness: scratch DB + simulator seed + wired TestClient
# ---------------------------------------------------------------------------


class DemoHarness:
    """Owns the scratch DB file, the seeded dataset, and a TestClient whose
    DB/gateway dependencies point at the scratch world (never app.db)."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.engine = None
        self.Session: sessionmaker | None = None
        self.gateway = DemoGateway()
        self.client: TestClient | None = None

    # -- lifecycle -----------------------------------------------------------

    def reset(self) -> None:
        """Delete any previous scratch DB and recreate an empty schema."""
        self.close()
        for suffix in ("", "-wal", "-shm"):
            target = Path(str(self.db_path) + suffix)
            target.unlink(missing_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        url = f"sqlite:///{self.db_path.as_posix()}"
        self.engine = sa.create_engine(url, connect_args={"check_same_thread": False})
        from app.db import enable_sqlite_fk

        enable_sqlite_fk(self.engine)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(
            bind=self.engine, autoflush=False, expire_on_commit=False
        )
        self.gateway = DemoGateway()
        self.client = self._make_client()

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
            self.client = None
        if self.engine is not None:
            self.engine.dispose()
            self.engine = None

    def _make_client(self) -> TestClient:
        app = create_app()
        assert self.Session is not None

        def _get_db():
            db = self.Session()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[app_db.get_db] = _get_db
        # One gateway dependency seam (app.api.deps) shared by every router:
        # execution AND webhook signature verification use this gateway.
        app.dependency_overrides[get_gateway_dependency] = lambda: self.gateway
        return TestClient(app, raise_server_exceptions=True)

    # -- seeding --------------------------------------------------------------

    def seed(self, config: SimulatorConfig, say: Say) -> dict[str, Any]:
        assert self.Session is not None
        session = self.Session()
        try:
            result = run_simulation(config, session)
            stats = dict(result.stats or {})
        finally:
            session.close()
        rows = stats.get("rows", {})
        say(
            f"[SEED] simulator run {result.run_id} - "
            f"{rows.get('payment_events', '?')} payment_events, "
            f"{rows.get('payments', '?')} payments, "
            f"{rows.get('customers', '?')} customers over {config.days} day(s) "
            f"(seed={config.seed}, end={DEMO_END.date()} - fully deterministic)"
        )
        return {"run_id": result.run_id, "stats": stats}

    # -- HTTP helpers ----------------------------------------------------------

    def post(self, path: str, body: dict | None = None, headers: dict | None = None) -> dict:
        assert self.client is not None
        hdrs = {"X-API-Key": settings.API_KEY}
        if headers:
            hdrs.update(headers)
        resp = self.client.post(path, json=body or {}, headers=hdrs)
        if resp.status_code >= 400:
            raise RuntimeError(f"POST {path} -> {resp.status_code}: {resp.text[:400]}")
        return resp.json()

    def post_raw(self, path: str, content: bytes, headers: dict) -> tuple[int, dict]:
        assert self.client is not None
        resp = self.client.post(path, content=content, headers=headers)
        return resp.status_code, resp.json()

    def get(self, path: str) -> dict:
        assert self.client is not None
        resp = self.client.get(path, headers={"X-API-Key": settings.API_KEY})
        if resp.status_code >= 400:
            raise RuntimeError(f"GET {path} -> {resp.status_code}: {resp.text[:400]}")
        return resp.json()

    # -- DB read helpers (direct reads of the tables the list APIs serve) ------

    def session(self):
        assert self.Session is not None
        return self.Session()

    def audit_rows(
        self,
        *,
        entity_type: str | None = None,
        entity_id: str | None = None,
        limit: int = 12,
    ) -> list[AuditLog]:
        session = self.session()
        try:
            stmt = sa.select(AuditLog).order_by(AuditLog.created_at, AuditLog.id)
            if entity_type:
                stmt = stmt.where(AuditLog.entity_type == entity_type)
            if entity_id:
                stmt = stmt.where(AuditLog.entity_id == entity_id)
            return list(session.scalars(stmt))[-limit:]
        finally:
            session.close()


# ---------------------------------------------------------------------------
# scenario configs (fixed seed + fixed end_date => reproducible numbers)
# ---------------------------------------------------------------------------


def _degradation_spec(duration_hours: float, fail_boost: float, latency: float = 2.5) -> IncidentSpec:
    return IncidentSpec(
        IncidentKind.GATEWAY_DEGRADATION,
        day_fraction=1.0,  # last simulated day
        start_hour_ist=INCIDENT_START_IST,
        duration_hours=duration_hours,
        params={"fail_boost": fail_boost, "latency_multiplier": latency},
    )


def config_a() -> SimulatorConfig:
    """Tuned major degradation: fail_boost 0.12 is strong enough that the
    diagnosis model's confidence (measured 0.8997) keeps timeout-retry
    strategies at 0.8817 — genuinely above the 0.85 auto-execute floor —
    while landing a double-digit-point success-rate drop with Rs 8L+ at risk.
    Latency is left unmultiplied so the signature is a pure success-rate drop."""
    return SimulatorConfig(
        seed=42,
        days=2,
        target_events=140_000,
        customers=2_000,
        scenario="demo_a_major_degradation",
        end_date=DEMO_END,
        incidents=(_degradation_spec(duration_hours=2.5, fail_boost=0.12, latency=1.0),),
    )


def config_b() -> SimulatorConfig:
    return SimulatorConfig(
        seed=7,
        days=2,
        target_events=60_000,
        customers=1_500,
        scenario="demo_b_safe_autonomous",
        end_date=DEMO_END,
        incidents=(_degradation_spec(duration_hours=1.5, fail_boost=0.35),),
    )


def config_c() -> SimulatorConfig:
    return SimulatorConfig(
        seed=9,
        days=2,
        target_events=60_000,
        customers=1_500,
        scenario="demo_c_human_approval",
        end_date=DEMO_END,
        incidents=(_degradation_spec(duration_hours=1.5, fail_boost=0.35),),
    )


def config_d() -> SimulatorConfig:
    return SimulatorConfig(
        seed=13,
        days=2,
        target_events=60_000,
        customers=1_500,
        scenario="demo_d_gateway_timeout",
        end_date=DEMO_END,
        incidents=(_degradation_spec(duration_hours=1.5, fail_boost=0.35),),
    )


def config_e() -> SimulatorConfig:
    return SimulatorConfig(
        seed=11,
        days=2,
        target_events=20_000,
        customers=1_000,
        scenario="demo_e_unsafe_ai",
        end_date=DEMO_END,
        incidents=(_degradation_spec(duration_hours=1.5, fail_boost=0.35),),
    )


# ---------------------------------------------------------------------------
# shared pipeline steps
# ---------------------------------------------------------------------------


def _incident_window_utc(config: SimulatorConfig) -> tuple[datetime, datetime]:
    """Resolve the first incident spec to absolute UTC (mirrors the engine)."""
    spec = config.incidents[0]
    day = min(round(spec.day_fraction * max(config.days - 1, 0)), config.days - 1)
    day_start = DEMO_END - timedelta(days=config.days) + timedelta(days=day)
    start = day_start + timedelta(hours=spec.start_hour_ist - IST_TO_UTC)
    return start, start + timedelta(hours=spec.duration_hours)


def _detect(
    h: DemoHarness, say: Say, config: SimulatorConfig, *, window_minutes: int
) -> dict[str, Any]:
    """POST /api/v1/detection/run, anchored just after the injected incident."""
    inc_start, inc_end = _incident_window_utc(config)
    as_of = inc_end + timedelta(minutes=25)
    say(
        f"[DETECT] POST /api/v1/detection/run - metric=payment_success_rate, "
        f"window={window_minutes}m, bucket=10m, anchored {as_of:%Y-%m-%d %H:%M} UTC "
        f"(injected window {inc_start:%H:%M}-{inc_end:%H:%M} UTC)"
    )
    resp = h.post(
        "/api/v1/detection/run",
        {
            "metrics": ["payment_success_rate"],
            "window_minutes": window_minutes,
            "bucket_minutes": 10,
            "baseline_buckets": 6,
            "as_of": as_of.isoformat(),
        },
    )
    incidents = [
        i for i in resp.get("incidents", []) if i["metric"] == "payment_success_rate"
    ]
    if not incidents:
        raise RuntimeError(f"detection found no incident: {resp.get('detail')}")
    chosen = max(incidents, key=lambda i: i["revenue_at_risk_paise"])
    say(
        f"        anomaly -> incident {chosen['incident_id']}: success rate "
        f"{pct(chosen['baseline_value'])} -> {pct(chosen['observed_value'])} "
        f"({chosen['deviation_pct']}%), severity={chosen['severity']}"
    )
    say(
        f"        blast radius: {chosen['affected_payments_count']} failed payments, "
        f"{rs(chosen['revenue_at_risk_paise'])} at risk"
    )

    # The answer key, for honesty: what the simulator actually injected.
    truth: dict[str, Any] = {}
    session = h.session()
    try:
        row = session.scalar(
            sa.select(SimulatorGroundTruth).where(
                SimulatorGroundTruth.entity_type == "incident"
            )
        )
        if row is not None:
            truth = dict(row.truth or {})
    finally:
        session.close()
    if truth:
        say(
            f"        ground truth (answer key): kind={truth.get('kind')}, "
            f"affected={truth.get('affected_count')}, injected_failures="
            f"{truth.get('injected_failures')}, expected_cause="
            f"{truth.get('expected_root_cause')}"
        )
    return {
        "incident_id": chosen["incident_id"],
        "baseline_value": chosen["baseline_value"],
        "observed_value": chosen["observed_value"],
        "deviation_pct": chosen["deviation_pct"],
        "severity": str(chosen["severity"]),
        "affected_payments_count": chosen["affected_payments_count"],
        "revenue_at_risk_paise": chosen["revenue_at_risk_paise"],
        "ground_truth": {
            "kind": truth.get("kind"),
            "affected_count": truth.get("affected_count"),
            "injected_failures": truth.get("injected_failures"),
            "expected_root_cause": truth.get("expected_root_cause"),
            "affected_amount_paise": truth.get("affected_amount_paise"),
        },
    }


def _investigate(h: DemoHarness, say: Say, incident_id: str) -> dict[str, Any]:
    """POST /api/v1/incidents/{id}/investigate (diagnosis + AI reasoner)."""
    resp = h.post(f"/api/v1/incidents/{incident_id}/investigate", {})
    report = resp["report"]
    # The diagnoses row is the system of record for the ML classification.
    from app.models import Diagnosis

    session = h.session()
    try:
        diag = session.scalar(
            sa.select(Diagnosis)
            .where(Diagnosis.incident_id == incident_id)
            .order_by(Diagnosis.version.desc(), Diagnosis.id.desc())
            .limit(1)
        )
        diagnosis = (
            {
                "label": diag.predicted_cause,
                "confidence": float(diag.confidence),
                "model_name": diag.model_name,
                "model_version": diag.model_version,
            }
            if diag is not None
            else None
        )
    finally:
        session.close()
    if diagnosis:
        say(
            f"[DIAGNOSE] ML root-cause: {diagnosis['label']} "
            f"(confidence {diagnosis['confidence']:.4f}, model "
            f"{diagnosis['model_name']}@{diagnosis['model_version']})"
        )
    say(
        f"[INVESTIGATE] AI investigator ({report.get('reasoner', 'reasoner')}): "
        f"{report['summary']}"
    )
    if report.get("recommended_next_step"):
        step = report["recommended_next_step"]
        say(
            f"        AI proposes: {step['action_type']} - {step['rationale'][:120]}"
        )
    say(
        f"        (the AI only PROPOSES; the deterministic policy gate decides "
        f"what may execute)"
    )
    return {
        "report_id": resp["report_id"],
        "diagnosis": diagnosis,
        "summary": report["summary"],
        "confidence": report.get("confidence"),
        "recommended_action_count": len(report.get("recommended_actions", [])),
    }


def _build(h: DemoHarness, say: Say, incident_id: str) -> dict[str, Any]:
    resp = h.post(
        "/api/v1/recovery/opportunities/build",
        {"incident_id": incident_id, "actor": "agent:strategist"},
    )
    total_amount = sum(o["amount_paise"] for o in resp["opportunities"])
    say(
        f"[QUANTIFY] POST /api/v1/recovery/opportunities/build -> "
        f"{resp['created_count']} per-payment opportunities "
        f"({rs(total_amount)} of failed payments in scope), strategies generated"
    )
    return {
        "created_count": resp["created_count"],
        "existing_count": resp["existing_count"],
        "total_amount_paise": total_amount,
    }


def _pick_opportunity(
    h: DemoHarness,
    incident_id: str,
    *,
    lane: str,
    exclude_customers: set[str] | None = None,
    failure_classes: tuple[FailureClass, ...] = (
        FailureClass.SOFT_DECLINE,
        FailureClass.TIMEOUT,
    ),
) -> dict[str, Any]:
    """Deterministic pick of a recoverable opportunity.

    lane="auto":     smallest amount <= Rs 5000 (policy auto-execute ceiling)
    lane="approval": largest amount > Rs 5000 (must take the human lane)
    Only the given failure classes (retry is the high-fit strategy) on
    non-opted-out customers are eligible picks.

    Ordering is (amount_paise, payment id): simulator payment ids are
    deterministic (pay_<run_id>_<seq>), unlike uuid4 opportunity ids, so two
    fresh runs always pick the same opportunity even on amount ties.
    """
    from app.models import Customer

    exclude_customers = exclude_customers or set()
    session = h.session()
    try:
        rows = session.execute(
            sa.select(RecoveryOpportunity, Payment, Customer.opted_out)
            .join(Payment, RecoveryOpportunity.payment_id == Payment.id)
            .outerjoin(Customer, RecoveryOpportunity.customer_id == Customer.id)
            .where(RecoveryOpportunity.incident_id == incident_id)
            .order_by(RecoveryOpportunity.amount_paise, Payment.id)
        ).all()
    finally:
        session.close()
    candidates: list[dict[str, Any]] = []
    for opp, pay, opted_out in rows:
        if opted_out:
            continue
        cls = classify_failure(pay)
        if cls not in failure_classes:
            continue
        if opp.customer_id in exclude_customers:
            continue
        candidates.append(
            {
                "opportunity_id": opp.id,
                "payment_id": pay.id,
                "gateway_payment_id": pay.gateway_payment_id,
                "customer_id": opp.customer_id,
                "amount_paise": opp.amount_paise,
                "method": pay.method,
                "failure_class": cls.value,
                "error": pay.error_description or pay.error_code,
            }
        )
    ceiling = 500_000  # Rs 5,000 in paise
    if lane == "auto":
        eligible = [c for c in candidates if c["amount_paise"] <= ceiling]
        if not eligible:
            raise RuntimeError("no auto-lane opportunity found")
        # Prefer a narratively readable amount (>= Rs 500) when one exists.
        readable = [c for c in eligible if c["amount_paise"] >= 50_000]
        return (readable or eligible)[0]  # ascending order: smallest first
    eligible = [c for c in candidates if c["amount_paise"] > ceiling]
    if not eligible:
        raise RuntimeError("no approval-lane opportunity (> Rs 5000) found")
    return eligible[-1]  # largest amount


def _plan(h: DemoHarness, say: Say, opp_id: str) -> dict[str, Any]:
    plan = h.get(f"/api/v1/recovery/{opp_id}/plan")
    rec = next(
        (s for s in plan["strategies"] if s["id"] == plan["recommended_strategy_id"]),
        None,
    )
    preview = plan.get("policy_preview") or {}
    if rec:
        say(
            f"[STRATEGIZE] GET plan -> recommended: {rec['action_type']} "
            f"(expected recovery {rs(rec['expected_recovery_paise'])}, "
            f"confidence {rec['confidence']:.4f}, risk {rec['risk']}); "
            f"policy preview: {preview.get('outcome')}"
        )
    return {
        "recommended_strategy_id": plan["recommended_strategy_id"],
        "recommended_action_type": rec["action_type"] if rec else None,
        "expected_recovery_paise": rec["expected_recovery_paise"] if rec else 0,
        "confidence": rec["confidence"] if rec else None,
        "policy_preview_outcome": preview.get("outcome"),
    }


def _inject_payment_captured(h: DemoHarness, pick: dict[str, Any]) -> dict[str, Any]:
    """Deliver a genuinely-signed payment.captured webhook for the picked
    payment through POST /webhooks/razorpay."""
    entity = {
        "id": pick["gateway_payment_id"],
        "entity": "payment",
        "status": "captured",
        "captured": True,
        "amount": pick["amount_paise"],
        "currency": "INR",
        "method": pick["method"],
    }
    body, signature, event_id = h.gateway.build_event("payment.captured", entity)
    status, ack = h.post_raw(
        "/webhooks/razorpay",
        body,
        headers={
            "Content-Type": "application/json",
            "X-Razorpay-Signature": signature,
            "X-Razorpay-Event-Id": event_id,
        },
    )
    if status >= 400:
        raise RuntimeError(f"webhook injection failed: {status} {ack}")
    return ack


def _audit_summary(h: DemoHarness, say: Say, *, opportunity_id: str, limit: int = 12) -> int:
    """Print the opportunity's audit trail via the real detail API (it joins
    the opportunity's and its actions' audit_rows rows)."""
    detail = h.get(f"/api/v1/recovery/{opportunity_id}")
    rows = detail.get("audit", [])[-limit:]
    say(f"[AUDIT] append-only audit trail for {opportunity_id} (last {len(rows)} rows):")
    for row in rows:
        say(f"        {row['actor']:<38} {row['action']}")
    return len(detail.get("audit", []))


# ---------------------------------------------------------------------------
# execution flows
# ---------------------------------------------------------------------------


def _execute_flow(
    h: DemoHarness, say: Say, pick: dict[str, Any], *, lane: str
) -> dict[str, Any]:
    """plan -> execute -> (approve if gated) -> webhook verify. Real calls only."""
    opp_id = pick["opportunity_id"]
    plan = _plan(h, say, opp_id)
    say(
        f"[EXECUTE] POST /api/v1/recovery/{opp_id}/execute - "
        f"{rs(pick['amount_paise'])} {pick['method']} payment "
        f"({pick['failure_class']}: {pick['error']})"
    )
    res = h.post(
        f"/api/v1/recovery/{opp_id}/execute", {"actor": "agent:strategist"}
    )
    decision = res.get("policy_decision") or {}
    outcome = decision.get("outcome")
    rules = decision.get("rules_matched") or []
    approved = False
    if res["status"] == RecoveryStatus.PENDING_APPROVAL.value:
        why = []
        if "approval.amount" in rules:
            why.append(f"{rs(pick['amount_paise'])} is above the Rs 5,000 auto-execute ceiling")
        if "approval.confidence" in rules:
            why.append("confidence is below the 0.85 auto-execute floor")
        if not why:
            why.append("policy requires a human decision")
        say(
            f"[POLICY] gate: {outcome} (rules: {', '.join(rules)}) - "
            f"{'; '.join(why)}; routing to a human"
        )
        h.post(
            f"/api/v1/recovery/{opp_id}/approve",
            {"actor": "human:ops@pulserecover.demo", "note": "demo approval"},
        )
        say("[APPROVE] human:ops approved -> APPROVED; executing on the recorded decision")
        res = h.post(
            f"/api/v1/recovery/{opp_id}/execute", {"actor": "agent:strategist"}
        )
        approved = True
    else:
        say(
            f"[POLICY] gate: {outcome} (rules: {', '.join(rules) or 'auto_execute.ok'})"
            f" - auto-execute lane (<= Rs 5,000, confidence >= 0.85)"
        )
    say(f"        executor: {res['status']} - {res['message']}")

    ack = _inject_payment_captured(h, pick)
    final = h.get(f"/api/v1/recovery/{opp_id}")
    final_status = final["status"]
    say(
        f"[VERIFY] webhook payment.captured (HMAC signature valid, "
        f"event id deduped) -> action {final_status}"
        + (f" - {rs(pick['amount_paise'])} recovered" if final_status == "RECOVERED" else "")
    )
    return {
        "opportunity_id": opp_id,
        "lane": lane,
        "amount_paise": pick["amount_paise"],
        "failure_class": pick["failure_class"],
        "recommended_action_type": plan["recommended_action_type"],
        "confidence": plan["confidence"],
        "policy_outcome": outcome,
        "rules_matched": list(rules),
        "approved_by_human": approved,
        "webhook_status": ack.get("status"),
        "final_status": final_status,
        "recovered_paise": pick["amount_paise"] if final_status == "RECOVERED" else 0,
    }


# ---------------------------------------------------------------------------
# scenarios
# ---------------------------------------------------------------------------


def scenario_a(h: DemoHarness, say: Say) -> dict[str, Any]:
    say("=" * 78)
    say("SCENARIO A - MAJOR DEGRADATION: detect -> diagnose -> quantify -> recover -> verify")
    say("=" * 78)
    h.reset()
    cfg = config_a()
    seed = h.seed(cfg, say)
    det = _detect(h, say, cfg, window_minutes=240)
    inv = _investigate(h, say, det["incident_id"])
    build = _build(h, say, det["incident_id"])

    used: set[str] = set()
    executions = []
    for lane, count in (("approval", 1), ("auto", 2)):
        for _ in range(count):
            # Auto-lane picks are timeout-class failures: retry has the
            # highest action-fit prior (0.98) there, so the strategy
            # confidence genuinely clears the 0.85 auto-execute floor.
            classes = (FailureClass.TIMEOUT,) if lane == "auto" else (
                FailureClass.SOFT_DECLINE,
                FailureClass.TIMEOUT,
            )
            pick = _pick_opportunity(
                h,
                det["incident_id"],
                lane=lane,
                exclude_customers=used,
                failure_classes=classes,
            )
            used.add(pick["customer_id"])
            executions.append(_execute_flow(h, say, pick, lane=lane))

    recovered = sum(e["recovered_paise"] for e in executions)
    n_recovered = sum(1 for e in executions if e["final_status"] == "RECOVERED")
    audit_count = _audit_summary(h, say, opportunity_id=executions[0]["opportunity_id"], limit=8)
    say(
        f"[RESULT] {n_recovered}/{len(executions)} executions verified RECOVERED - "
        f"{rs(recovered)} of {rs(det['revenue_at_risk_paise'])} at risk recovered "
        f"in this run (each one webhook-verified, each state change audited)"
    )
    return {
        "scenario": "A",
        "seed_run_id": seed["run_id"],
        "detection": det,
        "diagnosis": inv["diagnosis"],
        "opportunities_created": build["created_count"],
        "executions": executions,
        "recovered_total_paise": recovered,
        "gateway_mutation_attempts": h.gateway.mutation_attempts,
        "audit_rows_sampled": audit_count,
    }


def scenario_b(h: DemoHarness, say: Say) -> dict[str, Any]:
    say("=" * 78)
    say("SCENARIO B - SAFE AUTONOMOUS RECOVERY: low value + high confidence -> auto-execute")
    say("=" * 78)
    h.reset()
    cfg = config_b()
    seed = h.seed(cfg, say)
    det = _detect(h, say, cfg, window_minutes=180)
    inv = _investigate(h, say, det["incident_id"])
    build = _build(h, say, det["incident_id"])

    pick = _pick_opportunity(h, det["incident_id"], lane="auto")
    execution = _execute_flow(h, say, pick, lane="auto")
    audit_count = _audit_summary(h, say, opportunity_id=pick["opportunity_id"], limit=10)
    say(
        f"[RESULT] no human touched this run: policy ALLOWED, executed, and the "
        f"webhook verified {rs(execution['recovered_paise'])} RECOVERED"
    )
    return {
        "scenario": "B",
        "seed_run_id": seed["run_id"],
        "detection": det,
        "diagnosis": inv["diagnosis"],
        "opportunities_created": build["created_count"],
        "executions": [execution],
        "recovered_total_paise": execution["recovered_paise"],
        "gateway_mutation_attempts": h.gateway.mutation_attempts,
        "audit_rows_sampled": audit_count,
    }


def scenario_c(h: DemoHarness, say: Say) -> dict[str, Any]:
    say("=" * 78)
    say("SCENARIO C - HUMAN APPROVAL: > Rs 5,000 -> PENDING_APPROVAL -> approve -> verify")
    say("=" * 78)
    h.reset()
    cfg = config_c()
    seed = h.seed(cfg, say)
    det = _detect(h, say, cfg, window_minutes=180)
    inv = _investigate(h, say, det["incident_id"])
    build = _build(h, say, det["incident_id"])

    pick = _pick_opportunity(h, det["incident_id"], lane="approval")
    execution = _execute_flow(h, say, pick, lane="approval")
    assert execution["approved_by_human"], "scenario C must pass through the human lane"
    audit_count = _audit_summary(h, say, opportunity_id=pick["opportunity_id"], limit=10)
    say(
        f"[RESULT] the gate held the {rs(pick['amount_paise'])} retry until a human "
        f"approved; webhook then verified it RECOVERED"
    )
    return {
        "scenario": "C",
        "seed_run_id": seed["run_id"],
        "detection": det,
        "diagnosis": inv["diagnosis"],
        "opportunities_created": build["created_count"],
        "executions": [execution],
        "recovered_total_paise": execution["recovered_paise"],
        "gateway_mutation_attempts": h.gateway.mutation_attempts,
        "audit_rows_sampled": audit_count,
    }


def scenario_d(h: DemoHarness, say: Say) -> dict[str, Any]:
    say("=" * 78)
    say("SCENARIO D - GATEWAY TIMEOUT: ambiguous outcome -> UNKNOWN, no blind retry, truthful resolve")
    say("=" * 78)
    h.reset()
    cfg = config_d()
    seed = h.seed(cfg, say)
    det = _detect(h, say, cfg, window_minutes=180)
    _investigate(h, say, det["incident_id"])
    build = _build(h, say, det["incident_id"])

    pick = _pick_opportunity(h, det["incident_id"], lane="auto")
    opp_id = pick["opportunity_id"]
    _plan(h, say, opp_id)

    say("[FAULT] injecting a gateway outage into the simulated gateway (503s on mutations)")
    h.gateway.set_outage(True)
    res = h.post(f"/api/v1/recovery/{opp_id}/execute", {"actor": "agent:strategist"})
    say(
        f"[EXECUTE] gateway timed out on the mutating call -> action {res['status']}: "
        f"{res['message']}"
    )
    if res["status"] != "UNKNOWN":
        raise RuntimeError(f"expected UNKNOWN after outage, got {res['status']}")
    action_id = res["action_id"]
    mutations_after_fire = h.gateway.mutation_attempts

    say(
        "[PAUSE] the UNKNOWN action occupies the opportunity: automation is paused, "
        "nothing is silently counted as recovered"
    )
    say("[RE-EXECUTE] operator retries the execute - the executor must NOT re-fire the mutation")
    res2 = h.post(f"/api/v1/recovery/{opp_id}/execute", {"actor": "human:ops@pulserecover.demo"})
    say(
        f"        same action {res2['action_id']} re-queried instead of re-fired "
        f"(gateway mutations attempted: {h.gateway.mutation_attempts} total - "
        f"duplicate execution is impossible)"
    )
    if res2["action_id"] != action_id or h.gateway.mutation_attempts != mutations_after_fire:
        raise RuntimeError("duplicate protection failed: a second mutation was attempted")

    say("[RECOVER] outage clears; the gateway's own records now show the payment captured")
    h.gateway.set_outage(False)
    h.gateway.payments[pick["gateway_payment_id"]] = {
        "id": pick["gateway_payment_id"],
        "entity": "payment",
        "status": "captured",
        "captured": True,
        "amount": pick["amount_paise"],
        "currency": "INR",
        "method": pick["method"],
    }
    res3 = h.post(f"/api/v1/recovery/{opp_id}/execute", {"actor": "human:ops@pulserecover.demo"})
    final = h.get(f"/api/v1/recovery/{opp_id}")
    say(
        f"[RESOLVE] GET-only re-query (fetch_payment) proves the capture -> "
        f"action {final['status']} - resolved on gateway evidence, never guessed"
    )
    _audit_summary(h, say, opportunity_id=opp_id, limit=12)
    say(
        f"[RESULT] timeout -> UNKNOWN -> paused -> resolved RECOVERED on gateway "
        f"evidence; exactly {h.gateway.mutation_attempts} mutating call was ever "
        f"attempted ({rs(pick['amount_paise'])} at stake, never double-charged)"
    )
    return {
        "scenario": "D",
        "seed_run_id": seed["run_id"],
        "detection": det,
        "opportunities_created": build["created_count"],
        "action_id": action_id,
        "status_after_timeout": res["status"],
        "status_after_requery": res2["status"],
        "final_status": final["status"],
        "gateway_mutation_attempts": h.gateway.mutation_attempts,
        "amount_paise": pick["amount_paise"],
    }


def scenario_e(h: DemoHarness, say: Say) -> dict[str, Any]:
    say("=" * 78)
    say("SCENARIO E - UNSAFE AI RECOMMENDATION: a forced refund proposal hits the deterministic gate")
    say("=" * 78)
    h.reset()
    cfg = config_e()
    seed = h.seed(cfg, say)
    det = _detect(h, say, cfg, window_minutes=180)
    _investigate(h, say, det["incident_id"])
    build = _build(h, say, det["incident_id"])

    pick = _pick_opportunity(h, det["incident_id"], lane="auto")
    opp_id = pick["opportunity_id"]

    # Force the unsafe proposal: a refund strategy the AI layer never generates
    # on its own, planted here to prove the gate stops it (ADR 0003 threat T1).
    session = h.session()
    try:
        refund = RecoveryStrategy(
            opportunity_id=opp_id,
            action_type=ActionType.REFUND,
            rank=99,
            expected_recovery_paise=pick["amount_paise"],
            confidence=0.99,
            risk="high",
            eligibility=True,
            reason="planted unsafe AI proposal: refund the payment in full",
            constraints={},
            generated_by="agent:compromised_llm",
            selected=False,
        )
        session.add(refund)
        session.commit()
        refund_id = refund.id
    finally:
        session.close()
    say(
        f"[ATTACK] a manipulated AI run proposes action_type=refund for "
        f"{rs(pick['amount_paise'])} (confidence 0.99 - confidence is not authority)"
    )
    res = h.post(
        f"/api/v1/recovery/{opp_id}/execute",
        {"actor": "agent:compromised_llm", "strategy_id": refund_id},
    )
    decision = res.get("policy_decision") or {}
    say(
        f"[POLICY] gate: {decision.get('outcome')} (rules: "
        f"{', '.join(decision.get('rules_matched') or [])}) - refund is not on the "
        f"allowlist and is in never_auto_execute; there is no approval lane"
    )
    say(
        f"[RESULT] action {res['status']} - {res['message']}. "
        f"Gateway mutations attempted: {h.gateway.mutation_attempts} (zero money moved)."
    )
    blocked_audit = h.audit_rows(entity_type="recovery_action", entity_id=res["action_id"])
    say(f"[AUDIT] block is recorded in the append-only audit trail ({len(blocked_audit)} rows):")
    for row in blocked_audit:
        say(f"        {row.actor:<26} {row.action}")
    return {
        "scenario": "E",
        "seed_run_id": seed["run_id"],
        "detection": det,
        "opportunities_created": build["created_count"],
        "forced_action_type": "refund",
        "policy_outcome": decision.get("outcome"),
        "rules_matched": list(decision.get("rules_matched") or []),
        "final_status": res["status"],
        "gateway_mutation_attempts": h.gateway.mutation_attempts,
        "audit_rows_for_action": len(blocked_audit),
    }


SCENARIOS: dict[str, Callable[[DemoHarness, Say], dict[str, Any]]] = {
    "A": scenario_a,
    "B": scenario_b,
    "C": scenario_c,
    "D": scenario_d,
    "E": scenario_e,
}


def run_scenario(name: str, db_path: str | Path, say: Say = lambda *_: None) -> dict[str, Any]:
    """Reset --db, run one scenario, return its key numbers (test entry point)."""
    harness = DemoHarness(db_path)
    try:
        return SCENARIOS[name.upper()](harness, lambda msg: say(_clean(msg)))
    finally:
        harness.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="demo_run",
        description="Deterministic, resettable PulseRecover demo scenarios.",
    )
    parser.add_argument(
        "--scenario",
        required=True,
        choices=[*SCENARIOS, "all"],
        help="which demo scenario to run",
    )
    parser.add_argument(
        "--db",
        default="scripts/.demo_scratch.db",
        help="scratch sqlite DB path (reset on every run; never the app DB)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the machine-readable key numbers after the narrative",
    )
    args = parser.parse_args(argv)

    # The narrative is the interface; keep structured app logs off stdout.
    import logging

    logging.disable(logging.CRITICAL)

    names = list(SCENARIOS) if args.scenario == "all" else [args.scenario]
    results: dict[str, Any] = {}
    for name in names:
        results[name] = run_scenario(name, args.db, say=print)
        print()
    if args.json:
        print(json.dumps(_jsonable(results), indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
