"""human_approval node: LangGraph interrupt point for SQL execution approval."""
from __future__ import annotations

import structlog
from langgraph.types import interrupt

from autotext2sql.agent.state import AgentState

logger = structlog.get_logger(__name__)


def human_approval(state: AgentState) -> AgentState:
    sql = state.get("generated_sql", "")
    objects = state.get("retrieved_objects", [])
    target_db = objects[0].get("db_name", "unknown") if objects else "unknown"

    # Interrupt the graph – LangGraph will pause here and resume when the
    # client calls graph.invoke(Command(resume=approved_bool), config=...)
    approved: bool = interrupt(
        {
            "type": "approval_request",
            "sql": sql,
            "target_db": target_db,
            "explanation": f"Execute this SQL against '{target_db}'?",
        }
    )

    logger.info("human_approval_result", approved=approved)
    return {**state, "human_approved": bool(approved)}
