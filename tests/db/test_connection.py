from pathlib import Path

from orbitkb.db.connection import open_db


def test_schema_initializes(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    row = conn.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
    assert row["value"] == "46"


def test_schema_adds_invocation_count_without_rewriting_older_runs(tmp_path: Path):
    path = tmp_path / "legacy-index-runs.db"
    conn = open_db(path)
    conn.execute("INSERT INTO index_runs (started_at, llm_calls) VALUES ('before-v3', 3)")
    conn.execute("ALTER TABLE index_runs DROP COLUMN llm_invocations")
    conn.execute("UPDATE schema_meta SET value = '41' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()

    upgraded = open_db(path)

    run = upgraded.execute("SELECT llm_calls, llm_invocations FROM index_runs").fetchone()
    assert run["llm_calls"] == 3
    assert run["llm_invocations"] is None
    assert upgraded.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()["value"] == "46"


def test_schema_adds_private_unit_usage_without_backfilling_old_runs(tmp_path: Path):
    path = tmp_path / "legacy-unit-usage.db"
    conn = open_db(path)
    conn.execute("INSERT INTO index_runs (started_at, llm_calls) VALUES ('before-v3', 3)")
    conn.execute("DROP TABLE index_run_unit_usage")
    conn.execute("UPDATE schema_meta SET value = '42' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()

    upgraded = open_db(path)

    columns = {row["name"] for row in upgraded.execute("PRAGMA table_info(index_run_unit_usage)")}
    assert {"unit_kind", "generated_units", "llm_invocations", "cost_usd"} <= columns
    assert not {"endpoint", "path", "prompt", "source"} & columns
    assert upgraded.execute("SELECT count(*) FROM index_run_unit_usage").fetchone()[0] == 0
    assert upgraded.execute("SELECT llm_calls FROM index_runs").fetchone()[0] == 3


def test_schema_adds_duration_to_existing_unit_usage_without_inventing_history(tmp_path: Path):
    path = tmp_path / "legacy-unit-duration.db"
    conn = open_db(path)
    conn.execute("INSERT INTO index_runs (id, started_at) VALUES (1, 'before-v3')")
    conn.execute(
        "INSERT INTO index_run_unit_usage (run_id, unit_kind, generated_units, llm_invocations, had_failure) "
        "VALUES (1, 'endpoint', 2, 2, 0)"
    )
    conn.execute("ALTER TABLE index_run_unit_usage DROP COLUMN backend_duration_ms")
    conn.execute("UPDATE schema_meta SET value = '43' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()

    upgraded = open_db(path)

    row = upgraded.execute("SELECT backend_duration_ms FROM index_run_unit_usage").fetchone()
    assert row["backend_duration_ms"] is None
    assert upgraded.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()["value"] == "46"


def test_schema_adds_per_unit_telemetry_without_backfilling_old_runs(tmp_path: Path):
    path = tmp_path / "legacy-per-unit.db"
    conn = open_db(path)
    conn.execute("INSERT INTO index_runs (id, started_at) VALUES (1, 'before-v3')")
    conn.execute("DROP TABLE index_run_units")
    conn.execute("UPDATE schema_meta SET value = '44' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()

    upgraded = open_db(path)

    columns = {row["name"] for row in upgraded.execute("PRAGMA table_info(index_run_units)")}
    assert {"unit_kind", "unit_key", "status", "llm_invocations", "cost_usd"} <= columns
    assert not {"path", "prompt", "source", "endpoint"} & columns
    assert upgraded.execute("SELECT count(*) FROM index_run_units").fetchone()[0] == 0


def test_schema_adds_cache_tokens_to_existing_unit_rows_without_guessing_history(tmp_path: Path):
    path = tmp_path / "legacy-cache.db"
    conn = open_db(path)
    conn.execute("INSERT INTO index_runs (id, started_at) VALUES (1, 'before-cache')")
    conn.execute(
        "INSERT INTO index_run_units (run_id, unit_kind, unit_key, status, llm_invocations, backend_duration_ms) "
        "VALUES (1, 'overview', 'opaque', 'success', 1, 10)"
    )
    conn.execute("ALTER TABLE index_run_units DROP COLUMN cached_input_tokens")
    conn.execute("UPDATE schema_meta SET value = '45' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()

    upgraded = open_db(path)
    assert upgraded.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()["value"] == "46"
    assert upgraded.execute("SELECT cached_input_tokens FROM index_run_units").fetchone()[0] is None


def test_schema_adds_message_version_to_an_existing_static_contract_table(tmp_path: Path):
    path = tmp_path / "legacy.db"
    conn = open_db(path)
    conn.execute("ALTER TABLE static_message_contracts DROP COLUMN message_version")
    conn.execute("UPDATE schema_meta SET value = '2' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()

    upgraded = open_db(path)

    columns = {row["name"] for row in upgraded.execute("PRAGMA table_info(static_message_contracts)")}
    assert "message_version" in columns
    assert upgraded.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()["value"] == "46"


def test_schema_adds_env_from_optional_and_container_role_to_existing_configuration_tables(tmp_path: Path):
    path = tmp_path / "legacy-env-from.db"
    conn = open_db(path)
    conn.execute("ALTER TABLE kubernetes_configuration_source_imports DROP COLUMN optional")
    conn.execute("ALTER TABLE kubernetes_configuration_source_import_unknowns DROP COLUMN optional")
    conn.execute("ALTER TABLE kubernetes_configuration_source_imports DROP COLUMN container_role")
    conn.execute("ALTER TABLE kubernetes_configuration_source_import_unknowns DROP COLUMN container_role")
    conn.execute("ALTER TABLE kubernetes_configuration_source_import_unknowns DROP COLUMN workload_kind")
    conn.execute("ALTER TABLE kubernetes_configuration_source_import_unknowns DROP COLUMN workload_name")
    conn.execute("ALTER TABLE kubernetes_configuration_source_import_unknowns DROP COLUMN container_name")
    conn.execute("UPDATE schema_meta SET value = '29' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()

    upgraded = open_db(path)

    source_import_columns = {
        row["name"] for row in upgraded.execute("PRAGMA table_info(kubernetes_configuration_source_imports)")
    }
    source_import_unknown_columns = {
        row["name"]
        for row in upgraded.execute("PRAGMA table_info(kubernetes_configuration_source_import_unknowns)")
    }
    assert "optional" in source_import_columns
    assert "optional" in source_import_unknown_columns
    assert "container_role" in source_import_columns
    assert "container_role" in source_import_unknown_columns
    assert {"workload_kind", "workload_name", "container_name"} <= source_import_unknown_columns
    assert upgraded.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()["value"] == "46"


def test_schema_adds_selected_decisions_to_an_existing_change_plan(tmp_path: Path):
    path = tmp_path / "legacy-plan.db"
    conn = open_db(path)
    conn.execute("ALTER TABLE change_plan_runs DROP COLUMN selected_decisions_json")
    conn.execute("UPDATE schema_meta SET value = '17' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()

    upgraded = open_db(path)

    columns = {row["name"] for row in upgraded.execute("PRAGMA table_info(change_plan_runs)")}
    assert "selected_decisions_json" in columns
    assert upgraded.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()["value"] == "46"


def test_schema_adds_change_units_to_an_existing_change_plan(tmp_path: Path):
    path = tmp_path / "legacy-plan-units.db"
    conn = open_db(path)
    conn.execute("ALTER TABLE change_plan_runs DROP COLUMN change_units_json")
    conn.execute("UPDATE schema_meta SET value = '17' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()

    upgraded = open_db(path)

    columns = {row["name"] for row in upgraded.execute("PRAGMA table_info(change_plan_runs)")}
    assert "change_units_json" in columns
    assert upgraded.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()["value"] == "46"


def test_schema_adds_token_measurement_to_an_existing_change_plan(tmp_path: Path):
    path = tmp_path / "legacy-plan-token-measurement.db"
    conn = open_db(path)
    conn.execute("ALTER TABLE change_plan_runs DROP COLUMN token_measurement")
    conn.execute("UPDATE schema_meta SET value = '31' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()

    upgraded = open_db(path)

    columns = {row["name"] for row in upgraded.execute("PRAGMA table_info(change_plan_runs)")}
    assert "token_measurement" in columns
    assert upgraded.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()["value"] == "46"


def test_schema_adds_token_measurement_to_existing_context_telemetry(tmp_path: Path):
    path = tmp_path / "legacy-context-token-measurement.db"
    conn = open_db(path)
    conn.execute("ALTER TABLE context_budget_runs DROP COLUMN token_measurement")
    conn.execute("UPDATE schema_meta SET value = '32' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()

    upgraded = open_db(path)

    columns = {row["name"] for row in upgraded.execute("PRAGMA table_info(context_budget_runs)")}
    assert "token_measurement" in columns
    assert upgraded.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()["value"] == "46"


def test_schema_adds_static_analysis_snapshots_to_an_existing_database(tmp_path: Path):
    path = tmp_path / "legacy-static-analysis.db"
    conn = open_db(path)
    conn.execute("DROP TABLE static_analysis_snapshots")
    conn.execute("UPDATE schema_meta SET value = '33' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()

    upgraded = open_db(path)

    assert upgraded.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'static_analysis_snapshots'"
    ).fetchone() is not None
    assert upgraded.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()["value"] == "46"


def test_schema_upgrades_legacy_entrypoint_constraint_without_losing_contracts(tmp_path: Path):
    path = tmp_path / "legacy-entrypoints.db"
    conn = open_db(path)
    conn.execute("INSERT INTO services (name, root_path, updated_at) VALUES ('orders', '/repos/orders', 'now')")
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.executescript(
        """
        DROP TABLE entrypoints;
        CREATE TABLE entrypoints (
            id          INTEGER PRIMARY KEY,
            service_id  INTEGER NOT NULL REFERENCES services(id) ON DELETE CASCADE,
            kind        TEXT NOT NULL CHECK (kind IN ('http', 'graphql', 'message', 'cli', 'job', 'rpc')),
            method      TEXT NOT NULL,
            name        TEXT NOT NULL,
            symbol      TEXT NOT NULL,
            file_path   TEXT NOT NULL,
            start_line  INTEGER NOT NULL,
            end_line    INTEGER NOT NULL,
            updated_at  TEXT NOT NULL,
            UNIQUE(service_id, kind, method, name, symbol)
        );
        INSERT INTO entrypoints VALUES (7, 1, 'http', 'GET', '/orders', 'Orders.get', 'Orders.java', 2, 4, 'now');
        INSERT INTO entrypoint_contracts (entrypoint_id, contract_json) VALUES (7, '{"source":"legacy"}');
        """
    )
    conn.execute("UPDATE schema_meta SET value = '19' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()

    upgraded = open_db(path)

    entrypoint = upgraded.execute("SELECT * FROM entrypoints WHERE id = 7").fetchone()
    contract = upgraded.execute("SELECT contract_json FROM entrypoint_contracts WHERE entrypoint_id = 7").fetchone()
    assert entrypoint["kind"] == "http"
    assert contract["contract_json"] == '{"source":"legacy"}'
    assert upgraded.execute("PRAGMA foreign_key_check").fetchall() == []


def test_schema_upgrades_configuration_binding_constraint_without_losing_facts(tmp_path: Path):
    path = tmp_path / "legacy-configuration.db"
    conn = open_db(path)
    conn.execute("INSERT INTO services (name, root_path, updated_at) VALUES ('orders', '/repos/orders', 'now')")
    conn.executescript(
        """
        DROP TABLE static_configuration_bindings;
        CREATE TABLE static_configuration_bindings (
            id          INTEGER PRIMARY KEY,
            service_id  INTEGER NOT NULL REFERENCES services(id) ON DELETE CASCADE,
            source      TEXT NOT NULL,
            key         TEXT NOT NULL,
            kind        TEXT NOT NULL CHECK (kind IN ('environment')),
            sensitive   INTEGER NOT NULL CHECK (sensitive IN (0, 1)),
            file_path   TEXT NOT NULL,
            start_line  INTEGER NOT NULL,
            end_line    INTEGER NOT NULL,
            updated_at  TEXT NOT NULL
        );
        INSERT INTO static_configuration_bindings
            VALUES (4, 1, 'Publisher.publish', 'ORDERS_TOPIC', 'environment', 0, 'publisher.ts', 2, 2, 'now');
        """
    )
    conn.execute("UPDATE schema_meta SET value = '21' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()

    upgraded = open_db(path)

    assert upgraded.execute("SELECT key FROM static_configuration_bindings").fetchone()["key"] == "ORDERS_TOPIC"
    upgraded.execute(
        """INSERT INTO static_configuration_bindings
           (service_id, source, key, kind, sensitive, file_path, start_line, end_line, updated_at)
           VALUES (1, 'Client.timeout', 'payments.timeout-ms', 'property', 0, 'Client.java', 3, 3, 'now')"""
    )
    assert upgraded.execute("PRAGMA foreign_key_check").fetchall() == []


def test_schema_preserves_old_architecture_findings_while_removing_kind_constraint(tmp_path: Path):
    path = tmp_path / "legacy.db"
    conn = open_db(path)
    conn.executescript(
        """
        DROP TABLE architecture_findings;
        CREATE TABLE architecture_findings (
            id INTEGER PRIMARY KEY,
            run_id INTEGER NOT NULL REFERENCES architecture_runs(id) ON DELETE CASCADE,
            kind TEXT NOT NULL CHECK (kind IN ('cycle', 'fan_in', 'fan_out', 'shared_database', 'duplicate_external_integration')),
            severity TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'critical')) DEFAULT 'info',
            services_json TEXT NOT NULL,
            detail_json TEXT,
            reason TEXT NOT NULL
        );
        INSERT INTO architecture_runs (created_at, services_indexed) VALUES ('2026-01-01', 1);
        INSERT INTO architecture_findings (run_id, kind, severity, services_json, detail_json, reason)
        VALUES (1, 'cycle', 'warning', '[\"orders\"]', '{}', 'old finding');
        """
    )
    conn.close()

    upgraded = open_db(path)

    assert upgraded.execute("SELECT kind FROM architecture_findings").fetchone()["kind"] == "cycle"
    upgraded.execute(
        """INSERT INTO architecture_findings (run_id, kind, severity, services_json, detail_json, reason)
           VALUES (1, 'possible_bff_domain_leakage', 'warning', '[\"orders\"]', '{}', 'new finding')"""
    )
    upgraded.execute(
        """INSERT INTO architecture_findings (run_id, kind, severity, services_json, detail_json, reason)
           VALUES (1, 'possible_read_entrypoint_side_effect', 'warning', '[\"orders\"]', '{}', 'newer finding')"""
    )
