"""Retriever data models."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ColumnInfo(BaseModel):
    name: str
    type: str
    nullable: bool = True
    default: str = ""
    description: str = ""


class ForeignKeyInfo(BaseModel):
    column: str
    target_table: str
    target_column: str
    target_schema: str = ""


class TableDocument(BaseModel):
    """Normalized table document used for indexing and retrieval."""

    db_name: str
    schema_name: str
    table_name: str
    columns: list[ColumnInfo] = []
    primary_keys: list[str] = []
    foreign_keys: list[ForeignKeyInfo] = []
    description: str = ""

    def to_text(self) -> str:
        """Build the text representation used for embedding."""
        lines = [
            f"Database: {self.db_name}",
            f"Schema: {self.schema_name}",
            f"Table: {self.table_name}",
        ]
        if self.description:
            lines.append(f"Description: {self.description}")
        if self.columns:
            col_strs = [
                f"{c.name} ({c.type}{'', ' nullable' if c.nullable else ''}{',' + ' ' + c.description if c.description else ''})"
                for c in self.columns
            ]
            lines.append("Columns: " + ", ".join(col_strs))
        if self.primary_keys:
            lines.append("Primary key: " + ", ".join(self.primary_keys))
        if self.foreign_keys:
            fk_strs = [f"{fk.column} -> {fk.target_table}.{fk.target_column}" for fk in self.foreign_keys]
            lines.append("Foreign keys: " + "; ".join(fk_strs))
        return "\n".join(lines)

    def doc_id(self) -> str:
        import hashlib

        key = f"{self.db_name}.{self.schema_name}.{self.table_name}"
        return hashlib.sha1(key.encode()).hexdigest()[:16]


class RankedDBObject(BaseModel):
    db_name: str
    schema_name: str
    table_name: str
    columns: list[ColumnInfo] = []
    foreign_keys: list[ForeignKeyInfo] = []
    relevance_score: float = 0.0
    enrichment_data: dict[str, Any] | None = None
    explanation: str = ""


class RetrieverInput(BaseModel):
    query: str
    entities: list[str] = []
    db_hint: str | None = None
    db_url: str | None = None
    db_urls: dict[str, str] | None = None
    selected_databases: list[str] | None = None
    top_k: int = 20
    top_n: int = 5


class RetrieverOutput(BaseModel):
    objects: list[RankedDBObject] = []
    confidence: float = 0.0
    latency_ms: float = 0.0
    cost_usd: float = 0.0
    db_urls: dict[str, str] = Field(default_factory=dict)
