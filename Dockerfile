FROM python:3.11-slim

WORKDIR /app

# Install system dependencies for psycopg2, pymysql, and MSSQL via FreeTDS/ODBC
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    unixodbc \
    tdsodbc \
    freetds-bin \
    && rm -rf /var/lib/apt/lists/*

# Install uv binary
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

ARG INSTALL_UI=1

# 1) Dependency layer (cache-friendly, without project source)
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    if [ "$INSTALL_UI" = "1" ]; then \
        uv sync --frozen --no-dev --extra ui --no-install-project; \
    else \
        uv sync --frozen --no-dev --no-install-project; \
    fi

# 2) Project layer
COPY autotext2sql/ ./autotext2sql/
COPY evals/ ./evals/
RUN --mount=type=cache,target=/root/.cache/uv \
    if [ "$INSTALL_UI" = "1" ]; then \
        uv sync --frozen --no-dev --extra ui; \
    else \
        uv sync --frozen --no-dev; \
    fi

# Create data directory for SQLite checkpointer
RUN mkdir -p /app/data

EXPOSE 8000

CMD ["uvicorn", "autotext2sql.api:app", "--host", "0.0.0.0", "--port", "8000"]
