"""AgentState TypedDict and supporting Pydantic models for external contracts."""
from __future__ import annotations

from typing import Any, TypedDict

from pydantic import BaseModel, Field

from autotext2sql.retriever.models import RankedDBObject


# ---------------------------------------------------------------------------
# External-boundary Pydantic models
# ---------------------------------------------------------------------------


class QueryRequest(BaseModel):
    query: str
    session_id: str | None = None
    user_id: str | None = None
    messages: list[dict[str, str]] = Field(default_factory=list)
    db_hint: str | None = None
    db_url: str | None = None
    db_urls: dict[str, str] | None = None
    selected_databases: list[str] | None = None


class DiscoverRequest(BaseModel):
    db_url: str


class IndexRebuildRequest(BaseModel):
    db_url: str | None = None      # raw server connection string from UI
    db_urls: dict[str, str] | None = None  # pre-discovered db_name→url map


class DiscoverResponse(BaseModel):
    databases: list[str] = Field(default_factory=list)
    db_urls: dict[str, str] = Field(default_factory=dict)


class ApprovalResponse(BaseModel):
    session_id: str
    approved: bool


class FinalResponse(BaseModel):
    session_id: str
    answer: str
    retrieved_objects: list[RankedDBObject] = []
    generated_sql: str | None = None
    sql_result: dict[str, Any] | None = None
    requires_clarification: bool = False
    clarification_message: str | None = None
    total_cost_usd: float = 0.0
    error: str | None = None


class ApprovalRequest(BaseModel):
    type: str = "approval_request"
    session_id: str
    sql: str
    target_db: str
    explanation: str


# ---------------------------------------------------------------------------
# LangGraph State TypedDict
# ---------------------------------------------------------------------------


class AgentState(TypedDict, total=False):
    # Inputs
    user_query: str
    session_id: str
    db_hint: str | None
    db_url: str | None
    selected_databases: list[str] | None
    db_urls: dict[str, str]

    # Analysis
    parsed_intent: str
    extracted_entities: list[str]

    # Retrieval
    retrieved_objects: list[dict[str, Any]]  # serialised RankedDBObject dicts
    relevance_scores: list[float]
    enrichment_data: dict[str, Any]
    # Generation
    response_text: str
    generated_sql: str
    sql_validation_result: dict[str, Any]
    sql_execution_result: dict[str, Any]

    # Flow control
    requires_clarification: bool
    clarification_message: str
    requires_human_approval: bool
    human_approved: bool

    # Cost + telemetry
    step_costs: list[float]
    total_cost: float
    error: str

    # History
    messages: list[dict[str, str]]
