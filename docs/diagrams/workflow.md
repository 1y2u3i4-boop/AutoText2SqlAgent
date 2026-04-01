# Workflow Diagram: Query Execution

Пошаговое выполнение запроса пользователя, включая все ветки ошибок и fallback.

## Основной flow

```mermaid
graph TD
    START([Пользователь отправляет NL-запрос]) --> INPUT_GUARD

    INPUT_GUARD{Input Guard}
    INPUT_GUARD -->|"injection / невалидный"| ERR_INPUT[/"Ответ: запрос заблокирован<br/>Причина логируется"/]
    INPUT_GUARD -->|pass| QUERY_ANALYZER

    QUERY_ANALYZER["Query Analyzer<br/>(LLM: intent + entities)"]
    QUERY_ANALYZER -->|"LLM timeout/error"| RETRY_QA{Retry?}
    RETRY_QA -->|"retry count < x"| QUERY_ANALYZER
    RETRY_QA -->|"retry count ≥ x"| FALLBACK_KW["Fallback: keyword search<br/>(без LLM)"]
    QUERY_ANALYZER -->|success| RETRIEVER

    FALLBACK_KW --> RETRIEVER

    RETRIEVER["Retriever<br/>(vector search + conditional rerank)"]
    RETRIEVER -->|"Qdrant error"| ERR_RETRIEVER[/"Ответ: ошибка поиска<br/>Переиндексация может помочь"/]
    RETRIEVER -->|"LLM rerank timeout"| RETRIEVER_NO_RERANK["Продолжить без reranking<br/>(только vector search scores)"]
    RETRIEVER -->|success| RELEVANCE_CHECK
    RETRIEVER_NO_RERANK --> RELEVANCE_CHECK

    RELEVANCE_CHECK{Relevance<br/>confidence ≥ x?}
    RELEVANCE_CHECK -->|"< x"| CLARIFICATION[/"Запрос уточнения<br/>у пользователя"/]
    CLARIFICATION --> START
    RELEVANCE_CHECK -->|"≥ x"| RESPONSE_GEN

    RESPONSE_GEN["Response Generator<br/>(LLM: structured answer)"]
    RESPONSE_GEN -->|"LLM error"| RETRY_RG{Retry?}
    RETRY_RG -->|"retry count < x"| RESPONSE_GEN
    RETRY_RG -->|"retry count ≥ x"| ERR_LLM[/"Ответ: сервис временно<br/>недоступен"/]
    RESPONSE_GEN -->|success| SQL_GEN

    SQL_GEN["SQL Generator<br/>(LLM: SELECT query)"]
    SQL_GEN -->|"LLM error"| ERR_SQL_GEN[/"Ответ: не удалось<br/>сгенерировать SQL"/]
    SQL_GEN -->|success| SQL_VALIDATOR

    SQL_VALIDATOR{SQL Validator<br/>sqlglot + policy}
    SQL_VALIDATOR -->|"rejected: DDL/DML/unsafe"| SQL_REJECTED[/"SQL отклонён policy<br/>Ответ без исполняемого SQL"/]
    SQL_REJECTED --> OUTPUT_GUARD
    SQL_VALIDATOR -->|pass| HUMAN_APPROVAL

    HUMAN_APPROVAL{Human Approval<br/>interrupt}
    HUMAN_APPROVAL -->|"пользователь отклонил"| OUTPUT_GUARD
    HUMAN_APPROVAL -->|"пользователь подтвердил"| SQL_EXECUTOR

    SQL_EXECUTOR["SQL Executor<br/>(read-only execution)"]
    SQL_EXECUTOR -->|"timeout / DB error"| ERR_SQL[/"SQL не выполнен<br/>Ответ без результатов SQL"/]
    ERR_SQL --> OUTPUT_GUARD
    SQL_EXECUTOR -->|success| OUTPUT_GUARD

    OUTPUT_GUARD["Output Guard<br/>(validate response)"]
    OUTPUT_GUARD -->|"validation failed"| ERR_OUTPUT[/"Internal error<br/>Логируется для отладки"/]
    OUTPUT_GUARD -->|pass| FINAL

    FINAL([Структурированный ответ пользователю])
```

## Таблица ветвей ошибок

| Точка отказа | Тип | Fallback | Ответ пользователю |
|-------------|------|----------|---------------------|
| Input Guard reject | Hard stop | - | "Запрос заблокирован по причине безопасности" |
| Query Analyzer LLM fail | Degraded | Keyword search | Менее точные, но доступные результаты |
| Qdrant error | Hard stop | - | "Ошибка поисковой системы" |
| Reranking LLM fail | Degraded | Vector scores only | Результаты без LLM-ранжирования |
| Low relevance | User action | Clarification | "Уточните запрос: ..." |
| Response Gen LLM fail | Hard stop | - | "Сервис временно недоступен" |
| SQL Gen LLM fail | Hard stop | - | "Не удалось сгенерировать SQL-запрос" |
| SQL Validator reject | Degraded | Skip SQL | Ответ без исполняемого SQL-запроса (SQL не прошёл проверку) |
| Human rejects SQL | Normal | - | Ответ без выполнения SQL |
| SQL Executor fail | Degraded | Skip results | Ответ с SQL-запросом, но без результатов |
| Output Guard fail | Hard stop | - | "Внутренняя ошибка" |
