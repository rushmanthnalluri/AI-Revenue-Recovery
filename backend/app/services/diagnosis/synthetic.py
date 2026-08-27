"""Mini labeled-synthetic generator for the diagnosis model.

Stands in for the full simulator (built in parallel by another agent) so that
``scripts/train_models.py`` runs standalone today. It mirrors the simulator's
fault taxonomy (see taxonomy.py) with hand-tuned injection signatures grounded
in Razorpay's documented failure telemetry (docs/research.md):
error_source/error_step/error_reason on failed payments, method/bank mixes,
and latency inflation for route degradation.

Each generated sample is one incident window: a baseline hour of "normal"
merchant traffic followed by a one-hour window with the cause injected. The
same ``compute_features`` used at inference time turns both into a feature
row, so training exercises the exact production feature path.

Windows are interleaved chronologically across causes (round-robin) so every
temporal split third contains every class — the split stays a honest
time-ordered split without starving the test set of any class.

This is a *small, clean* generator: signatures are deliberately separable.
Metrics on it are PRELIMINARY and upper-bound-ish; the later wave retrains on
full simulator output with noisier, mixed-cause windows.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
import pandas as pd

from app.services.diagnosis.features import FEATURE_NAMES, compute_features
from app.services.diagnosis.taxonomy import CAUSES, CauseLabel

Record = dict[str, Any]

BANKS = ("hdfc", "icici", "sbi", "axis", "kotak")
METHOD_MIX = {"upi": 0.45, "card": 0.30, "netbanking": 0.15, "wallet": 0.07, "emi": 0.03}

# Baseline "customer-intent" failure mix (reason -> probability), grounded in
# Razorpay's test-mode failure taxonomy.
BASE_REASON_MIX = {
    "incorrect_otp": 0.30,
    "insufficient_fund": 0.20,
    "payment_cancelled": 0.20,
    "card_declined": 0.20,
    "gateway_technical_error": 0.05,
    "bank_technical_error": 0.05,
}
# error_source/step consistent with Razorpay's telemetry taxonomy.
REASON_SOURCE = {
    "incorrect_otp": ("customer", "payment_authentication"),
    "authentication_failed": ("customer", "payment_authentication"),
    "insufficient_fund": ("customer", "payment_authorization"),
    "payment_cancelled": ("customer", "payment_authorization"),
    "card_declined": ("customer", "payment_authorization"),
    "transaction_limit_exceeded": ("customer", "payment_authorization"),
    "gateway_technical_error": ("gateway", "payment_authorization"),
    "payment_timed_out": ("gateway", "payment_authorization"),
    "bank_technical_error": ("bank", "payment_authorization"),
}

GENESIS = datetime(2026, 6, 1, tzinfo=timezone.utc)


@dataclass(frozen=True)
class SyntheticConfig:
    windows_per_class: int = 200
    seed: int = 42
    baseline_lambda: int = 120  # mean payments per hour
    window_hours: int = 1
    start: datetime = GENESIS


def _amount_paise(rng: np.random.Generator) -> int:
    return int(np.clip(rng.lognormal(11.5, 0.8), 1000, 1_000_000))


def _latency_ms(rng: np.random.Generator) -> float:
    return float(np.clip(rng.lognormal(5.3, 0.45), 50.0, 5000.0))


def _base_record(rng: np.random.Generator, t_offset_s: float) -> Record:
    methods = list(METHOD_MIX)
    method = str(rng.choice(methods, p=[METHOD_MIX[m] for m in methods]))
    if method in ("card", "netbanking"):
        bank = str(rng.choice(BANKS))
    elif method == "upi":
        bank = str(rng.choice(BANKS)) if rng.random() < 0.7 else None
    else:
        bank = None
    return {
        "outcome": "captured",
        "method": method,
        "bank": bank,
        "error_source": None,
        "error_step": None,
        "error_reason": None,
        "latency_ms": _latency_ms(rng),
        "is_subscription": bool(rng.random() < 0.10),
        "amount_paise": _amount_paise(rng),
        "t_offset_s": t_offset_s,
    }


def _fail(rec: Record, reason: str, rng: np.random.Generator | None = None) -> None:
    source, step = REASON_SOURCE[reason]
    rec["outcome"] = "failed"
    rec["error_source"] = source
    rec["error_step"] = step
    rec["error_reason"] = reason


def _sample_normal(rng: np.random.Generator, cfg: SyntheticConfig) -> list[Record]:
    """One hour of healthy traffic: ~8% failures (12% on subscriptions),
    6% abandonment proxy, lognormal latencies."""
    n = int(rng.poisson(cfg.baseline_lambda))
    window_s = cfg.window_hours * 3600
    records = [_base_record(rng, float(rng.uniform(0, window_s))) for _ in range(n)]
    reasons = list(BASE_REASON_MIX)
    reason_p = [BASE_REASON_MIX[r] for r in reasons]
    for rec in records:
        u = rng.random()
        if u < 0.06:
            rec["outcome"] = "pending"  # created, never completed in-window
            continue
        fail_p = 0.12 if rec["is_subscription"] else 0.08
        if u < 0.06 + fail_p:
            _fail(rec, str(rng.choice(reasons, p=reason_p)), rng)
    return records


def _inject(cause: str, records: list[Record], rng: np.random.Generator) -> None:
    """Mutate a freshly sampled normal window to express the fault signature."""
    ok = [r for r in records if r["outcome"] == "captured"]

    def convert(pool: list[Record], frac: float, reason: str) -> None:
        k = int(round(frac * len(pool)))
        if k <= 0:
            return
        for rec in rng.choice(np.array(pool, dtype=object), size=k, replace=False):
            _fail(rec, reason, rng)

    if cause == CauseLabel.NO_FAULT.value:
        return

    if cause == CauseLabel.GATEWAY_DEGRADATION.value:
        # Broad authorization failures at the gateway, all methods/banks.
        convert(ok, 0.25, "gateway_technical_error")
        for r in records:
            r["latency_ms"] *= 1.3

    elif cause == CauseLabel.ROUTE_LATENCY.value:
        # Latency triples; a tail of timeouts, mostly still succeeding.
        for r in records:
            r["latency_ms"] *= 3.2
        convert(ok, 0.06, "payment_timed_out")

    elif cause == CauseLabel.METHOD_OUTAGE.value:
        # One method's rails down (UPI most often), spread across banks so it
        # is not confusable with a single-bank downtime.
        method = str(rng.choice(["upi", "upi", "upi", "card", "netbanking"]))
        on_method = [r for r in ok if r["method"] == method]
        half = int(round(0.5 * len(on_method)))
        for i, rec in enumerate(rng.choice(np.array(on_method, dtype=object), size=int(round(0.9 * len(on_method))), replace=False) if on_method else []):
            _fail(rec, "payment_timed_out" if i < half else "bank_technical_error", rng)

    elif cause == CauseLabel.BANK_DOWNTIME.value:
        bank = str(rng.choice(BANKS))
        on_bank = [r for r in ok if r["bank"] == bank]
        convert(on_bank, 0.9, "bank_technical_error")

    elif cause == CauseLabel.ABANDONMENT_SPIKE.value:
        # Checkout drop-off: many more payments created and never completed;
        # failure rate among completed attempts stays ~normal.
        k = int(round(0.30 * len(records)))
        if k:
            for rec in rng.choice(np.array(records, dtype=object), size=k, replace=False):
                rec["outcome"] = "pending"
                rec["error_source"] = rec["error_step"] = rec["error_reason"] = None

    elif cause == CauseLabel.SUBSCRIPTION_FAILURE_SPIKE.value:
        # Recurring charges failing en masse (card expiry / mandate issues).
        subs = [r for r in ok if r["is_subscription"]]
        k = int(round(0.55 * len(subs)))
        if k:
            chosen = rng.choice(np.array(subs, dtype=object), size=k, replace=False)
            for i, rec in enumerate(chosen):
                _fail(rec, ("card_declined", "insufficient_fund", "authentication_failed")[i % 3], rng)

    elif cause == CauseLabel.CUSTOMER_INSUFFICIENT_FUNDS_WAVE.value:
        # Customer-side wave (e.g. end-of-month balance exhaustion): soft
        # declines spread across methods, banks, and sources stay "customer".
        convert(ok, 0.30, "insufficient_fund")

    else:  # pragma: no cover - guarded by callers
        raise ValueError(f"unknown cause {cause!r}")


def generate_window(
    cause: str, window_start: datetime, rng: np.random.Generator, cfg: SyntheticConfig
) -> tuple[list[Record], list[Record]]:
    """Return (baseline_records, window_records) for one labeled window."""
    baseline = _sample_normal(rng, cfg)
    window = _sample_normal(rng, cfg)
    _inject(cause, window, rng)
    return baseline, window


def generate_dataset(cfg: SyntheticConfig | None = None) -> pd.DataFrame:
    """Generate the full labeled feature dataset (one row per window).

    Columns: FEATURE_NAMES + label, window_id, window_start, window_end.
    Windows are chronologically interleaved across causes (round-robin).
    """
    cfg = cfg or SyntheticConfig()
    rng = np.random.default_rng(cfg.seed)
    rows: list[dict[str, Any]] = []
    step = timedelta(hours=3)
    t = cfg.start
    for i in range(cfg.windows_per_class):
        for cause in CAUSES:
            w_start = t
            w_end = t + timedelta(hours=cfg.window_hours)
            baseline, window = generate_window(cause, w_start, rng, cfg)
            feats = compute_features(window, baseline)
            rows.append(
                {
                    **feats,
                    "label": cause,
                    "window_id": f"syn_{cfg.seed}_{i}_{cause}",
                    "window_start": w_start,
                    "window_end": w_end,
                }
            )
            t += step
    return pd.DataFrame(rows)


__all__ = ["SyntheticConfig", "generate_window", "generate_dataset", "BANKS", "METHOD_MIX"]
