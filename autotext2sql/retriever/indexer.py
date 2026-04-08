"""Indexing pipeline: introspect databases → embed → store in Qdrant."""
from __future__ import annotations

import structlog

from autotext2sql.tools.database import DatabaseTool
from autotext2sql.tools.embedding import EmbeddingTool
from autotext2sql.tools.vector_store import VectorStoreTool
from autotext2sql.retriever.models import ColumnInfo, ForeignKeyInfo, TableDocument

logger = structlog.get_logger(__name__)

BATCH_SIZE = 32


def _build_documents(introspection_data: dict) -> list[TableDocument]:
    docs: list[TableDocument] = []
    db_name = introspection_data["db_name"]
    for schema in introspection_data.get("schemas", []):
        schema_name = schema["name"]
        for table in schema.get("tables", []):
            docs.append(
                TableDocument(
                    db_name=db_name,
                    schema_name=schema_name,
                    table_name=table["name"],
                    description=table.get("description", ""),
                    columns=[
                        ColumnInfo(
                            name=c["name"],
                            type=c["type"],
                            nullable=c.get("nullable", True),
                            default=c.get("default", ""),
                            description=c.get("description", ""),
                        )
                        for c in table.get("columns", [])
                    ],
                    primary_keys=table.get("primary_keys", []),
                    foreign_keys=[
                        ForeignKeyInfo(
                            column=fk["column"],
                            target_table=fk["target_table"],
                            target_column=fk["target_column"],
                            target_schema=fk.get("target_schema", ""),
                        )
                        for fk in table.get("foreign_keys", [])
                    ],
                )
            )
    return docs


class Indexer:
    def __init__(
        self,
        db_tools: list[DatabaseTool],
        embedding_tool: EmbeddingTool,
        vector_store: VectorStoreTool,
    ) -> None:
        self._db_tools = db_tools
        self._embedding_tool = embedding_tool
        self._vector_store = vector_store

    def run(self, rebuild: bool = True) -> dict:
        if rebuild:
            logger.info("indexer_rebuild_start")
            try:
                self._vector_store.delete_collection()
            except Exception:
                pass

        self._vector_store.ensure_collection(self._embedding_tool.vector_size)

        total_docs = 0
        for db_tool in self._db_tools:
            result = db_tool.introspect()
            if not result.success or result.data is None:
                logger.error("introspection_failed", db=db_tool._db_name, error=result.error)
                continue

            docs = _build_documents(result.data)
            logger.info("introspection_done", db=db_tool._db_name, tables=len(docs))

            # batch embed
            texts = [doc.to_text() for doc in docs]
            for batch_start in range(0, len(texts), BATCH_SIZE):
                batch_texts = texts[batch_start : batch_start + BATCH_SIZE]
                batch_docs = docs[batch_start : batch_start + BATCH_SIZE]

                embed_result = self._embedding_tool.embed(batch_texts, batch_size=BATCH_SIZE)
                if not embed_result.success or embed_result.data is None:
                    logger.error("embedding_batch_failed", error=embed_result.error)
                    continue

                vectors = embed_result.data["vectors"]
                points = [
                    {
                        "id": abs(hash(doc.doc_id())) % (2**63),
                        "vector": vectors[i],
                        "payload": {
                            "db_name": doc.db_name,
                            "schema_name": doc.schema_name,
                            "table_name": doc.table_name,
                            "object_type": "table",
                            "column_names": [c.name for c in doc.columns],
                            "has_description": bool(doc.description),
                            "doc_text": batch_texts[i],
                            "columns_json": [c.model_dump() for c in doc.columns],
                            "foreign_keys_json": [fk.model_dump() for fk in doc.foreign_keys],
                            "primary_keys": doc.primary_keys,
                            "description": doc.description,
                        },
                    }
                    for i, doc in enumerate(batch_docs)
                ]
                upsert_result = self._vector_store.upsert(points)
                if not upsert_result.success:
                    logger.error("qdrant_upsert_failed", error=upsert_result.error)
                    continue

                total_docs += len(points)
                logger.info(
                    "indexer_batch_upserted",
                    db=db_tool._db_name,
                    batch_start=batch_start,
                    count=len(points),
                )

        logger.info("indexer_complete", total_docs=total_docs)
        return {"total_docs": total_docs}
