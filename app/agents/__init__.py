"""The six investigation agents. Each one runs as an orchestration-engine task handler.

Start with ``OrchestratorAgent(llm, tools).investigate(incident)``. See docs/ARCHITECTURE.md.
"""

from app.agents.critic import CriticAgent
from app.agents.dependency import DependencyAgent
from app.agents.observability import ObservabilityAgent
from app.agents.orchestrator import AgentConfig, InvestigationResult, OrchestratorAgent, build_orchestrator
from app.agents.reasoning import ReasoningAgent
from app.agents.reporting import ReportingAgent

__all__ = [
    "AgentConfig",
    "CriticAgent",
    "DependencyAgent",
    "InvestigationResult",
    "ObservabilityAgent",
    "OrchestratorAgent",
    "ReasoningAgent",
    "ReportingAgent",
    "build_orchestrator",
]
