"""output_guard node: validate final response structure."""
from __future__ import annotations

import structlog
from pydantic import ValidationError

from autotext2sql.agent.state import AgentState
from autotext2sql.agent.state import FinalResponse

logger = structlog.get_logger(__name__)

_MAX_RESPONSE_LEN = 8000


def output_guard(state: AgentState) -> AgentState:
    response_text = state.get("response_text", "")

    if not response_text:
        logger.warning("output_guard_empty_response")
        return {**state, "error": "Empty response from generator."}

    if len(response_text) > _MAX_RESPONSE_LEN:
        response_text = response_text[:_MAX_RESPONSE_LEN] + "\n\n[Response truncated]"
        logger.warning("output_guard_response_truncated")

    # Validate that we can build a FinalResponse (external contract check)
    try:
        FinalResponse(
            session_id=state.get("session_id", ""),
            answer=response_text,
            total_cost_usd=state.get("total_cost", 0.0),
        )
    except ValidationError as exc:
        logger.error("output_guard_validation_error", error=str(exc))
        return {**state, "error": "Response validation failed."}

    logger.info("output_guard_passed")
    return {**state, "response_text": response_text, "error": ""}
