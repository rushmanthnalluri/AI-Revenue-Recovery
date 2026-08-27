"""Simulator configuration: scale knobs, incident specs, scenario presets.

A ``SimulatorConfig`` fully determines the generated dataset (given the code
version): same config → same aggregate data. ``config_hash()`` is the
idempotency key used by scripts/seed.py — a completed ``simulator_runs`` row
with the same deterministic run id means the dataset is already seeded.
"""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class IncidentKind(str, Enum):
    GATEWAY_DEGRADATION = "gateway_degradation"
    ROUTE_LATENCY = "route_latency"
    METHOD_OUTAGE = "method_outage"  # bank_downtime failure spike
    CHECKOUT_ABANDONMENT_SPIKE = "checkout_abandonment_spike"
    SUBSCRIPTION_FAILURE_SPIKE = "subscription_failure_spike"
    CUSTOMER_INSUFFICIENT_FUNDS_WAVE = "customer_insufficient_funds_wave"


@dataclass(frozen=True)
class IncidentSpec:
    """A parameterized incident window. ``day_fraction`` (0..1) positions the
    window within the run so the default schedule scales with ``--days``."""

    kind: IncidentKind
    day_fraction: float
    start_hour_ist: float
    duration_hours: float
    params: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "kind": self.kind.value,
            "day_fraction": self.day_fraction,
            "start_hour_ist": self.start_hour_ist,
            "duration_hours": self.duration_hours,
            "params": dict(self.params),
        }


def default_incidents() -> tuple[IncidentSpec, ...]:
    """The "standard" 30-day schedule: six well-separated incidents covering
    every kind, tuned so each measurably moves its target metric."""
    return (
        IncidentSpec(
            IncidentKind.GATEWAY_DEGRADATION, day_fraction=0.18,
            start_hour_ist=14.0, duration_hours=1.5,
            params={"fail_boost": 0.35, "latency_multiplier": 2.5},
        ),
        IncidentSpec(
            IncidentKind.METHOD_OUTAGE, day_fraction=0.35,
            start_hour_ist=20.0, duration_hours=2.0,
            params={"method": "upi", "fail_boost": 0.80},
        ),
        IncidentSpec(
            IncidentKind.ROUTE_LATENCY, day_fraction=0.55,
            start_hour_ist=9.0, duration_hours=2.0,
            params={"route": "pg_primary", "latency_multiplier": 8.0, "fail_boost": 0.06},
        ),
        IncidentSpec(
            IncidentKind.CUSTOMER_INSUFFICIENT_FUNDS_WAVE, day_fraction=0.70,
            start_hour_ist=0.0, duration_hours=20.0,
            params={"fail_boost": 0.30},
        ),
        IncidentSpec(
            IncidentKind.CHECKOUT_ABANDONMENT_SPIKE, day_fraction=0.82,
            start_hour_ist=18.0, duration_hours=2.5,
            params={"abandon_boost": 0.45},
        ),
        IncidentSpec(
            # spans 48h: monthly cycles charge once per 30 days, so a short
            # window would catch almost no subscription charges
            IncidentKind.SUBSCRIPTION_FAILURE_SPIKE, day_fraction=0.90,
            start_hour_ist=4.0, duration_hours=48.0,
            params={"fail_boost": 0.55},
        ),
    )


DEFAULT_TARGET_EVENTS = 65_000  # comfortably above the 60k payment_events bar
DEFAULT_DAYS = 30
DEFAULT_CUSTOMERS = 3_000
DEFAULT_SEED = 42


@dataclass(frozen=True)
class SimulatorConfig:
    seed: int = DEFAULT_SEED
    days: int = DEFAULT_DAYS
    target_events: int = DEFAULT_TARGET_EVENTS
    customers: int = DEFAULT_CUSTOMERS
    scenario: str = "standard"
    incidents: tuple[IncidentSpec, ...] = field(default_factory=default_incidents)
    # Anchor for the window: data covers [end_date - days, end_date).
    # None → today 00:00 UTC (deterministic within a calendar day).
    end_date: datetime | None = None
    merchant_name: str = "PulseRecover Demo Store"
    chunk_size: int = 4_000

    def config_dict(self) -> dict:
        return {
            "seed": self.seed,
            "days": self.days,
            "target_events": self.target_events,
            "customers": self.customers,
            "scenario": self.scenario,
            "incidents": [i.to_dict() for i in self.incidents],
            "end_date": self.end_date.isoformat() if self.end_date else None,
            "merchant_name": self.merchant_name,
        }

    def config_hash(self) -> str:
        blob = json.dumps(self.config_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()

    @property
    def run_id(self) -> str:
        """Deterministic run id — reseeding the same config targets the same
        simulator_runs row, which is what makes seed.py idempotent."""
        return f"sim_{self.seed}_{self.config_hash()[:10]}"


# ---------------------------------------------------------------------------
# Scenario presets (scripts/simulate.py, later the /api/v1/demo router)
# ---------------------------------------------------------------------------

def _quiet() -> SimulatorConfig:
    return SimulatorConfig(scenario="quiet", incidents=())


def _upi_outage_demo() -> SimulatorConfig:
    return SimulatorConfig(
        scenario="upi_outage_demo", days=10, target_events=20_000, customers=1_500,
        incidents=(
            IncidentSpec(
                IncidentKind.METHOD_OUTAGE, day_fraction=0.50,
                start_hour_ist=19.0, duration_hours=3.0,
                params={"method": "upi", "fail_boost": 0.85},
            ),
        ),
    )


def _payday_wave_demo() -> SimulatorConfig:
    return SimulatorConfig(
        scenario="payday_wave_demo", days=14, target_events=25_000, customers=2_000,
        incidents=(
            IncidentSpec(
                IncidentKind.CUSTOMER_INSUFFICIENT_FUNDS_WAVE, day_fraction=0.60,
                start_hour_ist=0.0, duration_hours=20.0,
                params={"fail_boost": 0.35},
            ),
        ),
    )


def _storm() -> SimulatorConfig:
    """Stress scenario: denser, partially overlapping incidents."""
    return SimulatorConfig(
        scenario="storm",
        incidents=default_incidents()
        + (
            IncidentSpec(
                IncidentKind.METHOD_OUTAGE, day_fraction=0.45,
                start_hour_ist=13.0, duration_hours=2.0,
                params={"method": "card", "bank": "HDFC", "fail_boost": 0.70},
            ),
            IncidentSpec(
                IncidentKind.GATEWAY_DEGRADATION, day_fraction=0.86,
                start_hour_ist=21.0, duration_hours=2.0,
                params={"fail_boost": 0.40, "latency_multiplier": 3.0},
            ),
        ),
    )


SCENARIOS: dict[str, tuple[str, object]] = {
    "standard": ("30 days, ~65k events, one incident of each kind", SimulatorConfig),
    "quiet": ("Clean baseline, no injected incidents", _quiet),
    "upi_outage_demo": ("10 days, prime-time UPI bank downtime", _upi_outage_demo),
    "payday_wave_demo": ("14 days, month-end insufficient-funds wave", _payday_wave_demo),
    "storm": ("30 days, 8 incidents with overlaps (stress)", _storm),
}


def list_scenarios() -> list[dict]:
    """Shape matches app.schemas.demo.ScenarioInfo for the demo router wave."""
    out = []
    for name, (description, factory) in sorted(SCENARIOS.items()):
        cfg = factory()  # type: ignore[operator]
        metric = None
        if cfg.incidents:
            # Hints must stay within detection's KNOWN_METRICS
            # (payment_success_rate, capture_latency_ms,
            # checkout_abandonment_rate, insufficient_fund_share).
            # Subscription failures are terminal payment failures, so they do
            # degrade the success rate. Abandoned checkouts stay `created`
            # (never terminal) — the detection engine sees them via the
            # attempt-based checkout_abandonment_rate metric.
            metric = {
                IncidentKind.GATEWAY_DEGRADATION: "payment_success_rate",
                IncidentKind.ROUTE_LATENCY: "capture_latency_ms",
                IncidentKind.METHOD_OUTAGE: "payment_success_rate",
                IncidentKind.CHECKOUT_ABANDONMENT_SPIKE: "checkout_abandonment_rate",
                IncidentKind.SUBSCRIPTION_FAILURE_SPIKE: "payment_success_rate",
                IncidentKind.CUSTOMER_INSUFFICIENT_FUNDS_WAVE: "payment_success_rate",
            }[cfg.incidents[0].kind]
        out.append(
            {"name": name, "description": description, "expected_incident_metric": metric}
        )
    return out
