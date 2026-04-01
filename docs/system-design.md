# System Design: AutoText2SQL Agent

## 1) Ключевые архитектурные решения

| # | Решение | Обоснование | Альтернативы и причины отказа |
|---|---------|-------------|-------------------------------|
| 1 | **LangGraph** как оркестратор агентного контура | Граф-ориентированная модель даёт явные переходы между шагами, встроенную поддержку state, retry и human-in-the-loop. Легко визуализируется и тестируется по узлам. | LangChain (менее явный control flow, breaking changes между версиями), самописный orchestrator (высокая стоимость поддержки) |
| 2 | **Qdrant** как векторное хранилище метаданных | Полноценный vector DB с rich filtering, gRPC/REST API, поддержкой payload-индексов. Запускается как Docker-контейнер. | ChromaDB (ограниченная фильтрация, single-node), Weaviate (тяжелее, избыточен для текущего объёма) |
| 3 | **LLM Gateway** как единая точка доступа к моделям | Gateway инкапсулирует выбор модели, OpenAI-compatible вызовы, schema parsing, structured completions, provider fallback и provider telemetry. Это снимает жёсткую привязку к одному провайдеру и позволяет менять маршрутизацию без переписывания агентного контура. | Прямые вызовы конкретной модели из оркестратора (сильная связность, сложная миграция), отдельная интеграция под каждого провайдера (рост сложности и дублирование логики) |
| 4 | **SQLAlchemy + Inspector** для интроспекции БД | Стандартный инструмент для работы с метаданными реляционных БД в Python. Поддерживает множество диалектов через драйверы. | Прямые SQL-запросы к information_schema (менее портабельно) |
| 5 | **FastAPI** как serving layer | Асинхронный, с автогенерацией OpenAPI-схемы, нативная поддержка Pydantic-моделей, SSE для streaming. | Flask (синхронный), gRPC (избыточен для текущей архитектуры) |
| 6 | **Read-only соединения** с целевыми БД | Исключает риск модификации данных на архитектурном уровне. | Row-level security (сложнее настроить, не исключает DML) |
| 7 | **Human-in-the-loop** для исполнения SQL | Любой SQL выполняется только после явного подтверждения пользователем. Реализуется через interrupt-узел в LangGraph. | Автоматическое исполнение с whitelist (риск пропуска опасных запросов) |
| 8 | **Pydantic v2** для контрактов данных и TypedDict для состояния графа | Pydantic подходит для внешних контрактов и строгой валидации, а TypedDict хорошо соответствует mutable state внутри LangGraph. | Только dataclasses (нет встроенной валидации), только TypedDict (слабая валидация внешних границ) |


## 2) Модули и их роли


| Модуль | Роль | Входы | Выходы |
|--------|------|-------|--------|
| **Metadata Introspector** | Подключение к БД, сбор схем/таблиц/колонок/ключей/связей, нормализация, построение эмбеддингов, запись в Qdrant | Конфигурация БД-источников | Индекс метаданных в Qdrant |
| **Retriever** | Семантический поиск по индексу метаданных, reranking результатов | Извлечённые сущности из запроса | Ранжированный список объектов БД |
| **Search Agent (Orchestrator)** | LangGraph-граф: координация шагов от входного запроса до финального ответа | запрос пользователя на естественном языке | Структурированный ответ + объяснение |
| **LLM Gateway** | Выбор модели, вызов провайдера, structured completions, schema parsing, fallback и telemetry | Запросы от оркестратора и tool layer | Нормализованный ответ модели и техническая телеметрия |
| **SQL Generator** | Формирование read-only SQL-запроса по найденным объектам | Объекты БД из retriever, контекст запроса | SQL-запрос (SELECT) |
| **Guardrails & Policy** | Валидация входа/выхода, фильтрация injection, проверка SQL-безопасности, контроль бюджета | Любые данные на границах модулей | pass / reject + причина |
| **Response Formatter** | Генерация структурированного ответа с путём поиска и объяснением | Результаты retriever + SQL + контекст | Финальный ответ пользователю |
| **API Layer (FastAPI)** | HTTP-интерфейс, SSE-стриминг, аутентификация, rate limiting | HTTP-запросы | HTTP-ответы |
| **Observability & Evals** | Сбор метрик, трейсов, логов; оценка качества | Телеметрия из всех модулей | Дашборды, алерты |
| **Config & Secrets** | Конфигурация модулей, управление секретами, версии моделей | Env vars, config files | Типизированные конфиги |

`SQL Generator`, `Response Formatter`, части `Guardrails & Policy`, а также cost/state control реализуются как внутренние части `Search Agent (Orchestrator)`, а не как самостоятельные сервисы.

## 3) Основной workflow выполнения запроса

Workflow реализован как **LangGraph StateGraph** с явными переходами. Основной сценарий выполнения запроса выглядит так:

1. Пользователь отправляет запрос на естественном языке.
   1. Запрос поступает в API layer и передаётся в граф оркестрации.
   2. Для запроса создаётся или восстанавливается session state.

2. Срабатывает Input Guard.
   1. Проверяется формат входа, длина запроса и базовые паттерны небезопасного ввода.
   2. Если вход не проходит проверку, граф завершается с Error Response.
   3. Если вход валиден, запрос передаётся на анализ.

3. Выполняется Query Analyzer.
   1. Вызов через LLM Gateway извлекает intent, сущности и возможные подсказки по БД или схеме.
   2. Результат сохраняется в state как основа для retrieval.

4. Запускается Retriever.
   1. Для запроса строится локальный embedding.
   2. Выполняется vector search в Qdrant.
   3. При необходимости применяется reranking через LLM Gateway.

5. Выполняется Relevance Check.
   1. Система оценивает confidence найденных результатов.
   2. Если confidence ниже порога, пользователю возвращается Clarification Request.
   3. Если confidence достаточен, граф продолжает выполнение.

6. Формируется основной ответ в Response Generator.
   1. LLM Gateway возвращает структурированное объяснение найденных объектов.
   2. В ответ включается путь поиска: БД → схема → таблица → поля → связи.
   3. Одновременно подготавливается контекст для обязательной генерации SQL-запроса.

7. Запускается SQL Generator.
   1. Генерируется read-only SQL-запрос на основе найденных объектов и контекста запроса.
   2. Сгенерированный SQL передаётся в SQL Validator.

8. Выполняется SQL Validator.
   1. Проверяется, что запрос соответствует policy: только допустимые read-only конструкции.
   2. Если SQL не проходит проверку, ветка SQL обрывается, а основной ответ всё равно может быть возвращён пользователю без выполнения SQL.
   3. Если SQL проходит проверку, граф переходит к этапу подтверждения.

9. Срабатывает Human Approval.
   1. Пользователю показывается SQL-запрос и краткое объяснение его назначения.
   2. Без явного подтверждения пользователя SQL не исполняется.

10. При подтверждении запускается SQL Executor.
   1. SQL выполняется через read-only соединение.
   2. Результат запроса добавляется в итоговый контекст ответа.

11. Срабатывает Output Guard.
   1. Проверяется валидность финальной структуры ответа.
   2. При необходимости применяются дополнительные ограничения на длину и формат ответа.

12. Пользователь получает Final Response.
   1. Ответ содержит структурированный результат поиска.
   2. Ответ всегда содержит SQL-запрос для валидации человеком.


## 4) State / Memory / Context Handling

### Agent State (per-request)

Per-request state передаётся между узлами LangGraph. В нём хранятся входные данные запроса, идентификатор сессии, результаты анализа намерения и извлечения сущностей, найденные объекты БД и их оценки релевантности, промежуточные результаты генерации ответа и SQL-запроса, флаги управления flow, история сообщений, а также технические поля для ошибок, стоимости шагов и общей telemetry. Внешние контракты при этом валидируются через Pydantic.

### Session Memory

- **Краткосрочная память (in-session):** в рамках одной сессии система хранит историю сообщений, промежуточные результаты поиска и служебные данные, нужные для продолжения workflow.
- **Персистентность:** session state сохраняется через checkpointer и восстанавливается между шагами графа.
- **Context budget:** для одного LLM-вызова используется фиксированный лимит контекста - default 16K tokens. Это ограничивает latency и cost.
- **Политика усечения:** если контекст превышает лимит, сначала сокращается история сообщений, затем объём schema context. Системные инструкции и текущий запрос сохраняются.

### Сборка контекста для LLM

В LLM-вызов попадает не весь session state, а только отобранная часть контекста:

```
[System Prompt]           - роль, правила, ограничения
[DB Schema Context]       - релевантные метаданные из retriever
[Session History]         - последние N сообщений (в пределах бюджета)
[Current Query]           - текущий запрос пользователя
```

## 5) Retrieval-контур

### Этапы

1. **Indexing pipeline (offline)**
   - Интроспекция БД через SQLAlchemy Inspector.
   - Формирование документов: один документ на одну таблицу со списком колонок, связей и описаний.
   - Построение embeddings через локальную embedding-модель.
   - Запись документов и payload-метаданных в Qdrant.

2. **Online retrieval pipeline**
   - Построение embedding пользовательского запроса той же embedding-моделью.
   - Vector search в Qdrant с `top-k = 20` и payload filtering, если пользователь указал конкретную БД или схему.
   - Conditional reranking через LLM Gateway для сужения результатов до `top-n = 5`.
   - Если reranking недоступен, система использует исходные vector scores.

3. **Context enrichment**
   - Для отобранных объектов подгружаются дополнительные metadata: связанные таблицы, foreign keys и описания.
   - Результатом этапа является контекст для генерации ответа и SQL-запроса.
   - Выполнение SQL для чтения строк данных на этом этапе не требуется.

### Индекс

| Параметр | Значение |
|----------|----------|
| Embedding model | Локальная embedding-модель (конфигурируется) |
| Chunk strategy | 1 документ = 1 таблица со всеми колонками и связями |
| Retrieval | top-k = 20, top-n = 5 после reranking |
| Payload fields | db_name, schema, table_name, object_type, column_names |
| Distance metric | Cosine similarity |
| Обновление | Полная переиндексация по явному запуску indexing job; плановый запуск допускается через scheduler |

## 6) Tool/API-интеграции

| Инструмент | Назначение | Протокол | Ограничения |
|------------|------------|----------|-------------|
| **Target DBs** (PostgreSQL/MySQL/MSSQL) | Источник метаданных и исполнение read-only SQL | SQLAlchemy + DB driver | Read-only connection, query timeout 10s |
| **LLM Gateway** | Model selection, OpenAI-compatible calls, schema parsing, provider fallback, structured completions, provider telemetry | HTTPS REST | Timeout 30s, policy routing, provider-specific limits |
| **Qdrant** | Векторное хранилище метаданных | REST / gRPC (qdrant-client) | Docker-контейнер, single node |
| **Local Embedding Model** | Локальное построение embeddings для метаданных и запросов | in-process Python inference | Требует CPU/GPU и локальную память |
| **LangSmith** | Трейсинг и отладка LLM-вызовов | HTTPS REST | Только для dev/staging |

### Контракты вызовов

Каждый внешний вызов оборачивается в адаптер с единым интерфейсом:

```python
class ToolResult(BaseModel):
    success: bool
    data: Any | None
    error: str | None
    latency_ms: float
    cost_usd: float | None
```

## 7) Failure Modes, Fallback & Guardrails

### Failure Modes

| Сбой | Вероятность | Обнаружение | Реакция |
|------|-------------|-------------|---------|
| LLM Gateway недоступен / timeout | Средняя | HTTP error / timeout | Retry 2x с exponential backoff → fallback на keyword search без LLM |
| Gateway вернул невалидный structured output | Средняя | Pydantic validation failure | Retry 1x с более строгой schema parsing policy → ошибка пользователю |
| Retriever возвращает нерелевантные результаты | Средняя | Низкий confidence score | Запрос уточнения у пользователя |
| Целевая БД недоступна | Низкая | Connection error | Сообщение об ошибке, работа с остальными БД |
| Injection-атака через пользовательский ввод | Средняя | Pattern matching + LLM classifier | Блокировка запроса, логирование инцидента |
| Превышение cost budget | Низкая | Счётчик cost в state | Остановка, уведомление пользователя |
| Qdrant недоступен / corrupted | Низкая | Connection error / Read error | Restart контейнера, re-index из источников |

### Guardrails

1. **Input guardrail:**
   - Длина запроса ≤ 2000 символов
   - Regex-фильтр SQL injection patterns (DROP, DELETE, UPDATE, INSERT, ALTER, EXEC, --, ;)
   - LLM-классификатор промпт-инъекций (binary: safe/unsafe)

2. **SQL guardrail:**
   - Whitelist: только SELECT, WITH (CTE)
   - Blacklist: DDL, DML, EXEC, xp_, dynamic SQL
   - Read-only connection на уровне драйвера

3. **Output guardrail:**
   - Проверка структуры ответа (Pydantic)


4. **Cost guardrail:**
   - Per-step cost tracking (токены × цена модели)
   - Per-task лимит (default: $0.10)
   - Daily лимит (default: $5.00)
   - Weekly лимит (default: $20.00)

### Retry Policy

| Операция | Max retries | Backoff | Timeout |
|----------|-------------|---------|---------|
| LLM Gateway call | 2 | Exponential (1s, 3s) | 30s |
| DB query | 1 | - | 10s |
| Embedding | 1 | - | 5s |
| Vector search | 1 | - | 5s |

## 8) Технические и операционные ограничения

### Latency Budget (p95 target: ≤ 8s)

| Этап | Budget |
|------|--------|
| Input validation | ≤ 50ms |
| Query analysis (LLM) | ≤ 2s |
| Vector search + rerank (LLM) | ≤ 3s |
| Enrichment (DB queries) | ≤ 1s |
| Response generation (LLM) | ≤ 2s |
| **Total** | **≤ 8s** |

### Cost Budget

| Лимит | Значение |
|-------|----------|
| Per-task | ≤ $0.10 |
| Daily | ≤ $5.00 |
| Weekly | ≤ $20.00 |
| LLM calls per task | ≤ 5 (query analysis + rerank + response + SQL gen + retry) |

### Reliability

| Метрика | Цель |
|---------|------|
| Error rate | ≤ 5% |
| Availability | Best-effort, single instance |
| Data durability | Qdrant backed by Docker volume; re-indexable from sources |
| Recovery | Manual restart; индекс Qdrant сохраняется, session state хранится в локальном SQLite без гарантий HA/failover |

### Масштаб системы

| Параметр | Ограничение |
|----------|-------------|
| Количество БД-источников | ≤ 100 |
| Общее количество таблиц | ≤ 5000 |
| Параллельные пользователи | ≤ 5 (single instance) |
| Размер индекса Qdrant | ≤ 100K documents |

## 9) Валидация качества агента

Качество системы должно проверяться не только через runtime-метрики, но и через регулярный eval-контур.

Подход к валидации:
- собственный eval dataset на доменных вопросах проекта;
- отдельный holdout-набор для regression testing после изменений в retrieval, prompt policy, routing и SQL generation;
- сценарии, вдохновлённые `BIRD` и `BIRD-Interact`, для проверки text-to-SQL на многошаговых и неоднозначных запросах.

Что именно проверяется:
- корректность найденных объектов БД;
- корректность и исполнимость SQL-запроса;
- устойчивость к сложным формулировкам, alias, implicit filters и multi-hop join;
- latency и cost на фиксированном eval-наборе.

`BIRD`-подобные сценарии используются как ориентир по сложности, а не как единственный критерий качества: для проекта важнее сочетать общие text-to-SQL кейсы с внутренними задачами по реальным схемам данных.