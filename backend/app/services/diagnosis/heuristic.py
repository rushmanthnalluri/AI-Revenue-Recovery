"""Rule-based fallback diagnosis.

Used when no trained artifact exists yet (cold start before the first training
run, or a missing/corrupt artifact). Deterministic, threshold-based, and
conservative: confidences are capped well below what a validated model would
claim, and the emitted pseudo-probability is explicitly *not* calibrated —
it spreads the remaining mass uniformly over the other classes so downstream
top-3 displays keep working.

Rule order matters: the most distinctive, least ambiguous signatures are
checked first (subscription, abandonment, method, bank), the broad
infrastructure signatures later. Thresholds were chosen against the synthetic
generator's regimes and are deliberately strict (better `no_fault` than a
confident wrong cause from rules).
"""

from typing import Any

from app.services.diagnosis.taxonomy import CAUSES, CauseLabel

HEURISTIC_VERSION = "heuristic-1"

#: Confidence caps per rule — rules never claim model-grade confidence.
_CONF = {"strong": 0.70, "medium": 0.60, "weak": 0.45}


def heuristic_diagnose(features: dict[str, float]) -> dict[str, Any]:
    """Return {label, confidence, reasons, proba} from hand rules."""
    f = features
    fired: list[str] = []

    def check(name: str, condition: bool) -> bool:
        if condition:
            fired.append(name)
        return condition

    label = CauseLabel.NO_FAULT.value
    confidence = _CONF["weak"]

    if check(
        "subscription_failure_share_delta>=0.25 and sub_failure_rate_delta>=0.20",
        f["sub_failure_share_delta"] >= 0.25 and f["sub_failure_rate_delta"] >= 0.20,
    ):
        label, confidence = CauseLabel.SUBSCRIPTION_FAILURE_SPIKE.value, _CONF["strong"]

    elif check(
        "abandonment_rate_delta>=0.15",
        f["abandonment_rate_delta"] >= 0.15,
    ):
        label, confidence = CauseLabel.ABANDONMENT_SPIKE.value, _CONF["strong"]

    elif check(
        "max_method_rate_delta>=0.50 and top_method_fail_share>=0.50",
        f["max_method_rate_delta"] >= 0.50 and f["top_method_fail_share"] >= 0.50,
    ):
        label, confidence = CauseLabel.METHOD_OUTAGE.value, _CONF["strong"]

    elif check(
        "bank-source failure share>=0.50 and bank_technical_error share>=0.40",
        f["src_fail_share_w_bank"] >= 0.50 and f["reason_share_w_bank_technical_error"] >= 0.40,
    ):
        label, confidence = CauseLabel.BANK_DOWNTIME.value, _CONF["strong"]

    elif check(
        "insufficient_fund share>=0.40 and delta>=0.20",
        f["reason_share_w_insufficient_fund"] >= 0.40
        and f["reason_share_delta_insufficient_fund"] >= 0.20,
    ):
        label, confidence = CauseLabel.CUSTOMER_INSUFFICIENT_FUNDS_WAVE.value, _CONF["medium"]

    elif check(
        "latency p90 doubled with coverage>=0.5",
        f["latency_p90_delta_ratio"] >= 1.0 and f["latency_coverage"] >= 0.5,
    ):
        label, confidence = CauseLabel.ROUTE_LATENCY.value, _CONF["medium"]

    elif check(
        "gateway-source failure share>=0.40 or gateway_technical_error share>=0.30",
        f["src_fail_share_w_gateway"] >= 0.40
        or f["reason_share_w_gateway_technical_error"] >= 0.30,
    ):
        label, confidence = CauseLabel.GATEWAY_DEGRADATION.value, _CONF["medium"]

    elif check("failure_rate_delta<=0.05", f["failure_rate_delta"] <= 0.05):
        label, confidence = CauseLabel.NO_FAULT.value, _CONF["medium"]
        fired.append("no signature crossed threshold")

    else:
        fired.append("no rule fired; ambiguous signature -> no_fault (weak)")

    # Pseudo-probability: confidence on the chosen label, the rest uniform.
    # NOT calibrated — consumers should treat heuristic confidences as
    # advisory only (they feed nothing but display; policy gates actions).
    rest = (1.0 - confidence) / (len(CAUSES) - 1)
    proba = {c: (confidence if c == label else rest) for c in CAUSES}

    return {"label": label, "confidence": float(confidence), "reasons": fired, "proba": proba}


__all__ = ["HEURISTIC_VERSION", "heuristic_diagnose"]
