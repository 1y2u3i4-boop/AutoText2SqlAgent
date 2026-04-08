"""Context builder: assemble and budget LLM context from agent state."""
from __future__ import annotations

from typing import Any

import tiktoken

from autotext2sql.agent.prompts import SYSTEM_PROMPT
from autotext2sql.config import ContextConfig

_enc = tiktoken.get_encoding("cl100k_base")


def _count(text: str) -> int:
    return len(_enc.encode(text))


def _render_schema_context(objects: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for obj in objects:
        lines.append(
            f"[{obj.get('db_name', '')}.{obj.get('schema_name', '')}.{obj.get('table_name', '')}]"
        )
        cols = obj.get("columns", [])
        if cols:
            col_str = ", ".join(
                f"{c['name']} ({c['type']})" for c in cols[:30]
            )
            lines.append(f"  Columns: {col_str}")
        fks = obj.get("foreign_keys", [])
        for fk in fks:
            lines.append(
                f"  FK: {fk['column']} -> {fk['target_table']}.{fk['target_column']}"
            )
        desc = obj.get("description") or obj.get("explanation", "")
        if desc:
            lines.append(f"  Note: {desc}")
    return "\n".join(lines)


def build_context(
    query: str,
    retrieved_objects: list[dict[str, Any]],
    messages: list[dict[str, str]],
    cfg: ContextConfig,
) -> list[dict[str, str]]:
    """
    Assemble the message list for an LLM call, respecting the token budget.
    Returns list of {"role": ..., "content": ...} dicts.
    """
    # Always include system prompt (non-truncatable)
    system_content = SYSTEM_PROMPT
    system_tokens = _count(system_content)

    # Schema context (DB objects from retriever)
    schema_text = _render_schema_context(retrieved_objects)
    schema_tokens = _count(schema_text)

    # Session history (FIFO truncation from oldest)
    history_budget = cfg.max_history_tokens
    schema_budget = cfg.max_schema_tokens
    query_tokens = _count(query)

    remaining_for_schema = schema_budget
    if schema_tokens > remaining_for_schema:
        # Reduce top-N objects until it fits
        for n in range(len(retrieved_objects) - 1, 0, -1):
            truncated = _render_schema_context(retrieved_objects[:n])
            if _count(truncated) <= remaining_for_schema:
                schema_text = truncated
                schema_tokens = _count(truncated)
                break

    # Build history within budget
    history_messages: list[dict[str, str]] = []
    used_history = 0
    for msg in reversed(messages):
        tokens = _count(msg.get("content", ""))
        if used_history + tokens > history_budget:
            break
        history_messages.insert(0, msg)
        used_history += tokens

    assembled: list[dict[str, str]] = [
        {"role": "system", "content": system_content},
        {"role": "system", "content": f"## Relevant Database Objects\n\n{schema_text}"},
        *history_messages,
        {"role": "user", "content": query},
    ]

    total = system_tokens + schema_tokens + used_history + query_tokens
    if total > cfg.max_total_tokens:
        # Hard fail — query too large after all truncations
        raise ValueError(f"Context budget exceeded: {total} > {cfg.max_total_tokens}")

    return assembled
