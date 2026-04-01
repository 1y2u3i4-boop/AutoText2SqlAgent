# Spec: Serving / Config

## Назначение

Конфигурация запуска системы, управление секретами, версиями моделей, параметрами модулей.

## Запуск

### Компоненты для запуска

| Компонент | Команда | Описание |
|-----------|---------|----------|
| **API Server** | Запуск ASGI-сервера приложения | FastAPI сервер, основная точка входа |
| **Indexing CLI** | Запуск процесса переиндексации | Построение/обновление индекса метаданных |
| **Health check** | HTTP-проверка готовности | Проверка готовности (Qdrant доступен, LLM Gateway reachable) |

### Порядок запуска

1. Проверить наличие конфигурации и секретов
2. Запустить indexing (--rebuild) для первоначального построения индекса
3. Запустить API Server
4. Проверить health endpoint

### Docker

Предполагаются два контейнера:
- контейнер приложения;
- контейнер Qdrant.

Схема запуска:
- Qdrant публикует порты 6333 и 6334;
- данные Qdrant сохраняются во внешнем volume;
- приложение зависит от Qdrant и получает URL Qdrant через переменную окружения AUTOTEXT2SQL_QDRANT_URL;
- для локальной разработки Qdrant удобно запускать в Docker, а приложение - напрямую.

Два контейнера: приложение + Qdrant. Для локальной разработки Qdrant запускается через Docker, приложение - напрямую.

## Конфигурация

### Структура конфига

Иерархическая конфигурация через Pydantic BaseSettings с поддержкой:
1. YAML файл (config.yaml)
2. Environment variables (override YAML)
3. .env файл (для секретов в dev)

Конфигурация организована как иерархическая структура с префиксом AUTOTEXT2SQL_ и вложенными секциями:
- llm;
- databases;
- retriever;
- orchestrator;
- context;
- api;
- observability;
- cost.

### Состав config.yaml

Секция llm должна содержать:
- gateway_url;
- default_route;
- route_overrides;
- fallback_enabled;
- embedding_model;
- embedding_device;
- temperature;
- max_tokens;
- timeout_seconds.

Секция databases должна содержать список источников с полями:
- name;
- url;
- readonly;
- query_timeout_seconds;
- pool_size.

Секция retriever должна содержать:
- top_k;
- top_n;
- confidence_threshold;
- rerank_enabled;
- enrichment_enabled;
- qdrant_url;
- qdrant_collection.

Секция orchestrator должна содержать:
- max_retries_per_node;
- backoff_base_seconds;
- human_approval_timeout_seconds;
- checkpointer_db.

Секция context должна содержать:
- max_total_tokens;
- system_prompt_tokens;
- max_history_tokens;
- max_schema_tokens;
- response_max_tokens.

Секция api должна содержать:
- host;
- port;
- cors_origins;
- rate_limit_per_minute.

Секция observability должна содержать:
- log_level;
- log_format;
- enable_langsmith;
- langsmith_project.

Секция cost должна содержать:
- per_task_limit_usd;
- daily_limit_usd;
- weekly_limit_usd;
- model_prices для используемой LLM.

## Секреты

### Переменные окружения

| Переменная | Описание | Обязательная |
|-----------|----------|-------------|
| LLM_GATEWAY_API_KEY | Ключ доступа к LLM Gateway | Да |
| LLM_GATEWAY_BASE_URL | Base URL LLM Gateway | Да |
| AUTOTEXT2SQL_QDRANT_URL | URL Qdrant (default: http://localhost:6333) | Нет (есть default) |
| AUTOTEXT2SQL_DB_PROD_URL | Connection string для prod_db | Да |
| AUTOTEXT2SQL_DB_ANALYTICS_URL | Connection string для analytics_db | Зависит от конфига |
| LANGSMITH_API_KEY | Ключ LangSmith (трейсинг) | Нет |

### Правила работы с секретами

- Секреты **никогда** не хранятся в конфиг-файлах или коде
- В config.yaml используется ${ENV_VAR} синтаксис для подстановки
- .env файл в .gitignore
- В production: секреты через переменные окружения контейнера

## Версии моделей

| Компонент | Назначение | Версия |
|-----------|-----------|-------------|
| llm_gateway route policy | NL analysis, reranking, response gen, SQL gen | Config-managed |
| local_embedding_model | Локальные embeddings метаданных и запросов | Config-managed |

### Политика обновления

- Используется конфигурируемая route policy gateway
- При переходе в prod: фиксируются route aliases и разрешённые provider/model pairs, обновление через explicit config change + eval run

## API Endpoints

| Endpoint | Method | Описание |
|----------|--------|----------|
| POST /api/v1/search | POST | NL-запрос → структурированный ответ (SSE stream) |
| POST /api/v1/search/{session_id}/approve | POST | Подтверждение SQL execution |
| GET /api/v1/sessions/{session_id} | GET | Состояние сессии |
| POST /api/v1/index/rebuild | POST | Перестроение индекса (admin) |
| GET /health | GET | Health check |
| GET /metrics | GET | Prometheus-совместимые метрики |

## Зависимости (Python packages)

| Пакет | Назначение |
|-------|-----------|
| langgraph | Agent orchestration |
| sentence-transformers | Локальная embedding-модель |
| transformers + torch | Локальная inference для embeddings |
| httpx | HTTP client для LLM Gateway |
| qdrant-client | Vector store client |
| sqlalchemy | DB introspection и query execution |
| fastapi + uvicorn | API server |
| pydantic + pydantic-settings | Data models и config |
| sqlglot | SQL parsing и validation |
| tiktoken | Token counting |
| structlog | Structured logging |
| opentelemetry-* | Distributed tracing |
| python-dotenv | .env loading |
