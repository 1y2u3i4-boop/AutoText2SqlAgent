# Spec: Memory / Context

## Назначение

Управление состоянием агента в рамках сессии: что помнит агент, как собирается контекст для LLM, как контролируется размер context window.

## Session State

### Модель состояния

Центральный state передаётся между всеми узлами LangGraph графа:

| Группа полей | Состав | Назначение |
|--------------|--------|------------|
| Входные данные | user_query, session_id | Исходный запрос и идентификатор сессии |
| Результаты анализа | parsed_intent, extracted_entities | Данные после разбора запроса |
| Результаты поиска | retrieved_objects, relevance_scores, enrichment_data | Данные retrieval и enrichment |
| Генерация | response_text, generated_sql, sql_validation_result, sql_execution_result | Промежуточные и финальные результаты генерации и исполнения SQL |
| Управление flow | requires_clarification, clarification_message, requires_human_approval, human_approved | Состояние переходов по графу |
| Cost и telemetry | step_costs, total_cost, error | Стоимость шагов и технические ошибки |
| History | messages | История сообщений для сборки контекста |

### Персистентность

| Компонент | Хранилище | Время жизни |
|-----------|-----------|-------------|
| AgentState | LangGraph checkpointer → SQLite | До завершения сессии или перезапуска сервера |
| messages (chat history) | Внутри AgentState | В рамках сессии |
| Cost counters | In-memory dict + periodic flush | Daily / weekly reset |
| Индекс Qdrant | Docker volume (persistent) | До переиндексации |

## Memory Policy

### Краткосрочная (in-session)

- **Что хранится:** полная история сообщений user ↔ agent в текущей сессии
- **Формат:** list[Message] где Message = {role, content, timestamp}
- **Использование:** включается в контекст LLM для поддержки multi-turn диалога
- **Управление:** FIFO-усечение при превышении context budget

### Межсессионная

- **Не реализуется.** Каждая сессия независима.

## Context Budget

### Распределение context window

Для LLM Gateway с большим context window у downstream-моделей, но с целевым budget ≤ 16K tokens на вызов ради latency и cost:

| Слот | Max tokens | Приоритет усечения |
|------|-----------|-------------------|
| **System Prompt** | 1,500 | Не усекается (неприкосновенный) |
| **DB Schema Context** (из retriever) | 6,000 | 2-й (уменьшение top-N) |
| **Session History** | 6,000 | 1-й (FIFO: старые сообщения первыми) |
| **Current Query** | 500 | Не усекается |
| **Response budget** | 2,000 | max_tokens для генерации |
| **Total** | **~16,000** | - |

### Алгоритм усечения

1. Подсчитывается число токенов для каждого слота контекста через tiktoken.
2. Если суммарный объём превышает budget:
   - сначала усекается Session History, начиная с самых старых пар user + assistant;
   - затем, если нужно, уменьшается число retriever results, например 5 -> 3 -> 1;
   - System Prompt и Current Query не усекаются.
3. Если после всех усечений budget всё равно превышен, запрос считается слишком длинным и завершается ошибкой.

### Сборка контекста для LLM

Контекст собирается из четырёх слоёв:
- System Prompt:
  - роль агента поиска по метаданным БД;
  - правила форматирования ответа;
  - ограничения по SQL и политике безопасности.
- DB Schema Context:
  - top-N результатов из retriever;
  - структура таблиц, колонок, типов и FK;
  - связанные metadata и описания из enrichment-контура.
- Session History:
  - предыдущие сообщения пользователя и агента;
  - история, уже усечённая по FIFO при необходимости.
- Current Query:
  - текущий запрос пользователя, который обрабатывается в графе.

### Разделение контекстов (security)

- System prompt передаётся как role: system
- DB Schema Context - как role: system (доверенный контент из нашего индекса)
- Session History - как чередование role: user / role: assistant
- Current Query - как role: user

Пользовательский ввод никогда не вставляется в system message напрямую.

## Конфигурация

| Параметр | Default | Описание |
|----------|---------|----------|
| context.max_total_tokens | 16000 | Общий бюджет контекста |
| context.system_prompt_tokens | 1500 | Резерв для system prompt |
| context.max_history_tokens | 6000 | Максимум для session history |
| context.max_schema_tokens | 6000 | Максимум для DB schema context |
| context.max_query_tokens | 500 | Максимум для текущего запроса |
| context.response_max_tokens | 2048 | max_tokens для LLM response |
| context.token_counter | tiktoken (cl100k_base) | Tokenizer для подсчёта |

## Ограничения

- Нет persistent memory между сессиями
- Нет summarization для длинных историй (только FIFO-усечение)
- Нет user-specific memory (предпочтения, частые запросы)
- Нет shared memory между пользователями
- Token counting - приблизительный (tiktoken), не exact match с API billing
