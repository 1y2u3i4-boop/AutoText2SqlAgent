"""Base contracts for all tool adapters."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class ToolResult(BaseModel):
    success: bool
    data: Any | None = None
    error: str | None = None
    latency_ms: float = 0.0
    cost_usd: float | None = None
    retries_used: int = 0
