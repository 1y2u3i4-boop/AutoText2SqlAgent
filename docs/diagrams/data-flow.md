# Data Flow Diagram: AutoText2SQL Agent

Как данные проходят через систему: что создаётся, что хранится, что логируется.

## 1) Indexing Pipeline (offline)

```mermaid
flowchart LR
    subgraph Sources["Источники"]
        DB1[(БД 1)]
        DB2[(БД 2)]
        DBn[(БД N)]
    end

    subgraph Introspection["Metadata Introspector"]
        INSPECT["SQLAlchemy Inspector<br/>tables, columns, keys,<br/>foreign keys, comments"]
        NORMALIZE["Normalizer<br/>унификация формата,<br/>обогащение описаниями"]
        CHUNK["Chunker<br/>1 doc = 1 table +<br/>all columns + relations"]
    end

    subgraph Embedding["Embedding"]
        EMBED["Local Embedding Model<br/>configurable"]
    end

    subgraph Storage["Хранилище"]
        QDRANT[(Qdrant<br/>vectors + payload)]
    end

    subgraph Logs["Логи"]
        IDX_LOG["index_log<br/>timestamp, db, tables_count,<br/>duration, errors"]
    end

    DB1 --> INSPECT
    DB2 --> INSPECT
    DBn --> INSPECT
    INSPECT --> NORMALIZE
    NORMALIZE --> CHUNK
    CHUNK -->|"text documents"| EMBED
    EMBED -->|"vectors + payload"| QDRANT
    INSPECT -.->|"stats"| IDX_LOG
    EMBED -.->|"token count, cost"| IDX_LOG
```

### Что хранится в Qdrant

| Поле | Тип | Пример |
|------|-----|--------|
| id | string | prod_db.public.orders |
| document | text | "Table: orders. Schema: public. DB: prod_db. Columns: id (int, PK), user_id (int, FK→users.id), status (varchar), created_at (timestamp). Описание: Заказы пользователей." |
| embedding | float[n] | - |
| metadata.db_name | string | prod_db |
| metadata.schema_name | string | public |
| metadata.table_name | string | orders |
| metadata.object_type | string | table |
| metadata.column_names | string | id,user_id,status,created_at |
| metadata.has_description | bool | true |

## 2) Query Pipeline (online)

```mermaid
flowchart TD
    subgraph Input["Вход"]
        USER_Q["NL-запрос пользователя"]
    end

    subgraph Processing["Обработка"]
        SANITIZE["Input Guard<br/>sanitization +<br/>injection check"]
        ANALYZE["Query Analyzer<br/>через LLM Gateway<br/>→ intent, entities, db_hint"]
        EMBED_Q["Embed Query<br/>local embedding model"]
        SEARCH["Qdrant Search<br/>cosine similarity<br/>top-k=20 + payload filter"]
        RERANK["LLM Reranker<br/>через gateway<br/>top-20 → top-5"]
        ENRICH["Enrichment<br/>FK + descriptions +<br/>related metadata"]
    end

    subgraph Context["Сборка контекста"]
        CTX["Сборка контекста для LLM<br/>system prompt +<br/>DB context +<br/>session history +<br/>current query"]
    end

    subgraph Generation["Генерация"]
        GATEWAY["LLM Gateway<br/>model selection + schema parsing + fallback"]
        RESP_GEN["Response Generator<br/>structured answer"]
        SQL_GEN["SQL Generator<br/>SELECT query"]
        SQL_VAL["SQL Validator<br/>sqlglot AST check"]
    end

    subgraph Output["Выход"]
        RESPONSE["Structured Response<br/>path + explanation +<br/>SQL query"]
    end

    USER_Q --> SANITIZE
    SANITIZE --> ANALYZE
    ANALYZE -->|entities| EMBED_Q
    EMBED_Q --> SEARCH
    ANALYZE -->|db_hint| SEARCH
    SEARCH --> RERANK
    RERANK --> ENRICH
    ENRICH --> CTX
    CTX --> GATEWAY
    GATEWAY --> RESP_GEN
    RESP_GEN --> SQL_GEN
    SQL_GEN --> SQL_VAL
    SQL_VAL --> RESPONSE
```

## 3) Что логируется

```mermaid
flowchart LR
    subgraph Events["События (structlog)"]
        E1["Lifecycle events<br/>request start / finish"]
        E2["Validation events<br/>guardrails + policy"]
        E3["Retrieval events<br/>scores, latency, confidence"]
        E4["Generation events<br/>tokens, latency, cost"]
        E5["Execution events<br/>SQL execution status"]
        E6["Error and fallback events<br/>retry, rejection, provider fallback"]
    end

    subgraph Stored["Хранение"]
        STDOUT["stdout / файл<br/>(structured JSON)"]
        OTEL["OpenTelemetry<br/>traces + spans"]
    end

    Events --> STDOUT
    Events --> OTEL
```

### Таблица логируемых данных

| Что | Логируется | НЕ логируется |
|-----|-----------|---------------|
| NL-запрос пользователя | ✅ Полный текст | - |
| Сгенерированный SQL | ✅ Полный текст + hash | - |
| Список затронутых объектов БД | ✅ db/schema/table | - |
| Результаты SQL (данные из БД) | ⚠️ Только row count | ❌ Содержимое строк |
| Метрики (latency, cost, tokens) | ✅ По каждому шагу | - |
| Ошибки и причины отказа | ✅ Полный стек-трейс | - |
| Оценки релевантности | ✅ Scores для top-k | - |
| PII / персональные данные | - | ❌ Не обрабатываются (тестовые стенды) |

## 4) Жизненный цикл данных

| Данные | Создание | Хранение | TTL | Обновление |
|--------|----------|----------|-----|------------|
| Метаданные БД (raw) | Introspection | Только в памяти во время индексации | - | При re-index |
| Embeddings + payload | Indexing pipeline | Qdrant (Docker volume) | Бессрочно | Полная переиндексация |
| Session state | Первый запрос в сессии | SQLite (LangGraph checkpointer) | До перезапуска сервера | Каждый шаг графа |
| Логи | Каждое событие | stdout / файл | 30 дней (ротация) | Append-only |
| Traces | Каждый LLM-вызов | OpenTelemetry → LangSmith | 7 дней (LangSmith free tier) | Append-only |
| Cost counters | Каждый LLM-вызов | In-memory + периодический flush в файл | Daily/weekly reset | Инкремент |
