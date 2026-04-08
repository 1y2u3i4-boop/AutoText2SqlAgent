"""input_guard node: validate and sanitize user input."""
from __future__ import annotations

import re

import structlog

from autotext2sql.agent.state import AgentState

logger = structlog.get_logger(__name__)

_MAX_QUERY_LEN = 2000

# Patterns that indicate SQL injection or prompt injection attempts
_INJECTION_PATTERNS = re.compile(
    r"\b(drop|delete|update|insert|alter|exec|execute|truncate|xp_|--)\b",
    re.IGNORECASE,
)


def input_guard(state: AgentState) -> AgentState:
    query = (state.get("user_query") or "").strip()

    if not query:
        logger.warning("input_guard_empty_query")
        return {**state, "error": "Query must not be empty."}

    if len(query) > _MAX_QUERY_LEN:
        logger.warning("input_guard_query_too_long", length=len(query))
        return {**state, "error": f"Query exceeds maximum length of {_MAX_QUERY_LEN} characters."}

    if _INJECTION_PATTERNS.search(query):
        logger.warning("input_guard_injection_detected", query=query[:100])
        return {**state, "error": "Query contains disallowed keywords. Please rephrase."}

    logger.info("input_guard_passed", query_length=len(query))
    return {**state, "error": ""}
