"""Database tool: introspection + read-only query execution via SQLAlchemy."""
from __future__ import annotations

import socket
import time
from typing import Any
from urllib.parse import quote_plus, unquote_plus

import sqlalchemy as sa
import sqlglot
import structlog
from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import QueuePool

from autotext2sql.tools import ToolResult

logger = structlog.get_logger(__name__)

_ALLOWED_STATEMENT_TYPES = {"select"}
_FORCED_LIMIT = 100
_MYSQL_SYSTEM_DATABASES = {"information_schema", "mysql", "performance_schema", "sys"}
_SQL_SERVER_PREFERRED_ODBC_DRIVERS = [
    "ODBC Driver 18 for SQL Server",
    "ODBC Driver 17 for SQL Server",
    "FreeTDS",
]


def _looks_like_sql_server_connection_string(value: str) -> bool:
    lowered = value.strip().lower()
    return "://" not in lowered and ";" in lowered and "=" in lowered and any(
        token in lowered
        for token in (
            "server=",
            "data source=",
            "address=",
            "addr=",
            "network address=",
        )
    )


def _parse_connection_kv_string(value: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for chunk in value.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise ValueError(f"Invalid connection string segment: {chunk}")
        key, raw_val = chunk.split("=", 1)
        parsed[key.strip().lower()] = raw_val.strip()
    return parsed


def _available_odbc_drivers() -> set[str]:
    try:
        import pyodbc
    except Exception:
        return set()
    try:
        return {driver.strip() for driver in pyodbc.drivers()}
    except Exception:
        return set()


def _resolve_sql_server_instance(server: str) -> tuple[str, str | None]:
    if "\\" not in server:
        return server, None

    host, instance = server.split("\\", 1)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(2.0)
    try:
        sock.sendto(b"\x04" + instance.upper().encode("ascii", errors="ignore") + b"\x00", (host, 1434))
        response, _ = sock.recvfrom(4096)
    except TimeoutError:
        logger.warning(
            "sql_server_browser_timeout",
            host=host,
            instance=instance,
            fallback_port="1433",
        )
        return host, "1433"
    finally:
        sock.close()

    decoded = response[3:].decode("ascii", errors="ignore")
    parts = [part for part in decoded.split(";") if part]
    data = dict(zip(parts[::2], parts[1::2]))
    return host, data.get("tcp")


def _parse_sql_server_url(value: str) -> dict[str, str] | None:
    try:
        parsed = make_url(value)
    except Exception:
        return None

    if not parsed.get_backend_name().startswith("mssql"):
        return None

    params: dict[str, str]
    if parsed.get_driver_name() == "pyodbc" and "odbc_connect" in parsed.query:
        params = _parse_connection_kv_string(unquote_plus(parsed.query["odbc_connect"]))
    else:
        server = parsed.host or ""
        if parsed.port:
            server = f"{server},{parsed.port}"
        params = {"server": server}
        if parsed.username:
            params["user id"] = parsed.username
        if parsed.password:
            params["password"] = parsed.password
        if parsed.database:
            params["database"] = parsed.database
        for key, val in parsed.query.items():
            params[key.lower()] = str(val)
    return params


def _choose_sql_server_odbc_driver(requested_driver: str | None = None) -> str:
    available = _available_odbc_drivers()
    if requested_driver and requested_driver in available:
        return requested_driver
    for driver in _SQL_SERVER_PREFERRED_ODBC_DRIVERS:
        if driver in available:
            return driver
    raise ModuleNotFoundError(
        "No supported SQL Server ODBC driver is installed. Install 'ODBC Driver 18 for SQL Server' "
        "or register the 'FreeTDS' ODBC driver."
    )


def _sql_server_url_from_params(params: dict[str, str], default_database: str | None = None) -> str:
    server = (
        params.get("server")
        or params.get("data source")
        or params.get("address")
        or params.get("addr")
        or params.get("network address")
    )
    if not server:
        raise ValueError("SQL Server connection string must include Server=...")

    user = params.get("user id") or params.get("uid") or params.get("user")
    password = params.get("password") or params.get("pwd")
    database = params.get("database") or params.get("initial catalog") or default_database or "master"
    requested_driver = params.get("driver", "").strip("{}") or None
    driver = _choose_sql_server_odbc_driver(requested_driver)

    odbc_parts = [f"DRIVER={{{driver}}}", f"DATABASE={database}"]
    if driver == "FreeTDS":
        host, port = _resolve_sql_server_instance(server)
        odbc_parts.extend([f"SERVER={host}", f"PORT={port or '1433'}", "TDS_Version=7.4"])
    else:
        odbc_parts.append(f"SERVER={server}")
    if user:
        odbc_parts.append(f"UID={user}")
    if password:
        odbc_parts.append(f"PWD={password}")

    if driver != "FreeTDS":
        for src_key, dest_key in (
            ("encrypt", "Encrypt"),
            ("trustservercertificate", "TrustServerCertificate"),
            ("applicationintent", "ApplicationIntent"),
        ):
            val = params.get(src_key)
            if val:
                odbc_parts.append(f"{dest_key}={val}")

    return f"mssql+pyodbc:///?odbc_connect={quote_plus(';'.join(odbc_parts))}"


def _sql_server_url_from_connection_string(value: str, default_database: str | None = None) -> str:
    return _sql_server_url_from_params(_parse_connection_kv_string(value), default_database=default_database)


def normalize_database_url(value: str, default_database: str | None = None) -> str:
    value = value.strip()
    if _looks_like_sql_server_connection_string(value):
        return _sql_server_url_from_connection_string(value, default_database=default_database)
    sql_server_params = _parse_sql_server_url(value)
    if sql_server_params is not None:
        return _sql_server_url_from_params(sql_server_params, default_database=default_database)
    return value


def _is_safe_sql(sql: str) -> bool:
    """Return True only if the SQL is a read-only SELECT/CTE."""
    try:
        statements = sqlglot.parse(sql)
        if not statements:
            return False
        for stmt in statements:
            if stmt is None:
                return False
            stmt_type = type(stmt).__name__.lower()
            if stmt_type not in ("select", "with"):
                return False
        return True
    except Exception:
        return False


def _inject_limit(sql: str, limit: int = _FORCED_LIMIT) -> str:
    """Wrap the query in a subquery with LIMIT to cap result size."""
    sql = sql.strip().rstrip(";").strip()
    return f"SELECT * FROM ({sql}) AS _q LIMIT {limit}"


def build_database_url(base_url: str, db_name: str) -> str:
    base_url = normalize_database_url(base_url, default_database=db_name)
    parsed = make_url(base_url)
    if parsed.get_backend_name() == "mssql" and "odbc_connect" in parsed.query:
        odbc_connect = unquote_plus(parsed.query["odbc_connect"])
        params = _parse_connection_kv_string(odbc_connect)
        params["database"] = db_name
        raw_conn_str = ";".join(f"{key}={value}" for key, value in params.items())
        return _sql_server_url_from_connection_string(raw_conn_str, default_database=db_name)
    return parsed.set(database=db_name).render_as_string(hide_password=False)


def discover_database_urls(base_url: str) -> dict[str, str]:
    base_url = normalize_database_url(base_url, default_database="master")
    parsed = make_url(base_url)
    backend = parsed.get_backend_name()

    if backend == "sqlite":
        db_name = parsed.database or "main"
        return {db_name: base_url}

    if backend.startswith("postgresql"):
        admin_db = parsed.database or "postgres"
        admin_url = parsed.set(database=admin_db).render_as_string(hide_password=False)
        engine = sa.create_engine(admin_url, pool_pre_ping=True)
        query = text(
            """
            SELECT datname
            FROM pg_database
            WHERE datistemplate = false AND datallowconn = true
            ORDER BY datname
            """
        )
        try:
            with engine.connect() as conn:
                names = [row[0] for row in conn.execute(query)]
        finally:
            engine.dispose()
        return {name: build_database_url(base_url, name) for name in names}

    if backend.startswith("mysql"):
        admin_db = parsed.database or "information_schema"
        admin_url = parsed.set(database=admin_db).render_as_string(hide_password=False)
        engine = sa.create_engine(admin_url, pool_pre_ping=True)
        query = text("SHOW DATABASES")
        try:
            with engine.connect() as conn:
                names = [row[0] for row in conn.execute(query)]
        finally:
            engine.dispose()
        names = [name for name in names if name not in _MYSQL_SYSTEM_DATABASES]
        return {name: build_database_url(base_url, name) for name in names}

    if backend.startswith("mssql"):
        admin_url = build_database_url(base_url, "master")
        engine = sa.create_engine(admin_url, pool_pre_ping=True)
        query = text(
            """
            SELECT name
            FROM sys.databases
            WHERE state = 0
            ORDER BY name
            """
        )
        try:
            with engine.connect() as conn:
                names = [row[0] for row in conn.execute(query)]
        finally:
            engine.dispose()
        return {name: build_database_url(base_url, name) for name in names}

    db_name = parsed.database or "default_db"
    return {db_name: base_url}


class DatabaseTool:
    def __init__(
        self,
        db_name: str,
        url: str,
        query_timeout: int = 10,
        pool_size: int = 5,
    ) -> None:
        self._db_name = db_name
        self._query_timeout = query_timeout
        url = normalize_database_url(url)
        self._engine = sa.create_engine(
            url,
            poolclass=QueuePool,
            pool_size=pool_size,
            max_overflow=2,
            pool_pre_ping=True,
            execution_options={"no_parameters": False},
        )

    # ------------------------------------------------------------------
    # Query execution
    # ------------------------------------------------------------------

    def execute(self, sql: str, params: dict[str, Any] | None = None) -> ToolResult:
        start = time.perf_counter()
        if not _is_safe_sql(sql):
            return ToolResult(success=False, error="SQL policy violation: only SELECT is allowed")

        safe_sql = _inject_limit(sql)
        try:
            with self._engine.connect() as conn:
                conn.execution_options(
                    statement_timeout=self._query_timeout * 1000,
                    postgresql_readonly=True,
                )
                result = conn.execute(text(safe_sql), params or {})
                columns = list(result.keys())
                rows = [dict(zip(columns, row)) for row in result.fetchall()]
                truncated = len(rows) == _FORCED_LIMIT
            latency_ms = (time.perf_counter() - start) * 1000
            return ToolResult(
                success=True,
                data={
                    "columns": columns,
                    "rows": rows,
                    "row_count": len(rows),
                    "truncated": truncated,
                    "latency_ms": latency_ms,
                },
                latency_ms=latency_ms,
            )
        except Exception as exc:
            latency_ms = (time.perf_counter() - start) * 1000
            logger.error("db_execute_error", db=self._db_name, error=str(exc))
            return ToolResult(success=False, error=str(exc), latency_ms=latency_ms)

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def introspect(self) -> ToolResult:
        start = time.perf_counter()
        try:
            inspector = inspect(self._engine)
            schemas = inspector.get_schema_names()
            result: dict[str, Any] = {
                "db_name": self._db_name,
                "schemas": [],
                "tables_count": 0,
            }
            total = 0
            for schema in schemas:
                if schema in ("information_schema", "pg_catalog"):
                    continue
                tables_data = []
                for table_name in inspector.get_table_names(schema=schema):
                    columns = [
                        {
                            "name": c["name"],
                            "type": str(c["type"]),
                            "nullable": c.get("nullable", True),
                            "default": str(c.get("default", "")),
                            "description": c.get("comment", ""),
                        }
                        for c in inspector.get_columns(table_name, schema=schema)
                    ]
                    pk_info = inspector.get_pk_constraint(table_name, schema=schema)
                    primary_keys = pk_info.get("constrained_columns", [])
                    foreign_keys = [
                        {
                            "column": fk["constrained_columns"][0] if fk["constrained_columns"] else "",
                            "target_table": fk["referred_table"],
                            "target_column": fk["referred_columns"][0] if fk["referred_columns"] else "",
                            "target_schema": fk.get("referred_schema", ""),
                        }
                        for fk in inspector.get_foreign_keys(table_name, schema=schema)
                    ]
                    try:
                        comment = inspector.get_table_comment(table_name, schema=schema).get("text", "")
                    except Exception:
                        comment = ""
                    tables_data.append(
                        {
                            "name": table_name,
                            "columns": columns,
                            "primary_keys": primary_keys,
                            "foreign_keys": foreign_keys,
                            "description": comment or "",
                        }
                    )
                    total += 1
                result["schemas"].append({"name": schema, "tables": tables_data})
            result["tables_count"] = total
            result["latency_ms"] = (time.perf_counter() - start) * 1000
            return ToolResult(success=True, data=result, latency_ms=result["latency_ms"])
        except Exception as exc:
            latency_ms = (time.perf_counter() - start) * 1000
            logger.error("db_introspect_error", db=self._db_name, error=str(exc))
            return ToolResult(success=False, error=str(exc), latency_ms=latency_ms)

    def dispose(self) -> None:
        self._engine.dispose()
