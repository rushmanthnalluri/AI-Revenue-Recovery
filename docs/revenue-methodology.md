# Revenue-at-Risk Methodology

How PulseRecover quantifies what a payment incident costs and what recovering
it is worth. Implementation: `backend/app/services/revenue/`
(`engine.py` — math; `config.py` — every tunable prior, with rationale;
`classify.py` — failure classes; `statistics.py` — uncertainty).

> **Cardinal rule: failed transactions ≠ lost revenue.**
> Summing failed payment amounts is the naive number, and it is wrong in both
> directions at once (see §7). Everything below exists to avoid it.

## 1. The four numbers (never interchangeable)

| Number | Kind | Meaning |
|---|---|---|
| `observed_loss` | estimate + band | Counterfactual expected revenue in the incident window minus what was actually captured. "What the degradation cost." |
| `recoverable` | estimate + band | The subset of `observed_loss` that is realistically winnable, after weighting each failure class by a recoverability factor. "What is worth chasing." |
| `expected_recovery` | estimate + band | `recoverable` discounted by a per-strategy effectiveness prior. Planning number for the strategy generator. "What executing strategy S should bring back." |
| `actual_recovered` | measured, no band | Sum of webhook-verified captured amounts on `recovery_actions` in status `RECOVERED`. "What verification proved came back." |

If these four ever collapse into one number, the methodology has failed. In
particular, `actual_recovered` is *never* an estimate: it is a ledger read,
and actions whose outcome is `UNKNOWN` are surfaced separately, never folded
in (architecture §3: verification proves).

## 2. Segmentation

Payments are segmented by:

- **method** — `upi` / `card` / `netbanking` / ... (`unknown` if missing),
- **amount band** — configurable edges in paise; default ₹500 / ₹2,000 /
  ₹10,000 (`le_50000`, `50000_200000`, `200000_1000000`, `gt_1000000`),
- **customer type** — `returning` (has a captured payment before the baseline
  window), `new` (exists but no prior capture), `unknown` (no customer id).

Success behaviour differs enough across these cells (UPI vs card rails,
ticket size, customer familiarity) that pooling them would hide exactly the
structure an incident usually lives in. The price of segmentation is small
samples — which the uncertainty machinery (§5) is built to surface, not hide.

A payment's **resolved outcome**: `captured` (status `captured`/`refunded` or
`captured` flag) vs `failed` (status `failed`). Payments still in flight
(`created`/`authorized`) are *pending* and excluded from rates, volumes, and
loss — in-flight payments must not inflate a loss estimate.

## 3. Counterfactual expected revenue and observed loss

For each segment, over a **baseline window** (default: the 7 days immediately
before the incident window; configurable):

```
baseline_success_rate = captured_baseline / n_baseline
avg_order_value       = Σ amount_baseline / n_baseline
```

For the **incident window** (from the incident row; fallback
`detected_at − 1h … detected_at` when detection hasn't backfilled it):

```
counterfactual_expected = attempted_count × baseline_success_rate × avg_order_value
observed_loss           = max(0, counterfactual_expected − actual_captured)
```

Clamping at zero is deliberate: a segment that outperformed its baseline
during the window has no loss to recover, and we do not net "wins" in one
segment against losses in another.

This is the defensibility core: the loss is measured against *what this
segment normally achieves*, not against a fantasy where every attempt
captures. Some failures are the normal background rate of payments; only the
*excess* failure is incident loss.

## 4. Recoverable: failure classes and recoverability factors

Not all loss is chaseable. Every failed payment in the window is classified
(`classify.py`, defensive substring matching on `error_reason`/`error_code`/
`error_description` with an `error_source` fallback — Razorpay's enums are
not canonical across docs pages, so we never strict-match):

| Class | Example reasons | Factor | Rationale |
|---|---|---|---|
| `timeout` | `payment_timed_out` | 0.70 | Pure infrastructure; customer intended to pay. Retrying usually works. |
| `soft_decline` | `gateway_technical_error`, `bank_technical_error`, `card_declined`, `payment_declined` | 0.60 | Transient for most; some customers never return. |
| `abandonment` | `payment_cancelled`, `incorrect_otp`, `incorrect_pin` | 0.35 | Customer intent uncertain; nudges/links win some back (Baymard: ~70% baseline cart abandonment). |
| `insufficient_funds` | `insufficient_fund` | 0.20 | Money isn't there now; payday-aware retries help, most attempts still fail. |
| `hard_decline` | `card_number_invalid`, `card_disabled_*`, `authentication_failed`, `pin_attempts_exceeded`, `debit_instrument_blocked` | 0.05 | Network rules discourage resubmission; only an instrument update saves these. |
| `unknown` | no classifiable signal | 0.10 | Conservative by design — closer to hard than soft. |

The segment's `observed_loss` is allocated to classes by each class's share
of the window's **failed amount**, then:

```
recoverable_class = allocated_loss_class × recoverability_factor(class)
recoverable       = Σ_class recoverable_class        ⇒  recoverable ≤ observed_loss
```

Calibration anchor (vendor claims, named as such): Stripe says ~55% of failed
payments are recovered on average, so transient classes sit above that mean
and customer-intent classes below it. The factors are *priors to be calibrated
per merchant from observed recovery outcomes* — they live in one documented
config dict (`RevenueConfig.recoverability`), and tests pin their ordering so
a careless edit fails loudly.

## 5. Expected recovery per strategy

```
expected_recovery(strategy) = recoverable × effectiveness_prior(strategy)
```

Priors (`RevenueConfig.strategy_effectiveness`): `retry_payment` 0.50,
`create_payment_link` 0.30 (Razorpay cites "up to 20%" for recovery links —
targeted links do somewhat better, still below a straight retry),
`resume_subscription` 0.25, `notify_customer` 0.15, `extend_grace_period`
0.10, `escalate_human` 0.05. `refund`, `pause_subscription`, `no_action` are
exactly 0.0 — protective or non-recovery actions must never inflate a plan.

For a **single opportunity** (`opportunity_estimate`) there is no population
to measure a rate over: the estimate is prior-driven, the band is the full
`[0, amount]` Bernoulli range, and `low_confidence` is always true. These
numbers rank strategies; they do not promise revenue.

## 6. Uncertainty

The only sampled quantity is the baseline success rate (binomial). Its
interval is the **Wilson score interval** (z = 1.96 ≈ 95%):

```
denom  = 1 + z²/n
center = (p̂ + z²/2n) / denom
half   = z · √(p̂(1−p̂)/n + z²/4n²) / denom
```

Wilson over the Wald/normal approximation because it stays sane for small n
and extreme rates (p̂ near 0 or 1) — the exact regime payment segments live
in — and with n = 0 it degrades honestly to [0, 1].

Propagation is deliberately simple: the loss formula is evaluated at the
rate's interval bounds (it is monotonic in the rate), giving
`[lower, upper] = [max(0, V·p_lo·AOV − C), max(0, V·p_hi·AOV − C)]`. Bands
scale outward (floor lower / ceil upper) through recoverability and
effectiveness factors. Aggregate bands **sum endpoints** — a conservative
choice (worst-case correlation); independence assumptions would be tighter
but less honest.

Alongside the band, every estimate carries:

- `confidence` — `min(1, n_baseline / 200)`, a blunt "how much data backs
  this" score for the dashboard (the band is the statistics; this is the UX);
- `low_confidence` — true when confidence < 0.5 or baseline n < 30;
- `point_paise = None` — when a segment has **zero** baseline signal. There
  is no defensible point; the band ([0, full attempted volume]) is the
  answer. Aggregates sum the points that exist, include the unknown segments'
  bands, flag `low_confidence`, and say so in `basis`.

## 7. Why not just sum failed amounts?

The naive `lost = Σ failed amount` is wrong in both directions:

1. **It overcounts**: a segment whose baseline success rate is 85% *normally*
   fails 15% of volume. During an incident degrading it to 50%, the naive sum
   counts 50% of volume as lost; the counterfactual says the incident cost
   35 percentage points. Attribution matters for diagnosis → action.
2. **It undercounts intent**: `insufficient_fund` failures are mostly
   unrecoverable *now*, while `payment_timed_out` failures mostly are. A flat
   sum treats ₹1,00,000 of hard declines as identical to ₹1,00,000 of
   timeouts, and the recovery plan built on it will chase the wrong money.

Worked example (from the test suite's synthetic population): baseline 400
UPI payments @ ₹1,000 with 90% captured; incident window 100 payments with
50% captured. Naive failed-sum = ₹50,000. Counterfactual: expected
100 × 0.90 × ₹1,000 = ₹90,000, captured ₹50,000 → `observed_loss` = ₹40,000
(Wilson band ≈ ₹36,700–₹42,600). All failures `payment_timed_out` →
`recoverable` = ₹28,000; `expected_recovery(retry)` = ₹14,000. Four
different, individually defensible answers to four different questions.

## 8. Actual recovered (measured, not estimated)

`recovered_revenue(start, end, incident_id=None)` sums `amount_paise` over
`recovery_actions` with status `RECOVERED` whose `verified_at` (falling back
to `completed_at`) lies in the window, broken down by incident and action
type. `UNKNOWN` actions are counted separately and excluded. No band: this is
the number the buildathon rubric calls "measured money recovered", and it is
only as good as the webhook verification that set those statuses — which is
exactly where the trust should live.

## 9. Assumptions and limitations

- **Windows use local ingestion time** (`payments.created_at`), not gateway
  time; clock skew between ingest and gateway can blur window edges.
- **Incident scope is global** — the `Incident` model has no merchant FK, so
  multi-merchant deployments currently pool merchants. Add a merchant filter
  when the model grows one.
- **AOV is taken as fixed** per segment; its sampling error is not propagated
  (only the rate's is). Documented simplification, kept for clarity.
- **Loss allocation to failure classes** assumes the excess failures are
  distributed like the observed failed-amount mix in the window. If the
  incident selectively hit one class, that *is* the observed mix, so this is
  self-consistent for the common case (one dominant failing class).
- **Late captures** (`payment.failed` followed by `payment.captured` for the
  same transaction — documented Razorpay behaviour) reduce `observed_loss`
  only if they land inside the incident window. A payment that recovers
  itself after the window is still counted as loss. Conservative; noted.
- **Recoverability/effectiveness factors are priors**, not measurements,
  until per-merchant recovery outcomes accumulate to calibrate against. They
  are labelled vendor-claim-anchored in `config.py`, and every consumer can
  see `low_confidence` and the band instead of trusting a point.
- **Band summation is conservative** (assumes worst-case correlation between
  segments/classes), so totals lean wide rather than falsely precise.
- Tests use small **synthetic, planted-rate populations**; all example numbers
  (including above) are preliminary/synthetic by construction, not production
  measurements.

## 10. Tuning

Everything is in `RevenueConfig` (`backend/app/services/revenue/config.py`):
windows, amount-band edges, sample thresholds, Wilson z, recoverability
factors, strategy priors, opportunity-type defaults. Pass a custom instance
to `RevenueService(session, config=...)` per merchant or per experiment. When
editing factors, run `tests/revenue/test_config_monotonicity.py` — the
ordering constraints are part of the methodology's credibility.
