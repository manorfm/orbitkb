from pathlib import Path

from orbitkb.generation.orchestrator import IndexResult, RouteSufficiency
from scripts.report_shadow_eligibility import collect_report, summarize_routes


def test_summary_counts_eligible_routes_without_treating_mock_diffs_as_quality():
    results = [
        ("jvm-spring", IndexResult(
            service_name="status", service_id=1, files_changed=2, llm_calls=3, status="ok",
            sufficiency_details=(
                RouteSufficiency("GET", "/status", None, "differs", ("summary", "description")),
                RouteSufficiency("POST", "/items", None),
            ),
        )),
        ("go", IndexResult(
            service_name="catalog", service_id=2, files_changed=2, llm_calls=4, status="ok",
            sufficiency_details=(RouteSufficiency("GET", "/catalog", None),),
        )),
    ]

    report = summarize_routes(results)

    assert report == {
        "services": 2,
        "regenerated_routes": 3,
        "eligible_routes": 1,
        "render_statuses": {"differs": 1, "ineligible": 2},
        "differing_fields": {"description": 1, "summary": 1},
        "by_stack": {
            "go": {"regenerated_routes": 1, "eligible_routes": 0},
            "jvm-spring": {"regenerated_routes": 2, "eligible_routes": 1},
        },
        "llm_calls": 7,
        "backend": "mock",
        "quality_evaluated": False,
    }


def test_repo_corpus_report_includes_direct_kotlin_fixture(tmp_path: Path):
    report = collect_report(tmp_path)

    assert report["services"] == 9
    assert report["regenerated_routes"] == 20
    assert report["eligible_routes"] == 1
    assert report["render_statuses"]["ineligible"] == 19
    assert report["render_statuses"]["differs"] == 1
    assert report["by_stack"]["jvm-spring"]["eligible_routes"] == 1
    assert report["quality_evaluated"] is False
