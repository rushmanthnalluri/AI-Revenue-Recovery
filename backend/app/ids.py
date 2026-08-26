"""Prefixed, human-greppable id helpers.

Every entity id is `<prefix><uuid4hex>`, e.g. `inc_9f2c...`. Prefixes make logs,
audit trails, and support conversations self-describing. Keep this module in
sync with app.models — every model's id default comes from here.
"""

import uuid


def new_id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex}"


def merchant_id() -> str: return new_id("mch_")
def customer_id() -> str: return new_id("cus_")
def order_id() -> str: return new_id("ord_")
def payment_id() -> str: return new_id("pay_")
def payment_event_id() -> str: return new_id("evt_")
def subscription_id() -> str: return new_id("sub_")
def incident_id() -> str: return new_id("inc_")
def evidence_id() -> str: return new_id("evd_")
def diagnosis_id() -> str: return new_id("dia_")
def opportunity_id() -> str: return new_id("opp_")
def strategy_id() -> str: return new_id("str_")
def action_id() -> str: return new_id("act_")
def policy_decision_id() -> str: return new_id("pol_")
def audit_id() -> str: return new_id("aud_")
def webhook_event_id() -> str: return new_id("whk_")
def experiment_id() -> str: return new_id("exp_")
def prediction_id() -> str: return new_id("prd_")
def evaluation_run_id() -> str: return new_id("run_")
def simulator_run_id() -> str: return new_id("sim_")
def ground_truth_id() -> str: return new_id("sgt_")
def agent_report_id() -> str: return new_id("agt_")
