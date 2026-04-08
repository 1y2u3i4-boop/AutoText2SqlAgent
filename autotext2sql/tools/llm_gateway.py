"""LLM Gateway adapter – OpenAI-compatible calls with retry and fallback."""
from __future__ import annotations

import time
from typing import Any

import httpx
import structlog
from pydantic import BaseModel

from autotext2sql.tools import ToolResult

logger = structlog.get_logger(__name__)


class LLMRequest(BaseModel):
    messages: list[dict[str, str]]
    route: str = "default"
    task_type: str = "respond"
    temperature: float = 0.0
    max_tokens: int = 2000
    response_format: dict[str, Any] | None = None
    schema_name: str | None = None


class LLMResponse(BaseModel):
    content: str
    parsed: Any | None = None
    selected_provider: str = ""
    selected_model: str = ""
    route: str = ""
    provider_status: str = "primary"
    failed_provider: str | None = None
    usage: dict[str, Any] = {}
    latency_ms: float = 0.0
    cost_usd: float = 0.0


class LLMGateway:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        default_model: str,
        route_overrides: dict[str, str] | None = None,
        timeout: int = 30,
        max_retries: int = 2,
        backoff_base: float = 1.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        if self._base_url.endswith("/v1"):
            self._base_url = self._base_url[: -len("/v1")]
        self._api_key = api_key
        self._default_model = default_model
        self._route_overrides = route_overrides or {}
        self._timeout = timeout
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._client = httpx.Client(
            base_url=self._base_url,
            headers={"Authorization": f"Bearer {self._api_key}"},
            timeout=timeout,
        )

    def _resolve_model(self, route: str) -> str:
        model = self._route_overrides.get(route) or self._default_model
        if not model:
            raise ValueError(f"No model configured for route '{route}'")
        return model

    def call(self, request: LLMRequest) -> ToolResult:
        start = time.perf_counter()
        last_error: str = ""
        for attempt in range(self._max_retries + 1):
            if attempt > 0:
                time.sleep(self._backoff_base * (2 ** (attempt - 1)))
            try:
                result = self._call_once(request)
                result.latency_ms = (time.perf_counter() - start) * 1000
                return ToolResult(
                    success=True,
                    data=result,
                    latency_ms=result.latency_ms,
                    cost_usd=result.cost_usd,
                    retries_used=attempt,
                )
            except Exception as exc:
                last_error = str(exc)
                logger.warning(
                    "llm_gateway_call_failed",
                    attempt=attempt,
                    error=last_error,
                    route=request.route,
                )

        latency_ms = (time.perf_counter() - start) * 1000
        return ToolResult(
            success=False,
            error=last_error,
            latency_ms=latency_ms,
            retries_used=self._max_retries,
        )

    def _call_once(self, request: LLMRequest) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self._resolve_model(request.route),
            "messages": request.messages,
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
        }
        if request.response_format:
            payload["response_format"] = request.response_format

        resp = self._client.post(
            "/v1/chat/completions",
            json=payload,
            headers={"X-Route": request.route, "X-Task-Type": request.task_type},
        )
        resp.raise_for_status()
        data = resp.json()

        content = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})
        raw_cost = usage.get("cost") if isinstance(usage, dict) else None
        try:
            cost = float(raw_cost or 0.0)
        except (TypeError, ValueError):
            cost = 0.0
        parsed: dict[str, Any] | None = None
        if request.response_format and isinstance(content, str):
            try:
                import json as _json

                parsed = _json.loads(content)
            except Exception:
                parsed = None

        return LLMResponse(
            content=content,
            parsed=parsed,
            selected_provider=data.get("x_provider", ""),
            selected_model=data.get("model", ""),
            route=request.route,
            provider_status=data.get("x_provider_status", "primary"),
            failed_provider=data.get("x_failed_provider"),
            usage=usage,
            cost_usd=cost,
        )

    def close(self) -> None:
        self._client.close()
