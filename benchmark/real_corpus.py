"""Load opt-in real-change corpus manifests without retaining sensitive artifacts.

The manifest intentionally contains only hashes, controlled categories and opaque
references. A local operator keeps any mapping from those references to repositories,
issues, diffs or source symbols outside the corpus file.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
SUPPORTED_STACKS = frozenset({"go", "graphql", "java-spring", "kotlin-spring", "node-ts"})
_ALLOWED_CASE_FIELDS = frozenset(
    {
        "id",
        "stack",
        "task_digest",
        "kb_snapshot_digest",
        "changed_unit_refs",
        "contract_refs",
        "migration_refs",
        "predicted_unit_refs",
        "test_refs",
        "decision_kinds",
        "size",
        "criticality",
    }
)
_REQUIRED_CASE_FIELDS = _ALLOWED_CASE_FIELDS - {"predicted_unit_refs"}
_FORBIDDEN_TEXT_FIELDS = frozenset(
    {
        "code",
        "decision_text",
        "description",
        "diff",
        "file_path",
        "file_paths",
        "issue",
        "prompt",
        "raw_diff",
        "source",
        "symbol",
        "symbols",
        "task",
        "task_text",
    }
)
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_REFERENCE = re.compile(r"[a-z][a-z0-9-]{0,63}\Z")
_SIZES = frozenset({"small", "medium", "large"})
_CRITICALITIES = frozenset({"low", "medium", "high", "critical"})


class CorpusValidationError(ValueError):
    """Raised when a real-change manifest is malformed or unsafe to retain."""


@dataclass(frozen=True)
class RealCorpusQualityGate:
    """Explicit operator-defined thresholds for a privacy-safe corpus gate."""

    required_stacks: frozenset[str]
    minimum_cases_per_stack: int
    minimum_precision: float
    minimum_recall: float

    def __post_init__(self) -> None:
        if not self.required_stacks:
            raise CorpusValidationError("required_stacks must not be empty")
        unsupported = self.required_stacks - SUPPORTED_STACKS
        if unsupported:
            raise CorpusValidationError(f"unsupported required stacks: {', '.join(sorted(unsupported))}")
        if (
            not isinstance(self.minimum_cases_per_stack, int)
            or isinstance(self.minimum_cases_per_stack, bool)
            or self.minimum_cases_per_stack < 1
        ):
            raise CorpusValidationError("minimum_cases_per_stack must be at least 1")
        for value, name in ((self.minimum_precision, "minimum_precision"), (self.minimum_recall, "minimum_recall")):
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= value <= 1:
                raise CorpusValidationError(f"{name} must be between 0 and 1")

    def as_dict(self) -> dict[str, int | float | list[str]]:
        return {
            "required_stacks": sorted(self.required_stacks),
            "minimum_cases_per_stack": self.minimum_cases_per_stack,
            "minimum_precision": self.minimum_precision,
            "minimum_recall": self.minimum_recall,
        }


@dataclass(frozen=True)
class RealCorpusGateViolation:
    """One quality requirement that a stack did not meet."""

    stack: str
    kind: str
    observed: int | float
    required: int | float

    def as_dict(self) -> dict[str, str | int | float]:
        return {
            "stack": self.stack,
            "kind": self.kind,
            "observed": self.observed,
            "required": self.required,
        }


@dataclass(frozen=True)
class RealCorpusGateResult:
    """Structured gate decision safe for a local CI log."""

    gate: RealCorpusQualityGate
    violations: tuple[RealCorpusGateViolation, ...]

    @property
    def passed(self) -> bool:
        return not self.violations

    def as_dict(self) -> dict[str, bool | dict[str, int | float | list[str]] | list[dict[str, str | int | float]]]:
        return {
            "passed": self.passed,
            "requirements": self.gate.as_dict(),
            "violations": [violation.as_dict() for violation in self.violations],
        }


@dataclass(frozen=True)
class RealCorpusCase:
    """A redacted, structured record of one reviewed real-world change."""

    id: str
    stack: str
    task_digest: str
    kb_snapshot_digest: str
    changed_unit_refs: tuple[str, ...]
    contract_refs: tuple[str, ...]
    migration_refs: tuple[str, ...]
    test_refs: tuple[str, ...]
    decision_kinds: tuple[str, ...]
    size: str
    criticality: str
    predicted_unit_refs: tuple[str, ...] | None = None


@dataclass(frozen=True)
class RealCorpus:
    """Validated local corpus with coverage information safe to display in CI."""

    cases: tuple[RealCorpusCase, ...]

    @property
    def case_count(self) -> int:
        return len(self.cases)

    @property
    def coverage_by_stack(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for case in self.cases:
            counts[case.stack] = counts.get(case.stack, 0) + 1
        return dict(sorted(counts.items()))

    def as_dict(self) -> dict[str, int | dict[str, int]]:
        """Return only aggregate metadata suitable for a local quality gate."""
        return {
            "schema_version": SCHEMA_VERSION,
            "cases": self.case_count,
            "coverage_by_stack": self.coverage_by_stack,
        }


@dataclass(frozen=True)
class RealCorpusEvaluation:
    """Aggregate prediction quality while retaining only opaque corpus references."""

    cases: tuple[RealCorpusCase, ...]
    quality_by_stack: dict[str, dict[str, int | float | None]]

    def as_dict(self) -> dict[str, int | dict[str, dict[str, int | float | None]]]:
        evaluated_cases = sum(case.predicted_unit_refs is not None for case in self.cases)
        return {
            "cases": len(self.cases),
            "evaluated_cases": evaluated_cases,
            "pending_cases": len(self.cases) - evaluated_cases,
            "quality_by_stack": self.quality_by_stack,
        }

    def apply_gate(self, gate: RealCorpusQualityGate) -> RealCorpusGateResult:
        """Evaluate explicit coverage and metric requirements without hidden defaults."""
        violations: list[RealCorpusGateViolation] = []
        for stack in sorted(gate.required_stacks):
            metrics = self.quality_by_stack.get(stack)
            cases = int(metrics["cases"]) if metrics else 0
            evaluated_cases = int(metrics["evaluated_cases"]) if metrics else 0
            if cases < gate.minimum_cases_per_stack:
                violations.append(
                    RealCorpusGateViolation(stack, "insufficient_cases", cases, gate.minimum_cases_per_stack)
                )
            if cases > evaluated_cases:
                violations.append(RealCorpusGateViolation(stack, "pending_cases", cases - evaluated_cases, 0))
            if not metrics or metrics["precision"] is None:
                continue
            precision = float(metrics["precision"])
            recall = float(metrics["recall"])
            if precision < gate.minimum_precision:
                violations.append(
                    RealCorpusGateViolation(stack, "minimum_precision", precision, gate.minimum_precision)
                )
            if recall < gate.minimum_recall:
                violations.append(RealCorpusGateViolation(stack, "minimum_recall", recall, gate.minimum_recall))
        return RealCorpusGateResult(gate, tuple(violations))



def load_real_corpus(path: Path) -> RealCorpus:
    """Validate and load a local JSON manifest without accepting raw change data."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise CorpusValidationError(f"cannot read corpus manifest: {path}") from error
    except json.JSONDecodeError as error:
        raise CorpusValidationError(f"invalid corpus JSON: {error.msg}") from error
    if not isinstance(document, Mapping):
        raise CorpusValidationError("corpus manifest must be a JSON object")
    _reject_forbidden_fields(document)
    _require_exact_keys(document, {"schema_version", "cases"}, "corpus manifest")
    if document["schema_version"] != SCHEMA_VERSION:
        raise CorpusValidationError(f"unsupported schema_version: {document['schema_version']!r}")
    raw_cases = document["cases"]
    if not isinstance(raw_cases, list) or not raw_cases:
        raise CorpusValidationError("cases must be a non-empty array")
    cases = tuple(_parse_case(raw_case, index) for index, raw_case in enumerate(raw_cases))
    if len({case.id for case in cases}) != len(cases):
        raise CorpusValidationError("case ids must be unique")
    return RealCorpus(cases)


def evaluate_real_corpus(corpus: RealCorpus) -> RealCorpusEvaluation:
    """Measure predicted versus changed unit references for each stack.

    A case without ``predicted_unit_refs`` is intentionally reported as pending:
    treating it as an empty prediction would distort precision and recall.
    """
    cases_by_stack: dict[str, list[RealCorpusCase]] = {}
    for case in corpus.cases:
        cases_by_stack.setdefault(case.stack, []).append(case)
    return RealCorpusEvaluation(
        cases=corpus.cases,
        quality_by_stack={
            stack: _evaluate_stack(cases)
            for stack, cases in sorted(cases_by_stack.items())
        },
    )


def _parse_case(raw_case: object, index: int) -> RealCorpusCase:
    if not isinstance(raw_case, Mapping):
        raise CorpusValidationError(f"cases[{index}] must be an object")
    _reject_forbidden_fields(raw_case)
    _require_allowed_keys(raw_case, _REQUIRED_CASE_FIELDS, _ALLOWED_CASE_FIELDS, f"cases[{index}]")
    case_id = _require_reference(raw_case["id"], f"cases[{index}].id")
    stack = _require_choice(raw_case["stack"], SUPPORTED_STACKS, f"cases[{index}].stack")
    task_digest = _require_digest(raw_case["task_digest"], f"cases[{index}].task_digest")
    kb_snapshot_digest = _require_digest(raw_case["kb_snapshot_digest"], f"cases[{index}].kb_snapshot_digest")
    return RealCorpusCase(
        id=case_id,
        stack=stack,
        task_digest=task_digest,
        kb_snapshot_digest=kb_snapshot_digest,
        changed_unit_refs=_require_references(raw_case["changed_unit_refs"], f"cases[{index}].changed_unit_refs", minimum=1),
        contract_refs=_require_references(raw_case["contract_refs"], f"cases[{index}].contract_refs"),
        migration_refs=_require_references(raw_case["migration_refs"], f"cases[{index}].migration_refs"),
        test_refs=_require_references(raw_case["test_refs"], f"cases[{index}].test_refs"),
        decision_kinds=_require_references(raw_case["decision_kinds"], f"cases[{index}].decision_kinds"),
        size=_require_choice(raw_case["size"], _SIZES, f"cases[{index}].size"),
        criticality=_require_choice(raw_case["criticality"], _CRITICALITIES, f"cases[{index}].criticality"),
        predicted_unit_refs=(
            _require_references(raw_case["predicted_unit_refs"], f"cases[{index}].predicted_unit_refs")
            if "predicted_unit_refs" in raw_case
            else None
        ),
    )


def _reject_forbidden_fields(value: object) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if key in _FORBIDDEN_TEXT_FIELDS:
                raise CorpusValidationError(f"{key} is not allowed in a redacted corpus")
            _reject_forbidden_fields(nested)
    elif isinstance(value, list):
        for nested in value:
            _reject_forbidden_fields(nested)


def _require_exact_keys(value: Mapping[str, Any], expected: set[str] | frozenset[str], location: str) -> None:
    actual = set(value)
    missing = expected - actual
    unexpected = actual - expected
    if missing:
        raise CorpusValidationError(f"{location} is missing fields: {', '.join(sorted(missing))}")
    if unexpected:
        raise CorpusValidationError(f"{location} has unsupported fields: {', '.join(sorted(unexpected))}")


def _require_allowed_keys(
    value: Mapping[str, Any], required: set[str] | frozenset[str], allowed: set[str] | frozenset[str], location: str,
) -> None:
    actual = set(value)
    missing = required - actual
    unexpected = actual - allowed
    if missing:
        raise CorpusValidationError(f"{location} is missing fields: {', '.join(sorted(missing))}")
    if unexpected:
        raise CorpusValidationError(f"{location} has unsupported fields: {', '.join(sorted(unexpected))}")


def _require_digest(value: object, location: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise CorpusValidationError(f"{location} must be a sha256 digest")
    return value


def _require_reference(value: object, location: str) -> str:
    if not isinstance(value, str) or not _REFERENCE.fullmatch(value):
        raise CorpusValidationError(f"{location} must be an opaque lowercase reference")
    return value


def _require_references(value: object, location: str, minimum: int = 0) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) < minimum:
        raise CorpusValidationError(f"{location} must contain at least {minimum} opaque references")
    references = tuple(_require_reference(item, location) for item in value)
    if len(set(references)) != len(references):
        raise CorpusValidationError(f"{location} must not repeat references")
    return references


def _require_choice(value: object, choices: frozenset[str], location: str) -> str:
    if not isinstance(value, str) or value not in choices:
        raise CorpusValidationError(f"{location} must be one of: {', '.join(sorted(choices))}")
    return value


def _evaluate_stack(cases: list[RealCorpusCase]) -> dict[str, int | float | None]:
    evaluated = [case for case in cases if case.predicted_unit_refs is not None]
    if not evaluated:
        return {"cases": len(cases), "evaluated_cases": 0, "precision": None, "recall": None}
    expected = {(case.id, reference) for case in evaluated for reference in case.changed_unit_refs}
    predicted = {
        (case.id, reference)
        for case in evaluated
        for reference in case.predicted_unit_refs or ()
    }
    true_positives = len(expected & predicted)
    return {
        "cases": len(cases),
        "evaluated_cases": len(evaluated),
        "precision": _ratio(true_positives, len(predicted)),
        "recall": _ratio(true_positives, len(expected)),
    }


def _ratio(numerator: int, denominator: int) -> float:
    return 1.0 if denominator == 0 else numerator / denominator
