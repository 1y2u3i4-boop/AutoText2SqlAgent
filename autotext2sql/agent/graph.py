"""LangGraph SearchAgentGraph — wires all nodes into the state graph."""
from __future__ import annotations

import os
from typing import Any

import structlog
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, StateGraph
from langgraph.types import Command

from autotext2sql.agent.state import AgentState
from autotext2sql.agent.nodes.input_guard import input_guard
from autotext2sql.agent.nodes.query_analyzer import make_query_analyzer
from autotext2sql.agent.nodes.retriever_node import make_retriever_node
from autotext2sql.agent.nodes.relevance_check import make_relevance_check
from autotext2sql.agent.nodes.response_generator import make_response_generator
from autotext2sql.agent.nodes.sql_generator import make_sql_generator
from autotext2sql.agent.nodes.sql_validator import sql_validator
from autotext2sql.agent.nodes.human_approval import human_approval
from autotext2sql.agent.nodes.sql_executor import make_sql_executor
from autotext2sql.agent.nodes.output_guard import output_guard
from autotext2sql.config import Settings
from autotext2sql.retriever.search import Retriever
from autotext2sql.tools.database import DatabaseTool
from autotext2sql.tools.embedding import EmbeddingTool
from autotext2sql.tools.llm_gateway import LLMGateway
from autotext2sql.tools.vector_store import VectorStoreTool

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Routing functions
# ---------------------------------------------------------------------------


def route_after_input_guard(state: AgentState) -> str:
    return "reject" if state.get("error") else "pass"


def route_after_relevance(state: AgentState) -> str:
    return "insufficient" if state.get("requires_clarification") else "sufficient"


def route_after_sql_validation(state: AgentState) -> str:
    validation = state.get("sql_validation_result", {})
    return "pass" if validation.get("valid") else "reject"


def route_after_approval(state: AgentState) -> str:
    return "approved" if state.get("human_approved") else "rejected"


def route_after_cost_check(state: AgentState) -> str:
    return "exceeded" if state.get("error", "").startswith("Cost") else "ok"


# ---------------------------------------------------------------------------
# Cost controller (inline, post-LLM-node check)
# ---------------------------------------------------------------------------


def make_cost_controller(per_task_limit: float):
    def cost_controller(state: AgentState) -> AgentState:
        total = state.get("total_cost", 0.0)
        if total > per_task_limit:
            logger.warning("cost_controller_limit_exceeded", total=total, limit=per_task_limit)
            return {
                **state,
                "error": f"Cost limit exceeded: ${total:.4f} > ${per_task_limit:.2f}",
            }
        return state

    return cost_controller


# ---------------------------------------------------------------------------
# Graph factory
# ---------------------------------------------------------------------------


def build_graph(settings: Settings) -> Any:
    """Build and compile the LangGraph SearchAgentGraph."""
    # Instantiate tools
    llm = LLMGateway(
        base_url=settings.llm_gateway_base_url or settings.llm.gateway_url,
        api_key=settings.llm_gateway_api_key,
        default_model=settings.llm.default_route,
        route_overrides=settings.llm.route_overrides,
        timeout=settings.llm.timeout_seconds,
        max_retries=settings.orchestrator.max_retries_per_node,
        backoff_base=settings.orchestrator.backoff_base_seconds,
    )

    embedding_tool = EmbeddingTool(
        model_name=settings.llm.embedding_model,
        base_url=settings.llm_gateway_base_url or settings.llm.gateway_url,
        api_key=settings.llm_gateway_api_key,
        timeout=settings.llm.timeout_seconds,
    )

    vector_store = VectorStoreTool(
        url=settings.retriever.qdrant_url,
        collection=settings.retriever.qdrant_collection,
    )

    retriever = Retriever(
        embedding_tool=embedding_tool,
        vector_store=vector_store,
        llm_gateway=llm if settings.retriever.rerank_enabled else None,
        top_k=settings.retriever.top_k,
        top_n=settings.retriever.top_n,
        confidence_threshold=settings.retriever.confidence_threshold,
        rerank_enabled=settings.retriever.rerank_enabled,
        enrichment_enabled=settings.retriever.enrichment_enabled,
    )

    db_tools: dict[str, DatabaseTool] = {}
    for db_cfg in settings.databases:
        db_tools[db_cfg.name] = DatabaseTool(
            db_name=db_cfg.name,
            url=db_cfg.url,
            query_timeout=db_cfg.query_timeout_seconds,
            pool_size=db_cfg.pool_size,
        )

    # Build node callables
    cost_controller = make_cost_controller(settings.cost.per_task_limit_usd)

    # Wrap LLM nodes with cost check after them
    def _with_cost(node_fn):
        def wrapped(state: AgentState) -> AgentState:
            new_state = node_fn(state)
            new_state = cost_controller(new_state)
            return new_state

        wrapped.__name__ = node_fn.__name__ if hasattr(node_fn, "__name__") else "wrapped"
        return wrapped

    # Graph definition
    graph = StateGraph(AgentState)

    graph.add_node("input_guard", input_guard)
    graph.add_node("query_analyzer", _with_cost(make_query_analyzer(llm)))
    graph.add_node("retriever", _with_cost(make_retriever_node(retriever)))
    graph.add_node("relevance_check", make_relevance_check(settings.retriever.confidence_threshold))
    graph.add_node("response_generator", _with_cost(make_response_generator(llm)))
    graph.add_node("sql_generator", _with_cost(make_sql_generator(llm)))
    graph.add_node("sql_validator", sql_validator)
    graph.add_node("human_approval", human_approval)
    graph.add_node("sql_executor", make_sql_executor(db_tools))
    graph.add_node("output_guard", output_guard)

    # Entry point
    graph.set_entry_point("input_guard")

    # Edges
    graph.add_conditional_edges(
        "input_guard",
        route_after_input_guard,
        {"reject": END, "pass": "query_analyzer"},
    )
    graph.add_edge("query_analyzer", "retriever")
    graph.add_conditional_edges(
        "retriever",
        route_after_cost_check,
        {"exceeded": END, "ok": "relevance_check"},
    )
    graph.add_conditional_edges(
        "relevance_check",
        route_after_relevance,
        {"insufficient": END, "sufficient": "response_generator"},
    )
    graph.add_conditional_edges(
        "response_generator",
        route_after_cost_check,
        {"exceeded": END, "ok": "sql_generator"},
    )
    graph.add_conditional_edges(
        "sql_generator",
        route_after_cost_check,
        {"exceeded": END, "ok": "sql_validator"},
    )
    graph.add_conditional_edges(
        "sql_validator",
        route_after_sql_validation,
        {"reject": "output_guard", "pass": "human_approval"},
    )
    graph.add_conditional_edges(
        "human_approval",
        route_after_approval,
        {"rejected": "output_guard", "approved": "sql_executor"},
    )
    graph.add_edge("sql_executor", "output_guard")
    graph.add_edge("output_guard", END)

    # Checkpointer – keep context manager reference alive to prevent GC closing it
    os.makedirs(os.path.dirname(settings.orchestrator.checkpointer_db) or ".", exist_ok=True)
    try:
        _cm = SqliteSaver.from_conn_string(settings.orchestrator.checkpointer_db)
        checkpointer = _cm.__enter__() if hasattr(_cm, "__enter__") else _cm
        # Store reference on the checkpointer so GC doesn't collect the CM
        checkpointer._context_manager = _cm
    except Exception:
        logger.warning("sqlite_checkpointer_unavailable_falling_back_to_memory")
        checkpointer = InMemorySaver()

    compiled = graph.compile(checkpointer=checkpointer, interrupt_before=["human_approval"])
    logger.info("graph_compiled")
    return compiled
