"""Checks the evidence boundaries of OrbitKB's reproducible readiness audit."""
import json
from pathlib import Path

from benchmark.readiness import run_readiness_audit
from benchmark.real_corpus import RealCorpus, RealCorpusCase, RealCorpusQualityGate
from scripts.run_readiness_audit import main as run_readiness_audit_main


def _write_manifest(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "cases": [
                    {
                        "id": "payment-status-001",
                        "stack": "node-ts",
                        "task_digest": "sha256:" + "a" * 64,
                        "kb_snapshot_digest": "sha256:" + "b" * 64,
                        "changed_unit_refs": ["unit-001"],
                        "predicted_unit_refs": ["unit-001"],
                        "contract_refs": [],
                        "migration_refs": [],
                        "test_refs": [],
                        "decision_kinds": [],
                        "size": "small",
                        "criticality": "high",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_readiness_audit_reports_deterministic_evidence_and_conditional_limits(tmp_path: Path):
    report = run_readiness_audit(tmp_path)

    assert report.status == "conditional"
    assert report.static_facts_passed is True
    assert report.change_surface_candidates_passed is True
    assert any("container" in item for item in report.conditions)
    assert any("real repository" in item for item in report.conditions)
    assert report.as_dict()["status"] == "conditional"


def test_readiness_audit_records_a_passing_explicit_real_corpus_gate(tmp_path: Path):
    corpus = RealCorpus(
        (
            RealCorpusCase(
                id="payment-status-001",
                stack="node-ts",
                task_digest="sha256:" + "a" * 64,
                kb_snapshot_digest="sha256:" + "b" * 64,
                changed_unit_refs=("unit-001",),
                contract_refs=(),
                migration_refs=(),
                test_refs=(),
                decision_kinds=(),
                size="small",
                criticality="high",
                predicted_unit_refs=("unit-001",),
            ),
        )
    )
    gate = RealCorpusQualityGate(
        required_stacks=frozenset({"node-ts"}),
        minimum_cases_per_stack=1,
        minimum_precision=1.0,
        minimum_recall=1.0,
    )

    report = run_readiness_audit(tmp_path, real_corpus=corpus, real_corpus_gate=gate)

    assert report.status == "conditional"
    assert report.real_corpus_gate_passed is True
    assert report.as_dict()["real_corpus_gate_passed"] is True
    assert not any("real corpus" in item for item in report.conditions)


def test_readiness_audit_blocks_when_an_explicit_real_corpus_gate_fails(tmp_path: Path):
    corpus = RealCorpus(
        (
            RealCorpusCase(
                id="payment-status-001",
                stack="node-ts",
                task_digest="sha256:" + "a" * 64,
                kb_snapshot_digest="sha256:" + "b" * 64,
                changed_unit_refs=("unit-001", "unit-002"),
                contract_refs=(),
                migration_refs=(),
                test_refs=(),
                decision_kinds=(),
                size="small",
                criticality="high",
                predicted_unit_refs=("unit-001",),
            ),
        )
    )
    gate = RealCorpusQualityGate(
        required_stacks=frozenset({"node-ts"}),
        minimum_cases_per_stack=1,
        minimum_precision=1.0,
        minimum_recall=1.0,
    )

    report = run_readiness_audit(tmp_path, real_corpus=corpus, real_corpus_gate=gate)

    assert report.status == "blocked"
    assert report.real_corpus_gate_passed is False
    assert any("real corpus" in item for item in report.conditions)


def test_readiness_audit_cli_accepts_an_explicit_real_corpus_gate(tmp_path: Path, capsys):
    manifest = _write_manifest(tmp_path / "corpus.json")

    exit_code = run_readiness_audit_main(
        [
            "--corpus",
            str(manifest),
            "--required-stack",
            "node-ts",
            "--min-cases-per-stack",
            "1",
            "--min-precision",
            "1.0",
            "--min-recall",
            "1.0",
        ]
    )

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["real_corpus_gate_passed"] is True
