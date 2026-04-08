from __future__ import annotations

import os
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------


class LLMConfig(BaseModel):
    gateway_url: str = "http://localhost:8080"
    default_route: str = "qwen/qwen3.6-plus"
    route_overrides: dict[str, str] = Field(default_factory=dict)
    fallback_enabled: bool = True
    embedding_model: str = "openai/text-embedding-3-small"
    temperature: float = 0.0
    max_tokens: int = 2000
    timeout_seconds: int = 30


class DatabaseConfig(BaseModel):
    name: str
    url: str
    readonly: bool = True
    query_timeout_seconds: int = 10
    pool_size: int = 5


class RetrieverConfig(BaseModel):
    top_k: int = 20
    top_n: int = 5
    confidence_threshold: float = 0.6
    rerank_enabled: bool = True
    enrichment_enabled: bool = True
    qdrant_url: str = "http://localhost:6333"
    qdrant_collection: str = "metadata_index"


class OrchestratorConfig(BaseModel):
    max_retries_per_node: int = 2
    backoff_base_seconds: float = 1.0
    human_approval_timeout_seconds: int = 300
    checkpointer_db: str = "./data/sessions.db"


class ContextConfig(BaseModel):
    max_total_tokens: int = 16000
    system_prompt_tokens: int = 1500
    max_history_tokens: int = 6000
    max_schema_tokens: int = 6000
    response_max_tokens: int = 2000


class APIConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000
    cors_origins: list[str] = Field(default_factory=lambda: ["*"])
    rate_limit_per_minute: int = 60


class ObservabilityConfig(BaseModel):
    log_level: str = "INFO"
    log_format: str = "json"
    enable_langsmith: bool = False
    langsmith_project: str = "autotext2sql"
    otlp_endpoint: str = ""


class CostConfig(BaseModel):
    per_task_limit_usd: float = 0.10
    daily_limit_usd: float = 5.00
    weekly_limit_usd: float = 20.00
    model_prices: dict[str, dict[str, float]] = Field(default_factory=dict)


class MemoryConfig(BaseModel):
    enabled: bool = False
    provider: str = "mem0"
    collection_name: str = "user_memory"
    embedding_dimensions: int = 1536
    search_top_k: int = 4
    max_memories: int = 4
    max_prompt_tokens: int = 1500
    write_enabled: bool = True


# ---------------------------------------------------------------------------
# Root settings
# ---------------------------------------------------------------------------


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AUTOTEXT2SQL_",
        env_nested_delimiter="__",
        env_file=".env",
        extra="ignore",
    )

    llm: LLMConfig = Field(default_factory=LLMConfig)
    databases: list[DatabaseConfig] = Field(default_factory=list)
    retriever: RetrieverConfig = Field(default_factory=RetrieverConfig)
    orchestrator: OrchestratorConfig = Field(default_factory=OrchestratorConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)
    api: APIConfig = Field(default_factory=APIConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    cost: CostConfig = Field(default_factory=CostConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)

    # Env-only secrets (not in YAML)
    llm_gateway_api_key: str = Field(default="", alias="LLM_GATEWAY_API_KEY")
    llm_gateway_base_url: str = Field(default="", alias="LLM_GATEWAY_BASE_URL")
    langsmith_api_key: str = Field(default="", alias="LANGSMITH_API_KEY")

    model_config = SettingsConfigDict(
        env_prefix="AUTOTEXT2SQL_",
        env_nested_delimiter="__",
        env_file=".env",
        extra="ignore",
        populate_by_name=True,
    )

    def db_by_name(self, name: str) -> DatabaseConfig | None:
        for db in self.databases:
            if db.name == name:
                return db
        return None


def _load_yaml(path: str) -> dict[str, Any]:
    if not os.path.exists(path) or not os.path.isfile(path):
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


def load_settings(config_path: str = "config.yaml") -> Settings:
    """Load settings from YAML file merged with environment variables."""
    yaml_data = _load_yaml(config_path)
    # Environment variables override YAML
    return Settings(**yaml_data)


# Module-level singleton (lazy)
_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings
