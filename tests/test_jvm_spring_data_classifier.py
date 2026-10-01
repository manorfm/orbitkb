from pathlib import Path

from orbitkb.analysis.jvm_spring_data import SpringDataClassifier
from orbitkb.analysis.models import AnalysisResult, Evidence, FlowEdge, Injection


def test_spring_data_classifier_uses_local_repositories_for_derived_and_query_methods(tmp_path: Path):
    repository = tmp_path / "OrderRepository.java"
    repository.write_text(
        '''interface OrderRepository extends JpaRepository<Order, String> {
    Order findByStatus(String status);
    @Query("update Order o set o.archived = true") @Modifying
    int archiveExpired();
}
''', encoding="utf-8",
    )
    evidence = Evidence("Orders.java", 3, 3)
    analysis = AnalysisResult(
        edges=[
            FlowEdge("Orders.find", "repository.findByStatus", "invokes", evidence),
            FlowEdge("Orders.archive", "repository.archiveExpired", "invokes", evidence),
            FlowEdge("Unproven.find", "repository.findByStatus", "invokes", evidence),
        ],
        injections=[
            Injection("Orders.repository", "OrderRepository", None, evidence),
            Injection("Unproven.repository", "UnknownRepository", None, evidence),
        ],
    )

    SpringDataClassifier().classify(analysis, [repository])

    assert [(edge.kind, edge.boundary_kind) for edge in analysis.edges] == [
        ("reads", "persistence"),
        ("writes", "persistence"),
        ("invokes", None),
    ]
