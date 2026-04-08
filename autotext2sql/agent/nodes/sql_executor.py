"""sql_executor node: run validated SQL via read-only database connection."""
from __future__ import annotations

import structlog

from autotext2sql.agent.state import AgentState
from autotext2sql.tools.database import DatabaseTool

logger = structlog.get_logger(__name__)


def make_sql_executor(db_tools: dict[str, DatabaseTool]):
    def sql_executor(state: AgentState) -> AgentState:
        sql = state.get("generated_sql", "")
        objects = state.get("retrieved_objects", [])
        target_db = objects[0].get("db_name", "") if objects else ""
        db_url = state.get("db_url")
        db_urls = state.get("db_urls", {})

        tool = db_tools.get(target_db)
        temp_tool: DatabaseTool | None = None
        if db_url:
            target_url = db_urls.get(target_db) or db_url
            temp_tool = DatabaseTool(
                db_name=target_db or "adhoc_db",
                url=target_url,
                query_timeout=10,
                pool_size=1,
            )
            tool = temp_tool
        if not tool:
            logger.warning("sql_executor_no_tool", db=target_db)
            return {
                **state,
                "sql_execution_result": {"error": f"No database connection for '{target_db}'"},
            }

        result = tool.execute(sql)
        if temp_tool is not None:
            temp_tool.dispose()
        if result.success and result.data:
            exec_data = result.data
            logger.info(
                "sql_executor_done",
                rows=exec_data.get("row_count", 0),
                truncated=exec_data.get("truncated", False),
            )
            return {
                **state,
                "response_text": state.get("response_text") or "SQL executed successfully.",
                "sql_execution_result": exec_data,
            }
        else:
            logger.error("sql_executor_failed", error=result.error)
            return {
                **state,
                "response_text": state.get("response_text") or "SQL execution failed.",
                "sql_execution_result": {"error": result.error or "SQL execution failed"},
            }

    return sql_executor
