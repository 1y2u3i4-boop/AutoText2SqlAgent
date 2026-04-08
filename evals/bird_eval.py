"""
BIRD benchmark runner for AutoText2SQL.

Evaluation metric: Execution Accuracy (EX) — whether the execution result of
a generated SQL matches the execution result of the gold SQL.

BIRD dataset structure expected at --bird-dir:
  dev/
    dev.json              <- list of questions with db_id, question, SQL, evidence
    dev_databases/
      <db_id>/
        <db_id>.sqlite    <- SQLite database file

Usage:
  python -m evals.bird_eval \\
      --bird-dir /path/to/bird \\
      --api-url http://localhost:8000 \\
      --split dev \\
      --limit 100 \\
      --output results/bird_results.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

import httpx
import structlog

from evals.metrics import compute_ex, compute_metrics

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# BIRD dataset loading
# ---------------------------------------------------------------------------


def load_bird_questions(bird_dir: str, split: str = "dev") -> list[dict[str, Any]]:
    """Load BIRD questions from the dataset directory."""
    split_dir = Path(bird_dir) / split
    questions_file = split_dir / f"{split}.json"

    if not questions_file.exists():
        raise FileNotFoundError(f"BIRD questions file not found: {questions_file}")

    with open(questions_file, encoding="utf-8") as f:
        questions = json.load(f)

    return questions


def get_db_path(bird_dir: str, split: str, db_id: str) -> str:
    """Return path to a BIRD SQLite database."""
    return str(Path(bird_dir) / split / f"{split}_databases" / db_id / f"{db_id}.sqlite")


# ---------------------------------------------------------------------------
# SQL execution on SQLite
# ---------------------------------------------------------------------------


def execute_sqlite(db_path: str, sql: str) -> Any:
    """Execute SQL on a SQLite database and return results as a frozenset of tuples."""
    try:
        conn = sqlite3.connect(db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(sql)
        rows = cursor.fetchall()
        conn.close()
        # Normalise: frozenset of sorted tuples for order-insensitive comparison
        result = frozenset(tuple(str(v) if v is not None else "NULL" for v in row) for row in rows)
        return result, None
    except Exception as exc:
        return None, str(exc)


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------


async def call_agent(
    client: httpx.AsyncClient,
    api_url: str,
    question: str,
    db_id: str,
    timeout: float = 60.0,
) -> dict[str, Any]:
    """Call the AutoText2SQL API and return the response."""
    payload = {
        "query": question,
        "db_hint": db_id,
    }
    try:
        resp = await client.post(
            f"{api_url}/query",
            json=payload,
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logger.warning("api_call_failed", question=question[:60], error=str(exc))
        return {"error": str(exc), "generated_sql": None}


# ---------------------------------------------------------------------------
# Main evaluation loop
# ---------------------------------------------------------------------------


async def run_eval(
    bird_dir: str,
    api_url: str,
    split: str = "dev",
    limit: int | None = None,
    output_path: str | None = None,
    concurrency: int = 4,
    auto_approve: bool = True,
) -> dict[str, Any]:
    """
    Run BIRD evaluation.

    Args:
        bird_dir: Path to BIRD dataset root.
        api_url: Base URL of AutoText2SQL API.
        split: Dataset split to use ('dev' or 'train').
        limit: Max number of questions to evaluate (None = all).
        output_path: Where to save detailed results JSON.
        concurrency: Number of concurrent API calls.
        auto_approve: Whether to automatically approve SQL execution
                      (for BIRD eval, we want to execute and compare).
    """
    questions = load_bird_questions(bird_dir, split)
    if limit:
        questions = questions[:limit]

    logger.info("bird_eval_start", total=len(questions), split=split)

    results: list[dict[str, Any]] = []
    semaphore = asyncio.Semaphore(concurrency)

    async with httpx.AsyncClient() as client:

        async def process_one(q: dict[str, Any]) -> dict[str, Any]:
            async with semaphore:
                db_id = q.get("db_id", "")
                question = q.get("question", "")
                gold_sql = q.get("SQL", q.get("query", ""))
                evidence = q.get("evidence", "")
                difficulty = q.get("difficulty", "unknown")

                # Enrich question with evidence if provided
                query_text = question
                if evidence:
                    query_text = f"{question}\n\nHint: {evidence}"

                db_path = get_db_path(bird_dir, split, db_id)
                db_exists = os.path.exists(db_path)

                # Execute gold SQL
                gold_result, gold_err = None, None
                if db_exists:
                    gold_result, gold_err = execute_sqlite(db_path, gold_sql)

                # Call agent
                t0 = time.perf_counter()
                agent_response = await call_agent(client, api_url, query_text, db_id)
                latency = (time.perf_counter() - t0) * 1000

                generated_sql = agent_response.get("generated_sql") or ""
                agent_error = agent_response.get("error")

                # If the agent paused for approval, auto-approve
                session_id = agent_response.get("session_id", "")
                if auto_approve and session_id and not agent_error:
                    try:
                        approve_resp = await client.post(
                            f"{api_url}/query/approve",
                            json={"session_id": session_id, "approved": True},
                            timeout=60.0,
                        )
                        approve_resp.raise_for_status()
                        final = approve_resp.json()
                        generated_sql = final.get("generated_sql") or generated_sql
                        sql_result = final.get("sql_result")
                    except Exception:
                        sql_result = None
                else:
                    sql_result = agent_response.get("sql_result")

                # Execute generated SQL for comparison
                pred_result, pred_err = None, None
                if db_exists and generated_sql:
                    pred_result, pred_err = execute_sqlite(db_path, generated_sql)

                ex_match = compute_ex(gold_result, pred_result)

                record: dict[str, Any] = {
                    "question_id": q.get("question_id", ""),
                    "db_id": db_id,
                    "question": question,
                    "difficulty": difficulty,
                    "gold_sql": gold_sql,
                    "generated_sql": generated_sql,
                    "ex_match": ex_match,
                    "latency_ms": latency,
                    "agent_error": agent_error,
                    "pred_exec_error": pred_err,
                    "gold_exec_error": gold_err,
                    "total_cost_usd": agent_response.get("total_cost_usd", 0.0),
                }
                return record

        tasks = [process_one(q) for q in questions]
        results = await asyncio.gather(*tasks)

    metrics = compute_metrics(results)
    logger.info("bird_eval_done", **{k: f"{v:.4f}" if isinstance(v, float) else v for k, v in metrics.items()})

    output: dict[str, Any] = {
        "summary": metrics,
        "results": results,
    }

    if output_path:
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=2, ensure_ascii=False)
        print(f"\nResults saved to: {output_path}")

    _print_summary(metrics)
    return output


def _print_summary(metrics: dict[str, Any]) -> None:
    print("\n" + "=" * 50)
    print("  BIRD Benchmark Results")
    print("=" * 50)
    print(f"  Total questions : {metrics.get('total', 0)}")
    print(f"  EX Accuracy     : {metrics.get('ex_accuracy', 0):.2%}")
    print(f"  SQL generated   : {metrics.get('sql_generated_rate', 0):.2%}")
    print(f"  Avg latency     : {metrics.get('avg_latency_ms', 0):.0f} ms")
    print(f"  Avg cost/query  : ${metrics.get('avg_cost_usd', 0):.4f}")
    print("\nBy difficulty:")
    for diff, stats in metrics.get("by_difficulty", {}).items():
        print(f"  {diff:12s}: EX={stats['ex_accuracy']:.2%}  (n={stats['total']})")
    print("=" * 50)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="BIRD benchmark evaluation for AutoText2SQL")
    parser.add_argument("--bird-dir", required=True, help="Path to BIRD dataset root directory")
    parser.add_argument(
        "--api-url",
        default="http://localhost:8000",
        help="AutoText2SQL API base URL (default: http://localhost:8000)",
    )
    parser.add_argument(
        "--split",
        default="dev",
        choices=["dev", "train"],
        help="Dataset split to evaluate (default: dev)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limit number of questions (default: all)",
    )
    parser.add_argument(
        "--output",
        default="results/bird_results.json",
        help="Output file for detailed results (default: results/bird_results.json)",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=4,
        help="Number of concurrent API calls (default: 4)",
    )
    parser.add_argument(
        "--no-auto-approve",
        action="store_true",
        default=False,
        help="Disable automatic SQL approval (human-in-the-loop mode)",
    )
    args = parser.parse_args()

    asyncio.run(
        run_eval(
            bird_dir=args.bird_dir,
            api_url=args.api_url,
            split=args.split,
            limit=args.limit,
            output_path=args.output,
            concurrency=args.concurrency,
            auto_approve=not args.no_auto_approve,
        )
    )


if __name__ == "__main__":
    main()
