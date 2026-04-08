"""query_analyzer node: extract intent and entities from the user query via LLM."""
from __future__ import annotations

import asyncio
import json
import re

import structlog

from autotext2sql.agent.prompts import QUERY_ANALYZER_PROMPT
from autotext2sql.agent.state import AgentState
from autotext2sql.tools.llm_gateway import LLMGateway, LLMRequest

logger = structlog.get_logger(__name__)

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _keyword_fallback(query: str) -> tuple[str, list[str]]:
    words = re.findall(r"\b[A-Za-zА-Яа-я_][A-Za-zА-Яа-я0-9_]{2,}\b", query)
    entities = list(dict.fromkeys(w.lower() for w in words if len(w) > 3))[:10]
    return query, entities


def make_query_analyzer(llm: LLMGateway):
    def query_analyzer(state: AgentState) -> AgentState:
        query = state.get("user_query", "")
        step_costs: list[float] = list(state.get("step_costs", []))

        prompt = QUERY_ANALYZER_PROMPT.format(query=query)
        request = LLMRequest(
            messages=[
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": prompt},
            ],
            route="analyze",
            task_type="analyze",
            temperature=0.0,
            max_tokens=256,
            response_format={"type": "json_object"},
        )

        intent = ""
        entities: list[str] = []
        retries = 0

        for attempt in range(3):
            try:
                result = llm.call(request)
                if result.success and result.data:
                    cost = result.data.cost_usd or 0.0
                    step_costs.append(cost)
                    content = result.data.content
                    m = _JSON_RE.search(content)
                    if m:
                        parsed = json.loads(m.group())
                        intent = parsed.get("intent", query)
                        entities = parsed.get("entities", [])
                    break
                retries = attempt + 1
            except Exception as exc:
                logger.warning("query_analyzer_attempt_failed", attempt=attempt, error=str(exc))
                retries = attempt + 1

        if not intent:
            logger.warning("query_analyzer_fallback")
            intent, entities = _keyword_fallback(query)

        total_cost = sum(step_costs)
        logger.info("query_analyzer_done", intent=intent[:80], entities=entities)
        return {
            **state,
            "parsed_intent": intent,
            "extracted_entities": entities,
            "step_costs": step_costs,
            "total_cost": total_cost,
        }

    return query_analyzer
