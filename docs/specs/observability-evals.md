# Spec: Observability / Evals

## Назначение

Определение метрик, логов, трейсов и процедур оценки качества для мониторинга системы и контроля качества агента.

## Observability Stack

| Компонент | Инструмент | Назначение |
|-----------|-----------|------------|
| Structured Logging | structlog | JSON-логи с контекстом (session_id, step, latency) |
| Distributed Tracing | OpenTelemetry → stdout/LangSmith | Span-трейсы по шагам графа |
| Metrics | In-process counters + /metrics endpoint | Latency, error rate, cost, retrieval quality |
| LLM Tracing | LangSmith | Детальные трейсы промптов и ответов LLM |

## Метрики

### Продуктовые

| Метрика | Тип | Как считаем | Цель |
|---------|-----|-------------|----------|
| search.success_rate | Ratio | Запросы с полезным ответом / все запросы | ≥ 75% |
| search.mean_time_seconds | Gauge | Среднее время от запроса до ответа | ≤ 20s |
| search.clarification_rate | Ratio | Запросы с уточнением / все запросы | Мониторинг (нет таргета) |

### Агентские (качество)

| Метрика | Тип | Как считаем | Цель |
|---------|-----|-------------|----------|
| retrieval.top3_accuracy | Ratio | % запросов, где нужный объект в top-3 | ≥ 80% |
| retrieval.mrr | Gauge | Mean Reciprocal Rank | Мониторинг |
| response.correctness_rate | Ratio | % корректных NL-ответов (eval dataset) | ≥ 70% |
| sql.validation_pass_rate | Ratio | % SQL, прошедших policy-валидацию | Мониторинг |
| guardrail.injection_block_rate | Counter | Количество заблокированных injection-попыток | Мониторинг |

### Технические

| Метрика | Тип | Как считаем | Цель |
|---------|-----|-------------|----------|
| latency.e2e_p95 | Histogram | 95-й перцентиль E2E latency | ≤ 8s |
| latency.per_step | Histogram | Latency каждого узла графа | По бюджету (см. system-design) |
| error.rate | Ratio | % запросов с технической ошибкой | ≤ 5% |
| error.by_type | Counter | Ошибки по типам (LLM timeout, DB error, validation fail) | Мониторинг |
| llm.provider_fallback_rate | Ratio | Доля запросов, где gateway переключился на fallback provider | Мониторинг |
| llm.provider_error_rate | Ratio | Ошибки по downstream provider | Мониторинг |
| cost.per_task_usd | Histogram | Стоимость обработки одного запроса | ≤ $0.10 |
| cost.daily_usd | Gauge | Суммарная стоимость за день | ≤ $5.00 |
| cost.weekly_usd | Gauge | Суммарная стоимость за неделю | ≤ $20.00 |
| llm.tokens_total | Counter | Суммарное потребление токенов | Мониторинг |
| qdrant.index_size | Gauge | Количество документов в индексе | Мониторинг |

## Дашборды и графики

Для ежедневного мониторинга используются следующие дашборды:

| Дашборд | Графики | Зачем нужен |
|---------|------------------|-------------|
| Search Health | request rate, success_rate, clarification_rate, error.rate | Быстро понять, работает ли система и не выросла ли доля неуспешных ответов |
| Latency | e2e p50/p95, latency по шагам графа, latency LLM Gateway, latency Qdrant, latency SQL execution | Найти узкие места и деградацию по этапам |
| Retrieval Quality | top3_accuracy, mrr, confidence distribution, доля clarification | Видеть деградацию retrieval до того, как это заметят пользователи |
| LLM Gateway / Providers | provider_error_rate, provider_fallback_rate, latency по provider, доля запросов по route/provider | Понять, какой provider деградирует и как часто срабатывает fallback |
| SQL Safety | sql.validation_pass_rate, доля policy reject, доля human reject, sql execution failure rate | Контролировать качество SQL generation и безопасность выполнения |
| Cost | cost.per_task_usd, daily_usd, weekly_usd, tokens_total, cost breakdown по route/provider | Следить за бюджетом и дорогими маршрутами |
| Infrastructure | qdrant.index_size, health status gateway/Qdrant, error.by_type | Видеть инфраструктурные сбои и состояние зависимостей |

### Основные алерты

- рост `error.rate` выше рабочего порога;
- рост `latency.e2e_p95` выше целевого budget;
- рост `llm.provider_error_rate` для одного provider;
- резкий рост `llm.provider_fallback_rate`;
- падение `sql.validation_pass_rate`;
- превышение `cost.daily_usd`.

## Логи

### Формат

Логи пишутся в формате JSON structured logs через structlog.

### Логируемые события

| Категория | Что логируется |
|-----------|----------------|
| Lifecycle | Начало и завершение обработки запроса |
| Validation | Результаты input/output validation и policy checks |
| Retrieval | Метрики поиска, reranking и confidence |
| Generation | Факты генерации ответа и SQL, latency и token usage |
| Execution | Результат выполнения SQL без содержимого строк |
| Errors | Тип ошибки, шаг, причина, traceback |
| Budget / Gateway | Retry, fallback, budget status, provider routing |

При недоступности downstream provider в лог обязательно пишутся:
- route;
- failed_provider;
- fallback_provider, если переключение удалось;
- failure_reason;
- request outcome после fallback.

### Что НЕ логируется

- Результаты SQL (данные из БД) - только row count
- API ключи и секреты
- Полные промпты LLM в production (только в DEBUG / LangSmith)

## Traces

### OpenTelemetry Spans

Каждый запрос пользователя - один root span. Узлы графа - child spans.

Внутри root span создаются дочерние spans для ключевых этапов графа: validation, retrieval, LLM generation, SQL validation/execution, human approval и output validation. Отдельно трассируются вызовы к внешним инструментам и LLM Gateway.

### Span attributes

| Attribute | Тип | Описание |
|-----------|-----|----------|
| session.id | string | Идентификатор сессии |
| step.name | string | Имя шага графа |
| step.latency_ms | float | Длительность шага |
| llm.route | string | Route в LLM Gateway |
| llm.provider | string | Выбранный провайдер |
| llm.model | string | Выбранная модель |
| llm.tokens.prompt | int | Число входных токенов |
| llm.tokens.completion | int | Число выходных токенов |
| llm.cost_usd | float | Стоимость вызова |
| retrieval.top_k | int | Размер candidate set до reranking |
| retrieval.max_score | float | Максимальная оценка релевантности |
| error | bool | Признак ошибки |
| error.type | string | Тип ошибки |

## Evals

### Offline Evaluation

Для оценки качества агента используется eval dataset: набор пар (запрос, ожидаемые объекты БД). Набор строится из внутренних доменных сценариев и задач, вдохновлённых `BIRD` / `BIRD-Interact`, чтобы проверять не только lookup по схеме, но и полноценные text-to-SQL кейсы.

#### Eval dataset

Каждый элемент eval dataset должен содержать:
- query - текстовый вопрос пользователя;
- expected_objects - ожидаемые объекты БД;
- expected_intent - ожидаемый intent;
- tags - теги сценария, например basic или single_table.

Для SQL-ориентированных кейсов дополнительно фиксируются:
- expected_sql_pattern - ожидаемая структура запроса или ключевые SQL-компоненты;
- expected_execution_result - ожидаемый результат выполнения или инварианты результата;
- difficulty - уровень сложности, например simple, join, nested, ambiguous, interactive.

Для expected_objects обычно фиксируются:
- db;
- schema;
- table;
- columns.

#### Eval метрики

| Метрика | Формула | Цель |
|---------|---------|------|
| **Precision@3** | (релевантные в top-3) / 3 | ≥ 0.80 |
| **Recall@5** | (найденные ожидаемые) / (все ожидаемые) | ≥ 0.75 |
| **MRR** | 1 / rank первого релевантного | ≥ 0.70 |
| **NL Correctness** | LLM-as-judge: "ответ корректен?" (binary) | ≥ 0.70 |
| **SQL Validity** | sqlglot parse success + policy pass | ≥ 0.95 |
| **E2E Latency p95** | 95-й перцентиль | ≤ 8s |
| **Cost per query** | Среднее | ≤ $0.10 |

#### Процесс eval

1. Подготовить eval dataset (≥ 30 вопросов, покрывающих базовые и edge-кейсы)
   - включить в него отдельный срез `bird_like`, где есть join-heavy, ambiguous и multi-hop SQL-задачи;
2. Запустить встроенный eval runner приложения на подготовленном наборе данных
3. Для каждого вопроса: прогнать через граф, собрать результаты
4. Рассчитать метрики, сохранить отчёт
5. Сравнить с предыдущим запуском (regression detection)

### Online Monitoring

В production:
- Dashboards с ключевыми метриками
- Алерты при degradation:
  - Error rate > 10%
  - p95 latency > 15s
  - Daily cost > $7.00
  - Retrieval quality drop (если есть feedback loop)
  - Provider error / fallback spike по LLM Gateway

### Feedback Loop

- **Implicit:** логируем, когда пользователь просит уточнить (= текущий ответ не помог)
- **Explicit:** опциональная кнопка "полезно / не полезно" на ответ
- Собранный feedback используется для обновления eval dataset