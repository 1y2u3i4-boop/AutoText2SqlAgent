"""Embedding tool using an OpenAI-compatible embeddings API."""
from __future__ import annotations

import math
import time
from typing import Any

import httpx
import structlog

from autotext2sql.tools import ToolResult

logger = structlog.get_logger(__name__)


class EmbeddingTool:
    def __init__(
        self,
        model_name: str = "openai/text-embedding-3-small",
        base_url: str = "",
        api_key: str = "",
        timeout: int = 30,
    ) -> None:
        self._model_name = model_name
        self._base_url = base_url.rstrip("/")
        if self._base_url.endswith("/v1"):
            self._base_url = self._base_url[: -len("/v1")]
        self._api_key = api_key
        self._timeout = timeout
        self._vector_size: int | None = None
        self._client = httpx.Client(
            base_url=self._base_url,
            headers={"Authorization": f"Bearer {self._api_key}"},
            timeout=timeout,
        )

    @staticmethod
    def _normalize_vector(vector: list[float]) -> list[float]:
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0:
            return vector
        return [value / norm for value in vector]

    @property
    def vector_size(self) -> int:
        if self._vector_size is None:
            result = self.embed(["schema metadata probe"], batch_size=1, normalize=False)
            if not result.success or not result.data:
                raise RuntimeError(result.error or "Failed to determine embedding size")
            self._vector_size = len(result.data["vectors"][0])
        return self._vector_size

    def embed(self, texts: list[str], batch_size: int = 32, normalize: bool = True) -> ToolResult:
        start = time.perf_counter()
        if not self._base_url:
            return ToolResult(success=False, error="Embedding API base URL is not configured")
        if not self._api_key:
            return ToolResult(success=False, error="Embedding API key is not configured")

        try:
            resp = self._client.post(
                "/v1/embeddings",
                json={
                    "model": self._model_name,
                    "input": texts,
                    "encoding_format": "float",
                },
            )
            resp.raise_for_status()
            data = resp.json()
            items = sorted(data.get("data", []), key=lambda item: item.get("index", 0))
            vectors = [item["embedding"] for item in items]
            if normalize:
                vectors = [self._normalize_vector(vector) for vector in vectors]
            if vectors and self._vector_size is None:
                self._vector_size = len(vectors[0])
            latency_ms = (time.perf_counter() - start) * 1000
            return ToolResult(
                success=True,
                data={"vectors": vectors, "model": self._model_name, "latency_ms": latency_ms},
                latency_ms=latency_ms,
                retries_used=0,
            )
        except Exception as exc:
            latency_ms = (time.perf_counter() - start) * 1000
            logger.error("embedding_failed", error=str(exc))
            return ToolResult(success=False, error=str(exc), latency_ms=latency_ms, retries_used=0)
