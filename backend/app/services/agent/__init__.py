"""AI investigation agent (ADR 0004): probabilistic, advisory only.

Public surface:

    from app.services.agent import AgentService, AgentTools, HeuristicReasoner, LlmReasoner

    service = AgentService(db)
    report_row = service.investigate(incident_id)      # persists agent_reports + audit
    latest = service.latest(incident_id)

Trust boundary: reasoners only reach data through the whitelisted AgentTools;
all financial facts originate from tools; the two request_* tools are the only
mutation path and every proposal is gated by the deterministic PolicyEngine.
Default mode is the deterministic offline HeuristicReasoner; the LLM reasoner
is optional and advisory only.
"""

from app.services.agent.reasoners import (
    HeuristicReasoner,
    LlmError,
    LlmReasoner,
    choose_reasoner,
)
from app.services.agent.service import (
    AgentError,
    AgentService,
    IncidentNotFoundError,
)
from app.services.agent.tools import (
    AGENT_ACTOR,
    AgentTools,
    ToolError,
    ToolNotAllowed,
    ToolResult,
)

__all__ = [
    "AGENT_ACTOR",
    "AgentError",
    "AgentService",
    "AgentTools",
    "HeuristicReasoner",
    "IncidentNotFoundError",
    "LlmError",
    "LlmReasoner",
    "ToolError",
    "ToolNotAllowed",
    "ToolResult",
    "choose_reasoner",
]
