from pathlib import Path

from orbitkb.db.connection import open_db
from orbitkb.db.repositories import apis as apis_repo
from orbitkb.db.repositories import persistence as persistence_repo
from orbitkb.db.repositories import repositories as repositories_repo
from orbitkb.db.repositories import services as services_repo
from orbitkb.export.markdown import export_markdown
from orbitkb.export.mermaid import export_mermaid
from orbitkb.export.paths import service_output_dirs


def test_exports_keep_same_named_services_in_separate_repositories(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    for repository in ("alpha", "beta"):
        repo_id = repositories_repo.ensure_repository(conn, repository, f"/tmp/{repository}")
        service_id = services_repo.ensure_service(
            conn, "orders", f"/tmp/{repository}/orders", "python", repository_id=repo_id,
        )
        services_repo.update_service_overview(conn, service_id, f"{repository} overview", "details")
        apis_repo.upsert_api(conn, service_id, "GET", f"/{repository}", "s", "d", [], [])
        persistence_repo.replace_persistence_entities(conn, service_id, [
            {"name": f"{repository}_records", "kind": "sql_table", "engine": "postgres",
             "schema_json": [{"field": "id", "type_desc": "string"}]},
        ], [])

    out_dir = tmp_path / "docs"
    markdown_paths = export_markdown(conn, out_dir)
    mermaid_paths = export_mermaid(conn, out_dir)

    for repository in ("alpha", "beta"):
        service_dir = out_dir / f"{repository}--orders"
        index = service_dir / "index.md"
        er = service_dir / "er.mmd"
        assert index in markdown_paths
        assert er in mermaid_paths
        assert f"{repository} overview" in index.read_text(encoding="utf-8")
        assert f"{repository}_records" in er.read_text(encoding="utf-8")
        other = "beta" if repository == "alpha" else "alpha"
        assert f"{other} overview" not in index.read_text(encoding="utf-8")
        assert f"{other}_records" not in er.read_text(encoding="utf-8")


def test_output_dirs_remain_unique_and_inside_root_for_colliding_names(tmp_path: Path):
    services = [
        {"id": 3, "name": "alpha--orders--1", "repository_name": None},
        {"id": 4, "name": "alpha--orders", "repository_name": None},
        {"id": 1, "name": "orders", "repository_name": "alpha"},
        {"id": 2, "name": "orders", "repository_name": "beta"},
        {"id": 5, "name": "../outside", "repository_name": None},
        {"id": 6, "name": "topology.mmd", "repository_name": None},
    ]

    paths = service_output_dirs(tmp_path / "docs", services)

    assert len(set(paths.values())) == len(services)
    assert all(path.parent == tmp_path / "docs" for path in paths.values())
    assert all(path.name != "topology.mmd" for path in paths.values())
