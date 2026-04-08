"""FastAPI serving layer: REST endpoints + SSE streaming."""
from __future__ import annotations

import asyncio
import json
import re
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import structlog
import tiktoken
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from autotext2sql.agent.state import (
    AgentState,
    ApprovalResponse,
    DiscoverRequest,
    DiscoverResponse,
    FinalResponse,
    IndexRebuildRequest,
    QueryRequest,
)
from autotext2sql.config import Settings, get_settings
from autotext2sql.observability import setup as setup_observability
from autotext2sql.tools.database import DatabaseTool
from autotext2sql.tools.llm_gateway import LLMGateway, LLMRequest
from autotext2sql.tools.memory_store import BaseMemoryStore, MemoryRecord, NoopMemoryStore, build_memory_store

logger = structlog.get_logger(__name__)
_enc = tiktoken.get_encoding("cl100k_base")

# Legacy graph is kept only for /query/stream compatibility. The main /query
# endpoint uses the simpler Text2SQL flow below.
_graph: Any = None
_ui_enabled = False
_schema_cache: dict[str, dict[str, Any]] = {}
_pending_queries: dict[str, dict[str, Any]] = {}
_memory_store: BaseMemoryStore = NoopMemoryStore()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    setup_observability(
        level=settings.observability.log_level,
        fmt=settings.observability.log_format,
        otlp_endpoint=settings.observability.otlp_endpoint,
    )
    logger.info("app_startup_complete")
    yield
    logger.info("app_shutdown")


app = FastAPI(
    title="AutoText2SQL",
    description="Agentic Text-to-SQL system with metadata search",
    version="0.1.0",
    lifespan=lifespan,
)


# CORS
def _add_cors(application: FastAPI, settings: Settings) -> None:
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.api.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )


@app.on_event("startup")
async def _configure_cors() -> None:
    global _memory_store
    settings = get_settings()
    _add_cors(app, settings)
    _memory_store = build_memory_store(settings)


def _get_memory_store() -> BaseMemoryStore:
    global _memory_store
    if isinstance(_memory_store, NoopMemoryStore) and get_settings().memory.enabled:
        _memory_store = build_memory_store(get_settings())
    return _memory_store


# ---------------------------------------------------------------------------
# Root redirect to UI
# ---------------------------------------------------------------------------


@app.get("/")
async def root() -> RedirectResponse:
    return RedirectResponse(url="/ui" if _ui_enabled else "/docs")


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@app.get("/health")
async def health() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


@app.get("/metrics")
async def metrics() -> StreamingResponse:
    return StreamingResponse(
        iter([generate_latest()]),
        media_type=CONTENT_TYPE_LATEST,
    )


# ---------------------------------------------------------------------------
# Query endpoint (SSE streaming)
# ---------------------------------------------------------------------------


def _build_initial_state(request: QueryRequest) -> AgentState:
    session_id = request.session_id or str(uuid.uuid4())
    return AgentState(
        user_query=request.query,
        session_id=session_id,
        db_hint=request.db_hint,
        db_url=request.db_url,
        db_urls=request.db_urls or {},
        selected_databases=request.selected_databases,
        messages=[],
        step_costs=[],
        total_cost=0.0,
        extracted_entities=[],
        retrieved_objects=[],
        relevance_scores=[],
    )


_SQL_FENCE_RE = re.compile(r"```(?:sql)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)


def _select_database(request: QueryRequest) -> tuple[str, str, int, int]:
    settings = get_settings()
    if request.db_url:
        return request.db_hint or "adhoc", request.db_url, 10, 1

    db_cfg = settings.db_by_name(request.db_hint) if request.db_hint else None
    if db_cfg is None and settings.databases:
        db_cfg = settings.databases[0]
    if db_cfg is None:
        raise HTTPException(status_code=400, detail="No database configured")
    return db_cfg.name, db_cfg.url, db_cfg.query_timeout_seconds, db_cfg.pool_size


def _introspect_database(db_name: str, db_url: str, query_timeout: int, pool_size: int) -> dict[str, Any]:
    cache_key = f"{db_name}:{db_url}"
    cached = _schema_cache.get(cache_key)
    if cached:
        return cached

    db_tool = DatabaseTool(
        db_name=db_name,
        url=db_url,
        query_timeout=query_timeout,
        pool_size=pool_size,
    )
    try:
        result = db_tool.introspect()
        if not result.success or result.data is None:
            raise RuntimeError(result.error or "Database introspection failed")
        _schema_cache[cache_key] = result.data
        return result.data
    finally:
        db_tool.dispose()


def _render_schema_for_prompt(introspection: dict[str, Any]) -> str:
    lines: list[str] = []
    for schema in introspection.get("schemas", []):
        schema_name = schema.get("name", "")
        for table in schema.get("tables", []):
            table_name = table.get("name", "")
            columns = table.get("columns", [])
            rendered_columns = ", ".join(
                f"{c.get('name')} {c.get('type')}"
                for c in columns
            )
            table_line = f"{schema_name}.{table_name}({rendered_columns})"
            if description := table.get("description"):
                table_line += f" -- {description}"
            lines.append(table_line)
            fks = table.get("foreign_keys", [])
            for fk in fks:
                target_schema = fk.get("target_schema") or schema_name
                lines.append(
                    f"FK: {schema_name}.{table_name}.{fk.get('column')} -> "
                    f"{target_schema}.{fk.get('target_table')}.{fk.get('target_column')}"
                )
    return "\n".join(lines).strip()


def _count_tokens(text: str) -> int:
    return len(_enc.encode(text))


def _sanitize_messages(messages: list[dict[str, str]] | None) -> list[dict[str, str]]:
    sanitized: list[dict[str, str]] = []
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "").strip().lower()
        content = str(message.get("content") or "").strip()
        if role not in {"user", "assistant"} or not content:
            continue
        sanitized.append({"role": role, "content": content})
    return sanitized


def _trim_history_messages(
    messages: list[dict[str, str]],
    current_query: str,
    max_tokens: int,
) -> list[dict[str, str]]:
    history = _sanitize_messages(messages)
    if history and history[-1]["role"] == "user" and history[-1]["content"].strip() == current_query.strip():
        history = history[:-1]

    trimmed: list[dict[str, str]] = []
    used_tokens = 0
    for message in reversed(history):
        content = message["content"]
        tokens = _count_tokens(content)
        if used_tokens + tokens > max_tokens:
            break
        trimmed.insert(0, message)
        used_tokens += tokens
    return trimmed


def _trim_memory_records(records: list[MemoryRecord], max_tokens: int) -> list[MemoryRecord]:
    trimmed: list[MemoryRecord] = []
    used_tokens = 0
    for record in records:
        tokens = _count_tokens(record.text)
        if used_tokens + tokens > max_tokens:
            break
        trimmed.append(record)
        used_tokens += tokens
    return trimmed


def _render_history_for_prompt(messages: list[dict[str, str]]) -> str:
    return "\n".join(f"{m['role'].upper()}: {m['content']}" for m in messages)


def _render_memory_for_prompt(records: list[MemoryRecord]) -> str:
    return "\n".join(f"- {record.text}" for record in records)


def _build_decision_prompt(
    *,
    question: str,
    schema_text: str,
    history_messages: list[dict[str, str]],
    memory_records: list[MemoryRecord],
    retry_guidance: str | None = None,
    force_sql: bool = False,
) -> str:
    history_text = _render_history_for_prompt(history_messages)
    memory_text = _render_memory_for_prompt(memory_records)

    sections = [
        "You are a database assistant. Decide whether the user needs a direct textual answer about",
        "the database schema or a SQL query to retrieve data.",
        "",
        "Database schema:",
        schema_text or "(schema unavailable)",
    ]

    if memory_text:
        sections.extend(
            [
                "",
                "Relevant long-term memory about this user or prior work:",
                memory_text,
                "Use this as advisory context only. The current user request and database schema always take priority.",
            ]
        )

    if history_text:
        sections.extend(
            [
                "",
                "Recent conversation history:",
                history_text,
            ]
        )

    if retry_guidance:
        sections.extend(
            [
                "",
                "Previous SQL attempt failed and must be corrected:",
                retry_guidance,
            ]
        )

    sections.extend(
        [
            "",
            "Rules:",
            "- Return ONLY valid JSON. No markdown.",
            '- If the user asks what the database contains, what tables/columns exist, how tables relate,',
            '  or any other schema/documentation question, return:',
            '  {"mode":"answer","answer":"...","sql":null}',
            '- If answering requires reading rows, counts, aggregates, filtering, ordering, top-N, latest,',
            '  or any computation over data, return:',
            '  {"mode":"sql","answer":"","sql":"SELECT ..."}',
            "- SQL must be exactly one PostgreSQL read-only SELECT or WITH query.",
            "- Use schema.table names exactly as listed.",
            "- Never use INSERT, UPDATE, DELETE, DROP, CREATE, ALTER, TRUNCATE.",
            "- Add LIMIT 100 unless the user explicitly asks for a smaller limit.",
            "- If the user asks for an aggregate over the latest/top-N rows, first select those rows in a subquery,",
            "  then aggregate in the outer query. Example pattern:",
            "  SELECT SUM(x) FROM (SELECT x FROM schema.table ORDER BY created_at DESC LIMIT 10) AS t",
            '- When retrying after an SQL error, return mode="sql" with a corrected query.',
            "",
            f"Current user question: {question}",
            "JSON:",
        ]
    )
    if force_sql:
        insert_at = sections.index("JSON:")
        sections.insert(insert_at, '- You must return {"mode":"sql","answer":"","sql":"SELECT ..."} for this retry.')
    return "\n".join(sections)


def _extract_sql(text: str) -> str:
    match = _SQL_FENCE_RE.search(text)
    if match:
        text = match.group(1)
    else:
        match = re.search(r"((?:WITH|SELECT)\s.+)", text, re.IGNORECASE | re.DOTALL)
        if match:
            text = match.group(1)
    return text.strip().rstrip(";").strip()


def _validate_readonly_sql(sql: str) -> str | None:
    import sqlglot
    from sqlglot import exp

    try:
        statements = sqlglot.parse(sql)
    except Exception as exc:
        return f"SQL parse error: {exc}"
    if len(statements) != 1 or statements[0] is None:
        return "Expected exactly one SQL statement"
    statement = statements[0]
    if not isinstance(statement, (exp.Select, exp.With)):
        return f"Only SELECT/WITH queries are allowed, got {type(statement).__name__}"
    blocked = (exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create, exp.Command)
    for node in statement.walk():
        if isinstance(node, blocked):
            return f"Disallowed SQL operation: {type(node).__name__}"
    return None


def _parse_llm_decision(content: str) -> dict[str, Any]:
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*", "", content)
        content = re.sub(r"\s*```$", "", content)
    match = re.search(r"\{.*\}", content, re.DOTALL)
    if match:
        content = match.group(0)
    parsed = json.loads(content)
    if not isinstance(parsed, dict):
        raise ValueError("LLM response must be a JSON object")
    mode = parsed.get("mode")
    if mode not in {"answer", "sql"}:
        raise ValueError("LLM response mode must be 'answer' or 'sql'")
    return parsed


def _ask_llm(
    question: str,
    schema_text: str,
    history_messages: list[dict[str, str]],
    memory_records: list[MemoryRecord],
    retry_guidance: str | None = None,
    force_sql: bool = False,
) -> tuple[dict[str, Any], float]:
    settings = get_settings()
    gateway = LLMGateway(
        base_url=settings.llm_gateway_base_url or settings.llm.gateway_url,
        api_key=settings.llm_gateway_api_key,
        default_model=settings.llm.default_route,
        route_overrides=settings.llm.route_overrides,
        timeout=min(settings.llm.timeout_seconds, 20),
        max_retries=0,
        backoff_base=settings.orchestrator.backoff_base_seconds,
    )
    try:
        history_budget = min(settings.context.max_history_tokens, 2500)
        history_messages = _trim_history_messages(history_messages, question, history_budget)
        memory_budget = max(0, settings.memory.max_prompt_tokens)
        memory_records = _trim_memory_records(memory_records, memory_budget)
        prompt = _build_decision_prompt(
            question=question,
            schema_text=schema_text,
            history_messages=history_messages,
            memory_records=memory_records,
            retry_guidance=retry_guidance,
            force_sql=force_sql,
        )
        result = gateway.call(
            LLMRequest(
                messages=[
                    {"role": "system", "content": "You are a precise Text2SQL compiler."},
                    {"role": "user", "content": prompt},
                ],
                route="text2sql",
                task_type="text2sql",
                temperature=0.0,
                max_tokens=1000,
                response_format={"type": "json_object"},
            )
        )
        if not result.success or not result.data:
            raise RuntimeError(result.error or "LLM response generation failed")
        decision = result.data.parsed
        if not isinstance(decision, dict):
            decision = _parse_llm_decision(result.data.content)
        return decision, result.data.cost_usd or 0.0
    finally:
        gateway.close()


def _build_sql_retry_guidance(failed_sql: str, error_message: str) -> str:
    sql_text = failed_sql.strip() or "<no SQL returned>"
    error_text = error_message.strip() or "Unknown SQL error"
    return (
        f"Previous SQL:\n{sql_text}\n\n"
        f"Database/validation error:\n{error_text}\n\n"
        "Return a corrected read-only PostgreSQL SELECT query that fixes this error."
    )


def _retry_generate_sql(
    *,
    question: str,
    schema_text: str,
    history_messages: list[dict[str, str]],
    memory_records: list[MemoryRecord],
    failed_sql: str,
    error_message: str,
    max_retries: int,
) -> tuple[str | None, float, str | None]:
    total_cost = 0.0
    retry_guidance = _build_sql_retry_guidance(failed_sql, error_message)
    last_error = error_message
    last_sql = failed_sql

    for _ in range(max_retries):
        decision, cost = _ask_llm(
            question,
            schema_text,
            history_messages,
            memory_records,
            retry_guidance=retry_guidance,
            force_sql=True,
        )
        total_cost += cost

        candidate_sql = _extract_sql(str(decision.get("sql") or ""))
        if not candidate_sql:
            last_error = "Retry generation did not return SQL"
            retry_guidance = _build_sql_retry_guidance(last_sql, last_error)
            continue

        validation_error = _validate_readonly_sql(candidate_sql)
        if validation_error:
            last_sql = candidate_sql
            last_error = validation_error
            retry_guidance = _build_sql_retry_guidance(candidate_sql, validation_error)
            continue

        return candidate_sql, total_cost, None

    return None, total_cost, last_error


def _persist_memory_turn(
    *,
    user_id: str,
    session_id: str,
    query: str,
    db_name: str,
    answer: str = "",
    generated_sql: str | None = None,
    sql_result: dict[str, Any] | None = None,
    error: str | None = None,
    clarification_message: str | None = None,
) -> None:
    settings = get_settings()
    if not settings.memory.enabled or not settings.memory.write_enabled:
        return
    if not user_id.strip() or not query.strip() or error:
        return

    assistant_summary = ""
    if clarification_message:
        assistant_summary = clarification_message
    elif answer:
        assistant_summary = answer
    elif generated_sql and sql_result and not error:
        assistant_summary = (
            f"The assistant generated and executed a read-only SQL query against the "
            f"'{db_name}' database to answer the user's request."
        )

    if not assistant_summary:
        return

    _get_memory_store().store_turn(
        user_id=user_id,
        session_id=session_id,
        messages=[
            {"role": "user", "content": query},
            {"role": "assistant", "content": assistant_summary},
        ],
        metadata={
            "source": "autotext2sql_ui",
            "db_name": db_name,
            "contains_sql": bool(generated_sql),
        },
    )


def _make_db_tool(db_name: str, db_url: str, query_timeout: int, pool_size: int = 1) -> DatabaseTool:
    return DatabaseTool(
        db_name=db_name,
        url=db_url,
        query_timeout=query_timeout,
        pool_size=pool_size,
    )


async def _stream_graph(request: QueryRequest) -> AsyncIterator[str]:
    """Run the graph and stream events as SSE."""
    state = _build_initial_state(request)
    config = {"configurable": {"thread_id": state["session_id"]}}
    import concurrent.futures

    loop = asyncio.get_running_loop()

    def _collect_events():
        events = []
        for event in _graph.stream(state, config=config, stream_mode="values"):
            events.append(event)
        return events

    try:
        events = await loop.run_in_executor(None, _collect_events)
        for event in events:
            yield f"data: {json.dumps({'type': 'state_update', 'state': _safe_state(event)})}\n\n"

        # Check if interrupted (human approval needed)
        snapshot = _graph.get_state(config)
        if snapshot.next:
            sql = snapshot.values.get("generated_sql", "")
            objects = snapshot.values.get("retrieved_objects", [])
            target_db = objects[0].get("db_name", "unknown") if objects else "unknown"
            approval_req = {
                "type": "approval_request",
                "session_id": state["session_id"],
                "sql": sql,
                "target_db": target_db,
                "explanation": f"Execute the SQL query against '{target_db}'?",
            }
            yield f"data: {json.dumps(approval_req)}\n\n"
        else:
            final = snapshot.values
            response = FinalResponse(
                session_id=state["session_id"],
                answer=final.get("response_text", ""),
                retrieved_objects=final.get("retrieved_objects", []),
                generated_sql=final.get("generated_sql"),
                sql_result=final.get("sql_execution_result"),
                requires_clarification=final.get("requires_clarification", False),
                clarification_message=final.get("clarification_message"),
                total_cost_usd=final.get("total_cost", 0.0),
                error=final.get("error") or None,
            )
            yield f"data: {json.dumps({'type': 'final', 'data': response.model_dump()})}\n\n"
    except Exception as exc:
        logger.error("graph_stream_error", error=str(exc))
        yield f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n"
    yield "data: [DONE]\n\n"


def _safe_state(state: dict) -> dict:
    """Return a JSON-serialisable subset of state."""
    return {
        "session_id": state.get("session_id", ""),
        "step": "in_progress",
        "total_cost": state.get("total_cost", 0.0),
    }


@app.post("/query/stream")
async def query_stream(request: QueryRequest) -> StreamingResponse:
    """Submit a natural-language query and receive SSE stream of events."""
    if _graph is None:
        raise HTTPException(status_code=503, detail="Graph not initialised")
    return StreamingResponse(
        _stream_graph(request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _invoke_graph_sync(state, config):
    """Run graph.invoke in the current thread (must NOT be the async event-loop thread)."""
    return _graph.invoke(state, config=config)


@app.post("/query", response_model=FinalResponse)
async def query(request: QueryRequest) -> FinalResponse:
    """Submit a question using the simplified Text2SQL flow."""
    session_id = request.session_id or str(uuid.uuid4())
    user_id = request.user_id or session_id
    settings = get_settings()

    db_name, db_url, query_timeout, pool_size = _select_database(request)
    loop = asyncio.get_running_loop()

    try:
        introspection = await loop.run_in_executor(
            None,
            _introspect_database,
            db_name,
            db_url,
            query_timeout,
            pool_size,
        )
    except Exception as exc:
        logger.error("schema_introspection_failed", error=str(exc))
        raise HTTPException(status_code=500, detail=str(exc))

    schema_text = _render_schema_for_prompt(introspection)
    history_messages = _sanitize_messages(request.messages)
    memory_records: list[MemoryRecord] = []
    if user_id:
        memory_records = await loop.run_in_executor(
            None,
            lambda: _get_memory_store().search(request.query, user_id),
        )
    try:
        decision, cost = await loop.run_in_executor(
            None,
            _ask_llm,
            request.query,
            schema_text,
            history_messages,
            memory_records,
        )
    except Exception as exc:
        logger.error("llm_decision_failed", error=str(exc))
        raise HTTPException(status_code=500, detail=str(exc))

    if decision.get("mode") == "answer":
        response = FinalResponse(
            session_id=session_id,
            answer=str(decision.get("answer") or ""),
            total_cost_usd=cost,
        )
        await loop.run_in_executor(
            None,
            lambda: _persist_memory_turn(
                user_id=user_id,
                session_id=session_id,
                query=request.query,
                db_name=db_name,
                answer=response.answer,
            ),
        )
        return response

    generated_sql = _extract_sql(str(decision.get("sql") or ""))
    generation_error = ""
    if not generated_sql:
        generation_error = "LLM selected SQL mode but did not return SQL"
    else:
        generation_error = _validate_readonly_sql(generated_sql) or ""

    if generation_error:
        retried_sql, retry_cost, final_retry_error = await loop.run_in_executor(
            None,
            lambda: _retry_generate_sql(
                question=request.query,
                schema_text=schema_text,
                history_messages=history_messages,
                memory_records=memory_records,
                failed_sql=generated_sql,
                error_message=generation_error,
                max_retries=settings.orchestrator.max_retries_per_node,
            ),
        )
        cost += retry_cost
        if not retried_sql:
            return FinalResponse(
                session_id=session_id,
                answer=str(decision.get("answer") or ""),
                generated_sql=generated_sql or None,
                total_cost_usd=cost,
                error=final_retry_error or generation_error,
            )
        generated_sql = retried_sql

    _pending_queries[session_id] = {
        "db_name": db_name,
        "db_url": db_url,
        "query_timeout": query_timeout,
        "sql": generated_sql,
        "question": request.query,
        "cost": cost,
        "user_id": user_id,
        "history_messages": history_messages,
    }

    return FinalResponse(
        session_id=session_id,
        answer="",
        generated_sql=generated_sql,
        total_cost_usd=cost,
    )


@app.post("/query/approve")
async def approve_sql(response: ApprovalResponse) -> FinalResponse:
    """Execute a previously generated SQL query after user approval."""
    pending = _pending_queries.pop(response.session_id, None)
    if not pending:
        raise HTTPException(status_code=404, detail="No pending SQL query for this session_id")
    settings = get_settings()

    sql = pending["sql"]
    if not response.approved:
        return FinalResponse(
            session_id=response.session_id,
            answer="SQL execution rejected.",
            generated_sql=sql,
            total_cost_usd=pending.get("cost", 0.0),
        )

    validation_error = _validate_readonly_sql(sql)
    if validation_error:
        return FinalResponse(
            session_id=response.session_id,
            answer="",
            generated_sql=sql,
            total_cost_usd=pending.get("cost", 0.0),
            error=validation_error,
        )

    loop = asyncio.get_running_loop()

    def _execute() -> dict[str, Any]:
        db_tool = _make_db_tool(
            pending["db_name"],
            pending["db_url"],
            pending["query_timeout"],
        )
        try:
            result = db_tool.execute(sql)
            if result.success and result.data is not None:
                return result.data
            return {"error": result.error or "SQL execution failed"}
        finally:
            db_tool.dispose()

    total_cost = float(pending.get("cost", 0.0) or 0.0)
    history_messages = _sanitize_messages(pending.get("history_messages", []))
    memory_records: list[MemoryRecord] = []
    user_id = str(pending.get("user_id") or "")
    if user_id:
        memory_records = await loop.run_in_executor(
            None,
            lambda: _get_memory_store().search(str(pending.get("question") or ""), user_id),
        )

    result = await loop.run_in_executor(None, _execute)
    error = result.get("error") if isinstance(result, dict) else None

    for _ in range(settings.orchestrator.max_retries_per_node):
        if not error:
            break

        introspection = await loop.run_in_executor(
            None,
            _introspect_database,
            pending["db_name"],
            pending["db_url"],
            pending["query_timeout"],
            1,
        )
        schema_text = _render_schema_for_prompt(introspection)
        retried_sql, retry_cost, retry_error = await loop.run_in_executor(
            None,
            lambda: _retry_generate_sql(
                question=str(pending.get("question") or ""),
                schema_text=schema_text,
                history_messages=history_messages,
                memory_records=memory_records,
                failed_sql=sql,
                error_message=error,
                max_retries=1,
            ),
        )
        total_cost += retry_cost
        if not retried_sql:
            error = retry_error or error
            break

        sql = retried_sql
        validation_error = _validate_readonly_sql(sql)
        if validation_error:
            error = validation_error
            continue

        result = await loop.run_in_executor(None, _execute)
        error = result.get("error") if isinstance(result, dict) else None

    final_response = FinalResponse(
        session_id=response.session_id,
        answer="",
        generated_sql=sql,
        sql_result=result,
        total_cost_usd=total_cost,
        error=error,
    )
    await loop.run_in_executor(
        None,
        lambda: _persist_memory_turn(
            user_id=user_id,
            session_id=response.session_id,
            query=str(pending.get("question") or ""),
            db_name=str(pending.get("db_name") or ""),
            generated_sql=sql,
            sql_result=result if isinstance(result, dict) else None,
            error=error,
        ),
    )
    return final_response


@app.post("/server/discover", response_model=DiscoverResponse)
async def discover_server_databases(request: DiscoverRequest) -> DiscoverResponse:
    from autotext2sql.tools.database import discover_database_urls

    try:
        db_urls = discover_database_urls(request.db_url)
    except Exception as exc:
        logger.error("server_discovery_failed", error=str(exc))
        raise HTTPException(status_code=400, detail=str(exc))

    return DiscoverResponse(databases=sorted(db_urls), db_urls=db_urls)


@app.post("/index/rebuild")
async def rebuild_index(request: IndexRebuildRequest = None) -> dict[str, Any]:
    """Trigger a full metadata index rebuild.

    If request.db_urls is provided, indexes those databases directly.
    Otherwise falls back to databases configured in settings.
    """
    settings = get_settings()
    from autotext2sql.tools.database import DatabaseTool
    from autotext2sql.tools.embedding import EmbeddingTool
    from autotext2sql.tools.vector_store import VectorStoreTool
    from autotext2sql.retriever.indexer import Indexer

    from autotext2sql.tools.database import discover_database_urls

    db_urls: dict[str, str] = {}

    if request and request.db_urls:
        db_urls = request.db_urls
    elif request and request.db_url:
        try:
            db_urls = discover_database_urls(request.db_url)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Failed to discover databases: {exc}")
    else:
        db_urls = {db.name: db.url for db in settings.databases}

    if not db_urls:
        raise HTTPException(
            status_code=400,
            detail="No databases found. Enter a connection string in the UI.",
        )

    db_tools = [
        DatabaseTool(db_name=name, url=url, query_timeout=10, pool_size=1)
        for name, url in db_urls.items()
    ]

    embedding_tool = EmbeddingTool(
        model_name=settings.llm.embedding_model,
        base_url=settings.llm_gateway_base_url or settings.llm.gateway_url,
        api_key=settings.llm_gateway_api_key,
        timeout=settings.llm.timeout_seconds,
    )
    vector_store = VectorStoreTool(settings.retriever.qdrant_url, settings.retriever.qdrant_collection)
    indexer = Indexer(db_tools, embedding_tool, vector_store)

    loop = asyncio.get_event_loop()
    stats = await loop.run_in_executor(None, lambda: indexer.run(rebuild=True))
    return stats


# ---------------------------------------------------------------------------
# Gradio UI (mounted at /ui)
# ---------------------------------------------------------------------------

try:
    import gradio as gr
    from autotext2sql.ui import create_ui

    _gradio_app = create_ui()
    app = gr.mount_gradio_app(app, _gradio_app, path="/ui")
    _ui_enabled = True
except Exception as exc:
    logger.warning("gradio_ui_mount_failed", error=str(exc))


def serve() -> None:
    import uvicorn
    from autotext2sql.config import get_settings

    s = get_settings()
    uvicorn.run("autotext2sql.api:app", host=s.api.host, port=s.api.port, reload=False)


if __name__ == "__main__":
    serve()
