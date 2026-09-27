import json
from pathlib import Path

from orbitkb.db.connection import open_db
from orbitkb.db.repositories import persistence as persistence_repo
from orbitkb.db.repositories import services as services_repo

EVIDENCE = [{"file": "main.py", "start_line": 10, "end_line": 20}]


def test_persistence_stores_evidence(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    service_id = services_repo.ensure_service(conn, "orders-service", "/tmp/orders", "python")
    persistence_repo.replace_persistence_entities(
        conn, service_id, [{"name": "orders", "kind": "sql_table", "schema_json": []}], EVIDENCE,
    )

    entities = persistence_repo.list_persistence(conn, service_id)
    assert entities[0]["evidence_json"] == '[{"file": "main.py", "start_line": 10, "end_line": 20}]'


def test_persistence_stores_engine(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    service_id = services_repo.ensure_service(conn, "orders-service", "/tmp/orders", "python")
    persistence_repo.replace_persistence_entities(
        conn, service_id,
        [{"name": "orders", "kind": "sql_table", "schema_json": [], "engine": "postgres"}],
        EVIDENCE,
    )

    entities = persistence_repo.list_persistence(conn, service_id)
    assert entities[0]["engine"] == "postgres"


def test_persistence_round_trips_a_field_level_foreign_key_reference(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    service_id = services_repo.ensure_service(conn, "orders-service", "/tmp/orders", "python")
    persistence_repo.replace_persistence_entities(
        conn, service_id,
        [{
            "name": "orders", "kind": "sql_table",
            "schema_json": [{
                "field": "customer_id", "type_desc": "string",
                "references": {"target_entity": "customers", "unique": False},
            }],
        }],
        EVIDENCE,
    )

    entities = persistence_repo.list_persistence(conn, service_id)
    fields = json.loads(entities[0]["schema_json"])
    assert fields[0]["references"] == {"target_entity": "customers", "unique": False}


def test_persistence_engine_defaults_to_unknown_when_absent(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    service_id = services_repo.ensure_service(conn, "orders-service", "/tmp/orders", "python")
    persistence_repo.replace_persistence_entities(
        conn, service_id, [{"name": "orders", "kind": "sql_table", "schema_json": []}], EVIDENCE,
    )

    entities = persistence_repo.list_persistence(conn, service_id)
    assert entities[0]["engine"] == "unknown"
