from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path

SCHEMA_VERSION = "50"
DEFAULT_DB_PATH = Path.home() / ".orbitkb" / "orbitkb.db"


def open_db(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    _init_schema(conn)
    return conn


def open_readonly_db(db_path: Path) -> sqlite3.Connection:
    """Open an existing knowledge base without creating or migrating it.

    Local monitoring must never contend with an indexer by attempting schema work
    or writing its own state. SQLite's read-only URI mode also turns a missing
    database into an explicit operator error instead of silently creating one.
    """
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def _init_schema(conn: sqlite3.Connection) -> None:
    schema_sql = resources.files("orbitkb.db").joinpath("schema.sql").read_text()
    conn.executescript(schema_sql)
    _add_column_if_missing(conn, "index_runs", "llm_invocations", "INTEGER")
    _add_column_if_missing(conn, "index_run_unit_usage", "backend_duration_ms", "REAL CHECK (backend_duration_ms >= 0)")
    _add_column_if_missing(conn, "index_run_units", "cached_input_tokens", "INTEGER")
    _add_column_if_missing(conn, "index_run_units", "prompt_chars", "INTEGER CHECK (prompt_chars >= 0)")
    _add_column_if_missing(conn, "components", "input_digest", "TEXT")
    _add_column_if_missing(conn, "static_message_contracts", "message_version", "TEXT")
    _add_column_if_missing(conn, "service_index_locks", "process_id", "INTEGER")
    _add_column_if_missing(conn, "change_plan_runs", "decision_points_json", "TEXT NOT NULL DEFAULT '[]'")
    _add_column_if_missing(conn, "change_plan_runs", "selected_decisions_json", "TEXT NOT NULL DEFAULT '[]'")
    _add_column_if_missing(conn, "change_plan_runs", "change_units_json", "TEXT NOT NULL DEFAULT '[]'")
    _add_column_if_missing(conn, "change_plan_runs", "token_measurement", "TEXT NOT NULL DEFAULT 'byte_estimate'")
    _add_column_if_missing(conn, "context_budget_runs", "token_measurement", "TEXT NOT NULL DEFAULT 'byte_estimate'")
    _add_column_if_missing(conn, "kubernetes_configuration_source_imports", "optional", "INTEGER CHECK (optional IN (0, 1))")
    _add_column_if_missing(
        conn, "kubernetes_configuration_source_import_unknowns", "optional", "INTEGER CHECK (optional IN (0, 1))",
    )
    _add_column_if_missing(
        conn, "kubernetes_configuration_source_imports", "container_role",
        "TEXT CHECK (container_role IN ('application', 'initialization'))",
    )
    _add_column_if_missing(
        conn, "kubernetes_configuration_source_import_unknowns", "container_role",
        "TEXT CHECK (container_role IN ('application', 'initialization'))",
    )
    _add_column_if_missing(conn, "kubernetes_configuration_source_import_unknowns", "workload_kind", "TEXT")
    _add_column_if_missing(conn, "kubernetes_configuration_source_import_unknowns", "workload_name", "TEXT")
    _add_column_if_missing(conn, "kubernetes_configuration_source_import_unknowns", "container_name", "TEXT")
    _migrate_configuration_binding_kind_if_needed(conn)
    _migrate_entrypoint_kind_if_needed(conn)
    _migrate_architecture_findings_if_needed(conn)
    row = conn.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?)", (SCHEMA_VERSION,)
        )
    elif row["value"] != SCHEMA_VERSION:
        conn.execute("UPDATE schema_meta SET value = ? WHERE key = 'schema_version'", (SCHEMA_VERSION,))
    conn.commit()


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}  # nosec B608 - table is a module-owned constant.
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")  # nosec B608 - identifiers are module-owned constants.


def _migrate_configuration_binding_kind_if_needed(conn: sqlite3.Connection) -> None:
    """Expand configuration-binding kinds while retaining source-proven facts."""
    table_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'static_configuration_bindings'"
    ).fetchone()["sql"]
    if "'property'" in table_sql:
        return
    conn.executescript(
        """
        CREATE TABLE static_configuration_bindings_replacement (
            id          INTEGER PRIMARY KEY,
            service_id  INTEGER NOT NULL REFERENCES services(id) ON DELETE CASCADE,
            source      TEXT NOT NULL,
            key         TEXT NOT NULL,
            kind        TEXT NOT NULL CHECK (kind IN ('environment', 'property')),
            sensitive   INTEGER NOT NULL CHECK (sensitive IN (0, 1)),
            file_path   TEXT NOT NULL,
            start_line  INTEGER NOT NULL,
            end_line    INTEGER NOT NULL,
            updated_at  TEXT NOT NULL
        );
        INSERT INTO static_configuration_bindings_replacement
            SELECT id, service_id, source, key, kind, sensitive, file_path, start_line, end_line, updated_at
            FROM static_configuration_bindings;
        DROP TABLE static_configuration_bindings;
        ALTER TABLE static_configuration_bindings_replacement RENAME TO static_configuration_bindings;
        CREATE INDEX idx_static_configuration_bindings_service
            ON static_configuration_bindings(service_id);
        """
    )


def _migrate_entrypoint_kind_if_needed(conn: sqlite3.Connection) -> None:
    """Expand the fixed entrypoint kind enum without discarding indexed flow data."""
    table_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'entrypoints'"
    ).fetchone()["sql"]
    if "'grpc'" in table_sql:
        return
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.executescript(
            """
            CREATE TABLE entrypoints_replacement (
                id          INTEGER PRIMARY KEY,
                service_id  INTEGER NOT NULL REFERENCES services(id) ON DELETE CASCADE,
                kind        TEXT NOT NULL CHECK (kind IN ('http', 'graphql', 'grpc', 'message', 'cli', 'job', 'rpc')),
                method      TEXT NOT NULL,
                name        TEXT NOT NULL,
                symbol      TEXT NOT NULL,
                file_path   TEXT NOT NULL,
                start_line  INTEGER NOT NULL,
                end_line    INTEGER NOT NULL,
                updated_at  TEXT NOT NULL,
                UNIQUE(service_id, kind, method, name, symbol)
            );
            INSERT INTO entrypoints_replacement
                SELECT id, service_id, kind, method, name, symbol, file_path, start_line, end_line, updated_at
                FROM entrypoints;
            DROP TABLE entrypoints;
            ALTER TABLE entrypoints_replacement RENAME TO entrypoints;
            """
        )
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def _migrate_architecture_findings_if_needed(conn: sqlite3.Connection) -> None:
    """Remove the obsolete finding-kind constraint without losing historical runs.

    SQLite cannot alter a CHECK constraint in place. The table is intentionally
    rebuilt only for databases whose fixed enum would make new deterministic
    detectors require a schema migration for every finding category.
    """
    table_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'architecture_findings'"
    ).fetchone()["sql"]
    if "kind IN (" not in table_sql:
        return
    conn.executescript(
        """
        CREATE TABLE architecture_findings_replacement (
            id            INTEGER PRIMARY KEY,
            run_id        INTEGER NOT NULL REFERENCES architecture_runs(id) ON DELETE CASCADE,
            kind          TEXT NOT NULL,
            severity      TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'critical')) DEFAULT 'info',
            services_json TEXT NOT NULL,
            detail_json   TEXT,
            reason        TEXT NOT NULL
        );
        INSERT INTO architecture_findings_replacement
            SELECT id, run_id, kind, severity, services_json, detail_json, reason
            FROM architecture_findings;
        DROP TABLE architecture_findings;
        ALTER TABLE architecture_findings_replacement RENAME TO architecture_findings;
        CREATE INDEX idx_architecture_findings_run ON architecture_findings(run_id);
        CREATE INDEX idx_architecture_findings_kind ON architecture_findings(kind);
        """
    )
