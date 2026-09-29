from pathlib import Path

import pytest

from orbitkb.analysis.canonical_projection import project_analysis
from orbitkb.analysis.models import (
    AnalysisResult,
    CloudFact,
    EntryPoint,
    Evidence,
    FlowEdge,
    SecurityRequirement,
    Symbol,
)
from orbitkb.db.connection import open_db
from orbitkb.db.repositories import canonical_snapshots, repositories, services
from orbitkb.domain.canonical import CanonicalSnapshot, ServiceKey


def test_canonical_snapshot_round_trip_preserves_identity_status_sources_and_nested_attributes(tmp_path: Path):
    db_path = tmp_path / "canonical.db"
    conn = open_db(db_path)
    repository_id = repositories.ensure_repository(conn, "shop", str(tmp_path / "shop"))
    service_id = services.ensure_service(conn, "menus", str(tmp_path / "shop" / "menus"), "jvm-spring", repository_id)
    key = ServiceKey("menus", repository="shop")
    analysis = AnalysisResult(
        entrypoints=[EntryPoint("http", "GET", "/menus", "Menu.list", Evidence("Menu.kt", 3, 6),
                                {"responses": ["Menu"]})],
        symbols=[Symbol("Menu.list", "Menu", "list", Evidence("Menu.kt", 3, 6), implements=("MenuApi",))],
        edges=[FlowEdge("Menu.list", "MenuService.find", "invokes", Evidence("Menu.kt", 5, 5),
                        confidence="medium", origin="codegraph"),
               FlowEdge("Menu.list", "repository.findByStatus", "reads", Evidence("Menu.kt", 6, 6),
                        boundary_kind="persistence")],
        security_requirements=[SecurityRequirement(None, None, "Menu.list", "custom:MenuPolicy", ("ADMIN",),
                                                   Evidence("Security.kt", 1, 2))],
        cloud_facts=[CloudFact("aws", "queue", "sqs", "SendMessage", "publish", "aws-sdk", None,
                               Evidence("Publisher.kt", 8, 8))],
    )
    snapshot = project_analysis(key, analysis)

    canonical_snapshots.replace_snapshot(conn, service_id, snapshot)
    conn.commit()
    conn.close()

    reopened = open_db(db_path)
    assert canonical_snapshots.read_snapshot(reopened, service_id) == snapshot
    assert canonical_snapshots.service_key(reopened, service_id) == key

    canonical_snapshots.replace_snapshot(reopened, service_id, CanonicalSnapshot(key, ()))
    assert canonical_snapshots.read_snapshot(reopened, service_id) == CanonicalSnapshot(key, ())


def test_canonical_snapshot_rejects_wrong_service_identity(tmp_path: Path):
    conn = open_db(tmp_path / "wrong-service.db")
    service_id = services.ensure_service(conn, "menus", str(tmp_path / "menus"), "go")

    with pytest.raises(ValueError, match="service identity"):
        canonical_snapshots.replace_snapshot(conn, service_id, CanonicalSnapshot(ServiceKey("orders"), ()))
    assert canonical_snapshots.read_snapshot(conn, service_id) is None
