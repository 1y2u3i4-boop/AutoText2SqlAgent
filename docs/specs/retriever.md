# Spec: Retriever

## Назначение

Модуль семантического поиска по индексу метаданных БД. Принимает извлечённые из запроса сущности и возвращает ранжированный список релевантных объектов (таблиц, колонок, связей).

## Источники данных

| Источник | Тип | Что содержит |
|----------|-----|--------------|
| Qdrant | Vector store | Эмбеддинги метаданных: по одному документу на таблицу (с колонками, ключами, FK, описаниями) |
| Metadata cache / index payload | Internal metadata layer | Описания, FK-связи и дополнительные metadata для enrichment top-N результатов |

## Indexing Pipeline

### Шаги построения индекса

1. **Introspection**: SQLAlchemy Inspector извлекает из каждой сконфигурированной БД:
   - Список схем и таблиц
   - Колонки: имя, тип, nullable, default, comment
   - Primary keys, unique constraints
   - Foreign keys (source → target table.column)
   - Table comments / descriptions

2. **Normalization**: приведение к единому формату документа таблицы.
   - Для каждой таблицы фиксируются:
     - имя БД;
     - имя схемы;
     - имя таблицы;
     - список колонок;
     - список primary key;
     - список foreign key;
     - текстовое описание таблицы;
     - итоговое текстовое представление для embedding.

3. **Chunking**: 1 документ = 1 таблица.
   - В текст документа включаются:
     - имя БД;
     - схема;
     - таблица;
     - описание;
     - список колонок с типами и nullable;
     - primary key;
     - foreign key и целевые связи.

4. **Embedding**: внешняя embedding-модель через OpenAI-compatible API. Batch embedding для эффективности.

5. **Storage**: запись в Qdrant collection metadata_index с payload fields:
   - db_name, schema_name, table_name, object_type, column_names, has_description

### Триггеры обновления индекса
- Ручной запуск процесса переиндексации через CLI-команду приложения
- Полная переиндексация (не инкрементальная)
- Обновление схемы контролируется через hash сравнение структуры источников

## Search Pipeline

### Шаги поиска

1. **Query embedding**: та же внешняя embedding-модель для запроса
2. **Vector search**: Qdrant search() с параметрами:
   - limit: 20 (configurable)
   - query_filter: payload filter (если из Query Analyzer пришёл db_hint)
   - Distance: cosine similarity
3. **Reranking** (LLM-based):
   - Вход: top-20 кандидатов + оригинальный запрос
   - Промпт: "Оцени релевантность каждого объекта БД к запросу по шкале 0-10"
   - LLM: через LLM Gateway, structured output
   - Выход: top-5 с оценками
4. **Enrichment** (для top-5):
   - Загрузка FK-графа на 1 hop (связанные таблицы)
   - Подтягивание описаний и связанных metadata
   - Формирование расширенного контекста без чтения строк данных из БД

### Параметры (конфигурируемые)

| Параметр | Default | Описание |
|----------|---------|----------|
| retriever.top_k | 20 | Количество кандидатов из vector search |
| retriever.top_n | 5 | Количество результатов после reranking |
| retriever.confidence_threshold | 0.6 | Минимальный confidence для продолжения без clarification |
| retriever.rerank_enabled | true | Включить/выключить LLM reranking |
| retriever.enrichment_enabled | true | Включить/выключить metadata enrichment |
| retriever.embedding_model | configurable | Внешняя модель для embeddings через OpenAI-compatible API |

## Контракты

### Вход

| Поле | Тип | Описание |
|------|-----|----------|
| query | string | Оригинальный запрос пользователя |
| entities | list[string] | Сущности, извлечённые из запроса |
| db_hint | string \| null | Необязательная подсказка по БД |
| top_k | integer | Число кандидатов для vector search |
| top_n | integer | Число объектов после reranking |

### Выход

| Поле | Тип | Описание |
|------|-----|----------|
| objects | list[RankedDBObject] | Ранжированный список найденных объектов |
| confidence | float | Максимальный или агрегированный confidence по top-N |
| latency_ms | float | Время выполнения retrieval |
| cost_usd | float | Стоимость LLM-этапов retrieval, если они использовались |

| Поле RankedDBObject | Тип | Описание |
|---------------------|-----|----------|
| db_name | string | Имя БД |
| schema_name | string | Имя схемы |
| table_name | string | Имя таблицы |
| columns | list | Список колонок таблицы |
| foreign_keys | list | Список внешних ключей |
| relevance_score | float | Оценка релевантности |
| enrichment_data | object \| null | Дополнительные metadata: связанные таблицы, описания, FK-контекст |
| explanation | string | Краткое объяснение, почему объект релевантен |

## Ограничения

| Ограничение | Значение | Причина |
|-------------|----------|---------|
| Max документов в индексе | ~100K | Достаточно для текущего масштаба (Qdrant поддерживает значительно больше) |
| Embedding batch size | 32 | Ограничение по размеру батча и latency budget |
| Reranking latency | ≤ 2s | Budget из общего p95 target 8s |
| Vector search latency | ≤ 500ms | Qdrant single-node |
| Enrichment volume | ≤ 5 объектов (по числу top-N) | Ограничение размера контекста и latency budget |

## Failure Modes

| Сбой | Обнаружение | Fallback |
|------|-------------|----------|
| Qdrant unavailable | ConnectionError | Hard stop, ошибка пользователю |
| Embedding API failure | HTTPError / Timeout | Retry по HTTP client policy → hard stop |
| Reranking LLM timeout | HTTPError / Timeout | Продолжить без reranking (vector scores only) |
| Enrichment metadata unavailable | Missing payload / cache miss | Продолжить без enrichment |
| Пустой результат поиска | len(objects) == 0 | Clarification request к пользователю |
