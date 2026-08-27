"""Failure taxonomy and payment-method reference data for the simulator.

The failure telemetry mirrors Razorpay's documented error model
(``error_code`` / ``error_source`` / ``error_step`` / ``error_reason``) — see
docs/research.md ("Failure telemetry", "Deterministic failure simulation").
Razorpay's own enums are inconsistent across docs pages, so the mapping below
is a documented, defensible choice rather than a strict copy:

- ``error_source`` ∈ {customer, bank, gateway}  (issuer ≈ bank)
- ``error_step``   ∈ {payment_authentication, payment_authorization}
- ``error_code``   BAD_REQUEST_ERROR for customer/bank declines,
                   GATEWAY_ERROR for infrastructure failures.

``upi_timeout`` is kept as a distinct reason (instead of Razorpay's generic
UPI ``payment_timed_out`` trigger) so the diagnosis engine can tell UPI collect
timeouts apart from card 3DS timeouts; docs/simulator.md notes the mapping.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class FailureSpec:
    reason: str
    error_code: str
    error_source: str  # customer | bank | gateway
    error_step: str  # payment_authentication | payment_authorization
    description: str
    # soft decline: worth retrying / recoverable by PulseRecover interventions
    recoverable: bool


FAILURES: dict[str, FailureSpec] = {
    # --- customer-side -------------------------------------------------------
    "insufficient_fund": FailureSpec(
        "insufficient_fund", "BAD_REQUEST_ERROR", "customer", "payment_authorization",
        "The transaction was declined due to insufficient funds in the customer's account.",
        recoverable=True,
    ),
    "incorrect_otp": FailureSpec(
        "incorrect_otp", "BAD_REQUEST_ERROR", "customer", "payment_authentication",
        "The OTP entered by the customer was incorrect.",
        recoverable=True,
    ),
    "authentication_failed": FailureSpec(
        "authentication_failed", "BAD_REQUEST_ERROR", "customer", "payment_authentication",
        "3D Secure authentication failed for the transaction.",
        recoverable=True,
    ),
    "incorrect_pin": FailureSpec(
        "incorrect_pin", "BAD_REQUEST_ERROR", "customer", "payment_authentication",
        "The UPI PIN entered by the customer was incorrect.",
        recoverable=True,
    ),
    "pin_attempts_exceeded": FailureSpec(
        "pin_attempts_exceeded", "BAD_REQUEST_ERROR", "customer", "payment_authentication",
        "Maximum UPI PIN attempts exceeded for the customer.",
        recoverable=False,
    ),
    "payment_cancelled": FailureSpec(
        "payment_cancelled", "BAD_REQUEST_ERROR", "customer", "payment_authentication",
        "The customer cancelled the payment before completion.",
        recoverable=True,
    ),
    "card_number_invalid": FailureSpec(
        "card_number_invalid", "BAD_REQUEST_ERROR", "customer", "payment_authorization",
        "The card number provided is invalid.",
        recoverable=False,
    ),
    # --- bank / issuer-side --------------------------------------------------
    "card_declined": FailureSpec(
        "card_declined", "BAD_REQUEST_ERROR", "bank", "payment_authorization",
        "The card was declined by the issuing bank.",
        recoverable=True,
    ),
    "card_disabled_for_online_payments": FailureSpec(
        "card_disabled_for_online_payments", "BAD_REQUEST_ERROR", "bank", "payment_authorization",
        "The card is disabled for online transactions by the issuer.",
        recoverable=False,
    ),
    "transaction_limit_exceeded": FailureSpec(
        "transaction_limit_exceeded", "BAD_REQUEST_ERROR", "bank", "payment_authorization",
        "The transaction exceeds the limit set on the customer's instrument.",
        recoverable=True,
    ),
    "debit_instrument_blocked": FailureSpec(
        "debit_instrument_blocked", "BAD_REQUEST_ERROR", "bank", "payment_authorization",
        "The debit instrument is blocked by the issuing bank.",
        recoverable=False,
    ),
    "payment_declined": FailureSpec(
        "payment_declined", "BAD_REQUEST_ERROR", "bank", "payment_authorization",
        "The payment was declined by the bank.",
        recoverable=True,
    ),
    "bank_technical_error": FailureSpec(
        "bank_technical_error", "GATEWAY_ERROR", "bank", "payment_authorization",
        "The bank's systems returned a technical error.",
        recoverable=True,
    ),
    "bank_downtime": FailureSpec(
        "bank_downtime", "GATEWAY_ERROR", "bank", "payment_authorization",
        "The bank is currently experiencing downtime.",
        recoverable=True,
    ),
    # --- gateway / infrastructure --------------------------------------------
    "gateway_technical_error": FailureSpec(
        "gateway_technical_error", "GATEWAY_ERROR", "gateway", "payment_authorization",
        "A technical error occurred at the payment gateway.",
        recoverable=True,
    ),
    "payment_timed_out": FailureSpec(
        "payment_timed_out", "BAD_REQUEST_ERROR", "gateway", "payment_authorization",
        "The payment timed out before authorization completed.",
        recoverable=True,
    ),
    "upi_timeout": FailureSpec(
        "upi_timeout", "BAD_REQUEST_ERROR", "gateway", "payment_authorization",
        "The UPI collect request timed out waiting for customer approval.",
        recoverable=True,
    ),
    "duplicate_request": FailureSpec(
        "duplicate_request", "BAD_REQUEST_ERROR", "gateway", "payment_authorization",
        "A duplicate request was detected for the transaction.",
        recoverable=False,
    ),
}

# Natural (no-incident) failure-reason mix per method. Weights sum to 1.
METHOD_FAILURE_WEIGHTS: dict[str, tuple[tuple[str, float], ...]] = {
    "card": (
        ("card_declined", 0.30),
        ("insufficient_fund", 0.28),
        ("authentication_failed", 0.12),
        ("incorrect_otp", 0.08),
        ("payment_timed_out", 0.10),
        ("card_disabled_for_online_payments", 0.04),
        ("card_number_invalid", 0.03),
        ("gateway_technical_error", 0.05),
    ),
    "upi": (
        ("upi_timeout", 0.30),
        ("payment_declined", 0.18),
        ("insufficient_fund", 0.18),
        ("incorrect_pin", 0.10),
        ("bank_technical_error", 0.12),
        ("transaction_limit_exceeded", 0.06),
        ("pin_attempts_exceeded", 0.04),
        ("debit_instrument_blocked", 0.02),
    ),
    "netbanking": (
        ("bank_technical_error", 0.35),
        ("payment_cancelled", 0.25),
        ("payment_timed_out", 0.15),
        ("insufficient_fund", 0.10),
        ("bank_downtime", 0.10),
        ("payment_declined", 0.05),
    ),
    "wallet": (
        ("insufficient_fund", 0.45),
        ("payment_cancelled", 0.30),
        ("gateway_technical_error", 0.15),
        ("transaction_limit_exceeded", 0.10),
    ),
}

# Method mix (Indian e-commerce-ish): (method, weight).
METHOD_MIX: tuple[tuple[str, float], ...] = (
    ("upi", 0.46),
    ("card", 0.33),
    ("netbanking", 0.13),
    ("wallet", 0.08),
)

# Baseline success probability per method (natural, no incidents).
BASE_SUCCESS_RATE: dict[str, float] = {
    "card": 0.86,
    "upi": 0.84,
    "netbanking": 0.80,
    "wallet": 0.92,
}

# Acquirer / issuing bank codes (Razorpay-style) with portfolio weights.
BANKS: tuple[tuple[str, float], ...] = (
    ("SBIN", 0.28),
    ("HDFC", 0.24),
    ("ICIC", 0.20),
    ("UTIB", 0.12),  # Axis
    ("KKBK", 0.09),
    ("PUNB", 0.07),
)

CARD_NETWORKS: tuple[tuple[str, float], ...] = (
    ("Visa", 0.45),
    ("MasterCard", 0.35),
    ("RuPay", 0.18),
    ("Amex", 0.02),
)

CARD_TYPES: tuple[tuple[str, float], ...] = (
    ("debit", 0.60),
    ("credit", 0.35),
    ("prepaid", 0.05),
)

UPI_FLOWS: tuple[tuple[str, float], ...] = (
    ("intent", 0.55),
    ("collect", 0.40),
    ("in_app", 0.05),
)

WALLETS: tuple[tuple[str, float], ...] = (
    ("paytm", 0.45),
    ("phonepe", 0.30),
    ("amazonpay", 0.20),
    ("mobikwik", 0.05),
)

# Gateway routes (Razorpay Optimizer-style); incidents can target one route.
ROUTES: tuple[tuple[str, float], ...] = (
    ("pg_primary", 0.80),
    ("pg_secondary", 0.20),
)

METHODS: tuple[str, ...] = tuple(m for m, _ in METHOD_MIX)
