"""Measured customer-outcome model for the evaluation harness (DEF-03).

The harness used to decide whether a recovery action (or no action at all)
converts via a hand-set (failure-class x action) ``CONVERSION`` table plus a
flat ``GATEWAY_SUCCESS_RATE`` — documented priors, but priors nonetheless:
the causal comparison's outcome was decided by assumption, not measurement
(the stored pre-fix run showed holdout lift −2.55 pp with 0 ALLOWED gate
decisions, a number fully determined by those tables).

This module replaces every assumed outcome probability with rates MEASURED
from the simulator's own customer-behavior mechanism, fit on each arm's own
scratch database immediately after ``run_simulation`` (before any recovery
action or webhook mutates the data). Both arms and both holdout groups are
therefore scored by the SAME outcome generator — the simulator's observed
behavior — instead of by harness priors.

What the simulator actually models (app/simulator/engine.py), and what we
measure from its output:

- **Re-attempt after a failure** — the simulator's only "customer pays after
  a failure" mechanism: organic checkout retries (``CHECKOUT_RETRY_RATE`` of
  failures, one follow-up payment on the same order, succeeding at
  ``CHECKOUT_RETRY_SUCCESS * reliability``) and subscription dunning retries
  (T+1/T+2/T+3 at ``SUB_RETRY_SUCCESS * reliability``). Measured per failure
  class as P(a re-attempt is captured | the attempt it follows failed with
  class C) over all observed order-level attempt chains.
- **Payment-level self-resolution** — the simulator's late-capture quirk
  (``LATE_CAPTURE_RATE``: a failed payment flips to captured on the SAME
  payment id, 30–900 s later). Measured pooled as P(payment.captured after
  payment.failed on the same payment id | the payment reached failed), with
  the empirical lag distribution retained for the holdout's right-censoring
  draw. Pooled, not per-class: the simulator's late-capture draw is
  class-independent by construction, and per-class cells would be ~1% of
  already-small class buckets — a disclosed measurement-design choice.

Column mapping for the action columns the harness needs:

- ``immediate_retry[C]`` := retry_success[C]            — MEASURED.
- ``delayed_retry[C]``   := retry_success[C]            — MEASURED rate; the
  simulator's re-attempt success does not condition on wall-clock delay, so
  sharing the measured rate is an explicit DELAY-INVARIANCE assumption
  (ASSUMPTIONS[0]). In the batch harness delayed actions park in SCHEDULED
  and never fire (wall-clock delay never elapses), so this column is
  currently unexercised — disclosed, not silently dead.
- ``payment_link``       := pooled retry_success        — decided INSIDE the
  gateway twin as before, but the twin's flat rate is now the measured
  pooled re-attempt success instead of the hand-set 0.35. The simulator has
  no payment-link mechanism, so "a link converts like a re-attempt" is an
  explicit anchoring assumption (ASSUMPTIONS[1]).
- ``notify[C]``          := organic_return[C]           — P(the order sees a
  later captured payment | first attempt failed with class C), the
  simulator's observed "customer comes back and pays on their own" rate.
  The simulator has no notification channel, so "a nudged customer behaves
  like an organically returning one" is an explicit anchoring assumption
  (ASSUMPTIONS[2]).
- ``no_action``          := self_resolution (pooled)    — MEASURED
  late-capture rate. NOTE: the simulator's ORDER-level organic recovery
  (a successful retry on a new payment row) is outside the harness's
  payment-level verified-capture estimand and is never counted on either
  side — symmetric across arms and groups.

Determinism: the model is a pure function of the scratch database contents;
same seed + same config ⇒ identical data ⇒ identical rates. All remaining
draws stay seeded on stable simulator identities, exactly as before.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.models import Payment, PaymentEvent
from app.services.revenue.classify import FailureClass, classify_failure

# Minimum per-class cell size; smaller cells fall back to the pooled rate.
# At standard scale (~5k failures) every class clears it for re-attempts; at
# tiny plumbing scale most classes pool — noisy but still measured.
MIN_CELL = 30

#: Residual assumptions the measurement cannot remove, each with its value.
#: Recorded verbatim on every run (metrics + experiment config).
ASSUMPTIONS: tuple[str, ...] = (
    "delay-invariance: delayed_retry uses the measured re-attempt success "
    "rate — the simulator's re-attempt mechanism does not condition on "
    "wall-clock delay (value: same as immediate_retry per class)",
    "payment-link anchor: the gateway twin's flat inline link rate is set "
    "to the measured pooled re-attempt success — the simulator has no "
    "payment-link mechanism (value: pooled retry_success)",
    "notify anchor: a notified customer converts at the measured organic "
    "return-and-pay rate — the simulator has no notification channel "
    "(value: organic_return per class)",
    "self-resolution is payment-level late capture only: the simulator's "
    "order-level organic recovery (successful customer retry on a new "
    "payment row) is outside the verified-per-payment estimand and is "
    "never counted on either side (value: measured late-capture rate)",
)


@dataclass(frozen=True)
class OutcomeModel:
    """The measured outcome generator for one arm. All rates in [0, 1],
    keyed by FailureClass value where per-class."""

    retry_success: dict[str, float]
    organic_return: dict[str, float]
    self_resolution: float
    self_resolution_lags_minutes: tuple[float, ...]
    pooled_retry_success: float
    pooled_organic_return: float
    cells: dict[str, Any]
    assumptions: tuple[str, ...] = ASSUMPTIONS
    provenance: str = "measured_from_simulator_behavior"

    def rate_for(self, column: str, cls: FailureClass) -> float:
        """P(conversion) for one (action column, failure class) — the ONLY
        entry point the harness's outcome draws use."""
        if column in ("immediate_retry", "delayed_retry"):
            return self.retry_success.get(cls.value, self.pooled_retry_success)
        if column == "payment_link":
            return self.pooled_retry_success
        if column == "notify":
            return self.organic_return.get(cls.value, self.pooled_organic_return)
        if column == "no_action":
            return self.self_resolution
        raise ValueError(f"unknown outcome column {column!r}")

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe run-record form (metrics + experiment config)."""
        return {
            "provenance": self.provenance,
            "retry_success": {k: round(v, 6) for k, v in sorted(self.retry_success.items())},
            "organic_return": {k: round(v, 6) for k, v in sorted(self.organic_return.items())},
            "pooled_retry_success": round(self.pooled_retry_success, 6),
            "pooled_organic_return": round(self.pooled_organic_return, 6),
            "self_resolution": round(self.self_resolution, 6),
            "self_resolution_lags_minutes": {
                "observations": len(self.self_resolution_lags_minutes),
                "max": (round(max(self.self_resolution_lags_minutes), 4)
                        if self.self_resolution_lags_minutes else None),
            },
            "min_cell": MIN_CELL,
            "cells": self.cells,
            "column_mapping": {
                "immediate_retry": "retry_success[class] (measured)",
                "delayed_retry": "retry_success[class] (measured; delay-invariance assumption)",
                "payment_link": "pooled retry_success, twin-decided inline (anchor assumption)",
                "notify": "organic_return[class] (anchor assumption)",
                "no_action": "self_resolution, pooled late capture (measured)",
            },
            "assumptions": list(self.assumptions),
        }


def _per_class(
    hits: dict[str, int], totals: dict[str, int]
) -> tuple[dict[str, float], float]:
    """Per-class rates with pooled fallback under MIN_CELL."""
    pooled_hits = sum(hits.values())
    pooled_totals = sum(totals.values())
    pooled = pooled_hits / pooled_totals if pooled_totals else 0.0
    rates = {
        cls: (hits[cls] / totals[cls] if totals[cls] >= MIN_CELL else pooled)
        for cls in totals
    }
    return rates, pooled


def measure_outcomes(db: Session) -> OutcomeModel:
    """Fit the outcome model on one arm's scratch database.

    MUST be called after ``run_simulation`` and BEFORE any recovery action,
    webhook delivery, or status mutation — the measurement reads final
    payment statuses and event sequences as the simulator wrote them.
    Pure function of the data: iteration is fully ordered, no RNG.
    """
    payments = list(
        db.scalars(
            sa.select(Payment).order_by(Payment.order_id, Payment.created_at, Payment.id)
        )
    )

    # -- re-attempt chains (checkout organic retries + subscription dunning) --
    # Walk each order's attempts in time order; every attempt that follows a
    # failed attempt is a re-attempt, attributed to the failed attempt's class.
    retry_hits: dict[str, int] = {}
    retry_totals: dict[str, int] = {}
    # -- organic return: first attempt failed -> a LATER attempt captured ----
    return_hits: dict[str, int] = {}
    return_totals: dict[str, int] = {}

    current_order: str | None = None
    chain: list[Payment] = []

    def _flush_chain() -> None:
        for prev, nxt in zip(chain, chain[1:]):
            if prev.status != "failed":
                continue
            cls = classify_failure(prev).value
            retry_totals[cls] = retry_totals.get(cls, 0) + 1
            if nxt.status == "captured":
                retry_hits[cls] = retry_hits.get(cls, 0) + 1
        if chain and chain[0].status == "failed":
            cls = classify_failure(chain[0]).value
            return_totals[cls] = return_totals.get(cls, 0) + 1
            if any(p.status == "captured" for p in chain[1:]):
                return_hits[cls] = return_hits.get(cls, 0) + 1

    for payment in payments:
        if payment.order_id != current_order:
            _flush_chain()
            current_order = payment.order_id
            chain = []
        chain.append(payment)
    _flush_chain()

    # -- payment-level self-resolution (late capture on the same payment id) --
    # Event sequences as the simulator wrote them: payment.failed followed by
    # payment.captured on the SAME payment id.
    events = list(
        db.scalars(
            sa.select(PaymentEvent).order_by(
                PaymentEvent.payment_id, PaymentEvent.occurred_at, PaymentEvent.id
            )
        )
    )
    failed_at: dict[str, Any] = {}
    late_captures = 0
    lags: list[float] = []
    for ev in events:
        if ev.event_type == "payment.failed":
            failed_at.setdefault(ev.payment_id, ev.occurred_at)
        elif ev.event_type == "payment.captured" and ev.payment_id in failed_at:
            start = failed_at.pop(ev.payment_id)
            late_captures += 1
            lags.append(max(0.0, (ev.occurred_at - start).total_seconds() / 60))
    failed_events = late_captures + len(failed_at)

    retry_rates, pooled_retry = _per_class(retry_hits, retry_totals)
    return_rates, pooled_return = _per_class(return_hits, return_totals)
    self_resolution = late_captures / failed_events if failed_events else 0.0

    return OutcomeModel(
        retry_success=retry_rates,
        organic_return=return_rates,
        self_resolution=self_resolution,
        self_resolution_lags_minutes=tuple(sorted(lags)),
        pooled_retry_success=pooled_retry,
        pooled_organic_return=pooled_return,
        cells={
            "reattempts_by_class": dict(sorted(retry_totals.items())),
            "reattempts_captured_by_class": dict(sorted(retry_hits.items())),
            "first_failures_by_class": dict(sorted(return_totals.items())),
            "payments_with_failed_event": failed_events,
            "late_captures": late_captures,
        },
    )


__all__ = ["ASSUMPTIONS", "MIN_CELL", "OutcomeModel", "measure_outcomes"]
