"""CLI entry point for building/rebuilding the metadata index."""
from __future__ import annotations

import argparse
import sys

import structlog

from autotext2sql.config import get_settings
from autotext2sql.observability import setup as setup_observability
from autotext2sql.retriever.indexer import Indexer
from autotext2sql.tools.database import DatabaseTool
from autotext2sql.tools.embedding import EmbeddingTool
from autotext2sql.tools.vector_store import VectorStoreTool

logger = structlog.get_logger(__name__)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="AutoText2SQL metadata indexer")
    parser.add_argument(
        "--rebuild",
        action="store_true",
        default=False,
        help="Drop existing collection and rebuild from scratch",
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config.yaml (default: config.yaml)",
    )
    args = parser.parse_args(argv)

    from autotext2sql.config import load_settings

    settings = load_settings(args.config)

    setup_observability(
        level=settings.observability.log_level,
        fmt=settings.observability.log_format,
        otlp_endpoint=settings.observability.otlp_endpoint,
    )

    if not settings.databases:
        logger.error("no_databases_configured")
        print("ERROR: No databases configured. Add entries under 'databases' in config.yaml", file=sys.stderr)
        sys.exit(1)

    db_tools = [
        DatabaseTool(
            db_name=db.name,
            url=db.url,
            query_timeout=db.query_timeout_seconds,
            pool_size=db.pool_size,
        )
        for db in settings.databases
    ]

    embedding_tool = EmbeddingTool(
        model_name=settings.llm.embedding_model,
        base_url=settings.llm_gateway_base_url or settings.llm.gateway_url,
        api_key=settings.llm_gateway_api_key,
        timeout=settings.llm.timeout_seconds,
    )

    vector_store = VectorStoreTool(
        url=settings.retriever.qdrant_url,
        collection=settings.retriever.qdrant_collection,
    )

    indexer = Indexer(db_tools=db_tools, embedding_tool=embedding_tool, vector_store=vector_store)

    logger.info("indexer_start", rebuild=args.rebuild, databases=[db.name for db in settings.databases])
    stats = indexer.run(rebuild=args.rebuild)

    print(f"\nIndexing complete. Documents indexed: {stats['total_docs']}")


if __name__ == "__main__":
    main()
