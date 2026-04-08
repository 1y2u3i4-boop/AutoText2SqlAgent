"""relevance_check node: decide whether results are good enough."""
from __future__ import annotations

import structlog

from autotext2sql.agent.state import AgentState

logger = structlog.get_logger(__name__)


def make_relevance_check(confidence_threshold: float = 0.6):
    def relevance_check(state: AgentState) -> AgentState:
        scores = state.get("relevance_scores", [])
        objects = state.get("retrieved_objects", [])

        if not objects:
            logger.info("relevance_check_no_results")
            return {
                **state,
                "requires_clarification": True,
                "clarification_message": (
                    "I couldn't find relevant tables for your query. "
                    "Could you provide more details or mention the database/table name?"
                ),
            }

        max_score = max(scores) if scores else 0.0
        if max_score < confidence_threshold:
            logger.info("relevance_check_low_confidence", max_score=max_score)
            return {
                **state,
                "requires_clarification": True,
                "clarification_message": (
                    f"The best match has a low relevance score ({max_score:.2f}). "
                    "Could you clarify what you are looking for?"
                ),
            }

        logger.info("relevance_check_passed", max_score=max_score)
        return {**state, "requires_clarification": False, "clarification_message": ""}

    return relevance_check
