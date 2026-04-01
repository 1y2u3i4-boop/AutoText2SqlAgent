# Spec: Tools / APIs

## Назначение

Слой адаптеров для всех внешних зависимостей системы. Каждый внешний вызов оборачивается в адаптер с единым интерфейсом, обработкой ошибок, timeout и cost tracking.

## Общий контракт

Все tool-адаптеры возвращают ToolResult:

| Поле | Тип | Описание |
|------|-----|----------|
| success | bool | Успешно ли выполнен вызов |
| data | any \| null | Полезная нагрузка результата |
| error | string \| null | Текст ошибки |
| latency_ms | float | Время выполнения |
| cost_usd | float \| null | Стоимость вызова, если применимо |
| retries_used | integer | Сколько повторов было выполнено |

## Реестр инструментов

### 1. LLM Gateway

| Параметр | Значение |
|----------|----------|
| **Роль** | Model selection, OpenAI-compatible calls, schema parsing, provider fallback |
| **Маршрутизация** | По task type, latency budget, availability и policy |
| **Primary route** | Конфигурируется в gateway |
| **Fallback route** | Конфигурируется в gateway |
| **Timeout** | 30s |
| **Max retries** | 2 |
| **Backoff** | Exponential: 1s, 3s |
| **Rate limit handling** | Retry-After header → sleep |

#### Контракт вызова

| Поле запроса | Тип | Описание |
|--------------|-----|----------|
| messages | list | История сообщений для модели |
| route | string | Логическое имя маршрута в gateway |
| task_type | string | Тип задачи: analyze, rerank, respond, sql_generate |
| temperature | float | Температура генерации |
| max_tokens | integer | Максимальный размер ответа |
| response_format | object \| null | Описание ожидаемого структурированного формата |
| schema_name | string \| null | Имя ожидаемой схемы structured output |

| Поле ответа | Тип | Описание |
|-------------|-----|----------|
| content | string | Текстовый ответ модели |
| parsed | object \| null | Разобранный structured output |
| selected_provider | string | Провайдер, выбранный gateway |
| selected_model | string | Модель, выбранная gateway |
| route | string | Использованный логический маршрут |
| usage | object | Статистика токенов |
| latency_ms | float | Время вызова |
| cost_usd | float | Стоимость вызова |

#### Cost tracking

Для каждого вызова через gateway фиксируются:
- prompt_tokens;
- completion_tokens;
- total_tokens.

Стоимость рассчитывается по telemetry gateway: выбранный provider, выбранная model, входные и выходные токены, route и итоговая цена вызова.

#### Защиты
- Все промпты формируются из шаблонов (не raw string concatenation)
- System prompt отделён от user content (separate message roles)
- Response validation через Pydantic structured output и schema parsing в gateway
- Max tokens ограничен для предотвращения runaway generation
- Provider fallback срабатывает только для allowlisted маршрутов

### 2. Database Tool (SQLAlchemy)

| Параметр | Значение |
|----------|----------|
| **Driver** | psycopg2 / pymysql / pyodbc (по типу БД) |
| **Connection mode** | Read-only (execution_options={"postgresql_readonly": True}, либо read-only user) |
| **Query timeout** | 10s |
| **Max retries** | 1 |
| **Connection pool** | pool_size=5, max_overflow=2 per database |
| **Row limit** | LIMIT 100 принудительно добавляется к каждому SELECT |

#### Контракт вызова

| Поле запроса | Тип | Описание |
|--------------|-----|----------|
| db_name | string | Целевая БД |
| sql | string | SQL-запрос |
| params | dict \| null | Параметры запроса |
| timeout_seconds | integer | Лимит времени выполнения |

| Поле ответа | Тип | Описание |
|-------------|-----|----------|
| columns | list[string] | Названия колонок |
| rows | list[dict] | Результирующие строки |
| row_count | integer | Количество строк |
| truncated | bool | Признак, что ответ был усечён LIMIT |
| latency_ms | float | Время выполнения |

#### Контракт интроспекции

| Поле | Тип | Описание |
|------|-----|----------|
| db_name | string | Имя БД для интроспекции |
| schemas | list | Список схем с таблицами |
| tables_count | integer | Общее число найденных таблиц |
| latency_ms | float | Время выполнения интроспекции |

Для каждой схемы хранятся:
- имя схемы;
- список таблиц.

Для каждой таблицы хранятся:
- имя таблицы;
- колонки;
- primary keys;
- foreign keys;
- описание.

Для каждой колонки хранятся:
- имя;
- тип;
- nullable;
- default;
- описание.

Для каждого внешнего ключа хранятся:
- колонка-источник;
- целевая таблица;
- целевая колонка;
- целевая схема, если есть.

#### Защиты
- Read-only connection на уровне драйвера / DB user
- SQL парсинг через sqlglot перед выполнением: только SELECT / WITH допускаются
- Принудительный LIMIT 100
- Statement timeout на уровне connection
- Никакого dynamic SQL или parameter interpolation в строку (только parameterized queries)

### 3. Local Embedding Tool

| Параметр | Значение |
|----------|----------|
| **Provider** | local embedding runtime |
| **Модель** | Конфигурируемая локальная embedding-модель |
| **Inference mode** | in-process через sentence-transformers / FlagEmbedding |
| **Vector size** | Зависит от выбранной модели |
| **Context length** | Зависит от выбранной модели |
| **Timeout** | 5s на batch |
| **Max retries** | 1 |

#### Контракт вызова

| Поле запроса | Тип | Описание |
|--------------|-----|----------|
| texts | list[string] | Набор текстов для векторизации |
| batch_size | integer | Размер батча |
| normalize | bool | Нормализовать ли векторы |

| Поле ответа | Тип | Описание |
|-------------|-----|----------|
| vectors | list[list[float]] | Построенные embedding-векторы |
| model | string | Имя локальной модели |
| latency_ms | float | Время inference |

#### Защиты и ограничения
- Batch size ограничивается локальной памятью и доступной VRAM
- При ошибке GPU inference допускается fallback на CPU
- Никакие данные не отправляются во внешний сервис

### 4. Vector Store Tool (Qdrant)

| Параметр | Значение |
|----------|----------|
| **Mode** | Client-server (Docker container) / in-memory для тестов |
| **URL** | http://localhost:6333 (configurable) |
| **Collection** | metadata_index |
| **Distance** | Cosine |
| **Vector size** | Должен совпадать с размерностью выбранной embedding-модели |
| **Timeout** | 5s |

#### Контракт вызова

| Поле запроса | Тип | Описание |
|--------------|-----|----------|
| query_embedding | list[float] | Вектор запроса |
| limit | integer | Число результатов |
| query_filter | dict \| null | Фильтр по payload |

| Поле ответа | Тип | Описание |
|-------------|-----|----------|
| ids | list[string] | Идентификаторы найденных документов |
| payloads | list[dict] | Метаданные найденных документов |
| scores | list[float] | Оценки релевантности |
| latency_ms | float | Время поиска |

#### Side effects
- search() - только чтение
- upsert() - только в indexing pipeline (offline)
- delete_collection() / recreate_collection() - только при re-index (offline)

## Общие правила для всех tools

### Timeout и ошибки

| Tool | Timeout | Retries | Ошибка при исчерпании |
|------|---------|---------|----------------------|
| LLM Gateway | 30s | 2 | ToolResult(success=False, error="LLM gateway unavailable") |
| DB Query | 10s | 1 | ToolResult(success=False, error="DB timeout") |
| DB Introspection | 60s | 1 | ToolResult(success=False, error="DB introspection failed") |
| Embedding | 5s | 1 | ToolResult(success=False, error="Embedding failed") |
| Qdrant | 5s | 1 | ToolResult(success=False, error="Vector search failed") |

### Логирование

Каждый вызов tool логирует:
- tool_name, operation
- latency_ms
- success: bool
- error (если есть)
- cost_usd (для вызовов через gateway)
- selected_provider, selected_model, route
- retries_used

### Side Effects

| Tool | Read | Write | Side effects |
|------|------|-------|-------------|
| LLM Gateway | Да (generation) | Нет | Нет (stateless gateway API) |
| DB Query | Да | Нет | Нет (read-only) |
| DB Introspection | Да | Нет | Нет |
| Embedding | Да | Нет | Локальная inference-нагрузка на CPU/GPU |
| Qdrant Query | Да | Нет | Нет |
| Qdrant Index | Нет | Да | Изменение индекса (только offline) |
