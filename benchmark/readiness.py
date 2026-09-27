"""Compose deterministic evaluation evidence into an honest readiness summary."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from benchmark.real_corpus import (
    RealCorpus,
    RealCorpusQualityGate,
    evaluate_real_corpus,
)
from benchmark.runner import run_retrieval_recall
from benchmark.static_evaluation import CASES, run_static_evaluation
from benchmark.tasks import TASKS

BASE_CONDITIONS = (
    "run the opt-in container E2E on the target Docker/CI environment",
    "operate SQLite on one host/process domain or provide distributed coordination",
    "profile representative repositories before introducing an AST cache or parallel indexing",
)
_REAL_CORPUS_CONDITION = "verify change-surface precision and recall against real repository changes using Git and a redacted real corpus"
_REAL_CORPUS_FAILURE = "resolve the explicit real corpus quality-gate violations before relying on its evidence"


@dataclass(frozen=True)
class ReadinessAudit:
    static_facts_passed: bool
    change_surface_candidates_passed: bool
    real_corpus_gate_passed: bool | None
    conditions: tuple[str, ...]

    @property
    def status(self) -> str:
        """Never report unconditional production approval from synthetic evidence."""
        if not self.static_facts_passed or not self.change_surface_candidates_passed:
            return "blocked"
        return "blocked" if self.real_corpus_gate_passed is False else "conditional"

    def as_dict(self) -> dict[str, bool | None | str | list[str]]:
        return {
            "status": self.status,
            "static_facts_passed": self.static_facts_passed,
            "change_surface_candidates_passed": self.change_surface_candidates_passed,
            "real_corpus_gate_passed": self.real_corpus_gate_passed,
            "conditions": list(self.conditions),
        }


def run_readiness_audit(
    work_dir: Path,
    real_corpus: RealCorpus | None = None,
    real_corpus_gate: RealCorpusQualityGate | None = None,
) -> ReadinessAudit:
    """Run cheap, local evidence checks and preserve remaining operator conditions."""
    if (real_corpus is None) != (real_corpus_gate is None):
        raise ValueError("real_corpus and real_corpus_gate must be supplied together")
    work_dir.mkdir(parents=True, exist_ok=True)
    static = run_static_evaluation(CASES, work_dir / "static")
    candidates = run_retrieval_recall(TASKS, work_dir / "change-surface")
    real_corpus_gate_passed = None
    conditions = list(BASE_CONDITIONS)
    if real_corpus is None:
        conditions.append(_REAL_CORPUS_CONDITION)
    else:
        real_corpus_gate_passed = evaluate_real_corpus(real_corpus).apply_gate(real_corpus_gate).passed
        if not real_corpus_gate_passed:
            conditions.append(_REAL_CORPUS_FAILURE)
    return ReadinessAudit(
        static_facts_passed=static.aggregate_precision == 1.0 and static.aggregate_recall == 1.0,
        change_surface_candidates_passed=(
            candidates.aggregate_recall == 1.0
            and candidates.aggregate_candidate_precision == 1.0
            and candidates.aggregate_candidate_recall == 1.0
        ),
        real_corpus_gate_passed=real_corpus_gate_passed,
        conditions=tuple(conditions),
    )
