"""Gradio chat UI for AutoText2SQL, mounted onto the FastAPI application."""
from __future__ import annotations

import uuid
from typing import Any

import gradio as gr
import httpx

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fmt_sql(sql: str) -> str:
    return f"```sql\n{sql}\n```"


def _fmt_cost(cost: Any) -> str:
    try:
        value = float(cost or 0.0)
    except (TypeError, ValueError):
        value = 0.0
    if value == 0.0:
        return "`Cost: local/no LLM`"
    return f"`Cost: ${value:.6f}`"


def _http_error_message(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        try:
            payload = exc.response.json()
        except Exception:
            payload = None
        if isinstance(payload, dict) and payload.get("detail"):
            return str(payload["detail"])
        body = exc.response.text.strip()
        if body:
            return body
    return str(exc)


def _fmt_table(result: dict[str, Any]) -> str:
    cols = result.get("columns", [])
    rows = result.get("rows", [])
    if not cols or not rows:
        return "*No rows returned.*"
    header = "| " + " | ".join(cols) + " |"
    sep = "| " + " | ".join("---" for _ in cols) + " |"
    body = "\n".join(
        "| " + " | ".join(str(r.get(c, "")) for c in cols) + " |"
        for r in rows[:50]
    )
    table = f"{header}\n{sep}\n{body}"
    if len(rows) > 50:
        table += f"\n\n*Showing 50 of {len(rows)} rows.*"
    return table


def _ensure_id(value: Any) -> str:
    text = str(value or "").strip()
    return text or str(uuid.uuid4())


def _history_for_api(history: list[Any]) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    for message in history:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "").strip().lower()
        content = message.get("content")
        if role not in {"user", "assistant"} or not isinstance(content, str) or not content.strip():
            continue
        messages.append({"role": role, "content": content})
    return messages


def _query_sync(
    user_message: str,
    history: list,
    session_id: str,
    user_id: str,
) -> tuple[list, str, Any, str, str]:
    """Send query to /query endpoint, return updated history + session state."""
    session_id = _ensure_id(session_id)
    user_id = _ensure_id(user_id)
    history = list(history) + [{"role": "user", "content": user_message}]

    payload: dict[str, Any] = {
        "query": user_message,
        "session_id": session_id,
        "user_id": user_id,
        "messages": _history_for_api(history),
    }

    try:
        with httpx.Client(base_url="http://127.0.0.1:8000", timeout=120) as client:
            resp = client.post("/query", json=payload)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        history.append({"role": "assistant", "content": f"**Error:** {_http_error_message(exc)}"})
        return history, "", None, session_id, user_id

    session_id = data.get("session_id", session_id)
    needs_approval = bool(data.get("generated_sql") and not data.get("sql_result") and not data.get("error"))

    # Build assistant message
    parts: list[str] = []

    if data.get("error"):
        parts.append(f"**Error:** {data['error']}")
    elif data.get("requires_clarification") and data.get("clarification_message"):
        parts.append(data["clarification_message"])
    else:
        if data.get("answer"):
            parts.append(data["answer"])
        if data.get("generated_sql"):
            parts.append(_fmt_sql(data["generated_sql"]))
        if data.get("sql_result"):
            parts.append(_fmt_table(data["sql_result"]))
        if needs_approval:
            parts.append("---\n**SQL is ready.** Click *Execute SQL* below.")

    parts.append(f"\n{_fmt_cost(data.get('total_cost_usd', 0))}")

    history.append({"role": "assistant", "content": "\n\n".join(parts)})

    approval_state = {"session_id": session_id, "needs_approval": needs_approval} if needs_approval else None
    return history, "", approval_state, session_id, user_id


def _execute_sql(approval_state: Any, history: list) -> tuple[list, Any]:
    if not approval_state or not approval_state.get("needs_approval"):
        return history, None

    session_id = approval_state["session_id"]
    try:
        with httpx.Client(base_url="http://127.0.0.1:8000", timeout=120) as client:
            resp = client.post("/query/approve", json={"session_id": session_id, "approved": True})
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        history.append({"role": "assistant", "content": f"**Error during SQL execution:** {_http_error_message(exc)}"})
        return history, None

    parts = ["**Execution results:**"]
    if data.get("answer"):
        parts.append(data["answer"])
    if data.get("generated_sql"):
        parts.append(_fmt_sql(data["generated_sql"]))
    if data.get("sql_result"):
        parts.append(_fmt_table(data["sql_result"]))
    if data.get("error"):
        parts.append(f"**Error:** {data['error']}")
    parts.append(f"\n{_fmt_cost(data.get('total_cost_usd', 0))}")
    history.append({"role": "assistant", "content": "\n\n".join(parts)})
    return history, None


# ---------------------------------------------------------------------------
# Gradio Blocks UI
# ---------------------------------------------------------------------------


def create_ui() -> gr.Blocks:
    with gr.Blocks(title="AutoText2SQL") as demo:
        gr.HTML(
            """
            <style>
            #chatbot { height: 65vh !important; }
            .approval-row { margin-top: 4px; }
            footer { display: none !important; }
            </style>
            """
        )
        gr.Markdown(
            "# AutoText2SQL\n"
            "Ask questions about the connected database in natural language.\n\n"
            "The LLM decides whether to answer from the schema or generate SQL. SQL is shown first and runs only after you click Execute SQL."
        )

        approval_state = gr.State(None)
        session_state = gr.State("")
        user_id_state = gr.BrowserState(storage_key="autotext2sql_user_id")

        chatbot = gr.Chatbot(
            elem_id="chatbot",
            buttons=["copy"],
            placeholder="Ask a question, for example: о чем эта бд",
        )

        with gr.Row():
            msg = gr.Textbox(
                placeholder="e.g. о чем эта бд / Покажи 5 последних бронирований",
                show_label=False,
                scale=6,
                container=False,
            )
            send_btn = gr.Button("Send", variant="primary", scale=1)

        with gr.Row(elem_classes="approval-row"):
            execute_btn = gr.Button("Execute SQL", variant="primary", size="sm", scale=1, interactive=False)

        def on_submit(message, hist, session_id, user_id):
            if not message or not str(message).strip():
                return (
                    hist,
                    "",
                    None,
                    session_id,
                    user_id,
                    gr.update(interactive=False),
                )
            hist, cleared, state, session_id, user_id = _query_sync(message, hist, session_id, user_id)
            execute_vis = state is not None
            return (
                hist,
                cleared,
                state,
                session_id,
                user_id,
                gr.update(interactive=execute_vis),
            )

        submit_inputs = [msg, chatbot, session_state, user_id_state]
        submit_outputs = [chatbot, msg, approval_state, session_state, user_id_state, execute_btn]

        msg.submit(on_submit, submit_inputs, submit_outputs, show_progress="hidden")
        send_btn.click(on_submit, submit_inputs, submit_outputs, show_progress="hidden")

        def on_execute(state, hist):
            hist, state = _execute_sql(state, hist)
            return hist, state, gr.update(interactive=False)

        approval_outputs = [chatbot, approval_state, execute_btn]
        execute_btn.click(on_execute, [approval_state, chatbot], approval_outputs, show_progress="hidden")

    return demo
