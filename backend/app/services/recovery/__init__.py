"""Recovery execution engine — the closed loop's write side.

Public surface:

    from app.services.recovery import (
        OpportunityBuilder, StrategyGenerator, RecoveryExecutor,
    )

    builder = OpportunityBuilder(db)
    result = builder.build_for_incident(incident_id)     # idempotent

    planner = StrategyGenerator(db)
    strategies = planner.generate(opportunity_id)        # find-or-create

    executor = RecoveryExecutor(db, gateway)             # policy gate built in
    action = executor.execute(opportunity_id, actor="human:console")
    action = executor.approve(opportunity_id, actor="human:ops", note="...")
    action = executor.resolve(action_id, actor="system:poller")

Every financial action passes the deterministic policy gate before any gateway
call; every state transition lands in audit_logs. See docs/recovery.md.
"""

from app.services.recovery.builder import BuildResult, OpportunityBuilder
from app.services.recovery.executor import (
    CANCELLABLE_STATES,
    IN_FLIGHT_STATES,
    OPEN_STATES,
    TERMINAL_STATES,
    GatewayNotConfiguredError,
    InvalidStateError,
    RecoveryError,
    RecoveryExecutor,
    RecoveryNotFoundError,
)
from app.services.recovery.reconcile import ReconcileReport, run_reconciliation
from app.services.recovery.strategies import (
    DELAY_SECONDS,
    DIAGNOSIS_FREE_EVIDENCE,
    StrategyGenerator,
)

__all__ = [
    "BuildResult",
    "CANCELLABLE_STATES",
    "DELAY_SECONDS",
    "DIAGNOSIS_FREE_EVIDENCE",
    "GatewayNotConfiguredError",
    "IN_FLIGHT_STATES",
    "InvalidStateError",
    "OPEN_STATES",
    "OpportunityBuilder",
    "ReconcileReport",
    "RecoveryError",
    "RecoveryExecutor",
    "RecoveryNotFoundError",
    "StrategyGenerator",
    "TERMINAL_STATES",
    "run_reconciliation",
]
