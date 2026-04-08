"""response_generator node: produce the natural-language answer."""
from __future__ import annotations

import asyncio

import structlog

from autotext2sql.agent.prompts import RESPONSE_GENERATOR_PROMPT
from autotext2sql.agent.state import AgentState
from autotext2sql.context import _render_schema_context
from autotext2sql.tools.llm_gateway import LLMGateway, LLMRequest

logger = structlog.get_logger(__name__)


def make_response_generator(llm: LLMGateway):
    def response_generator(state: AgentState) -> AgentState:
        query = state.get("user_query", "")
        objects = state.get("retrieved_objects", [])
        step_costs: list[float] = list(state.get("step_costs", []))

        schema_context = _render_schema_context(objects)
        prompt = RESPONSE_GENERATOR_PROMPT.format(query=query, schema_context=schema_context)

        request = LLMRequest(
            messages=[
                {"role": "system", "content": "You are a helpful database metadata assistant."},
                {"role": "user", "content": prompt},
            ],
            route="respond",
            task_type="respond",
            temperature=0.0,
            max_tokens=1500,
        )

        response_text = ""
        for attempt in range(3):
            try:
                result = llm.call(request)
                if result.success and result.data:
                    step_costs.append(result.data.cost_usd or 0.0)
                    response_text = result.data.content
                break
            except Exception as exc:
                logger.warning("response_generator_attempt_failed", attempt=attempt, error=str(exc))

        if not response_text:
            response_text = "I encountered an error generating the response. Please retry."

        logger.info("response_generator_done", length=len(response_text))
        return {
            **state,
            "response_text": response_text,
            "step_costs": step_costs,
            "total_cost": sum(step_costs),
        }

    return response_generator
