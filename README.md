# AutoText2SQL

Простой Text2SQL-сервис: принимает вопрос на естественном языке, смотрит схему подключенной БД, при необходимости подтягивает краткосрочную историю и user memory, генерирует read-only SQL и выполняет его только после явного `Execute SQL`.

## Flow

```text
POST /query
  ├─ introspect schema
  ├─ load recent chat history + user memory
  ├─ ask LLM to choose mode: answer | sql
  ├─ mode=answer -> return text answer
  └─ mode=sql    -> validate SELECT -> retry if SQL is invalid -> return SQL + session_id

POST /query/approve
  └─ approved=true  -> execute read-only SQL -> retry/regenerate on SQL error -> return rows
```

## Что важно

- Основной production path `/query` не использует LangGraph metadata retrieval; legacy graph остаётся только для `/query/stream`.
- LLM сама решает, нужно ли ответить текстом по схеме или сгенерировать SQL.
- UI показывает пользователю SQL и кнопку `Execute SQL`; backend вызывает `/query/approve` с `approved=true`.
- Для conversational memory основной `/query` использует `session_id`, историю сообщений и user-specific memory через OSS `mem0`.
- `Qdrant` используется для user memory и semantic lookup в memory layer.
- Выполняются только read-only `SELECT`/`WITH` запросы.
- Перед выполнением запрос дополнительно оборачивается forced `LIMIT`.
- При невалидном SQL или ошибке исполнения backend делает ограниченные ретраи генерации с текстом ошибки.

## Быстрый запуск

### Что нужно

- Docker + Docker Compose
- ключ к OpenAI-compatible gateway, например OpenRouter

### Шаг 1. Скопируйте конфиги

```bash
cp .env.example .env
cp config.yaml.example config.yaml
```

### Шаг 2. Заполните `.env`

Минимально нужны только эти две переменные:

```env
LLM_GATEWAY_BASE_URL=https://openrouter.ai/api/v1
LLM_GATEWAY_API_KEY=sk-...
```

Если используете локальный gateway, укажите его URL вместо OpenRouter.

### Шаг 3. Проверьте `config.yaml`

Для demo-стенда обычно достаточно значений из шаблона. Самое важное:

```yaml
llm:
  gateway_url: "https://openrouter.ai/api/v1"
  default_route: "qwen/qwen3.6-plus"
  route_overrides:
    text2sql: "qwen/qwen3.5-flash-02-23"

databases:
  - name: "demo"
    url: "postgresql://postgres:postgres@demo-db:5432/demo"
    readonly: true
```

Если хотите использовать conversational memory, включите её явно:

```yaml
memory:
  enabled: true
  provider: "mem0"
  collection_name: "user_memory"
  embedding_dimensions: 1536
  search_top_k: 4
  max_memories: 4
  max_prompt_tokens: 1500
  write_enabled: true
```

Если память не нужна, оставьте `memory.enabled: false`.

### Шаг 4. Поднимите сервисы

```bash
docker compose up -d --build
```

Что поднимется:

- API/UI: [http://localhost:8000](http://localhost:8000)
- Swagger: [http://localhost:8000/docs](http://localhost:8000/docs)
- Gradio UI: [http://localhost:8000/ui/](http://localhost:8000/ui/)
- Demo PostgreSQL: `localhost:5433`
- Qdrant: [http://localhost:6333](http://localhost:6333)

### Шаг 5. Проверьте, что всё живо

```bash
curl http://localhost:8000/health
```

Ожидаемый ответ:

```json
{"status":"ok"}
```

После этого откройте `http://localhost:8000/ui/`, задайте вопрос вроде `Покажи 5 последних бронирований`, затем нажмите `Execute SQL`.

## Что настраивать чаще всего

### Хочу подключить свою БД

Измените секцию `databases` в `config.yaml`:

```yaml
databases:
  - name: "prod"
    url: "postgresql://user:password@host:5432/dbname"
    readonly: true
    query_timeout_seconds: 10
    pool_size: 5
```

### Хочу выключить память

```yaml
memory:
  enabled: false
```

### Хочу оставить память включённой

Ничего дополнительно в `.env` для `mem0` не нужно: OSS memory использует тот же `LLM_GATEWAY_BASE_URL` и `LLM_GATEWAY_API_KEY`, а векторное хранилище - локальный `Qdrant`.

## API

### Вопрос по схеме

```bash
curl -sS -X POST http://localhost:8000/query \
  -H "Content-Type: application/json" \
  -d '{"query":"о чем эта бд","db_hint":"demo"}'
```

Если LLM решит, что SQL не нужен, ответ содержит текст и `generated_sql: null`:

```json
{
  "session_id": "...",
  "answer": "База `demo` содержит ...",
  "generated_sql": null,
  "sql_result": null,
  "error": null
}
```

### Вопрос по данным

```bash
curl -sS -X POST http://localhost:8000/query \
  -H "Content-Type: application/json" \
  -d '{"query":"Покажи 5 последних бронирований","db_hint":"demo"}'
```

Если LLM решит, что нужны данные из таблиц, ответ возвращает SQL, но не выполняет его:

```json
{
  "session_id": "...",
  "answer": "",
  "generated_sql": "SELECT ...",
  "sql_result": null,
  "error": null
}
```

В UI этот ответ отображается с кнопкой `Execute SQL`.

### Выполнение SQL

```bash
curl -sS -X POST http://localhost:8000/query/approve \
  -H "Content-Type: application/json" \
  -d '{"session_id":"<session_id>","approved":true}'
```

Ответ:

```json
{
  "session_id": "...",
  "generated_sql": "SELECT ...",
  "sql_result": {
    "columns": ["..."],
    "rows": [],
    "row_count": 0,
    "truncated": false
  },
  "error": null
}
```

Если SQL падает на parse/validation/database error, backend может автоматически перегенерировать исправленный запрос и попробовать выполнить его повторно в пределах лимита ретраев.

### Conversational memory

`/query` принимает дополнительные поля для истории и user memory:

```json
{
  "query": "Как меня зовут и как отвечать?",
  "session_id": "stable-session-id",
  "user_id": "stable-user-id",
  "messages": [
    {"role": "user", "content": "Меня зовут Алексей, отвечай короче"},
    {"role": "assistant", "content": "Принято, Алексей. Буду отвечать кратко."}
  ]
}
```

UI отправляет эти поля автоматически. Для API-клиентов они опциональны, но без них persistent memory не будет работать корректно.

## Локальный запуск без Docker

```bash
uv venv
source .venv/bin/activate
uv pip install -e .[ui]
uvicorn autotext2sql.api:app --host 0.0.0.0 --port 8000 --reload
```

## Legacy

В репозитории ещё остаются модули LangGraph и metadata indexing. Сейчас они нужны в основном для legacy `/query/stream`, экспериментов и offline indexing. Основной production path `/query` использует упрощённый FastAPI flow с conversational memory.

Если нужен старый индексатор метаданных:

```bash
docker compose --profile indexing run --rm indexer
```
