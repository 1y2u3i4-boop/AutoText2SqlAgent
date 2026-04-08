"""Qdrant vector store adapter."""
from __future__ import annotations

import time
from typing import Any

import structlog
from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels

from autotext2sql.tools import ToolResult

logger = structlog.get_logger(__name__)

COLLECTION_NAME = "metadata_index"


class VectorStoreTool:
    def __init__(self, url: str = "http://localhost:6333", collection: str = COLLECTION_NAME) -> None:
        self._url = url
        self._collection = collection
        self._client = QdrantClient(url=url, timeout=5)

    def ensure_collection(self, vector_size: int) -> None:
        existing = {c.name for c in self._client.get_collections().collections}
        if self._collection not in existing:
            self._client.create_collection(
                collection_name=self._collection,
                vectors_config=qmodels.VectorParams(
                    size=vector_size,
                    distance=qmodels.Distance.COSINE,
                ),
            )
            logger.info("qdrant_collection_created", collection=self._collection)

    def upsert(self, points: list[dict[str, Any]]) -> ToolResult:
        """Upsert points. Each point: {id, vector, payload}"""
        start = time.perf_counter()
        try:
            qdrant_points = [
                qmodels.PointStruct(id=p["id"], vector=p["vector"], payload=p["payload"])
                for p in points
            ]
            self._client.upsert(collection_name=self._collection, points=qdrant_points)
            latency_ms = (time.perf_counter() - start) * 1000
            return ToolResult(success=True, data={"upserted": len(points)}, latency_ms=latency_ms)
        except Exception as exc:
            latency_ms = (time.perf_counter() - start) * 1000
            logger.error("qdrant_upsert_error", error=str(exc))
            return ToolResult(success=False, error=str(exc), latency_ms=latency_ms)

    def search(
        self,
        query_embedding: list[float],
        limit: int = 20,
        query_filter: dict[str, Any] | None = None,
    ) -> ToolResult:
        start = time.perf_counter()
        try:
            qdrant_filter = None
            if query_filter:
                conditions = [
                    qmodels.FieldCondition(
                        key=k,
                        match=qmodels.MatchValue(value=v),
                    )
                    for k, v in query_filter.items()
                ]
                qdrant_filter = qmodels.Filter(must=conditions)

            response = self._client.query_points(
                collection_name=self._collection,
                query=query_embedding,
                limit=limit,
                query_filter=qdrant_filter,
                with_payload=True,
            )
            hits = response.points
            latency_ms = (time.perf_counter() - start) * 1000
            return ToolResult(
                success=True,
                data={
                    "ids": [str(h.id) for h in hits],
                    "payloads": [h.payload for h in hits],
                    "scores": [h.score for h in hits],
                    "latency_ms": latency_ms,
                },
                latency_ms=latency_ms,
            )
        except Exception as exc:
            latency_ms = (time.perf_counter() - start) * 1000
            logger.error("qdrant_search_error", error=str(exc))
            return ToolResult(success=False, error=str(exc), latency_ms=latency_ms)

    def delete_collection(self) -> None:
        self._client.delete_collection(self._collection)
        logger.info("qdrant_collection_deleted", collection=self._collection)

    def count(self) -> int:
        try:
            info = self._client.get_collection(self._collection)
            return info.points_count or 0
        except Exception:
            return 0
