"""Simulation engine: deterministic synthetic payment traffic + injected
incidents, written to the shared commerce models and ``simulator_ground_truth``.

Determinism contract: every random draw comes from one ``random.Random(seed)``
consumed in a fixed code path (customers → subscriptions → day-by-day checkout
payments). Same config ⇒ same aggregate counts, same entity ids, same ground
truth. Entity ids are derived from the run id (``sim_{seed}_{hash}``) instead
of the ``app.ids`` uuid4 helpers — deliberate, so reseeding is idempotent and
ground-truth references are stable across runs.

Volume: ``target_events`` is met by generating ``target_events /
AVG_EVENTS_PER_PAYMENT`` checkout payments; subscription cycles, dunning
retries and checkout retries add a few percent on top (the target is a floor,
not a ceiling).
"""

import random
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.models import (
    Customer,
    Merchant,
    Order,
    Payment,
    PaymentEvent,
    SimulatorGroundTruth,
    SimulatorRun,
    Subscription,
)
from app.simulator.config import IncidentKind, SimulatorConfig
from app.simulator.distributions import (
    DOW_WEIGHTS,
    HOURLY_WEIGHTS_IST,
    NIGHT_SUCCESS_FACTOR,
    WeightedChoice,
    WeightedIndex,
    sample_amount_paise,
    sample_latency_ms,
)
from app.simulator.incidents import (
    ActiveIncident,
    IncidentEffect,
    PayContext,
    effect,
    expected_truth,
    matches,
)
from app.simulator.taxonomy import (
    BANKS,
    BASE_SUCCESS_RATE,
    CARD_NETWORKS,
    CARD_TYPES,
    FAILURES,
    METHOD_FAILURE_WEIGHTS,
    METHOD_MIX,
    ROUTES,
    UPI_FLOWS,
    WALLETS,
)

# Average payment_events produced per checkout payment (measured on the
# standard scenario; see docs/simulator.md). Sizes the payment count from the
# --events target.
AVG_EVENTS_PER_PAYMENT = 2.20

NATURAL_ABANDON_RATE = 0.04  # checkout abandonment background level
LATE_CAPTURE_RATE = 0.01  # Razorpay quirk: payment.failed later captured
CHECKOUT_RETRY_RATE = 0.15  # customer retries once after a failure
CHECKOUT_RETRY_SUCCESS = 0.65
SUB_CYCLE_FAILURE_RATE = 0.12  # involuntary-churn-ish baseline
SUB_RETRY_SUCCESS = (0.55, 0.50, 0.45)  # Razorpay T+1/T+2/T+3 dunning
CUSTOMER_OPT_OUT_RATE = 0.02

# (period, amount_paise, weight)
PLANS: tuple[tuple[str, int, float], ...] = (
    ("monthly", 29_900, 0.35),
    ("monthly", 49_900, 0.25),
    ("monthly", 99_900, 0.20),
    ("weekly", 9_900, 0.20),
)

CITIES: tuple[tuple[str, float], ...] = (
    ("Mumbai", 0.22),
    ("Delhi", 0.20),
    ("Bengaluru", 0.18),
    ("Hyderabad", 0.12),
    ("Pune", 0.10),
    ("Chennai", 0.10),
    ("Kolkata", 0.08),
)

_FLUSH_ORDER = (
    Merchant,
    Customer,
    Subscription,
    Order,
    Payment,
    PaymentEvent,
    SimulatorGroundTruth,
)


class _BulkWriter:
    """Buffers rows per model and bulk-inserts in chunks (parents first).

    Callers add parents before children; a chunk flush always inserts every
    buffered parent row before any child row, so FK order is respected even on
    Postgres. With ``deferred`` set, adds never trigger a flush (used while
    subscription outcomes are precomputed).
    """

    def __init__(self, session: Session, chunk_size: int):
        self.session = session
        self.chunk_size = chunk_size
        self.deferred = False
        self.buffers: dict[type, list[dict]] = {m: [] for m in _FLUSH_ORDER}
        self.rows_written = 0

    def add(self, model: type, row: dict) -> None:
        self.buffers[model].append(row)
        if not self.deferred and len(self.buffers[model]) >= self.chunk_size:
            self.flush()

    def flush(self) -> None:
        for model in _FLUSH_ORDER:
            buf = self.buffers[model]
            if buf:
                self.session.bulk_insert_mappings(model, buf)
                self.rows_written += len(buf)
                buf.clear()
        self.session.commit()


class SimResult:
    def __init__(self, run_id: str, stats: dict[str, Any], skipped: bool = False):
        self.run_id = run_id
        self.stats = stats
        self.skipped = skipped

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"SimResult(run_id={self.run_id!r}, skipped={self.skipped})"


class SimulatorEngine:
    def __init__(self, config: SimulatorConfig, session: Session):
        if config.days < 1:
            raise ValueError("days must be >= 1")
        if config.customers < 10:
            raise ValueError("customers must be >= 10")
        if config.target_events < 1_000:
            raise ValueError("target_events must be >= 1000")
        self.cfg = config
        self.session = session
        self.rng = random.Random(config.seed)
        self.writer = _BulkWriter(session, config.chunk_size)

        end = config.end_date
        if end is None:
            end = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        elif end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        self.end = end
        self.start = end - timedelta(days=config.days)

        self.run_id = config.run_id
        self.merchant_id = f"mch_{self.run_id}"
        # Gateway-facing ids must be unique per RUN, not per seed: two runs
        # sharing a seed (different configs) coexist in one database, and the
        # gateway_* columns are UNIQUE. run_id already carries the config
        # hash, so its suffix is the run discriminator.
        self._gw_tag = self.run_id.rsplit("_", 1)[-1]

        # sequence counters for deterministic entity ids
        self._seq = {"cus": 0, "sub": 0, "ord": 0, "pay": 0, "evt": 0, "sgt": 0}

        # resolved incident windows
        self.incidents = self._resolve_incidents()
        self.effects: dict[int, IncidentEffect] = {
            inc.index: effect(inc) for inc in self.incidents
        }
        self._kind_by_entity = {inc.entity_id: inc.kind for inc in self.incidents}
        self._forced_reason_pickers: dict[int, WeightedChoice] = {
            inc.index: WeightedChoice(self.effects[inc.index].reason_weights)
            for inc in self.incidents
            if self.effects[inc.index].reason_weights
        }
        self._gt_incidents: dict[int, dict[str, Any]] = {
            inc.index: {
                "affected_payment_ids": [],
                "injected_failures": 0,
                "injected_abandonments": 0,
                "latency_affected": 0,
                "affected_amount_paise": 0,
            }
            for inc in self.incidents
        }

        # samplers
        self._method_pick = WeightedChoice(METHOD_MIX)
        self._bank_pick = WeightedChoice(BANKS)
        self._network_pick = WeightedChoice(CARD_NETWORKS)
        self._ctype_pick = WeightedChoice(CARD_TYPES)
        self._flow_pick = WeightedChoice(UPI_FLOWS)
        self._wallet_pick = WeightedChoice(WALLETS)
        self._route_pick = WeightedChoice(ROUTES)
        self._city_pick = WeightedChoice(CITIES)
        self._plan_pick = WeightedChoice([((p, a), w) for p, a, w in PLANS])
        self._hour_pick = WeightedIndex(HOURLY_WEIGHTS_IST)
        self._reason_pick = {m: WeightedChoice(w) for m, w in METHOD_FAILURE_WEIGHTS.items()}

        # customer arrays (parallel)
        self._cust_ids: list[str] = []
        self._cust_reliability: list[float] = []
        self._cust_method: list[str] = []
        self._cust_weight: WeightedIndex | None = None

        # stats
        self._stats: dict[str, Any] = {
            "payments_by_status": {},
            "payments_by_method": {},
            "events_by_type": {},
            "failures_by_reason": {},
        }
        self._captured_amount = 0
        self._failed_amount = 0
        self._method_ok: dict[str, int] = {}
        self._sub_summary = {"active": 0, "halted": 0, "cancelled": 0}

    # ------------------------------------------------------------------
    # ids & helpers
    # ------------------------------------------------------------------

    def _next_id(self, kind: str) -> str:
        self._seq[kind] += 1
        width = {"cus": 5, "sub": 4, "ord": 7, "pay": 7, "evt": 9, "sgt": 6}[kind]
        return f"{kind}_{self.run_id}_{self._seq[kind]:0{width}d}"

    def _resolve_incidents(self) -> list[ActiveIncident]:
        out = []
        for i, spec in enumerate(self.cfg.incidents):
            day = min(round(spec.day_fraction * max(self.cfg.days - 1, 0)), self.cfg.days - 1)
            day_start = self.start + timedelta(days=day)
            s = day_start + timedelta(hours=spec.start_hour_ist - 5.5)  # IST → UTC
            e = s + timedelta(hours=spec.duration_hours)
            out.append(
                ActiveIncident(i, f"inc_{self.run_id}_{i:02d}", spec,
                               max(s, self.start), min(e, self.end))
            )
        return out

    def _bump(self, key: str, name: str) -> None:
        d = self._stats[key]
        d[name] = d.get(name, 0) + 1

    # ------------------------------------------------------------------
    # merchant & customers
    # ------------------------------------------------------------------

    def _gen_merchant(self) -> None:
        self.writer.add(
            Merchant,
            {
                "id": self.merchant_id,
                "name": self.cfg.merchant_name,
                "email": "ops@pulserecover-demo.example.com",
                "gateway_account_id": f"acc_sim{self.cfg.seed:06d}",
                "is_active": True,
                "meta": {"simulator_run_id": self.run_id, "scenario": self.cfg.scenario},
                "created_at": self.start,
                "updated_at": self.start,
            },
        )

    def _gen_customers(self) -> None:
        weights: list[float] = []
        for i in range(self.cfg.customers):
            cid = self._next_id("cus")
            activity = self.rng.lognormvariate(0.0, 0.9)  # heavy repeat buyers
            reliability = min(max(self.rng.gauss(1.0, 0.06), 0.85), 1.08)
            preferred = self._method_pick.pick(self.rng)
            city = self._city_pick.pick(self.rng)
            acquired_day = self.rng.randrange(max(self.cfg.days - 5, 1))
            opted_out = self.rng.random() < CUSTOMER_OPT_OUT_RATE
            phone_suffix = self.rng.randrange(999_999_999)
            self._cust_ids.append(cid)
            self._cust_reliability.append(reliability)
            self._cust_method.append(preferred)
            weights.append(activity)
            self.writer.add(
                Customer,
                {
                    "id": cid,
                    "merchant_id": self.merchant_id,
                    "email": f"user{i}s{self.cfg.seed}@example.com",
                    "phone": f"+91{9_000_000_000 + phone_suffix}",
                    "name": f"Sim User {i}",
                    "gateway_customer_id": f"cust_S{self.cfg.seed}_{self._gw_tag}{i:08d}",
                    "opted_out": opted_out,
                    "meta": {
                        "city": city,
                        "preferred_method": preferred,
                        "acquired_day": acquired_day,
                    },
                    "created_at": self.start + timedelta(days=acquired_day),
                    "updated_at": self.start + timedelta(days=acquired_day),
                },
            )
        self._cust_weight = WeightedIndex(weights)

    # ------------------------------------------------------------------
    # payment attempts
    # ------------------------------------------------------------------

    def _sample_ts(self, day_start: datetime) -> datetime:
        hour = self._hour_pick.pick(self.rng)
        minute = self.rng.randrange(60)
        second = self.rng.randrange(60)
        ts = day_start + timedelta(hours=hour - 5.5, minutes=minute, seconds=second)
        # clamp into the global window (IST hours can spill across UTC days)
        if ts < self.start:
            ts += timedelta(days=1)
        elif ts >= self.end:
            ts -= timedelta(days=1)
        return ts

    def _apply_incidents(
        self,
        ctx: PayContext,
        success: bool,
        abandoned: bool,
        latency_ms: int,
        amount_paise: int,
        payment_id: str,
    ) -> tuple[bool, bool, int, str | None, list[str]]:
        """Apply every matching incident window (config order). Returns
        (success, abandoned, latency_ms, forced_reason, incident_entity_ids)."""
        hit: list[str] = []
        forced_reason: str | None = None
        for inc in self.incidents:
            if not matches(inc, ctx):
                continue
            if abandoned:
                break  # nothing left to degrade; keeps the draw path stable
            eff = self.effects[inc.index]
            changed = False
            if eff.abandon_boost > 0 and self.rng.random() < eff.abandon_boost:
                abandoned = True
                self._gt_incidents[inc.index]["injected_abandonments"] += 1
                changed = True
            elif success and eff.fail_boost > 0 and self.rng.random() < eff.fail_boost:
                success = False
                forced_reason = self._forced_reason_pickers[inc.index].pick(self.rng)
                self._gt_incidents[inc.index]["injected_failures"] += 1
                changed = True
            if eff.latency_multiplier != 1.0:
                latency_ms = min(int(latency_ms * eff.latency_multiplier), 300_000)
                self._gt_incidents[inc.index]["latency_affected"] += 1
                changed = True
            if changed:
                hit.append(inc.entity_id)
                self._gt_incidents[inc.index]["affected_payment_ids"].append(payment_id)
                self._gt_incidents[inc.index]["affected_amount_paise"] += amount_paise
        return success, abandoned, latency_ms, forced_reason, hit

    def _error_payload(self, reason: str | None) -> dict:
        spec = FAILURES[reason or "payment_declined"]
        return {
            "error_code": spec.error_code,
            "error_description": spec.description,
            "error_source": spec.error_source,
            "error_step": spec.error_step,
            "error_reason": spec.reason,
        }

    def _run_attempt(
        self,
        *,
        ts: datetime,
        cust_idx: int,
        method: str,
        amount_paise: int,
        order_id: str,
        is_sub: bool,
        sub_id: str | None,
        is_retry: bool,
        success_p_override: float | None,
    ) -> tuple[str, str | None]:
        """Generate one payment attempt (+ optional checkout retry). Returns
        (first_attempt_status, retry_status_or_None)."""
        rng = self.rng
        route = self._route_pick.pick(rng)
        bank = self._bank_pick.pick(rng)
        network = wallet = flow = card_type = ""
        if method == "card":
            network = self._network_pick.pick(rng)
            card_type = self._ctype_pick.pick(rng)
        elif method == "upi":
            flow = self._flow_pick.pick(rng)
        elif method == "wallet":
            wallet = self._wallet_pick.pick(rng)
        latency_ms = sample_latency_ms(rng, method)

        # natural checkout abandonment (not for subscription auto-charges;
        # retry attempts are deliberate, so they never abandon)
        abandoned = (not is_sub and not is_retry
                     and rng.random() < NATURAL_ABANDON_RATE)

        reliability = self._cust_reliability[cust_idx]
        if success_p_override is not None:
            p_success = success_p_override
        elif is_sub:
            p_success = (1.0 - SUB_CYCLE_FAILURE_RATE) * reliability
        else:
            p_success = BASE_SUCCESS_RATE[method] * reliability
        ist_hour = (ts + timedelta(hours=5, minutes=30)).hour
        if ist_hour < 6:
            p_success *= NIGHT_SUCCESS_FACTOR
        p_success = min(max(p_success, 0.05), 0.995)

        success = True
        reason: str | None = None
        if not abandoned:
            success = rng.random() < p_success
            if not success:
                reason = self._reason_pick[method].pick(rng)
        natural_outcome = "created" if abandoned else ("captured" if success else "failed")

        payment_id = self._next_id("pay")
        ctx = PayContext(ts, method, bank, network, route, card_type, is_sub)
        success, abandoned, latency_ms, forced_reason, hit = self._apply_incidents(
            ctx, success, abandoned, latency_ms, amount_paise, payment_id
        )
        if forced_reason:
            reason = forced_reason

        late_capture = False
        if not abandoned and not success and rng.random() < LATE_CAPTURE_RATE:
            late_capture = True

        if abandoned:
            final_status = "created"
        elif success or late_capture:
            final_status = "captured"
        else:
            final_status = "failed"

        if final_status == "captured":
            self._captured_amount += amount_paise
        elif final_status == "failed":
            self._failed_amount += amount_paise

        err = self._error_payload(reason) if reason and not abandoned else {}
        meta: dict[str, Any] = {"route": route, "bank": bank}
        if network:
            meta["network"] = network
            meta["card_type"] = card_type
        if flow:
            meta["upi_flow"] = flow
        if wallet:
            meta["wallet"] = wallet
        if sub_id:
            meta["subscription_id"] = sub_id
        if err:
            meta["error_reason"] = err["error_reason"]
            meta["error_step"] = err["error_step"]

        events = self._build_events(
            payment_id=payment_id, order_id=order_id, ts=ts, method=method,
            amount_paise=amount_paise, final_status=final_status,
            latency_ms=latency_ms, reason=reason if not abandoned else None,
            sub_id=sub_id, late_capture=late_capture,
        )
        last_ts = events[-1]["occurred_at"]

        self.writer.add(
            Payment,
            {
                "id": payment_id,
                "merchant_id": self.merchant_id,
                "order_id": order_id,
                "customer_id": self._cust_ids[cust_idx],
                "gateway_payment_id": f"pay_S{self.cfg.seed}_{self._gw_tag}{self._seq['pay']:011d}",
                "amount_paise": amount_paise,
                "currency": "INR",
                "method": method,
                "status": final_status,
                "error_code": err.get("error_code"),
                "error_description": err.get("error_description"),
                "error_source": err.get("error_source"),
                "captured": final_status == "captured",
                "attempts": 1,
                "gateway_created_at": ts,
                "meta": meta,
                "created_at": ts,
                "updated_at": last_ts,
            },
        )
        for ev in events:
            self.writer.add(PaymentEvent, ev)
            self._bump("events_by_type", ev["event_type"])

        self._bump("payments_by_status", final_status)
        self._bump("payments_by_method", method)
        if final_status == "captured":
            self._method_ok[method] = self._method_ok.get(method, 0) + 1
        if reason and final_status == "failed":
            self._bump("failures_by_reason", reason)

        if hit:
            now = datetime.now(timezone.utc)
            self.writer.add(
                SimulatorGroundTruth,
                {
                    "id": self._next_id("sgt"),
                    "simulator_run_id": self.run_id,
                    "entity_type": "payment",
                    "entity_id": payment_id,
                    "truth": {
                        "incident_ids": hit,
                        "natural_outcome": natural_outcome,
                        "final_outcome": final_status,
                        "injected": natural_outcome != final_status,
                        "error_reason": reason,
                        "is_subscription": is_sub,
                        "recoverable": bool(reason and FAILURES[reason].recoverable),
                    },
                    "created_at": now,
                    "updated_at": now,
                },
            )

        # customer checkout retry: one follow-up payment on the same order
        retry_status: str | None = None
        if (
            not is_sub
            and not is_retry
            and final_status == "failed"
            and rng.random() < CHECKOUT_RETRY_RATE
        ):
            retry_ts = ts + timedelta(seconds=rng.uniform(300, 3600))
            if retry_ts < self.end:
                retry_status, _ = self._run_attempt(
                    ts=retry_ts, cust_idx=cust_idx, method=method,
                    amount_paise=amount_paise, order_id=order_id, is_sub=False,
                    sub_id=None, is_retry=True,
                    success_p_override=min(CHECKOUT_RETRY_SUCCESS * reliability, 0.995),
                )
        return final_status, retry_status

    def _build_events(
        self,
        *,
        payment_id: str,
        order_id: str,
        ts: datetime,
        method: str,
        amount_paise: int,
        final_status: str,
        latency_ms: int,
        reason: str | None,
        sub_id: str | None,
        late_capture: bool,
    ) -> list[dict]:
        base: dict[str, Any] = {
            "payment_id": payment_id,
            "order_id": order_id,
            "amount_paise": amount_paise,
            "currency": "INR",
            "method": method,
        }
        if sub_id:
            base["subscription_id"] = sub_id

        def mk(event_type: str, from_s: str | None, to_s: str, at: datetime,
               extra: dict | None = None) -> dict:
            payload = dict(base)
            payload["status"] = to_s
            if extra:
                payload.update(extra)
            return {
                "id": self._next_id("evt"),
                "payment_id": payment_id,
                "event_type": event_type,
                "from_status": from_s,
                "to_status": to_s,
                "source": "simulator",
                "payload": payload,
                "occurred_at": at,
                "created_at": at,
                "updated_at": at,
            }

        events = [mk("payment.created", None, "created", ts)]
        if final_status == "created":  # abandoned checkout — never attempted
            return events
        if late_capture:
            fail_at = ts + timedelta(milliseconds=int(latency_ms * 0.8))
            events.append(
                mk("payment.failed", "created", "failed", fail_at,
                   self._error_payload(reason))
            )
            cap_at = fail_at + timedelta(seconds=self.rng.uniform(30, 900))
            events.append(mk("payment.captured", "failed", "captured", cap_at))
        elif final_status == "captured":
            if method == "card":
                auth_at = ts + timedelta(milliseconds=int(latency_ms * 0.6))
                events.append(mk("payment.authorized", "created", "authorized", auth_at))
            cap_at = ts + timedelta(milliseconds=latency_ms)
            events.append(
                mk("payment.captured",
                   "authorized" if method == "card" else "created",
                   "captured", cap_at, {"latency_ms": latency_ms})
            )
        else:  # failed
            fail_at = ts + timedelta(milliseconds=int(latency_ms * 0.8))
            extra = self._error_payload(reason)
            extra["latency_ms"] = int(latency_ms * 0.8)
            events.append(mk("payment.failed", "created", "failed", fail_at, extra))
        return events

    def _extract_attempt_rows(self, marks: dict[type, int]) -> tuple[list, list, list]:
        """Move rows appended to the writer buffers since ``marks`` out into
        (pay_rows, ev_rows, gt_rows) lists — so callers can add the parent
        order row before any child row reaches the buffers."""
        pay_rows: list[dict] = []
        ev_rows: list[dict] = []
        gt_rows: list[dict] = []
        for model, target in ((Payment, pay_rows), (PaymentEvent, ev_rows),
                              (SimulatorGroundTruth, gt_rows)):
            buf = self.writer.buffers[model]
            target.extend(buf[marks[model]:])
            del buf[marks[model]:]
        return pay_rows, ev_rows, gt_rows

    def _gen_checkout(self, day_start: datetime) -> None:
        assert self._cust_weight is not None
        cust_idx = self._cust_weight.pick(self.rng)
        if self.rng.random() < 0.70:
            method = self._cust_method[cust_idx]
        else:
            method = self._method_pick.pick(self.rng)
        amount = sample_amount_paise(self.rng, method)
        ts = self._sample_ts(day_start)

        order_id = self._next_id("ord")
        order_created = ts - timedelta(seconds=2)
        # defer flushing so a chunk flush can never commit payment rows before
        # their parent order row has been buffered
        self.writer.deferred = True
        marks = {m: len(b) for m, b in self.writer.buffers.items()}
        status, retry_status = self._run_attempt(
            ts=ts, cust_idx=cust_idx, method=method, amount_paise=amount,
            order_id=order_id, is_sub=False, sub_id=None, is_retry=False,
            success_p_override=None,
        )
        pay_rows, ev_rows, gt_rows = self._extract_attempt_rows(marks)
        self.writer.deferred = False
        attempts = 1 + (1 if retry_status is not None else 0)
        if status == "captured" or retry_status == "captured":
            order_status = "paid"
        elif status == "created":
            order_status = "created"
        else:
            order_status = "attempted"
        self.writer.add(
            Order,
            {
                "id": order_id,
                "merchant_id": self.merchant_id,
                "customer_id": self._cust_ids[cust_idx],
                "gateway_order_id": f"order_S{self.cfg.seed}_{self._gw_tag}{self._seq['ord']:011d}",
                "amount_paise": amount,
                "currency": "INR",
                "status": order_status,
                "receipt": f"rcpt_{self.cfg.seed}_{self._seq['ord']:09d}",
                "meta": {"channel": "checkout", "attempts": attempts},
                "created_at": order_created,
                "updated_at": order_created,
            },
        )
        for r in pay_rows:
            self.writer.add(Payment, r)
        for r in ev_rows:
            self.writer.add(PaymentEvent, r)
        for r in gt_rows:
            self.writer.add(SimulatorGroundTruth, r)

    # ------------------------------------------------------------------
    # subscriptions (precomputed: outcomes decide the subscription row)
    # ------------------------------------------------------------------

    def _gen_subscriptions(self) -> dict[int, list[tuple]]:
        """Returns day_index -> list of (order_row, pay_rows, ev_rows,
        gt_rows) bundles, flushed when the day loop reaches that day."""
        by_day: dict[int, list[tuple]] = {}
        n_subs = max(30, self.cfg.customers // 20)
        rng = self.rng
        self.writer.deferred = True  # never auto-flush mid-cycle
        try:
            for _ in range(n_subs):
                self._gen_one_subscription(by_day)
        finally:
            self.writer.deferred = False
        return by_day

    def _gen_one_subscription(self, by_day: dict[int, list[tuple]]) -> None:
        rng = self.rng
        sub_id = self._next_id("sub")
        period, plan_amount = self._plan_pick.pick(rng)
        cust_idx = rng.randrange(self.cfg.customers)
        method = "card" if rng.random() < 0.60 else "upi"
        step = 7 if period == "weekly" else 30
        # cycle positions spread across the whole billing period, like a real
        # subscription book (subs are created on different calendar days)
        anchor_day = rng.randrange(min(step, self.cfg.days))
        anchor_hour = 4.0 + rng.random() * 4.0  # early-morning IST auto-charge
        cancelled = rng.random() < 0.03
        cancel_day = (
            anchor_day + rng.randrange(1, max(self.cfg.days - anchor_day, 2))
            if cancelled
            else None
        )

        cycle_days: list[int] = []
        d = anchor_day
        while d < self.cfg.days:
            if cancel_day is not None and d >= cancel_day:
                break
            cycle_days.append(d)
            d += step

        halted = False
        retry_count = 0
        spike_hits: list[str] = []
        last_cycle_day = cycle_days[-1] if cycle_days else anchor_day

        for ci, day in enumerate(cycle_days):
            if halted:
                break  # halted subs are never auto-charged again (Razorpay)
            order_id = self._next_id("ord")

            def attempt_ts(day_offset: int) -> datetime:
                base = self.start + timedelta(days=day + day_offset)
                ts = base + timedelta(hours=anchor_hour - 5.5, minutes=rng.randrange(60))
                return min(max(ts, self.start), self.end - timedelta(seconds=1))

            sink: list[tuple] = []
            final = self._sub_attempt(
                order_id=order_id, ts=attempt_ts(0), cust_idx=cust_idx,
                method=method, amount_paise=plan_amount, sub_id=sub_id,
                sink=sink, spike_hits=spike_hits,
            )
            attempts_used = 0
            if final == "failed":
                for ri, p_ok in enumerate(SUB_RETRY_SUCCESS):
                    if day + ri + 1 >= self.cfg.days:
                        break
                    attempts_used += 1
                    final = self._sub_attempt(
                        order_id=order_id, ts=attempt_ts(ri + 1), cust_idx=cust_idx,
                        method=method, amount_paise=plan_amount, sub_id=sub_id,
                        sink=sink, spike_hits=spike_hits,
                        success_p_override=p_ok * self._cust_reliability[cust_idx],
                    )
                    if final == "captured":
                        break
                if final != "captured":
                    halted = True
            retry_count = attempts_used

            order_row = {
                "id": order_id,
                "merchant_id": self.merchant_id,
                "customer_id": self._cust_ids[cust_idx],
                "gateway_order_id": f"order_S{self.cfg.seed}_{self._gw_tag}{self._seq['ord']:011d}",
                "amount_paise": plan_amount,
                "currency": "INR",
                "status": "paid" if final == "captured" else "attempted",
                "receipt": f"rcpt_sub_{self.cfg.seed}_{self._seq['ord']:08d}",
                "meta": {"channel": "subscription", "subscription_id": sub_id,
                         "cycle": ci, "attempts": attempts_used + 1},
                "created_at": self.start + timedelta(days=day),
                "updated_at": self.start + timedelta(days=day),
            }
            pay_rows: list[dict] = []
            ev_rows: list[dict] = []
            gt_rows: list[dict] = []
            for day_idx, kind, row in sink:
                if kind == "pay":
                    pay_rows.append((day_idx, row))
                elif kind == "evt":
                    ev_rows.append((day_idx, row))
                else:
                    gt_rows.append((day_idx, row))
            # bundle per actual day of each row (retries land on later days).
            # The early-morning IST anchor can stamp an attempt on the day
            # BEFORE its cycle day, so the parent order must ride the earliest
            # bundle day — a child payment may never reach the buffers before
            # its order (Postgres enforces the FK; SQLite does not).
            bundle_days = {day} | {d0 for d0, _ in pay_rows}
            order_day = min(bundle_days)
            for day_idx in bundle_days:
                bundle = (
                    order_row if day_idx == order_day else None,
                    [r for d0, r in pay_rows if d0 == day_idx],
                    [r for d0, r in ev_rows if d0 == day_idx],
                    [r for d0, r in gt_rows if d0 == day_idx],
                )
                by_day.setdefault(day_idx, []).append(bundle)

        if halted:
            status = "halted"
            self._sub_summary["halted"] += 1
        elif cancelled:
            status = "cancelled"
            self._sub_summary["cancelled"] += 1
        else:
            status = "active"
            self._sub_summary["active"] += 1

        period_start = self.start + timedelta(days=last_cycle_day)
        self.writer.add(
            Subscription,
            {
                "id": sub_id,
                "merchant_id": self.merchant_id,
                "customer_id": self._cust_ids[cust_idx],
                "gateway_subscription_id": f"sub_S{self.cfg.seed}_{self._gw_tag}{self._seq['sub']:06d}",
                "plan_id": f"plan_sim_{period}_{plan_amount}",
                "status": status,
                "amount_paise": plan_amount,
                "currency": "INR",
                "period": period,
                "current_period_start": period_start,
                "current_period_end": period_start + timedelta(days=step),
                "retry_count": retry_count,
                "meta": {"payment_method": method, "cycles_in_window": len(cycle_days)},
                "created_at": self.start + timedelta(days=anchor_day),
                "updated_at": period_start,
            },
        )
        if halted and spike_hits:
            now = datetime.now(timezone.utc)
            self.writer.add(
                SimulatorGroundTruth,
                {
                    "id": self._next_id("sgt"),
                    "simulator_run_id": self.run_id,
                    "entity_type": "subscription",
                    "entity_id": sub_id,
                    "truth": {
                        "kind": IncidentKind.SUBSCRIPTION_FAILURE_SPIKE.value,
                        "incident_ids": sorted(set(spike_hits)),
                        "halted": True,
                        "expected_recovery": "dunning_plus_card_update",
                    },
                    "created_at": now,
                    "updated_at": now,
                },
            )

    def _sub_attempt(self, *, order_id: str, ts: datetime, cust_idx: int,
                     method: str, amount_paise: int, sub_id: str,
                     sink: list[tuple], spike_hits: list[str],
                     success_p_override: float | None = None) -> str:
        """One subscription charge attempt; new rows are moved out of the
        writer buffers into ``sink`` as (day_index, kind, row) so the caller
        can bucket them per day (retries land on later days)."""
        day_index = (ts - self.start).days
        marks = {m: len(b) for m, b in self.writer.buffers.items()}
        status, _ = self._run_attempt(
            ts=ts, cust_idx=cust_idx, method=method, amount_paise=amount_paise,
            order_id=order_id, is_sub=True, sub_id=sub_id, is_retry=False,
            success_p_override=success_p_override,
        )
        pay_rows, ev_rows, gt_rows = self._extract_attempt_rows(marks)
        for row in pay_rows:
            sink.append((day_index, "pay", row))
        for row in ev_rows:
            sink.append((day_index, "evt", row))
        for row in gt_rows:
            sink.append((day_index, "sgt", row))
            spike_hits.extend(
                eid
                for eid in row["truth"].get("incident_ids", [])
                if self._kind_by_entity.get(eid)
                is IncidentKind.SUBSCRIPTION_FAILURE_SPIKE
            )
        return status

    # ------------------------------------------------------------------
    # run
    # ------------------------------------------------------------------

    def run(self) -> SimResult:
        t0 = time.perf_counter()
        run_row = SimulatorRun(
            id=self.run_id,
            scenario=self.cfg.scenario,
            seed=self.cfg.seed,
            config=self.cfg.config_dict(),
            status="running",
            started_at=datetime.now(timezone.utc),
            stats={},
        )
        self.session.add(run_row)
        self.session.commit()

        self._gen_merchant()
        self._gen_customers()
        sub_bundles = self._gen_subscriptions()
        self.writer.flush()  # merchant + customers + subscription rows

        n_payments = round(self.cfg.target_events / AVG_EVENTS_PER_PAYMENT)
        base_quota = n_payments / self.cfg.days
        produced = 0
        for day in range(self.cfg.days):
            day_start = self.start + timedelta(days=day)
            dow = (day_start + timedelta(hours=5, minutes=30)).weekday()
            jitter = self.rng.lognormvariate(0.0, 0.06)
            quota = max(0, round(base_quota * DOW_WEIGHTS[dow] * jitter))
            if day == self.cfg.days - 1:
                quota = max(0, n_payments - produced)
            produced += quota

            for bundle in sub_bundles.pop(day, []):
                order_row, pay_rows, ev_rows, gt_rows = bundle
                if order_row:
                    self.writer.add(Order, order_row)
                for r in pay_rows:
                    self.writer.add(Payment, r)
                for r in ev_rows:
                    self.writer.add(PaymentEvent, r)
                for r in gt_rows:
                    self.writer.add(SimulatorGroundTruth, r)

            for _ in range(quota):
                self._gen_checkout(day_start)
        self.writer.flush()

        self._write_incident_ground_truth()
        self.writer.flush()

        runtime_ms = int((time.perf_counter() - t0) * 1000)
        stats = self._final_stats(runtime_ms)
        run_row.status = "completed"
        run_row.finished_at = datetime.now(timezone.utc)
        run_row.stats = stats
        self.session.commit()
        return SimResult(self.run_id, stats)

    def _write_incident_ground_truth(self) -> None:
        now = datetime.now(timezone.utc)
        for inc in self.incidents:
            acc = self._gt_incidents[inc.index]
            truth = expected_truth(inc)
            truth.update(
                {
                    "start": inc.start.isoformat(),
                    "end": inc.end.isoformat(),
                    "affected_payment_ids": acc["affected_payment_ids"],
                    "affected_count": len(acc["affected_payment_ids"]),
                    "injected_failures": acc["injected_failures"],
                    "injected_abandonments": acc["injected_abandonments"],
                    "latency_affected": acc["latency_affected"],
                    "affected_amount_paise": acc["affected_amount_paise"],
                }
            )
            self.writer.add(
                SimulatorGroundTruth,
                {
                    "id": self._next_id("sgt"),
                    "simulator_run_id": self.run_id,
                    "entity_type": "incident",
                    "entity_id": inc.entity_id,
                    "truth": truth,
                    "created_at": now,
                    "updated_at": now,
                },
            )

    def _final_stats(self, runtime_ms: int) -> dict:
        n_pay = self._seq["pay"]
        n_ev = self._seq["evt"]
        rows_total = self.writer.rows_written + 1  # + the simulator_runs row
        success_by_method = {
            m: round(
                self._method_ok.get(m, 0)
                / max(self._stats["payments_by_method"].get(m, 1), 1),
                4,
            )
            for m in sorted(self._stats["payments_by_method"])
        }
        return {
            "window": {"start": self.start.isoformat(), "end": self.end.isoformat()},
            "rows": {
                "merchants": 1,
                "customers": self._seq["cus"],
                "subscriptions": self._seq["sub"],
                "orders": self._seq["ord"],
                "payments": n_pay,
                "payment_events": n_ev,
                "ground_truth": self._seq["sgt"],
                "total": rows_total,
            },
            "events_per_payment": round(n_ev / max(n_pay, 1), 4),
            "payments_by_status": dict(sorted(self._stats["payments_by_status"].items())),
            "payments_by_method": dict(sorted(self._stats["payments_by_method"].items())),
            "success_by_method": success_by_method,
            "events_by_type": dict(sorted(self._stats["events_by_type"].items())),
            "failures_by_reason": dict(sorted(self._stats["failures_by_reason"].items())),
            "captured_amount_paise": self._captured_amount,
            "failed_amount_paise": self._failed_amount,
            "currency": "INR",
            "subscriptions": dict(self._sub_summary),
            "incidents": [
                {
                    "entity_id": inc.entity_id,
                    "kind": inc.kind.value,
                    "start": inc.start.isoformat(),
                    "end": inc.end.isoformat(),
                    "affected_count": len(
                        self._gt_incidents[inc.index]["affected_payment_ids"]
                    ),
                    "injected_failures": self._gt_incidents[inc.index]["injected_failures"],
                }
                for inc in self.incidents
            ],
            "runtime_ms": runtime_ms,
            "rows_per_sec": round(rows_total / max(runtime_ms / 1000.0, 1e-6), 1),
        }


def run_simulation(config: SimulatorConfig, session: Session) -> SimResult:
    """Run a simulation into the given session. Raises ValueError if the
    deterministic run id already exists (use the CLI / seed.py for idempotent
    skip-or-force behavior)."""
    existing = session.get(SimulatorRun, config.run_id)
    if existing is not None:
        raise ValueError(
            f"simulator run {config.run_id} already exists (status={existing.status}); "
            "use --force to delete and regenerate"
        )
    return SimulatorEngine(config, session).run()


def delete_simulator_run(session: Session, run_id: str) -> dict[str, int]:
    """Delete a run and every commerce row it generated (manual cascade —
    works on SQLite without PRAGMA foreign_keys and on Postgres)."""
    run = session.get(SimulatorRun, run_id)
    if run is None:
        return {}
    merchant_id = f"mch_{run_id}"
    counts: dict[str, int] = {}

    pay_ids = sa.select(Payment.id).where(Payment.merchant_id == merchant_id)
    counts["payment_events"] = session.execute(
        sa.delete(PaymentEvent).where(PaymentEvent.payment_id.in_(pay_ids))
    ).rowcount
    counts["payments"] = session.execute(
        sa.delete(Payment).where(Payment.merchant_id == merchant_id)
    ).rowcount
    counts["orders"] = session.execute(
        sa.delete(Order).where(Order.merchant_id == merchant_id)
    ).rowcount
    counts["subscriptions"] = session.execute(
        sa.delete(Subscription).where(Subscription.merchant_id == merchant_id)
    ).rowcount
    counts["customers"] = session.execute(
        sa.delete(Customer).where(Customer.merchant_id == merchant_id)
    ).rowcount
    counts["simulator_ground_truth"] = session.execute(
        sa.delete(SimulatorGroundTruth).where(
            SimulatorGroundTruth.simulator_run_id == run_id
        )
    ).rowcount
    counts["merchants"] = session.execute(
        sa.delete(Merchant).where(Merchant.id == merchant_id)
    ).rowcount
    session.delete(run)
    counts["simulator_runs"] = 1
    session.commit()
    return counts
