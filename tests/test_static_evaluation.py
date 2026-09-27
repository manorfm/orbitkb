"""Golden regression checks for deterministic static analysis across supported stacks."""
from pathlib import Path

from benchmark.static_evaluation import (
    CASES,
    StaticEvaluationReport,
    StaticEvaluationResult,
    run_static_evaluation,
)


def test_static_golden_corpus_is_recalled_without_false_positive_scored_facts(tmp_path: Path):
    report = run_static_evaluation(CASES, tmp_path)

    assert {result.case_id for result in report.results} == {case.id for case in CASES}
    assert report.aggregate_recall == 1.0
    assert report.aggregate_precision == 1.0
    assert all(not result.missing for result in report.results)
    assert all(not result.unexpected for result in report.results)


def test_static_golden_report_records_reproducible_resource_measurements(tmp_path: Path):
    report = run_static_evaluation(CASES, tmp_path)

    assert report.elapsed_ms >= 0
    assert report.peak_memory_bytes > 0
    assert report.as_dict()["cases"] == len(CASES)
    assert report.as_dict()["precision"] == 1.0
    assert report.as_dict()["recall"] == 1.0
    assert report.as_dict()["stacks"] == {
        "go": {"cases": 1, "precision": 1.0, "recall": 1.0},
        "jvm-spring": {"cases": 2, "precision": 1.0, "recall": 1.0},
        "node-ts": {"cases": 1, "precision": 1.0, "recall": 1.0},
    }
    assert report.passes_quality_gate


def test_static_golden_aggregation_keeps_cases_independent():
    fact = ("entrypoint", "http", "POST", "/orders")
    report = StaticEvaluationReport(
        (
            StaticEvaluationResult("go", "go", frozenset({fact}), frozenset()),
            StaticEvaluationResult("java", "jvm-spring", frozenset({fact}), frozenset({fact})),
        ),
        elapsed_ms=0,
        peak_memory_bytes=1,
    )

    assert report.aggregate_precision == 1.0
    assert report.aggregate_recall == 0.5


def test_static_quality_gate_requires_full_precision_and_recall_for_each_stack():
    fact = ("entrypoint", "http", "POST", "/orders")
    report = StaticEvaluationReport(
        (
            StaticEvaluationResult("go-missed", "go", frozenset({fact}), frozenset()),
            StaticEvaluationResult("java-exact", "jvm-spring", frozenset({fact}), frozenset({fact})),
        ),
        elapsed_ms=0,
        peak_memory_bytes=1,
    )

    assert report.quality_by_stack == {
        "go": {"cases": 1, "precision": 1.0, "recall": 0.0},
        "jvm-spring": {"cases": 1, "precision": 1.0, "recall": 1.0},
    }
    assert not report.passes_quality_gate
