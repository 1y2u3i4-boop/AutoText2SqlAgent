"""Evaluation metrics for BIRD benchmark."""
from __future__ import annotations

from typing import Any


def compute_ex(gold_result: Any, pred_result: Any) -> bool:
    """
    Execution Accuracy (EX): results match when both are non-None
    and the frozensets of result rows are equal.
    """
    if gold_result is None or pred_result is None:
        return False
    return gold_result == pred_result


def compute_metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(results)
    if total == 0:
        return {"total": 0, "ex_accuracy": 0.0}

    ex_hits = sum(1 for r in results if r.get("ex_match"))
    sql_generated = sum(1 for r in results if r.get("generated_sql"))
    latencies = [r.get("latency_ms", 0.0) for r in results]
    costs = [r.get("total_cost_usd", 0.0) for r in results]

    # Break down by difficulty
    by_difficulty: dict[str, dict[str, Any]] = {}
    for r in results:
        diff = r.get("difficulty", "unknown")
        if diff not in by_difficulty:
            by_difficulty[diff] = {"total": 0, "ex_hits": 0}
        by_difficulty[diff]["total"] += 1
        if r.get("ex_match"):
            by_difficulty[diff]["ex_hits"] += 1

    for diff, stats in by_difficulty.items():
        stats["ex_accuracy"] = stats["ex_hits"] / stats["total"] if stats["total"] else 0.0

    return {
        "total": total,
        "ex_accuracy": ex_hits / total,
        "ex_hits": ex_hits,
        "sql_generated_rate": sql_generated / total,
        "avg_latency_ms": sum(latencies) / total,
        "avg_cost_usd": sum(costs) / total,
        "total_cost_usd": sum(costs),
        "by_difficulty": by_difficulty,
    }
