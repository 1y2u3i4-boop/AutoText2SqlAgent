# Spec: Agent / Orchestrator

## Назначение

Центральный модуль системы. Реализует граф обработки запроса как LangGraph StateGraph. Координирует все остальные модули, управляет переходами, retry, fallback и human-in-the-loop.

Этот модуль реализуется как один runtime-компонент внутри backend-приложения. Узлы графа ниже - это внутренние логические части оркестратора, а не отдельные сервисы или отдельные процессы.

## Архитектура графа

### Граф: SearchAgentGraph

**Тип:** StateGraph[AgentState]
**Checkpointer:** SqliteSaver
**Interrupt points:** human_approval (для SQL execution)

### Узлы (nodes)

| Node | Тип | LLM | Назначение |
|------|-----|-----|------------|
| input_guard | sync | Нет* | Валидация и санитизация входа |
| query_analyzer | sync | Да | Извлечение intent + entities из NL-запроса |
| retriever | sync | Да (rerank) | Поиск релевантных объектов в индексе |
| relevance_check | conditional | Нет | Оценка достаточности результатов |
| response_generator | sync | Да | Генерация структурированного ответа и контекста для SQL |
| sql_generator | sync | Да | Генерация read-only SQL-запроса |
| sql_validator | sync | Нет | AST-проверка SQL через sqlglot |
| human_approval | interrupt | Нет | Ожидание подтверждения пользователя |
| sql_executor | sync | Нет | Выполнение SQL через read-only connection |
| output_guard | sync | Нет | Финальная валидация ответа |
| cost_controller | sync | Нет | Кросс-секционный контроль бюджета после LLM-вызовов |

Input guard использует детектирование injection-паттернов как часть входной валидации.

`query_analyzer`, `response_generator`, `sql_generator`, `cost_controller` и state handling не выделяются в отдельные deployable modules.

### Рёбра (edges)

Граф строится следующим образом:
- входная точка - input_guard;
- затем последовательно запускаются query_analyzer, retriever и relevance_check;
- при достаточной релевантности граф переходит в response_generator;
- после response_generator всегда запускается sql_generator;
- затем выполняются sql_validator и, при успехе, human_approval;
- после подтверждения пользователя выполняется sql_executor;
- финальный этап - output_guard, после которого граф завершается.

`cost_controller` не является отдельным шагом main flow. Он срабатывает как внутренний post-check после LLM-вызовов в `query_analyzer`, `retriever` (rerank), `response_generator` и `sql_generator`.

## Правила переходов (routing functions)

### route_after_input_guard
Если в state присутствует ошибка, выбирается переход reject. Иначе выбирается переход pass.

### route_after_relevance
Если в state установлен флаг requires_clarification, выбирается переход insufficient. Иначе выбирается переход sufficient.

### route_after_sql_validation
Если результат SQL-валидации присутствует и помечен как валидный, выбирается переход pass. Во всех остальных случаях выбирается reject.

### route_after_approval
Если пользователь подтвердил выполнение SQL, выбирается переход approved. Иначе выбирается rejected.

## Stop Conditions

| Условие | Место проверки | Действие |
|---------|---------------|----------|
| Input rejected | input_guard | → END с error message |
| Budget exceeded | После каждого LLM-вызова через cost_controller | → END с budget error |
| Needs clarification | relevance_check | → END с clarification message (клиент может продолжить новым запросом) |
| Max retries exhausted | Внутри node (Query Analyzer, Response Gen, SQL Generator) | Fallback или → END с error |
| Output validation failed | output_guard | → END с internal error |
| Нормальное завершение | output_guard pass | → END с финальным ответом |

## Retry / Fallback

### Retry policy по узлам

| Node | Max retries | Strategy | Fallback |
|------|-------------|----------|----------|
| query_analyzer | 2 | Exponential backoff (1s, 3s) | Keyword search (без LLM intent extraction) |
| retriever (reranking) | 1 | Immediate | Продолжить с vector search scores |
| response_generator | 2 | Exponential backoff (1s, 3s) | Error message пользователю |
| sql_generator | 1 | Immediate | Internal error / остановка запроса, если SQL не удалось сгенерировать |
| sql_executor | 1 | Immediate | Ответ без SQL results |

### Реализация retry
Retry реализуется внутри каждого node по единой схеме:
- выполняется вызов внешнего инструмента;
- при успехе результат сохраняется в state и выполнение продолжается;
- при ошибке, если число попыток ещё не исчерпано, применяется backoff;
- после исчерпания лимита узел использует свой fallback или завершает граф ошибкой.

Для query_analyzer fallback - упрощённое keyword extraction без LLM.

## Human-in-the-Loop

### Механизм

1. LangGraph interrupt на узле human_approval
2. API Server получает interrupt, отправляет клиенту запрос подтверждения через SSE
3. Клиент отвечает approve / reject
4. API Server возобновляет граф через graph.invoke(Command(resume=response))

### Что видит пользователь

Пользователь получает объект подтверждения со следующими полями:
- type - тип сообщения approval_request;
- sql - SQL-запрос, который предлагается выполнить;
- target_db - целевая БД;
- explanation - краткое объяснение назначения запроса.

### Timeout

Если пользователь не отвечает в течение 5 минут - автоматический reject, ответ без SQL.

## Конфигурация

| Параметр | Default | Описание |
|----------|---------|----------|
| orchestrator.max_retries_per_node | 2 | Общий лимит retry для LLM-узлов |
| orchestrator.backoff_base_seconds | 1.0 | Базовое время backoff |
| orchestrator.human_approval_timeout | 300s | Timeout ожидания подтверждения |
| orchestrator.cost_per_task_limit | 0.10 USD | Per-task cost limit |
| orchestrator.checkpointer_db | ./data/sessions.db | Путь к SQLite для sessions |
