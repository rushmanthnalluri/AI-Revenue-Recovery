"""Tunable configuration for the revenue-at-risk engine.

Every number here is a documented, deliberately conservative prior — not a
measured fact. They exist so the engine never has to hard-code an assumption;
each one carries its rationale inline and can be overridden by passing a
custom `RevenueConfig` to `RevenueService` (e.g. per-merchant calibration).

Rationale sources (see docs/research.md for the verified citations):
- Stripe claims businesses recover ~55% of failed payments on average
  (vendor claim) — so *infrastructure/transient* classes must sit above that
  average and customer-intent classes below it for the mean to land sensibly.
- Razorpay claims "up to 20%" recovery for failed-payment recovery links
  (vendor claim) — anchors notification/link strategies well below retries.
- Network "system integrity" guidance caps resubmission of never-approve
  (hard) declines — hard declines are near-zero recoverable by retries.
- Baymard: ~70% cart abandonment average; a declined card is only one of many
  causes — abandonment recovery is real but modest.
"""

from dataclasses import dataclass, field
from datetime import timedelta

from app.ports import ActionType
from app.services.revenue.classify import FailureClass


@dataclass(frozen=True)
class RevenueConfig:
    """All knobs for the revenue-at-risk engine. Times are UTC."""

    # Baseline window: success rates are measured on the `baseline_window`
    # immediately preceding the incident window. 7 days smooths weekday
    # cycles while staying recent enough to represent the current mix.
    baseline_window: timedelta = timedelta(days=7)

    # Used only when the incident row has no window_start/window_end yet
    # (detection may not have backfilled them): [detected_at - this, detected_at].
    default_incident_window: timedelta = timedelta(hours=1)

    # Amount segmentation edges in paise (INR). With the defaults the bands
    # are: <=Rs.500, Rs.500-2,000, Rs.2,000-10,000, >Rs.10,000. Ticket-size
    # matters because high-value payments fail and recover differently.
    amount_band_edges_paise: tuple[int, ...] = (50_000, 200_000, 1_000_000)

    # Below this many baseline attempts in a segment the estimate is flagged
    # low_confidence (the Wilson interval is already wide; this is the
    # explicit "do not trust the point" marker for consumers).
    min_baseline_sample: int = 30

    # Baseline sample size at which confidence saturates to 1.0.
    # confidence = min(1, n / full_confidence_sample).
    full_confidence_sample: int = 200

    # z for the Wilson score interval. 1.96 ~= 95% two-sided.
    wilson_z: float = 1.96

    # Confidence attached to prior-driven single-opportunity estimates (no
    # sampling data behind them, so always low — never above 0.5, which is
    # the low_confidence threshold used by the engine).
    prior_confidence: float = 0.3

    # -- Recoverability factors -------------------------------------------
    # P(revenue is winnable | payment failed with this class) — the fraction
    # of observed_loss attributable to the class that any well-executed
    # recovery program could plausibly capture. Ordering is part of the
    # contract (asserted by tests): transient classes high, customer-intent
    # medium, funds/permanent low.
    recoverability: dict[FailureClass, float] = field(
        default_factory=lambda: {
            # Timeouts are pure infrastructure: the customer tried to pay and
            # the rail hiccuped. Retrying shortly after usually succeeds.
            FailureClass.TIMEOUT: 0.70,
            # Gateway/bank technical errors and generic issuer declines:
            # transient for most customers, but some never return.
            FailureClass.SOFT_DECLINE: 0.60,
            # The customer abandoned or failed customer-side auth (cancelled,
            # wrong OTP/pin). Intent is uncertain; a nudge/link wins some back.
            FailureClass.ABANDONMENT: 0.35,
            # The money is not there right now. Payday-aware retries help, but
            # most attempts fail again until the balance is replenished.
            FailureClass.INSUFFICIENT_FUNDS: 0.20,
            # Invalid/disabled instrument, auth permanently failed, blocked.
            # Network rules discourage resubmission; only an instrument update
            # saves these, which the customer rarely does unprompted.
            FailureClass.HARD_DECLINE: 0.05,
            # No classifiable signal. Conservative — closer to hard than soft.
            FailureClass.UNKNOWN: 0.10,
        }
    )

    # -- Strategy effectiveness priors ------------------------------------
    # P(capture | recoverable revenue, strategy executed well). Applied on
    # top of recoverability, so expected_recovery <= recoverable always.
    strategy_effectiveness: dict[ActionType, float] = field(
        default_factory=lambda: {
            # Direct retry of the same instrument: the strongest tool for
            # transient failures.
            ActionType.RETRY_PAYMENT: 0.50,
            # Fresh payment link to the customer (Razorpay cites "up to 20%"
            # for recovery links; targeted links with context do somewhat
            # better, still far below a straight retry).
            ActionType.CREATE_PAYMENT_LINK: 0.30,
            ActionType.RESUME_SUBSCRIPTION: 0.25,
            ActionType.NOTIFY_CUSTOMER: 0.15,
            ActionType.EXTEND_GRACE_PERIOD: 0.10,
            ActionType.ESCALATE_HUMAN: 0.05,
            # Not revenue-recovering actions: kept at exactly 0 so a blocked
            # or protective action never inflates the plan.
            ActionType.PAUSE_SUBSCRIPTION: 0.0,
            ActionType.REFUND: 0.0,
            ActionType.NO_ACTION: 0.0,
        }
    )

    # Default failure class per opportunity_type when the opportunity has no
    # classifiable payment attached (used by opportunity_estimate only).
    opportunity_class_defaults: dict[str, FailureClass] = field(
        default_factory=lambda: {
            "failed_payment_retry": FailureClass.UNKNOWN,
            "dropped_checkout": FailureClass.ABANDONMENT,
            # Razorpay's own T+1..T+3 retries already exhausted the transient
            # share, so what remains behaves like a soft decline, not a timeout.
            "subscription_halted": FailureClass.SOFT_DECLINE,
            "authorization_stuck": FailureClass.TIMEOUT,
            "refund_leakage": FailureClass.UNKNOWN,
        }
    )


DEFAULT_CONFIG = RevenueConfig()
