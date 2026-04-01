# C4 Container Diagram: AutoText2SQL Agent

Внутренние контейнеры системы и их взаимосвязи.

```mermaid
flowchart LR
    user["Пользователь"]

    subgraph system["AutoText2SQL Agent"]
        api["API Server<br/>FastAPI"]
        orchestrator["Agent Orchestrator<br/>LangGraph"]
        retriever["Retriever<br/>local embedding -> vector search -> conditional reranking"]
        tools["Tool Layer<br/>SQLAlchemy + gateway adapters"]
        llm_gateway["LLM Gateway Client<br/>routing + schema parsing"]
        vectordb["Vector Store<br/>Qdrant"]
        checkpointer["Session Store<br/>SQLite"]
        guardrails["Guardrails<br/>validation + SQL policy + budget"]
        observability["Observability<br/>logs + metrics + traces"]
    end

    providers["LLM Providers<br/>OpenAI-compatible"]
    dbs["Корпоративные БД"]
    langsmith["LangSmith"]

    user -->|"HTTP / SSE"| api
    api --> orchestrator
    orchestrator --> retriever
    orchestrator --> tools
    orchestrator --> llm_gateway
    orchestrator --> guardrails
    orchestrator --> checkpointer
    retriever -->|"REST / gRPC"| vectordb
    llm_gateway -->|"HTTPS"| providers
    llm_gateway -->|"provider telemetry"| langsmith
    tools -->|"DB driver"| dbs
    observability -->|"traces + metrics"| langsmith
```

## Ответственность контейнеров

| Контейнер | Технология | Что делает | Что НЕ делает |
|-----------|-----------|------------|---------------|
| **API Server** | FastAPI | Приём запросов, SSE-стриминг, authN, rate limit | Бизнес-логику, LLM-вызовы |
| **Agent Orchestrator** | LangGraph | Управление шагами графа, state transitions, retry/fallback. Внутри него живут `Query Analyzer`, `Response Generator`, `SQL Generator`, `Cost Controller` и state management | Прямые обращения к внешним API |
| **Retriever** | Python + Qdrant | Vector search, payload filtering, reranking | Индексацию (это задача offline pipeline) |
| **Tool Layer** | SQLAlchemy, internal adapters | Адаптеры к внешним системам с единым интерфейсом ToolResult | Принятие решений о control flow |
| **LLM Gateway Client** | HTTP client + schema parser | Обращение к gateway, нормализация structured output, fallback metadata | Прямой выбор бизнес-логики графа |
| **Vector Store** | Qdrant | Хранение и поиск эмбеддингов | Бизнес-логику, reranking |
| **Session Store** | SQLite | Персистентность state сессий LangGraph | Долгосрочное хранение данных |
| **Guardrails** | sqlglot, regex, Pydantic | Валидация, policy enforcement, cost control | Генерацию контента |
| **Observability** | structlog, OTEL | Логирование, метрики, трейсы | Alerting (внешняя система) |
