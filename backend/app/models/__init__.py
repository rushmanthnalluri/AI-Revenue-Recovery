"""All SQLAlchemy models — import this package to register every table on
`app.db.Base.metadata` (alembic env.py relies on that)."""

from app.models.base import (
    ENVIRONMENT_REAL_TEST,
    ENVIRONMENT_RESEARCH,
    KNOWN_ENVIRONMENTS,
    RAZORPAY_SOURCE_SYSTEM,
    SIMULATOR_SOURCE_SYSTEM,
    SOURCE_TYPE_RAZORPAY_LIVE,
    SOURCE_TYPE_RAZORPAY_TEST,
    SOURCE_TYPE_SIMULATOR,
    EnvironmentMixin,
    ProvenanceMixin,
    source_types_for_environment,
)
from app.models.commerce import (
    Customer,
    Merchant,
    Order,
    Payment,
    PaymentEvent,
    Subscription,
)
from app.models.evaluation import (
    AgentReport,
    EvaluationRun,
    Experiment,
    ModelPrediction,
    SimulatorGroundTruth,
    SimulatorRun,
)
from app.models.incidents import Diagnosis, Incident, IncidentEvidence
from app.models.notification import NotificationOutbox
from app.models.recovery import (
    PolicyDecisionRecord,
    RecoveryAction,
    RecoveryOpportunity,
    RecoveryStrategy,
)
from app.models.system import AuditLog, WebhookEvent
from app.models.sync import CONNECTION_STATE_SINGLETON_ID, ConnectionState, SyncRun

__all__ = [
    "Merchant",
    "Customer",
    "Order",
    "Payment",
    "PaymentEvent",
    "Subscription",
    "Incident",
    "IncidentEvidence",
    "Diagnosis",
    "RecoveryOpportunity",
    "RecoveryStrategy",
    "RecoveryAction",
    "PolicyDecisionRecord",
    "NotificationOutbox",
    "AuditLog",
    "WebhookEvent",
    "Experiment",
    "ModelPrediction",
    "EvaluationRun",
    "SimulatorRun",
    "SimulatorGroundTruth",
    "AgentReport",
    "SyncRun",
    "ConnectionState",
    "CONNECTION_STATE_SINGLETON_ID",
    "ProvenanceMixin",
    "EnvironmentMixin",
    "SOURCE_TYPE_SIMULATOR",
    "SOURCE_TYPE_RAZORPAY_TEST",
    "SOURCE_TYPE_RAZORPAY_LIVE",
    "SIMULATOR_SOURCE_SYSTEM",
    "RAZORPAY_SOURCE_SYSTEM",
    "ENVIRONMENT_REAL_TEST",
    "ENVIRONMENT_RESEARCH",
    "KNOWN_ENVIRONMENTS",
    "source_types_for_environment",
]
