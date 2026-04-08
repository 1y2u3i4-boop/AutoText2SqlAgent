"""Online retrieval pipeline: embed query → vector search → rerank → enrich."""
from __future__ import annotations

import json
import time
from typing import Any

import structlog

from autotext2sql.retriever.indexer import _build_documents, BATCH_SIZE
from autotext2sql.tools.embedding import EmbeddingTool
from autotext2sql.tools.database import DatabaseTool, discover_database_urls
from autotext2sql.tools.llm_gateway import LLMGateway, LLMRequest
from autotext2sql.tools.vector_store import VectorStoreTool
from autotext2sql.retriever.models import (
    ColumnInfo,
    ForeignKeyInfo,
    RankedDBObject,
    RetrieverInput,
    RetrieverOutput,
)

logger = structlog.get_logger(__name__)

_RERANK_PROMPT_TEMPLATE = """\
You are a database metadata expert. Given a user query and a list of database objects, \
score each object's relevance to the query on a scale from 0 to 10.

User query: {query}

Objects:
{objects_json}

Return ONLY a JSON array of objects with fields "index" (0-based) and "score" (float 0-10). \
Example: [{{"index": 0, "score": 8.5}}, {{"index": 1, "score": 3.0}}]
"""


def _score_vectors(query_vector: list[float], vectors: list[list[float]]) -> list[float]:
    scores: list[float] = []
    for vector in vectors:
        cosine = sum(a * b for a, b in zip(query_vector, vector))
        scores.append((cosine + 1.0) / 2.0)
    return scores


def _payload_from_doc(doc: Any, doc_text: str) -> dict[str, Any]:
    return {
        "db_name": doc.db_name,
        "schema_name": doc.schema_name,
        "table_name": doc.table_name,
        "object_type": "table",
        "column_names": [c.name for c in doc.columns],
        "has_description": bool(doc.description),
        "doc_text": doc_text,
        "columns_json": [c.model_dump() for c in doc.columns],
        "foreign_keys_json": [fk.model_dump() for fk in doc.foreign_keys],
        "primary_keys": doc.primary_keys,
        "description": doc.description,
    }


class Retriever:
    def __init__(
        self,
        embedding_tool: EmbeddingTool,
        vector_store: VectorStoreTool,
        llm_gateway: LLMGateway | None = None,
        top_k: int = 20,
        top_n: int = 5,
        confidence_threshold: float = 0.6,
        rerank_enabled: bool = True,
        enrichment_enabled: bool = True,
    ) -> None:
        self._embedding_tool = embedding_tool
        self._vector_store = vector_store
        self._llm = llm_gateway
        self._top_k = top_k
        self._top_n = top_n
        self._confidence_threshold = confidence_threshold
        self._rerank_enabled = rerank_enabled
        self._enrichment_enabled = enrichment_enabled

    def retrieve(self, inp: RetrieverInput) -> RetrieverOutput:
        start = time.perf_counter()
        total_cost = 0.0

        # 1. Build query embedding
        query_text = inp.query
        if inp.entities:
            query_text = inp.query + " " + " ".join(inp.entities)

        embed_result = self._embedding_tool.embed([query_text])
        if not embed_result.success or embed_result.data is None:
            logger.error("query_embedding_failed", error=embed_result.error)
            return RetrieverOutput()

        query_vector = embed_result.data["vectors"][0]

        # 2. Search indexed metadata or fall back to ad-hoc live introspection
        payloads: list[dict[str, Any]]
        scores: list[float]
        db_urls: dict[str, str] = {}
        if inp.db_urls:
            db_urls = dict(inp.db_urls)
            if inp.selected_databases:
                selected = set(inp.selected_databases)
                db_urls = {name: url for name, url in db_urls.items() if name in selected}
        elif inp.db_url:
            try:
                db_urls = discover_database_urls(inp.db_url)
            except Exception as exc:
                logger.error("adhoc_db_discovery_failed", error=str(exc))
                return RetrieverOutput()

            if inp.db_hint:
                db_urls = {name: url for name, url in db_urls.items() if name == inp.db_hint}
            if inp.selected_databases:
                selected = set(inp.selected_databases)
                db_urls = {name: url for name, url in db_urls.items() if name in selected}
            if not db_urls:
                logger.warning("adhoc_db_discovery_empty")
                return RetrieverOutput()

            docs = []
            for db_name, db_url in db_urls.items():
                db_tool = DatabaseTool(db_name=db_name, url=db_url, query_timeout=10, pool_size=1)
                introspection = db_tool.introspect()
                db_tool.dispose()
                if not introspection.success or introspection.data is None:
                    logger.warning("adhoc_db_introspection_failed", db=db_name, error=introspection.error)
                    continue
                docs.extend(_build_documents(introspection.data))
            if not docs:
                return RetrieverOutput()
            texts = [doc.to_text() for doc in docs]
            doc_vectors: list[list[float]] = []
            for batch_start in range(0, len(texts), BATCH_SIZE):
                batch_texts = texts[batch_start : batch_start + BATCH_SIZE]
                batch_embed_result = self._embedding_tool.embed(batch_texts, batch_size=BATCH_SIZE)
                if not batch_embed_result.success or batch_embed_result.data is None:
                    logger.error("adhoc_db_embedding_failed", error=batch_embed_result.error)
                    return RetrieverOutput()
                doc_vectors.extend(batch_embed_result.data["vectors"])

            scored = list(zip(docs, texts, _score_vectors(query_vector, doc_vectors), strict=False))
            scored.sort(key=lambda item: item[2], reverse=True)
            top_scored = scored[: inp.top_k or self._top_k]
            payloads = [_payload_from_doc(doc, text) for doc, text, _ in top_scored]
            scores = [score for _, _, score in top_scored]
        else:
            payload_filter = {"db_name": inp.db_hint} if inp.db_hint else None
            search_result = self._vector_store.search(
                query_embedding=query_vector,
                limit=inp.top_k or self._top_k,
                query_filter=payload_filter,
            )
            if not search_result.success or search_result.data is None:
                logger.error("qdrant_search_failed", error=search_result.error)
                return RetrieverOutput()

            payloads = search_result.data["payloads"]
            scores = search_result.data["scores"]

        if not payloads:
            return RetrieverOutput()

        # 3. Optional reranking via LLM
        if self._rerank_enabled and self._llm and len(payloads) > (inp.top_n or self._top_n):
            try:
                objects_json = json.dumps(
                    [
                        {
                            "index": i,
                            "db": p.get("db_name", ""),
                            "schema": p.get("schema_name", ""),
                            "table": p.get("table_name", ""),
                            "columns": p.get("column_names", [])[:10],
                            "description": p.get("description", ""),
                        }
                        for i, p in enumerate(payloads)
                    ],
                    ensure_ascii=False,
                )
                rerank_req = LLMRequest(
                    messages=[
                        {
                            "role": "system",
                            "content": "You are a helpful database metadata expert.",
                        },
                        {
                            "role": "user",
                            "content": _RERANK_PROMPT_TEMPLATE.format(
                                query=inp.query, objects_json=objects_json
                            ),
                        },
                    ],
                    route="rerank",
                    task_type="rerank",
                    temperature=0.0,
                    max_tokens=512,
                    response_format={"type": "json_object"},
                )
                rerank_result = self._llm.call(rerank_req)
                if rerank_result.success and rerank_result.data:
                    llm_resp = rerank_result.data
                    total_cost += llm_resp.cost_usd or 0.0
                    try:
                        ranked = json.loads(llm_resp.content)
                        if isinstance(ranked, list):
                            ranked_sorted = sorted(ranked, key=lambda x: x.get("score", 0), reverse=True)
                            top_indices = [r["index"] for r in ranked_sorted[: inp.top_n or self._top_n]]
                            payloads = [payloads[i] for i in top_indices if i < len(payloads)]
                            scores = [r.get("score", 0.0) / 10.0 for r in ranked_sorted[: len(payloads)]]
                    except Exception as exc:
                        logger.warning("rerank_parse_failed", error=str(exc))
            except Exception as exc:
                logger.warning("rerank_failed", error=str(exc))
        else:
            payloads = payloads[: inp.top_n or self._top_n]
            scores = scores[: len(payloads)]

        # 4. Build RankedDBObject list
        objects: list[RankedDBObject] = []
        for payload, score in zip(payloads, scores):
            columns = [ColumnInfo(**c) for c in payload.get("columns_json", [])]
            fks = [ForeignKeyInfo(**fk) for fk in payload.get("foreign_keys_json", [])]
            obj = RankedDBObject(
                db_name=payload.get("db_name", ""),
                schema_name=payload.get("schema_name", ""),
                table_name=payload.get("table_name", ""),
                columns=columns,
                foreign_keys=fks,
                relevance_score=float(score),
                explanation=f"Relevance score: {score:.2f}",
            )
            objects.append(obj)

        confidence = max((o.relevance_score for o in objects), default=0.0)
        latency_ms = (time.perf_counter() - start) * 1000
        logger.info("retrieval_done", objects=len(objects), confidence=confidence, latency_ms=latency_ms)

        return RetrieverOutput(
            objects=objects,
            confidence=confidence,
            latency_ms=latency_ms,
            cost_usd=total_cost,
            db_urls=db_urls,
        )
