"""sql_validator node: AST-validate SQL via sqlglot."""
from __future__ import annotations

import structlog
import sqlglot
from sqlglot import exp

from autotext2sql.agent.state import AgentState

logger = structlog.get_logger(__name__)

_BLOCKED_NODE_TYPES = tuple(
    node_type
    for node_type in (
        exp.Insert,
        exp.Update,
        exp.Delete,
        exp.Drop,
        exp.Create,
        getattr(exp, "AlterTable", None),  # Older sqlglot
        getattr(exp, "Alter", None),  # Newer sqlglot
        exp.Command,
    )
    if node_type is not None
)


def sql_validator(state: AgentState) -> AgentState:
    sql = (state.get("generated_sql") or "").strip()

    if not sql:
        result = {"valid": False, "reason": "No SQL was generated."}
        return {**state, "sql_validation_result": result}

    try:
        statements = sqlglot.parse(sql)
    except Exception as exc:
        result = {"valid": False, "reason": f"SQL parse error: {exc}"}
        logger.warning("sql_validator_parse_error", error=str(exc))
        return {**state, "sql_validation_result": result}

    if not statements:
        result = {"valid": False, "reason": "Empty SQL."}
        return {**state, "sql_validation_result": result}

    for stmt in statements:
        if stmt is None:
            continue
        stmt_type = type(stmt).__name__
        if not isinstance(stmt, (exp.Select, exp.With)):
            result = {"valid": False, "reason": f"Disallowed SQL statement type: {stmt_type}"}
            logger.warning("sql_validator_disallowed_type", stmt_type=stmt_type)
            return {**state, "sql_validation_result": result}

        # Walk AST for blocked node types
        for node in stmt.walk():
            if isinstance(node, _BLOCKED_NODE_TYPES):
                result = {
                    "valid": False,
                    "reason": f"Disallowed SQL operation: {type(node).__name__}",
                }
                logger.warning("sql_validator_blocked_node", node=type(node).__name__)
                return {**state, "sql_validation_result": result}

    result = {"valid": True, "reason": "OK"}
    logger.info("sql_validator_passed")
    return {**state, "sql_validation_result": result}
