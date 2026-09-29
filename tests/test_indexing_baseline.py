"""A frozen, public-output baseline over the repository's synthetic fixture."""
import json
from pathlib import Path

from benchmark.indexing_baseline import collect_baseline, compare_baseline
from scripts.check_indexing_baseline import main as check_baseline_main

SAMPLE_ROOT = Path(__file__).resolve().parents[1] / "verify" / "sample_project"
GOLDEN = Path(__file__).resolve().parent / "golden" / "indexing_baseline.json"


def test_mock_indexing_baseline_is_reproducible_and_matches_golden(tmp_path: Path):
    first = collect_baseline(SAMPLE_ROOT, tmp_path / "first")
    second = collect_baseline(SAMPLE_ROOT, tmp_path / "second")

    assert first == second
    assert compare_baseline(first, GOLDEN) == []
    assert first["stacks"] == ["jvm-spring", "node-ts", "python"]
    assert len(first["services"]) == 3
    assert all(service["api_count"] > 0 for service in first["services"])
    assert all("mermaid" in service for service in first["services"])
    assert all("root_path" not in service and "file_path" not in service for service in first["services"])
    assert all("/Users/" not in digest for digest in first["exports"].values())
    assert "svc_checkout_service -->|http| svc_payments_service" in first["integration_fixture"]["topology"]
    assert "payment_authorized" in first["integration_fixture"]["topology"]
    assert first["integration_fixture"]["exports"]
    assert all(metrics == {"false_positives": None, "omissions": None, "cost_usd": None}
               for metrics in first["quality_by_stack"].values())


def test_baseline_comparison_reports_changed_public_fields(tmp_path: Path):
    current = collect_baseline(SAMPLE_ROOT, tmp_path / "current")
    current["services"][0]["api_count"] += 1

    assert "services[0].api_count" in compare_baseline(current, GOLDEN)


def test_baseline_check_command_reports_drift(tmp_path: Path, capsys):
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    golden["services"][0]["api_count"] += 1
    changed = tmp_path / "changed.json"
    changed.write_text(json.dumps(golden), encoding="utf-8")

    assert check_baseline_main(["--baseline", str(changed)]) == 1
    assert "services[0].api_count" in capsys.readouterr().out
