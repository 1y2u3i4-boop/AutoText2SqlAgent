"""sql_generator node: produce a read-only SQL query."""
from __future__ import annotations

import asyncio
import re

import structlog

from autotext2sql.agent.prompts import SQL_GENERATOR_PROMPT
from autotext2sql.agent.state import AgentState
from autotext2sql.context import _render_schema_context
from autotext2sql.tools.llm_gateway import LLMGateway, LLMRequest

logger = structlog.get_logger(__name__)

_SQL_FENCE_RE = re.compile(r"```(?:sql)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)


def _normalize_table_references(sql: str, objects: list[dict]) -> str:
    normalized = sql.strip().rstrip(";").strip()
    for obj in objects:
        db_name = obj.get("db_name")
        schema_name = obj.get("schema_name")
        table_name = obj.get("table_name")
        if not db_name or not schema_name or not table_name:
            continue
        pattern = re.compile(
            rf'(?<![\w"])("?){re.escape(db_name)}\1\.\s*("?){re.escape(schema_name)}\2\.\s*("?){re.escape(table_name)}\3',
            re.IGNORECASE,
        )
        normalized = pattern.sub(f"{schema_name}.{table_name}", normalized)
    return normalized


def _extract_sql(text: str) -> str:
    m = _SQL_FENCE_RE.search(text)
    if m:
        return m.group(1).strip()
    # Try to find first SELECT statement
    match = re.search(r"((?:WITH|SELECT)\s.+)", text, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return text.strip()


def make_sql_generator(llm: LLMGateway):
    def sql_generator(state: AgentState) -> AgentState:
        query = state.get("user_query", "")
        objects = state.get("retrieved_objects", [])
        step_costs: list[float] = list(state.get("step_costs", []))

        schema_context = _render_schema_context(objects)
        prompt = SQL_GENERATOR_PROMPT.format(query=query, schema_context=schema_context)

        request = LLMRequest(
            messages=[
                {
                    "role": "system",
                    "content": "You are an expert SQL developer. Return only valid SQL.",
                },
                {"role": "user", "content": prompt},
            ],
            route="sql_generate",
            task_type="sql_generate",
            temperature=0.0,
            max_tokens=800,
        )

        generated_sql = ""
        for attempt in range(2):
            try:
                result = llm.call(request)
                if result.success and result.data:
                    step_costs.append(result.data.cost_usd or 0.0)
                    generated_sql = _normalize_table_references(
                        _extract_sql(result.data.content),
                        objects,
                    )
                    if generated_sql:
                        break
            except Exception as exc:
                logger.warning("sql_generator_attempt_failed", attempt=attempt, error=str(exc))

        logger.info("sql_generator_done", sql_length=len(generated_sql))
        return {
            **state,
            "generated_sql": generated_sql,
            "step_costs": step_costs,
            "total_cost": sum(step_costs),
        }

    return sql_generator
