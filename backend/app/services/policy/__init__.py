"""PulseRecover deterministic policy engine (ADR 0003).

Public surface:

    from app.services.policy import PolicyEngine, load_policy_config, audit

    config = load_policy_config()                  # strict; PolicyConfigError on bad file
    engine = PolicyEngine(config, session=db)      # or PolicyEngine.from_file(session=db)
    decision = engine.evaluate(action_context)     # total + deterministic

    audit.record(db, actor=..., action=..., entity_type=..., entity_id=..., ...)

`audit` is a small GENERIC helper over the append-only `audit_logs` table,
intended for every module that needs an audit trail — not just this package.
"""

from app.services.policy import audit
from app.services.policy.config import (
    PolicyConfig,
    PolicyConfigError,
    failsafe_config,
    load_policy_config,
)
from app.services.policy.engine import (
    META_CURRENT_ACTION_ID,
    META_IRREVERSIBLE,
    META_REQUEST_ID,
    META_STRATEGY_ID,
    PolicyEngine,
)
from app.services.policy.history import PolicyHistory, SqlPolicyHistory

__all__ = [
    "META_CURRENT_ACTION_ID",
    "META_IRREVERSIBLE",
    "META_REQUEST_ID",
    "META_STRATEGY_ID",
    "PolicyConfig",
    "PolicyConfigError",
    "PolicyEngine",
    "PolicyHistory",
    "SqlPolicyHistory",
    "audit",
    "failsafe_config",
    "load_policy_config",
]
