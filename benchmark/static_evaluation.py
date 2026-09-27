"""Reproducible golden corpus for OrbitKB's deterministic static analyzer.

The corpus is deliberately small and hand-authored. It validates only facts that
static analysis can prove from source; it does not claim to evaluate an LLM's change
judgment or generalise to arbitrary production codebases.
"""
from __future__ import annotations

import time
import tracemalloc
from dataclasses import dataclass
from pathlib import Path

from orbitkb.analysis.engine import StaticAnalysisEngine
from orbitkb.analysis.models import AnalysisResult

EntrypointFact = tuple[str, str, str]
EdgeFact = tuple[str, str]
MessageFact = tuple[str, str, str | None, str | None, str | None]
ContractFact = tuple[str, tuple[tuple[str, str], ...]]


@dataclass(frozen=True)
class StaticGoldenCase:
    """A source fixture and the deterministic facts intentionally scored from it."""

    id: str
    stack: str
    files: dict[str, str]
    entrypoints: frozenset[EntrypointFact] = frozenset()
    edges: frozenset[EdgeFact] = frozenset()
    messages: frozenset[MessageFact] = frozenset()
    contracts: frozenset[ContractFact] = frozenset()


CASES: tuple[StaticGoldenCase, ...] = (
    StaticGoldenCase(
        id="go-http-write-flow",
        stack="go",
        files={"main.go": '''package main
type Orders struct{}
func (o *Orders) Create() { o.useCase.Execute(); o.repo.Save() }
func main() { router.POST("/orders", orders.Create) }
'''},
        entrypoints=frozenset({("http", "POST", "/orders")}),
        edges=frozenset({("invokes", "o.useCase.Execute"), ("writes", "o.repo.Save")}),
    ),
    StaticGoldenCase(
        id="java-spring-controller-flow",
        stack="jvm-spring",
        files={
            "OrdersController.java": '''@RestController
class OrdersController {
  private final CreateOrderUseCase useCase;
  OrdersController(CreateOrderUseCase useCase) { this.useCase = useCase; }
  @PostMapping("/orders")
  Order create(Order order) { return useCase.execute(order); }
}
''',
            "CreateOrderUseCase.java": '''class CreateOrderUseCase {
  private final OrderRepository repository;
  Order execute(Order order) { return repository.save(order); }
}
''',
        },
        entrypoints=frozenset({("http", "POST", "/orders")}),
        edges=frozenset({("invokes", "CreateOrderUseCase.execute"), ("writes", "repository.save")}),
    ),
    StaticGoldenCase(
        id="kotlin-spring-scheduled-job",
        stack="jvm-spring",
        files={"ReconciliationJob.kt": '''class ReconciliationJob {
  @Scheduled(cron = "0 */5 * * * *")
  fun reconcile() { ledger.sync() }
}
'''},
        entrypoints=frozenset({("job", "SCHEDULED", "reconcile")}),
        contracts=frozenset({(
            "ReconciliationJob.reconcile",
            (("concurrency", "unknown"), ("idempotency", "unknown"), ("schedule", "0 */5 * * * *")),
        )}),
    ),
    StaticGoldenCase(
        id="node-graphql-rabbitmq",
        stack="node-ts",
        files={"resolvers.ts": '''export const resolvers = {
  Mutation: { createOrder: (_: unknown, input: CreateOrderInput, { service }) => { service.create(input); channel.publish("orders", "created", input, { headers: { schema_version: "1" } }); } }
};
'''},
        entrypoints=frozenset({("graphql", "MUTATION", "createOrder")}),
        edges=frozenset({("invokes", "service.create"), ("publishes", "channel.publish")}),
        messages=frozenset({("publishes", "orders", "created", "CreateOrderInput", "1")}),
    ),
)


@dataclass(frozen=True)
class StaticEvaluationResult:
    case_id: str
    stack: str
    expected: frozenset[tuple]
    observed: frozenset[tuple]

    @property
    def missing(self) -> frozenset[tuple]:
        return self.expected - self.observed

    @property
    def unexpected(self) -> frozenset[tuple]:
        return self.observed - self.expected

    @property
    def recall(self) -> float:
        return _ratio(len(self.expected & self.observed), len(self.expected), default=1.0)

    @property
    def precision(self) -> float:
        return _ratio(len(self.expected & self.observed), len(self.observed), default=1.0)


@dataclass(frozen=True)
class StaticEvaluationReport:
    results: tuple[StaticEvaluationResult, ...]
    elapsed_ms: float
    peak_memory_bytes: int

    @property
    def aggregate_recall(self) -> float:
        expected = _report_facts(self.results, "expected")
        observed = _report_facts(self.results, "observed")
        return _ratio(len(expected & observed), len(expected), default=1.0)

    @property
    def aggregate_precision(self) -> float:
        expected = _report_facts(self.results, "expected")
        observed = _report_facts(self.results, "observed")
        return _ratio(len(expected & observed), len(observed), default=1.0)

    @property
    def quality_by_stack(self) -> dict[str, dict[str, int | float]]:
        """Return independent fact-quality measurements for every supported stack."""
        results_by_stack: dict[str, list[StaticEvaluationResult]] = {}
        for result in self.results:
            results_by_stack.setdefault(result.stack, []).append(result)
        return {
            stack: _stack_quality(results)
            for stack, results in sorted(results_by_stack.items())
        }

    @property
    def passes_quality_gate(self) -> bool:
        """Require full precision and recall in every stack, not only in aggregate."""
        return all(
            metrics["precision"] == 1.0 and metrics["recall"] == 1.0
            for metrics in self.quality_by_stack.values()
        )

    def as_dict(self) -> dict[str, int | float | list[dict[str, object]]]:
        return {
            "cases": len(self.results),
            "precision": self.aggregate_precision,
            "recall": self.aggregate_recall,
            "stacks": self.quality_by_stack,
            "elapsed_ms": self.elapsed_ms,
            "peak_memory_bytes": self.peak_memory_bytes,
            "results": [
                {
                    "case_id": result.case_id,
                    "precision": result.precision,
                    "recall": result.recall,
                    "missing": sorted(map(repr, result.missing)),
                    "unexpected": sorted(map(repr, result.unexpected)),
                }
                for result in self.results
            ],
        }


def run_static_evaluation(cases: tuple[StaticGoldenCase, ...], work_dir: Path) -> StaticEvaluationReport:
    """Analyze each isolated source fixture and return fact-quality/resource metrics."""
    work_dir.mkdir(parents=True, exist_ok=True)
    tracemalloc.start()
    started = time.perf_counter()
    results = tuple(_run_case(case, work_dir / case.id) for case in cases)
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    _, peak_memory_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return StaticEvaluationReport(results, elapsed_ms, peak_memory_bytes)


def _run_case(case: StaticGoldenCase, root: Path) -> StaticEvaluationResult:
    for relative_path, source in case.files.items():
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    analysis = StaticAnalysisEngine().analyze(root, case.stack)
    expected = _expected_facts(case)
    return StaticEvaluationResult(case.id, case.stack, expected, _scored_facts(case, analysis))


def _expected_facts(case: StaticGoldenCase) -> frozenset[tuple]:
    return frozenset(
        {("entrypoint", *fact) for fact in case.entrypoints}
        | {("edge", *fact) for fact in case.edges}
        | {("message", *fact) for fact in case.messages}
        | {("contract", symbol, values) for symbol, values in case.contracts}
    )


def _scored_facts(case: StaticGoldenCase, analysis: AnalysisResult) -> frozenset[tuple]:
    facts: set[tuple] = set()
    if case.entrypoints:
        facts.update(("entrypoint", entry.kind, entry.method, entry.name) for entry in analysis.entrypoints)
    edge_targets = {target for _kind, target in case.edges}
    facts.update(("edge", edge.kind, edge.target) for edge in analysis.edges if edge.target in edge_targets)
    if case.messages:
        facts.update(
            ("message", item.direction, item.channel, item.routing_key, item.payload_type, item.message_version)
            for item in analysis.message_contracts
        )
    contract_symbols = {symbol for symbol, _values in case.contracts}
    for symbol in contract_symbols:
        values = analysis.contracts.get(symbol)
        if values is not None:
            normalized = tuple(sorted((key, str(value)) for key, value in values.items()))
            facts.add(("contract", symbol, normalized))
    return frozenset(facts)


def _ratio(numerator: int, denominator: int, default: float) -> float:
    return default if denominator == 0 else numerator / denominator


def _report_facts(results: tuple[StaticEvaluationResult, ...], attribute: str) -> set[tuple]:
    """Keep case identity while aggregating, so one case cannot satisfy another."""
    return {
        (result.case_id, *fact)
        for result in results
        for fact in getattr(result, attribute)
    }


def _stack_quality(results: list[StaticEvaluationResult]) -> dict[str, int | float]:
    expected = _report_facts(tuple(results), "expected")
    observed = _report_facts(tuple(results), "observed")
    matched = len(expected & observed)
    return {
        "cases": len(results),
        "precision": _ratio(matched, len(observed), default=1.0),
        "recall": _ratio(matched, len(expected), default=1.0),
    }
