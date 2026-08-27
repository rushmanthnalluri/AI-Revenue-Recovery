"""Incident behavior registry: how each injected incident kind matches and
modifies payments, and what the evaluation harness should expect to find.

An ``ActiveIncident`` is an ``IncidentSpec`` resolved to absolute UTC window
bounds. The engine asks two questions per payment:

- ``matches(incident, ctx)`` — is this payment in scope (window + target
  dimension: method / bank / route / subscription)?
- ``effect(incident)`` — the modifiers applied to in-scope payments:
  ``fail_boost`` (extra failure probability, with a forced reason mix),
  ``abandon_boost`` (checkout abandonment), ``latency_multiplier``.

``expected_truth`` feeds ``simulator_ground_truth.truth``: the expected
detection signature + root cause, i.e. the scoring key for diagnosis.
"""

from dataclasses import dataclass
from datetime import datetime

from app.simulator.config import IncidentKind, IncidentSpec


@dataclass(frozen=True)
class PayContext:
    """What an incident matcher may look at (no RNG state)."""

    ts: datetime  # payment creation time (tz-aware UTC)
    method: str
    bank: str
    network: str
    route: str
    card_type: str
    is_subscription: bool


@dataclass(frozen=True)
class ActiveIncident:
    index: int  # position in config.incidents
    entity_id: str  # deterministic ground-truth id: inc_sim_{seed}_{index:02d}
    spec: IncidentSpec
    start: datetime
    end: datetime

    @property
    def kind(self) -> IncidentKind:
        return self.spec.kind


@dataclass(frozen=True)
class IncidentEffect:
    fail_boost: float = 0.0
    abandon_boost: float = 0.0
    latency_multiplier: float = 1.0
    # forced failure-reason mix for payments flipped by fail_boost
    reason_weights: tuple[tuple[str, float], ...] = ()


_FAIL_REASONS: dict[IncidentKind, tuple[tuple[str, float], ...]] = {
    IncidentKind.GATEWAY_DEGRADATION: (
        ("gateway_technical_error", 0.80),
        ("payment_timed_out", 0.20),
    ),
    IncidentKind.ROUTE_LATENCY: (("payment_timed_out", 1.00),),
    IncidentKind.METHOD_OUTAGE: (
        ("bank_downtime", 0.70),
        ("bank_technical_error", 0.30),
    ),
    IncidentKind.CHECKOUT_ABANDONMENT_SPIKE: (),
    IncidentKind.SUBSCRIPTION_FAILURE_SPIKE: (
        ("insufficient_fund", 0.50),
        ("payment_declined", 0.30),
        ("payment_timed_out", 0.20),
    ),
    IncidentKind.CUSTOMER_INSUFFICIENT_FUNDS_WAVE: (
        ("insufficient_fund", 0.90),
        ("transaction_limit_exceeded", 0.10),
    ),
}


def effect(incident: ActiveIncident) -> IncidentEffect:
    kind, p = incident.kind, incident.spec.params
    if kind is IncidentKind.CHECKOUT_ABANDONMENT_SPIKE:
        return IncidentEffect(abandon_boost=float(p.get("abandon_boost", 0.45)))
    return IncidentEffect(
        fail_boost=float(p.get("fail_boost", 0.50)),
        latency_multiplier=float(p.get("latency_multiplier", 1.0)),
        reason_weights=_FAIL_REASONS[kind],
    )


def matches(incident: ActiveIncident, ctx: PayContext) -> bool:
    if not (incident.start <= ctx.ts < incident.end):
        return False
    kind, p = incident.kind, incident.spec.params
    if kind is IncidentKind.METHOD_OUTAGE:
        if ctx.method != p.get("method", "upi"):
            return False
        bank = p.get("bank")
        if bank and ctx.bank != bank:
            return False
        return True
    if kind is IncidentKind.ROUTE_LATENCY:
        return ctx.route == p.get("route", "pg_primary")
    if kind is IncidentKind.CHECKOUT_ABANDONMENT_SPIKE:
        return not ctx.is_subscription
    if kind is IncidentKind.SUBSCRIPTION_FAILURE_SPIKE:
        return ctx.is_subscription
    if kind is IncidentKind.CUSTOMER_INSUFFICIENT_FUNDS_WAVE:
        return ctx.method in ("card", "upi")
    # GATEWAY_DEGRADATION: everything in window
    return True


_EXPECTED: dict[IncidentKind, dict] = {
    IncidentKind.GATEWAY_DEGRADATION: {
        "expected_root_cause": "gateway_outage",
        "expected_signature": {
            "metric": "payment_success_rate",
            "dimension": {},
            "direction": "drop",
        },
        "recoverable": False,
        "recovery_hint": "wait_out_or_reroute",
    },
    IncidentKind.ROUTE_LATENCY: {
        "expected_root_cause": "gateway_route_latency",
        "expected_signature": {
            "metric": "capture_latency_ms",
            "dimension": {},
            "direction": "spike",
        },
        "recoverable": False,
        "recovery_hint": "shift_traffic_to_secondary_route",
    },
    IncidentKind.METHOD_OUTAGE: {
        "expected_root_cause": "bank_downtime",
        "expected_signature": {
            "metric": "payment_success_rate",
            "dimension": {},
            "direction": "drop",
        },
        "recoverable": True,
        "recovery_hint": "notify_customers_retry_after_downtime",
    },
    IncidentKind.CHECKOUT_ABANDONMENT_SPIKE: {
        "expected_root_cause": "checkout_abandonment",
        "expected_signature": {
            "metric": "checkout_abandonment_rate",
            "dimension": {},
            "direction": "spike",
        },
        "recoverable": True,
        "recovery_hint": "payment_links_with_reminders",
    },
    IncidentKind.SUBSCRIPTION_FAILURE_SPIKE: {
        "expected_root_cause": "subscription_soft_declines",
        "expected_signature": {
            "metric": "subscription_failure_rate",
            "dimension": {},
            "direction": "spike",
        },
        "recoverable": True,
        "recovery_hint": "dunning_retries_and_card_update_nudge",
    },
    IncidentKind.CUSTOMER_INSUFFICIENT_FUNDS_WAVE: {
        "expected_root_cause": "insufficient_balance_pattern",
        "expected_signature": {
            "metric": "payment_success_rate",
            "dimension": {"error_reason": "insufficient_fund"},
            "direction": "drop",
        },
        "recoverable": True,
        "recovery_hint": "retry_at_payday_or_salary_hours",
    },
}


def expected_truth(incident: ActiveIncident) -> dict:
    """Ground-truth metadata: what detection/diagnosis should find."""
    base = dict(_EXPECTED[incident.kind])
    signature = dict(base["expected_signature"])
    # fill the observed dimension for targeted incidents
    p = incident.spec.params
    if incident.kind is IncidentKind.METHOD_OUTAGE:
        signature["dimension"] = {"method": p.get("method", "upi")}
        if p.get("bank"):
            signature["dimension"]["bank"] = p["bank"]
    elif incident.kind is IncidentKind.ROUTE_LATENCY:
        signature["dimension"] = {"route": p.get("route", "pg_primary")}
    base["expected_signature"] = signature
    base["kind"] = incident.kind.value
    base["params"] = dict(p)
    return base
