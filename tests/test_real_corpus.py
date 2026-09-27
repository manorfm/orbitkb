"""Tests for the opt-in, privacy-preserving real-change corpus manifest."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchmark.real_corpus import CorpusValidationError, load_real_corpus


def _write_manifest(path: Path, cases: list[dict[str, object]]) -> Path:
    path.write_text(json.dumps({"schema_version": 1, "cases": cases}), encoding="utf-8")
    return path


def _valid_case(**overrides: object) -> dict[str, object]:
    case: dict[str, object] = {
        "id": "payment-status-001",
        "stack": "node-ts",
        "task_digest": "sha256:" + "a" * 64,
        "kb_snapshot_digest": "sha256:" + "b" * 64,
        "changed_unit_refs": ["unit-001", "unit-002"],
        "contract_refs": ["contract-001"],
        "migration_refs": [],
        "test_refs": ["test-001"],
        "decision_kinds": ["rollout"],
        "size": "small",
        "criticality": "high",
    }
    case.update(overrides)
    return case


def test_load_real_corpus_accepts_only_structured_redacted_change_metadata(tmp_path: Path):
    manifest = _write_manifest(
        tmp_path / "corpus.json",
        [_valid_case(), _valid_case(id="ledger-reconcile-001", stack="go", criticality="medium")],
    )

    corpus = load_real_corpus(manifest)

    assert corpus.case_count == 2
    assert corpus.coverage_by_stack == {"go": 1, "node-ts": 1}
    assert corpus.cases[0].changed_unit_refs == ("unit-001", "unit-002")
    assert corpus.cases[0].task_digest == "sha256:" + "a" * 64
    assert corpus.as_dict() == {
        "cases": 2,
        "coverage_by_stack": {"go": 1, "node-ts": 1},
        "schema_version": 1,
    }


@pytest.mark.parametrize("field", ["task_text", "raw_diff", "source", "prompt", "decision_text"])
def test_load_real_corpus_rejects_free_text_and_source_fields(tmp_path: Path, field: str):
    manifest = _write_manifest(tmp_path / "corpus.json", [_valid_case(**{field: "sensitive value"})])

    with pytest.raises(CorpusValidationError, match=field):
        load_real_corpus(manifest)


def test_load_real_corpus_rejects_non_opaque_artifact_references(tmp_path: Path):
    manifest = _write_manifest(
        tmp_path / "corpus.json",
        [_valid_case(changed_unit_refs=["src/payments/PaymentController.ts"])],
    )

    with pytest.raises(CorpusValidationError, match="changed_unit_refs"):
        load_real_corpus(manifest)
