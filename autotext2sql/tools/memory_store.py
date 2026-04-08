"""Memory store abstraction with an optional OSS Mem0-backed implementation."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import structlog

from autotext2sql.config import Settings

logger = structlog.get_logger(__name__)


@dataclass(slots=True)
class MemoryRecord:
    text: str
    score: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class BaseMemoryStore:
    def search(self, query: str, user_id: str) -> list[MemoryRecord]:
        return []

    def store_turn(
        self,
        *,
        user_id: str,
        session_id: str,
        messages: list[dict[str, str]],
        metadata: dict[str, Any] | None = None,
    ) -> None:
        return None


class NoopMemoryStore(BaseMemoryStore):
    pass


class Mem0MemoryStore(BaseMemoryStore):
    def __init__(self, settings: Settings) -> None:
        from mem0 import Memory

        gateway_url = settings.llm_gateway_base_url or settings.llm.gateway_url
        api_key = settings.llm_gateway_api_key
        if not gateway_url:
            raise ValueError("LLM gateway URL is required for OSS mem0")
        if not api_key:
            raise ValueError("LLM gateway API key is required for OSS mem0")

        qdrant_config = _build_qdrant_config(
            qdrant_url=settings.retriever.qdrant_url,
            collection_name=settings.memory.collection_name,
            embedding_dimensions=settings.memory.embedding_dimensions,
        )
        memory_config = {
            "vector_store": {
                "provider": "qdrant",
                "config": qdrant_config,
            },
            "llm": {
                "provider": "openai",
                "config": {
                    "model": settings.llm.default_route,
                    "api_key": api_key,
                    "openai_base_url": gateway_url,
                    "temperature": 0.0,
                    "max_tokens": min(settings.llm.max_tokens, 1500),
                },
            },
            "embedder": {
                "provider": "openai",
                "config": {
                    "model": settings.llm.embedding_model,
                    "api_key": api_key,
                    "openai_base_url": gateway_url,
                    "embedding_dims": settings.memory.embedding_dimensions,
                },
            },
            "history_db_path": "./data/mem0_history.db",
            "enable_graph": False,
        }

        self._client = Memory.from_config(memory_config)
        self._top_k = max(1, settings.memory.search_top_k)
        self._max_memories = max(1, settings.memory.max_memories)

    def search(self, query: str, user_id: str) -> list[MemoryRecord]:
        if not query.strip() or not user_id.strip():
            return []
        try:
            raw = self._client.search(query=query, user_id=user_id, limit=self._top_k)
        except Exception as exc:
            logger.warning("memory_search_failed", error=str(exc), user_id=user_id)
            return []
        return _normalize_mem0_results(raw, self._max_memories)

    def store_turn(
        self,
        *,
        user_id: str,
        session_id: str,
        messages: list[dict[str, str]],
        metadata: dict[str, Any] | None = None,
    ) -> None:
        payload = _sanitize_messages(messages)
        if not payload or not user_id.strip():
            return

        final_metadata = dict(metadata or {})
        if session_id:
            final_metadata.setdefault("session_id", session_id)

        try:
            self._client.add(payload, user_id=user_id, metadata=final_metadata)
        except Exception as exc:
            logger.warning("memory_write_failed", error=str(exc), user_id=user_id)


def build_memory_store(settings: Settings) -> BaseMemoryStore:
    if not settings.memory.enabled:
        logger.info("memory_disabled")
        return NoopMemoryStore()
    if settings.memory.provider != "mem0":
        logger.warning("memory_provider_unsupported", provider=settings.memory.provider)
        return NoopMemoryStore()

    try:
        store = Mem0MemoryStore(settings)
    except Exception as exc:
        logger.warning("memory_initialization_failed", error=str(exc))
        return NoopMemoryStore()

    logger.info("memory_enabled", provider=settings.memory.provider)
    return store


def _sanitize_messages(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    sanitized: list[dict[str, str]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "").strip().lower()
        content = str(message.get("content") or "").strip()
        if role not in {"user", "assistant"} or not content:
            continue
        sanitized.append({"role": role, "content": content})
    return sanitized


def _normalize_mem0_results(raw: Any, max_memories: int) -> list[MemoryRecord]:
    if isinstance(raw, dict):
        items = raw.get("results", [])
    elif isinstance(raw, list):
        items = raw
    else:
        items = []

    normalized: list[MemoryRecord] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        text = str(item.get("memory") or item.get("text") or item.get("content") or "").strip()
        if not text:
            continue
        score: float | None = None
        raw_score = item.get("score")
        if raw_score is not None:
            try:
                score = float(raw_score)
            except (TypeError, ValueError):
                score = None
        metadata = item.get("metadata")
        normalized.append(
            MemoryRecord(
                text=text,
                score=score,
                metadata=metadata if isinstance(metadata, dict) else {},
            )
        )
        if len(normalized) >= max_memories:
            break
    return normalized


def _build_qdrant_config(
    *,
    qdrant_url: str,
    collection_name: str,
    embedding_dimensions: int,
) -> dict[str, Any]:
    config: dict[str, Any] = {
        "collection_name": collection_name,
        "embedding_model_dims": embedding_dimensions,
    }
    parsed = urlparse(qdrant_url)
    if parsed.scheme and parsed.netloc:
        config["url"] = qdrant_url
        return config
    if parsed.hostname and parsed.port:
        config["host"] = parsed.hostname
        config["port"] = parsed.port
        return config
    if qdrant_url:
        config["url"] = qdrant_url
    return config
