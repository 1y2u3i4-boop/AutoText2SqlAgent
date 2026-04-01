# C4 Context Diagram: AutoText2SQL Agent

Границы системы, пользователи и внешние зависимости.

```mermaid
flowchart LR
    user["Пользователь<br/>Аналитик / Тестировщик / SA"]
    agent["AutoText2SQL Agent<br/>NL-запрос -> структурированный ответ + SQL-запрос"]
    gateway["LLM Gateway<br/>model selection + provider fallback"]
    providers["LLM Providers<br/>OpenAI-compatible"]
    dbs["Корпоративные БД<br/>PostgreSQL / MySQL / MSSQL"]
    langsmith["LangSmith<br/>dev/staging tracing"]

    user -->|"NL-запрос / подтверждение SQL<br/>HTTP / SSE"| agent
    agent -->|"LLM-запросы: анализ, reranking, генерация<br/>HTTPS REST"| gateway
    gateway -->|"OpenAI-compatible calls"| providers
    gateway -->|"provider telemetry"| langsmith
    agent -->|"Интроспекция метаданных и read-only SQL<br/>SQLAlchemy driver"| dbs
```

## Описание границ

| Элемент | Зона ответственности | Граница доверия |
|---------|---------------------|-----------------|
| **User** | Формулировка запроса, подтверждение SQL | Внешний. Ввод не доверенный, проходит input guardrail. |
| **AutoText2SQL Agent** | Весь контур обработки: от запроса до ответа | Внутренняя зона доверия. Все модули - единый процесс. |
| **LLM Gateway** | Выбор модели, schema parsing, structured completions и provider fallback | Внешний gateway API. Нормализует ответы и маршрутизирует вызовы по провайдерам. |
| **LLM Providers** | Конкретные модели и провайдеры | Используются gateway как downstream-слой. |
| **Local Embedding Model** | Embeddings метаданных и запросов | Внутренний компонент. Не требует внешнего API, но требует локальных ресурсов CPU/GPU. |
| **Корпоративные БД** | Источник истины по метаданным и данным | Внешние, read-only доступ. Timeout 10s на запрос. |
| **LangSmith** | Опциональная наблюдаемость | Внешний SaaS. Отсутствие не влияет на работу системы. |
