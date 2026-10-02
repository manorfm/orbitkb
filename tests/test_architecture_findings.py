"""TDD coverage for generation/architecture.py — deterministic, whole-graph structural
findings computed purely from already-seeded db.repositories.* facts, no LLM."""
from pathlib import Path

from orbitkb.analysis.models import (
    AnalysisResult,
    EntryPoint,
    Evidence,
    FlowBoundary,
    FlowEdge,
    PersistenceFact,
)
from orbitkb.db.connection import open_db
from orbitkb.db.repositories import apis as apis_repo
from orbitkb.db.repositories import architecture as architecture_repo
from orbitkb.db.repositories import flows as flows_repo
from orbitkb.db.repositories import (
    kubernetes_configuration as kubernetes_configuration_repo,
)
from orbitkb.db.repositories import persistence as persistence_repo
from orbitkb.db.repositories import repositories as repositories_repo
from orbitkb.db.repositories import service_calls as service_calls_repo
from orbitkb.db.repositories import services as services_repo
from orbitkb.generation.architecture import (
    COMPONENT_FAN_THRESHOLD,
    diff_architecture_runs,
    find_aggregate_ownership_overlap,
    find_component_cycles,
    find_component_fan_imbalance,
    find_cycles,
    find_duplicate_external_integrations,
    find_fan_imbalance,
    find_flow_hypotheses,
    find_kubernetes_configuration_key_mismatches,
    find_kubernetes_configuration_source_import_unknowns,
    find_kubernetes_configuration_source_unknowns,
    find_message_consumers_without_recovery_policy,
    find_read_entrypoint_side_effects,
    find_shared_database,
    recompute_architecture_view,
)
from orbitkb.iac.models import (
    KubernetesConfigurationKeyMismatch,
    KubernetesConfigurationSourceImportUnknown,
    KubernetesConfigurationSourceUnknown,
)
from orbitkb.mcp import queries

EVIDENCE = [{"file": "main.py", "start_line": 1, "end_line": 5}]


def _call(conn, from_id, api_id, to_name, target_kind="internal"):
    service_calls_repo.replace_calls_for_api(
        conn, from_id, api_id,
        [{
            "to_service_name": to_name, "call_kind": "http", "reason": "r", "data_needed": [],
            "purpose_kind": "other", "confidence": 0.9, "target_kind": target_kind,
        }],
        EVIDENCE,
    )


def test_find_cycles_detects_a_two_service_cycle(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    a = services_repo.ensure_service(conn, "a-service", "/tmp/a", "python")
    b = services_repo.ensure_service(conn, "b-service", "/tmp/b", "python")
    a_api = apis_repo.upsert_api(conn, a, "GET", "/a", "s", "d", [], EVIDENCE)
    b_api = apis_repo.upsert_api(conn, b, "GET", "/b", "s", "d", [], EVIDENCE)
    _call(conn, a, a_api, "b-service")
    _call(conn, b, b_api, "a-service")
    service_calls_repo.reconcile_service_call_targets(conn)

    findings = find_cycles(conn)

    assert len(findings) == 1
    assert set(findings[0]["services"]) == {"a-service", "b-service"}
    assert findings[0]["kind"] == "cycle"


def test_kubernetes_configuration_key_mismatch_is_a_conservative_architecture_warning(tmp_path: Path):
    conn = open_db(tmp_path / "configuration-mismatch.db")
    repository_id = repositories_repo.ensure_repository(conn, "shop", "/tmp/shop")
    services_repo.ensure_service(conn, "orders", "/tmp/shop/orders", "node-ts", repository_id=repository_id)
    kubernetes_configuration_repo.replace_kubernetes_configuration_key_mismatches(conn, repository_id, [
        KubernetesConfigurationKeyMismatch(
            environment_key="ORDERS_TOPIC", source_kind="config_map", source_name="orders-config",
            source_key="orders-topic", reference_file_path="deploy/orders.yaml", reference_start_line=12,
            reference_end_line=17, declaration_file_path="deploy/config.yaml", declaration_start_line=1,
            declaration_end_line=7, matched_service_name="orders",
        ),
    ])

    assert find_kubernetes_configuration_key_mismatches(conn) == [{
        "kind": "possible_kubernetes_configuration_key_not_declared",
        "severity": "warning",
        "services": ["orders"],
        "reason": (
            "orders references ConfigMap orders-config key orders-topic for ORDERS_TOPIC, but its single indexed "
            "declaration does not list that key."
        ),
        "detail": {
            "environment_key": "ORDERS_TOPIC", "source_kind": "config_map", "source_name": "orders-config",
            "source_key": "orders-topic", "confidence": 0.9,
            "evidence": [
                {"file": "deploy/orders.yaml", "start_line": 12, "end_line": 17},
                {"file": "deploy/config.yaml", "start_line": 1, "end_line": 7},
            ],
            "unknowns": ["Kustomize, admission controllers, or runtime mutation may add the key outside indexed YAML."],
            "remediation": ["Confirm the source declaration and workload reference agree before rollout."],
        },
    }]
    recompute_architecture_view(conn)
    response = queries.find_architecture_smells(conn)
    assert response["findings"][-1]["kind"] == "possible_kubernetes_configuration_key_not_declared"


def test_kubernetes_configuration_source_unknown_is_a_low_confidence_architecture_insight(tmp_path: Path):
    conn = open_db(tmp_path / "configuration-source-unknown.db")
    repository_id = repositories_repo.ensure_repository(conn, "shop", "/tmp/shop")
    services_repo.ensure_service(conn, "orders", "/tmp/shop/orders", "node-ts", repository_id=repository_id)
    kubernetes_configuration_repo.replace_kubernetes_configuration_source_unknowns(conn, repository_id, [
        KubernetesConfigurationSourceUnknown(
            environment_key="ORDERS_TOPIC", source_kind="config_map", source_name="external-config",
            source_key="orders-topic", reference_file_path="deploy/orders.yaml", reference_start_line=12,
            reference_end_line=17, matched_service_name="orders",
        ),
    ])

    assert find_kubernetes_configuration_source_unknowns(conn) == [{
        "kind": "possible_kubernetes_configuration_source_not_declared_locally",
        "severity": "info",
        "services": ["orders"],
        "reason": (
            "orders references ConfigMap external-config key orders-topic for ORDERS_TOPIC, but no matching source "
            "declaration was indexed locally."
        ),
        "detail": {
            "environment_key": "ORDERS_TOPIC", "source_kind": "config_map", "source_name": "external-config",
            "source_key": "orders-topic", "confidence": 0.4,
            "evidence": [{"file": "deploy/orders.yaml", "start_line": 12, "end_line": 17}],
            "unknowns": [
                "The source may be managed by another repository, Helm chart, controller, or deployment process.",
            ],
            "remediation": ["Confirm which delivery boundary owns this ConfigMap or Secret before changing it."],
        },
    }]
    recompute_architecture_view(conn)
    response = queries.find_architecture_smells(conn)
    assert response["findings"][-1]["kind"] == "possible_kubernetes_configuration_source_not_declared_locally"


def test_kubernetes_env_from_source_unknown_is_a_low_confidence_architecture_insight(tmp_path: Path):
    conn = open_db(tmp_path / "configuration-source-import-unknown.db")
    repository_id = repositories_repo.ensure_repository(conn, "shop", "/tmp/shop")
    services_repo.ensure_service(conn, "orders", "/tmp/shop/orders", "node-ts", repository_id=repository_id)
    kubernetes_configuration_repo.replace_kubernetes_configuration_source_import_unknowns(conn, repository_id, [
        KubernetesConfigurationSourceImportUnknown(
            source_kind="config_map", source_name="external-config", prefix="ORDERS_",
            reference_file_path="deploy/orders.yaml", reference_start_line=12, reference_end_line=15,
            workload_kind="Deployment", workload_name="orders", container_name="migrate",
            container_role="initialization", optional=True, matched_service_name="orders",
        ),
        KubernetesConfigurationSourceImportUnknown(
            source_kind="config_map", source_name="external-config", prefix="PAYMENTS_",
            reference_file_path="deploy/orders.yaml", reference_start_line=20, reference_end_line=23,
            workload_kind="Deployment", workload_name="orders", container_name="migrate",
            container_role="initialization", optional=True, matched_service_name="orders",
        ),
        KubernetesConfigurationSourceImportUnknown(
            source_kind="config_map", source_name="external-config", prefix=None,
            reference_file_path="deploy/orders.yaml", reference_start_line=28, reference_end_line=30,
            workload_kind="Deployment", workload_name="orders", container_name="migrate",
            container_role="initialization", optional=True, matched_service_name="orders",
        ),
    ])

    assert find_kubernetes_configuration_source_import_unknowns(conn) == [{
        "kind": "possible_kubernetes_configuration_source_import_not_declared_locally",
        "severity": "info",
        "services": ["orders"],
        "reason": (
            "orders imports keys from ConfigMap external-config through envFrom, but no matching source declaration "
            "was indexed locally."
        ),
        "detail": {
            "source_kind": "config_map", "source_name": "external-config",
            "prefixes": ["ORDERS_", "PAYMENTS_"], "includes_unprefixed_import": True,
            "container_roles": ["initialization"], "availability": "optional", "confidence": 0.4,
            "workloads": [{
                "kind": "Deployment", "name": "orders", "container": "migrate",
                "container_role": "initialization", "prefixes": ["ORDERS_", "PAYMENTS_"],
                "includes_unprefixed_import": True,
                "evidence": [
                    {"file": "deploy/orders.yaml", "start_line": 12, "end_line": 15},
                    {"file": "deploy/orders.yaml", "start_line": 20, "end_line": 23},
                    {"file": "deploy/orders.yaml", "start_line": 28, "end_line": 30},
                ],
            }],
            "evidence": [
                {"file": "deploy/orders.yaml", "start_line": 12, "end_line": 15},
                {"file": "deploy/orders.yaml", "start_line": 20, "end_line": 23},
                {"file": "deploy/orders.yaml", "start_line": 28, "end_line": 30},
            ],
            "unknowns": [
                "The source may be managed by another repository, Helm chart, controller, or deployment process.",
                "envFrom does not expose its imported environment keys as static facts.",
                "The source is optional and may be absent at runtime.",
                "An initialization container using this source must complete before application containers start.",
            ],
            "remediation": [
                "Confirm which delivery boundary owns this ConfigMap or Secret before relying on its imported keys.",
                "Verify requested behavior remains safe when the optional ConfigMap external-config is unavailable.",
                "Verify initialization completes before application containers start.",
            ],
        },
    }]
    recompute_architecture_view(conn)
    response = queries.find_architecture_smells(conn)
    assert response["findings"][-1]["kind"] == "possible_kubernetes_configuration_source_import_not_declared_locally"


def test_kubernetes_env_from_source_unknown_orders_initialization_before_application(tmp_path: Path):
    conn = open_db(tmp_path / "configuration-source-import-unknown-mixed-roles.db")
    repository_id = repositories_repo.ensure_repository(conn, "shop", "/tmp/shop")
    services_repo.ensure_service(conn, "orders", "/tmp/shop/orders", "node-ts", repository_id=repository_id)
    kubernetes_configuration_repo.replace_kubernetes_configuration_source_import_unknowns(conn, repository_id, [
        KubernetesConfigurationSourceImportUnknown(
            source_kind="config_map", source_name="external-config", prefix=None,
            reference_file_path="deploy/orders.yaml", reference_start_line=20, reference_end_line=23,
            workload_kind="Deployment", workload_name="orders", container_name="api",
            container_role="application", matched_service_name="orders",
        ),
        KubernetesConfigurationSourceImportUnknown(
            source_kind="config_map", source_name="external-config", prefix=None,
            reference_file_path="deploy/orders.yaml", reference_start_line=12, reference_end_line=15,
            workload_kind="Deployment", workload_name="orders", container_name="migrate",
            container_role="initialization", matched_service_name="orders",
        ),
    ])

    detail = find_kubernetes_configuration_source_import_unknowns(conn)[0]["detail"]

    assert detail["container_roles"] == ["initialization", "application"]
    assert detail["workloads"] == [
        {
            "kind": "Deployment", "name": "orders", "container": "migrate", "container_role": "initialization",
            "includes_unprefixed_import": True,
            "evidence": [{"file": "deploy/orders.yaml", "start_line": 12, "end_line": 15}],
        },
        {
            "kind": "Deployment", "name": "orders", "container": "api", "container_role": "application",
            "includes_unprefixed_import": True,
            "evidence": [{"file": "deploy/orders.yaml", "start_line": 20, "end_line": 23}],
        },
    ]
    assert detail["unknowns"].count(
        "An initialization container using this source must complete before application containers start.",
    ) == 1
    assert detail["remediation"].count(
        "Verify initialization completes before application containers start.",
    ) == 1


def test_kubernetes_env_from_source_with_mixed_availability_stays_conservative(tmp_path: Path):
    conn = open_db(tmp_path / "configuration-source-import-mixed-availability.db")
    repository_id = repositories_repo.ensure_repository(conn, "shop", "/tmp/shop")
    services_repo.ensure_service(conn, "orders", "/tmp/shop/orders", "node-ts", repository_id=repository_id)
    kubernetes_configuration_repo.replace_kubernetes_configuration_source_import_unknowns(conn, repository_id, [
        KubernetesConfigurationSourceImportUnknown(
            source_kind="secret", source_name="external-secrets", prefix=None,
            reference_file_path="deploy/orders.yaml", reference_start_line=12, reference_end_line=15,
            optional=True, matched_service_name="orders",
        ),
        KubernetesConfigurationSourceImportUnknown(
            source_kind="secret", source_name="external-secrets", prefix="PAYMENTS_",
            reference_file_path="deploy/orders.yaml", reference_start_line=20, reference_end_line=23,
            optional=False, matched_service_name="orders",
        ),
    ])

    finding = find_kubernetes_configuration_source_import_unknowns(conn)[0]

    assert finding["detail"]["availability"] == "mixed"
    assert finding["detail"]["includes_unprefixed_import"] is True
    assert "workloads" not in finding["detail"]
    assert finding["detail"]["remediation"][-1] == (
        "Confirm source availability before relying on imported configuration during rollout."
    )


def test_find_cycles_ignores_a_simple_chain(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    a = services_repo.ensure_service(conn, "a-service", "/tmp/a", "python")
    b = services_repo.ensure_service(conn, "b-service", "/tmp/b", "python")
    services_repo.ensure_service(conn, "c-service", "/tmp/c", "python")
    a_api = apis_repo.upsert_api(conn, a, "GET", "/a", "s", "d", [], EVIDENCE)
    b_api = apis_repo.upsert_api(conn, b, "GET", "/b", "s", "d", [], EVIDENCE)
    _call(conn, a, a_api, "b-service")
    _call(conn, b, b_api, "c-service")
    service_calls_repo.reconcile_service_call_targets(conn)

    assert find_cycles(conn) == []


def test_find_fan_imbalance_flags_high_fan_in(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    services_repo.ensure_service(conn, "hub-service", "/tmp/hub", "python")
    for i in range(4):
        caller = services_repo.ensure_service(conn, f"caller-{i}-service", f"/tmp/c{i}", "python")
        api_id = apis_repo.upsert_api(conn, caller, "GET", "/x", "s", "d", [], EVIDENCE)
        _call(conn, caller, api_id, "hub-service")
    service_calls_repo.reconcile_service_call_targets(conn)

    findings = find_fan_imbalance(conn)

    fan_in = [f for f in findings if f["kind"] == "fan_in"]
    assert len(fan_in) == 1
    assert fan_in[0]["services"] == ["hub-service"]
    assert fan_in[0]["detail"]["count"] == 4


def test_find_fan_imbalance_does_not_flag_below_threshold(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    a = services_repo.ensure_service(conn, "a-service", "/tmp/a", "python")
    services_repo.ensure_service(conn, "b-service", "/tmp/b", "python")
    a_api = apis_repo.upsert_api(conn, a, "GET", "/a", "s", "d", [], EVIDENCE)
    _call(conn, a, a_api, "b-service")
    service_calls_repo.reconcile_service_call_targets(conn)

    assert find_fan_imbalance(conn) == []


def test_find_component_cycles_detects_a_cycle_within_one_service(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    service_id = services_repo.ensure_service(conn, "checkout", "/tmp/checkout", "python")
    evidence = Evidence("checkout.py", 10, 10)
    flows_repo.replace_analysis(
        conn, service_id,
        AnalysisResult(edges=[
            FlowEdge("A.run", "B.process", "invokes", evidence),
            FlowEdge("B.process", "C.finish", "invokes", evidence),
            FlowEdge("C.finish", "A.run", "invokes", evidence),
        ]),
    )

    findings = find_component_cycles(conn)

    assert len(findings) == 1
    assert findings[0]["kind"] == "component_cycle"
    assert findings[0]["services"] == ["checkout"]
    assert set(findings[0]["detail"]["components"]) == {"A", "B", "C"}


def test_find_component_cycles_ignores_a_simple_chain_within_one_service(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    service_id = services_repo.ensure_service(conn, "checkout", "/tmp/checkout", "python")
    evidence = Evidence("checkout.py", 10, 10)
    flows_repo.replace_analysis(
        conn, service_id,
        AnalysisResult(edges=[
            FlowEdge("A.run", "B.process", "invokes", evidence),
            FlowEdge("B.process", "C.finish", "invokes", evidence),
        ]),
    )

    assert find_component_cycles(conn) == []


def test_find_component_cycles_ignores_cross_service_edges(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    a = services_repo.ensure_service(conn, "checkout", "/tmp/checkout", "python")
    b = services_repo.ensure_service(conn, "billing", "/tmp/billing", "python")
    evidence = Evidence("checkout.py", 10, 10)
    flows_repo.replace_analysis(conn, a, AnalysisResult(edges=[
        FlowEdge("A.run", "B.process", "invokes", evidence),
    ]))
    flows_repo.replace_analysis(conn, b, AnalysisResult(edges=[
        FlowEdge("B.process", "A.run", "invokes", evidence),
    ]))

    assert find_component_cycles(conn) == []


def test_find_component_fan_imbalance_flags_high_fan_out(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    service_id = services_repo.ensure_service(conn, "checkout", "/tmp/checkout", "python")
    evidence = Evidence("checkout.py", 10, 10)
    edges = [FlowEdge("Hub.run", f"Leaf{i}.handle", "invokes", evidence) for i in range(COMPONENT_FAN_THRESHOLD)]
    edges += [FlowEdge(f"Leaf{i}.handle", f"Sink{i}.absorb", "invokes", evidence) for i in range(COMPONENT_FAN_THRESHOLD)]
    flows_repo.replace_analysis(conn, service_id, AnalysisResult(edges=edges))

    findings = find_component_fan_imbalance(conn)

    fan_out = [f for f in findings if f["kind"] == "component_fan_out"]
    assert len(fan_out) == 1
    assert fan_out[0]["services"] == ["checkout"]
    assert fan_out[0]["detail"] == {
        "component": "Hub", "count": COMPONENT_FAN_THRESHOLD, "instability": 1.0,
    }


def test_find_component_fan_imbalance_flags_high_fan_in(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    service_id = services_repo.ensure_service(conn, "checkout", "/tmp/checkout", "python")
    evidence = Evidence("checkout.py", 10, 10)
    edges = [FlowEdge(f"Caller{i}.run", "Hub.handle", "invokes", evidence) for i in range(COMPONENT_FAN_THRESHOLD)]
    edges.append(FlowEdge("Hub.handle", "Sink.absorb", "invokes", evidence))
    flows_repo.replace_analysis(conn, service_id, AnalysisResult(edges=edges))

    findings = find_component_fan_imbalance(conn)

    fan_in = [f for f in findings if f["kind"] == "component_fan_in"]
    assert len(fan_in) == 1
    assert fan_in[0]["services"] == ["checkout"]
    assert fan_in[0]["detail"] == {
        "component": "Hub", "count": COMPONENT_FAN_THRESHOLD, "instability": 0.0,
    }


def test_find_component_fan_imbalance_does_not_flag_below_threshold(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    service_id = services_repo.ensure_service(conn, "checkout", "/tmp/checkout", "python")
    evidence = Evidence("checkout.py", 10, 10)
    below = COMPONENT_FAN_THRESHOLD - 1
    edges = [FlowEdge("Hub.run", f"Leaf{i}.handle", "invokes", evidence) for i in range(below)]
    edges += [FlowEdge(f"Leaf{i}.handle", f"Sink{i}.absorb", "invokes", evidence) for i in range(below)]
    flows_repo.replace_analysis(conn, service_id, AnalysisResult(edges=edges))

    assert find_component_fan_imbalance(conn) == []


def test_find_component_fan_imbalance_ignores_boundary_edges(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    service_id = services_repo.ensure_service(conn, "checkout", "/tmp/checkout", "python")
    evidence = Evidence("checkout.py", 10, 10)
    edges = [FlowEdge("Hub.run", f"Table{i}.save", "writes", evidence) for i in range(COMPONENT_FAN_THRESHOLD)]
    flows_repo.replace_analysis(conn, service_id, AnalysisResult(edges=edges))

    assert find_component_fan_imbalance(conn) == []


def test_component_cycle_and_fan_imbalance_are_exposed_via_find_architecture_smells(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    service_id = services_repo.ensure_service(conn, "checkout", "/tmp/checkout", "python")
    evidence = Evidence("checkout.py", 10, 10)
    edges = [
        FlowEdge("A.run", "B.process", "invokes", evidence),
        FlowEdge("B.process", "A.run", "invokes", evidence),
    ]
    edges += [FlowEdge("Hub.run", f"Leaf{i}.handle", "invokes", evidence) for i in range(COMPONENT_FAN_THRESHOLD)]
    edges += [FlowEdge(f"Leaf{i}.handle", f"Sink{i}.absorb", "invokes", evidence) for i in range(COMPONENT_FAN_THRESHOLD)]
    flows_repo.replace_analysis(conn, service_id, AnalysisResult(edges=edges))

    recompute_architecture_view(conn)
    response = queries.find_architecture_smells(conn)

    kinds = {item["kind"] for item in response["findings"]}
    assert "component_cycle" in kinds
    assert "component_fan_out" in kinds
    cycle = next(item for item in response["findings"] if item["kind"] == "component_cycle")
    assert cycle["services"] == ["checkout"]


def test_find_shared_database_flags_same_entity_on_same_engine(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    a = services_repo.ensure_service(conn, "a-service", "/tmp/a", "python")
    b = services_repo.ensure_service(conn, "b-service", "/tmp/b", "python")
    persistence_repo.replace_persistence_entities(
        conn, a, [{"name": "orders", "kind": "sql_table", "engine": "postgres", "schema_json": []}], EVIDENCE,
    )
    persistence_repo.replace_persistence_entities(
        conn, b, [{"name": "orders", "kind": "sql_table", "engine": "postgres", "schema_json": []}], EVIDENCE,
    )

    findings = find_shared_database(conn)

    assert len(findings) == 1
    assert set(findings[0]["services"]) == {"a-service", "b-service"}
    assert findings[0]["detail"]["entity"] == "orders"


def test_find_shared_database_ignores_unknown_engine(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    a = services_repo.ensure_service(conn, "a-service", "/tmp/a", "python")
    b = services_repo.ensure_service(conn, "b-service", "/tmp/b", "python")
    persistence_repo.replace_persistence_entities(
        conn, a, [{"name": "orders", "kind": "sql_table", "schema_json": []}], EVIDENCE,
    )
    persistence_repo.replace_persistence_entities(
        conn, b, [{"name": "orders", "kind": "sql_table", "schema_json": []}], EVIDENCE,
    )

    assert find_shared_database(conn) == []


def test_find_duplicate_external_integrations(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    a = services_repo.ensure_service(conn, "a-service", "/tmp/a", "python")
    b = services_repo.ensure_service(conn, "b-service", "/tmp/b", "python")
    a_api = apis_repo.upsert_api(conn, a, "GET", "/a", "s", "d", [], EVIDENCE)
    b_api = apis_repo.upsert_api(conn, b, "GET", "/b", "s", "d", [], EVIDENCE)
    _call(conn, a, a_api, "Stripe API", target_kind="external")
    _call(conn, b, b_api, "Stripe API", target_kind="external")

    findings = find_duplicate_external_integrations(conn)

    assert len(findings) == 1
    assert set(findings[0]["services"]) == {"a-service", "b-service"}
    assert findings[0]["detail"]["vendor"] == "Stripe API"


def test_flow_hypotheses_report_graphql_policy_leakage_and_non_atomic_publish_with_evidence(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    service_id = services_repo.ensure_service(conn, "checkout-bff", "/tmp/checkout", "node-ts")
    evidence = Evidence("resolvers.ts", 10, 10)
    flows_repo.replace_analysis(
        conn,
        service_id,
        AnalysisResult(
            entrypoints=[EntryPoint("graphql", "MUTATION", "checkout", "Mutation.checkout", evidence)],
            edges=[
                FlowEdge("Mutation.checkout", "ordersRepository.save", "writes", evidence),
                FlowEdge("Mutation.checkout", "events.publish", "publishes", Evidence("resolvers.ts", 11, 11)),
            ],
        ),
    )

    findings = find_flow_hypotheses(conn)

    bff = next(finding for finding in findings if finding["kind"] == "possible_bff_domain_leakage")
    non_atomic = next(finding for finding in findings if finding["kind"] == "possible_non_atomic_publish")
    assert bff["services"] == ["checkout-bff"]
    assert bff["detail"]["confidence"] == 0.6
    assert bff["detail"]["evidence"] == [
        {"file": "resolvers.ts", "start_line": 10, "end_line": 10},
        {"file": "resolvers.ts", "start_line": 11, "end_line": 11},
    ]
    assert bff["detail"]["unknowns"]
    assert non_atomic["detail"]["confidence"] == 0.5
    assert len(non_atomic["detail"]["evidence"]) == 2

    recompute_architecture_view(conn)
    response = queries.find_architecture_smells(conn)
    exposed = next(item for item in response["findings"] if item["kind"] == "possible_bff_domain_leakage")
    assert exposed["confidence"] == 0.6
    assert exposed["evidence"] == bff["detail"]["evidence"]
    assert exposed["unknowns"] == bff["detail"]["unknowns"]


def test_architecture_findings_qualify_duplicate_service_names_by_repository(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    sales_repo = repositories_repo.ensure_repository(conn, "sales", "/tmp/sales")
    support_repo = repositories_repo.ensure_repository(conn, "support", "/tmp/support")
    sales_orders = services_repo.ensure_service(conn, "orders", "/tmp/sales/orders", "node-ts", sales_repo)
    support_orders = services_repo.ensure_service(conn, "orders", "/tmp/support/orders", "node-ts", support_repo)
    evidence = Evidence("resolver.ts", 8, 8)
    for service_id, symbol in ((sales_orders, "Mutation.createOrder"), (support_orders, "Mutation.createTicket")):
        flows_repo.replace_analysis(
            conn,
            service_id,
            AnalysisResult(
                entrypoints=[EntryPoint("graphql", "MUTATION", symbol, symbol, evidence)],
                edges=[FlowEdge(symbol, "repository.save", "writes", evidence)],
            ),
        )

    findings = find_flow_hypotheses(conn)

    assert {finding["services"][0] for finding in findings} == {"sales/orders", "support/orders"}


def test_architecture_trend_keeps_flow_hypotheses_for_each_entrypoint(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    service_id = services_repo.ensure_service(conn, "checkout", "/tmp/checkout", "node-ts")
    evidence = Evidence("resolver.ts", 8, 8)
    first = EntryPoint("graphql", "MUTATION", "checkout", "Mutation.checkout", evidence)
    flows_repo.replace_analysis(
        conn, service_id,
        AnalysisResult(entrypoints=[first], edges=[FlowEdge(first.symbol, "repository.save", "writes", evidence)]),
    )
    run_1 = recompute_architecture_view(conn)
    second = EntryPoint("graphql", "MUTATION", "cancelCheckout", "Mutation.cancelCheckout", Evidence("resolver.ts", 20, 20))
    flows_repo.replace_analysis(
        conn, service_id,
        AnalysisResult(
            entrypoints=[first, second],
            edges=[
                FlowEdge(first.symbol, "repository.save", "writes", evidence),
                FlowEdge(second.symbol, "repository.cancel", "writes", second.evidence),
            ],
        ),
    )

    diff = diff_architecture_runs(conn, run_1, recompute_architecture_view(conn))

    assert [(finding["kind"], finding["detail"]["entrypoint"]["symbol"]) for finding in diff["new_findings"]] == [
        ("possible_bff_domain_leakage", "Mutation.cancelCheckout")
    ]


def test_non_atomic_publish_hypothesis_is_suppressed_when_a_transaction_boundary_exists(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    service_id = services_repo.ensure_service(conn, "orders", "/tmp/orders", "jvm-spring")
    evidence = Evidence("OrdersController.java", 10, 10)
    flows_repo.replace_analysis(
        conn,
        service_id,
        AnalysisResult(
            entrypoints=[EntryPoint("http", "POST", "/orders", "Orders.create", evidence)],
            edges=[
                FlowEdge("Orders.create", "repository.save", "writes", evidence),
                FlowEdge("Orders.create", "publisher.publish", "publishes", Evidence("OrdersController.java", 11, 11)),
            ],
            boundaries=[FlowBoundary("Orders.create", "transaction", evidence)],
        ),
    )

    assert find_flow_hypotheses(conn) == []


def test_read_entrypoint_side_effects_flag_get_and_graphql_query_with_remediation(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    service_id = services_repo.ensure_service(conn, "catalog", "/tmp/catalog", "node-ts")
    get_evidence = Evidence("CatalogController.java", 14, 14)
    query_evidence = Evidence("resolvers.ts", 27, 27)
    flows_repo.replace_analysis(
        conn,
        service_id,
        AnalysisResult(
            entrypoints=[
                EntryPoint("http", "GET", "/catalog/refresh", "Catalog.refresh", get_evidence),
                EntryPoint("graphql", "QUERY", "catalog", "Query.catalog", query_evidence),
                EntryPoint("http", "POST", "/catalog", "Catalog.create", Evidence("CatalogController.java", 40, 40)),
            ],
            edges=[
                FlowEdge("Catalog.refresh", "repository.save", "writes", get_evidence),
                FlowEdge("Query.catalog", "events.publish", "publishes", query_evidence),
                FlowEdge("Catalog.create", "repository.save", "writes", Evidence("CatalogController.java", 41, 41)),
            ],
        ),
    )

    findings = find_read_entrypoint_side_effects(conn)

    assert [finding["detail"]["entrypoint"]["symbol"] for finding in findings] == [
        "Catalog.refresh", "Query.catalog",
    ]
    assert all(finding["kind"] == "possible_read_entrypoint_side_effect" for finding in findings)
    assert all(finding["detail"]["confidence"] == 0.8 for finding in findings)
    assert findings[0]["detail"]["evidence"] == [
        {"file": "CatalogController.java", "start_line": 14, "end_line": 14},
    ]
    assert findings[0]["detail"]["remediation"] == [
        "Move externally observable writes or publications behind a command entrypoint, or document the exception.",
    ]

    recompute_architecture_view(conn)
    response = queries.find_architecture_smells(conn)
    exposed = next(item for item in response["findings"] if item["kind"] == "possible_read_entrypoint_side_effect")
    assert exposed["remediation"] == findings[0]["detail"]["remediation"]


def test_aggregate_ownership_overlap_preserves_each_declared_owner_and_evidence(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    orders = services_repo.ensure_service(conn, "orders", "/tmp/orders", "jvm-spring")
    fulfillment = services_repo.ensure_service(conn, "fulfillment", "/tmp/fulfillment", "go")
    orders_evidence = Evidence("Order.java", 8, 15)
    fulfillment_evidence = Evidence("shipment.go", 5, 11)
    flows_repo.replace_analysis(
        conn,
        orders,
        AnalysisResult(persistence_facts=[PersistenceFact("orders", "sql_table", "Order", orders_evidence)]),
    )
    flows_repo.replace_analysis(
        conn,
        fulfillment,
        AnalysisResult(persistence_facts=[PersistenceFact("orders", "sql_table", "ShipmentOrder", fulfillment_evidence)]),
    )

    findings = find_aggregate_ownership_overlap(conn)

    assert len(findings) == 1
    finding = findings[0]
    assert finding["kind"] == "possible_aggregate_ownership_overlap"
    assert finding["services"] == ["fulfillment", "orders"]
    assert finding["detail"]["aggregate"] == "orders"
    assert finding["detail"]["owners"] == [
        {"service": "fulfillment", "owner": "ShipmentOrder"},
        {"service": "orders", "owner": "Order"},
    ]
    assert finding["detail"]["evidence"] == [
        {"file": "shipment.go", "start_line": 5, "end_line": 11},
        {"file": "Order.java", "start_line": 8, "end_line": 15},
    ]
    assert finding["detail"]["confidence"] == 0.65
    assert finding["detail"]["unknowns"]

    recompute_architecture_view(conn)
    response = queries.find_architecture_smells(conn)
    exposed = next(item for item in response["findings"] if item["kind"] == finding["kind"])
    assert exposed["remediation"] == finding["detail"]["remediation"]


def test_aggregate_ownership_does_not_equate_logical_mongoose_model_names(tmp_path: Path):
    conn = open_db(tmp_path / "models.db")
    first = services_repo.ensure_service(conn, "orders", "/tmp/orders", "node-js")
    second = services_repo.ensure_service(conn, "fulfillment", "/tmp/fulfillment", "node-js")
    model = PersistenceFact("Order", "mongoose_model", "Order", Evidence("model.js", 2, 2))
    flows_repo.replace_analysis(conn, first, AnalysisResult(persistence_facts=[model]))
    flows_repo.replace_analysis(conn, second, AnalysisResult(persistence_facts=[model]))

    assert find_aggregate_ownership_overlap(conn) == []


def test_message_consumer_without_source_proven_recovery_policy_is_a_hypothesis(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    service_id = services_repo.ensure_service(conn, "billing", "/tmp/billing", "node-ts")
    weak_evidence = Evidence("billing-consumer.ts", 12, 15)
    protected_evidence = Evidence("ledger-consumer.ts", 20, 23)
    weak_consumer = EntryPoint("message", "CONSUME", "billing.created", "message.consume:billing.created", weak_evidence)
    protected_consumer = EntryPoint(
        "message", "CONSUME", "ledger.created", "message.consume:ledger.created", protected_evidence,
    )
    flows_repo.replace_analysis(
        conn,
        service_id,
        AnalysisResult(
            entrypoints=[weak_consumer, protected_consumer],
            contracts={
                weak_consumer.symbol: {
                    "transport": "rabbitmq", "direction": "consumes", "queue": "billing.created",
                },
                protected_consumer.symbol: {
                    "transport": "rabbitmq", "direction": "consumes", "queue": "ledger.created",
                    "dead_letter_routing_key": "ledger.dlq",
                },
            },
        ),
    )

    findings = find_message_consumers_without_recovery_policy(conn)

    assert len(findings) == 1
    finding = findings[0]
    assert finding["kind"] == "possible_message_consumer_without_recovery_policy"
    assert finding["services"] == ["billing"]
    assert finding["detail"]["consumer"] == {
        "queue": "billing.created", "symbol": "message.consume:billing.created",
    }
    assert finding["detail"]["confidence"] == 0.45
    assert finding["detail"]["evidence"] == [
        {"file": "billing-consumer.ts", "start_line": 12, "end_line": 15},
    ]
    assert finding["detail"]["source_proven"] == {
        "dead_letter_routing_key": None, "retry_boundary": False, "retry_delay_ms": None,
    }
    assert finding["detail"]["unknowns"]

    recompute_architecture_view(conn)
    response = queries.find_architecture_smells(conn)
    exposed = next(item for item in response["findings"] if item["kind"] == finding["kind"])
    assert exposed["remediation"] == finding["detail"]["remediation"]


def test_recompute_architecture_view_persists_a_new_run_with_findings(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    a = services_repo.ensure_service(conn, "a-service", "/tmp/a", "python")
    b = services_repo.ensure_service(conn, "b-service", "/tmp/b", "python")
    a_api = apis_repo.upsert_api(conn, a, "GET", "/a", "s", "d", [], EVIDENCE)
    b_api = apis_repo.upsert_api(conn, b, "GET", "/b", "s", "d", [], EVIDENCE)
    _call(conn, a, a_api, "b-service")
    _call(conn, b, b_api, "a-service")
    service_calls_repo.reconcile_service_call_targets(conn)

    run_id = recompute_architecture_view(conn)

    findings = architecture_repo.list_findings(conn, run_id)
    assert any(f["kind"] == "cycle" for f in findings)


def test_recompute_architecture_view_creates_a_fresh_run_each_time(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    services_repo.ensure_service(conn, "a-service", "/tmp/a", "python")

    run_1 = recompute_architecture_view(conn)
    run_2 = recompute_architecture_view(conn)

    assert run_1 != run_2
    assert architecture_repo.latest_run_id(conn) == run_2


def test_diff_architecture_runs_reports_a_new_finding(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    a = services_repo.ensure_service(conn, "a-service", "/tmp/a", "python")
    services_repo.ensure_service(conn, "b-service", "/tmp/b", "python")
    a_api = apis_repo.upsert_api(conn, a, "GET", "/a", "s", "d", [], EVIDENCE)
    run_1 = recompute_architecture_view(conn)  # no edges yet: no findings

    _call(conn, a, a_api, "b-service")
    service_calls_repo.reconcile_service_call_targets(conn)
    b = services_repo.get_service_by_name(conn, "b-service")["id"]
    b_api = apis_repo.upsert_api(conn, b, "GET", "/b", "s", "d", [], EVIDENCE)
    _call(conn, b, b_api, "a-service")
    service_calls_repo.reconcile_service_call_targets(conn)
    run_2 = recompute_architecture_view(conn)  # cycle now exists

    diff = diff_architecture_runs(conn, run_1, run_2)

    assert len(diff["new_findings"]) == 1
    assert diff["new_findings"][0]["kind"] == "cycle"
    assert diff["resolved_findings"] == []
    assert diff["count_deltas"] == []


def test_diff_architecture_runs_reports_a_resolved_finding(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    a = services_repo.ensure_service(conn, "a-service", "/tmp/a", "python")
    b = services_repo.ensure_service(conn, "b-service", "/tmp/b", "python")
    a_api = apis_repo.upsert_api(conn, a, "GET", "/a", "s", "d", [], EVIDENCE)
    b_api = apis_repo.upsert_api(conn, b, "GET", "/b", "s", "d", [], EVIDENCE)
    _call(conn, a, a_api, "b-service")
    _call(conn, b, b_api, "a-service")
    service_calls_repo.reconcile_service_call_targets(conn)
    run_1 = recompute_architecture_view(conn)  # cycle exists

    service_calls_repo.replace_calls_for_api(conn, b, b_api, [], EVIDENCE)  # break the cycle
    service_calls_repo.reconcile_service_call_targets(conn)
    run_2 = recompute_architecture_view(conn)

    diff = diff_architecture_runs(conn, run_1, run_2)

    assert diff["new_findings"] == []
    assert len(diff["resolved_findings"]) == 1
    assert diff["resolved_findings"][0]["kind"] == "cycle"


def test_diff_architecture_runs_reports_a_fan_in_count_delta(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    services_repo.ensure_service(conn, "hub-service", "/tmp/hub", "python")
    for i in range(4):
        caller = services_repo.ensure_service(conn, f"caller-{i}-service", f"/tmp/c{i}", "python")
        api_id = apis_repo.upsert_api(conn, caller, "GET", "/x", "s", "d", [], EVIDENCE)
        _call(conn, caller, api_id, "hub-service")
    service_calls_repo.reconcile_service_call_targets(conn)
    run_1 = recompute_architecture_view(conn)  # fan_in count = 4

    caller_5 = services_repo.ensure_service(conn, "caller-5-service", "/tmp/c5", "python")
    api_5 = apis_repo.upsert_api(conn, caller_5, "GET", "/x", "s", "d", [], EVIDENCE)
    _call(conn, caller_5, api_5, "hub-service")
    service_calls_repo.reconcile_service_call_targets(conn)
    run_2 = recompute_architecture_view(conn)  # fan_in count = 5

    diff = diff_architecture_runs(conn, run_1, run_2)

    assert diff["new_findings"] == []
    assert diff["resolved_findings"] == []
    fan_in_delta = next(d for d in diff["count_deltas"] if d["kind"] == "fan_in")
    assert fan_in_delta["previous_count"] == 4
    assert fan_in_delta["current_count"] == 5
    assert fan_in_delta["services"] == ["hub-service"]


def test_diff_architecture_runs_is_empty_when_nothing_changed(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    a = services_repo.ensure_service(conn, "a-service", "/tmp/a", "python")
    b = services_repo.ensure_service(conn, "b-service", "/tmp/b", "python")
    a_api = apis_repo.upsert_api(conn, a, "GET", "/a", "s", "d", [], EVIDENCE)
    b_api = apis_repo.upsert_api(conn, b, "GET", "/b", "s", "d", [], EVIDENCE)
    _call(conn, a, a_api, "b-service")
    _call(conn, b, b_api, "a-service")
    service_calls_repo.reconcile_service_call_targets(conn)
    run_1 = recompute_architecture_view(conn)
    run_2 = recompute_architecture_view(conn)  # nothing changed on disk

    diff = diff_architecture_runs(conn, run_1, run_2)

    assert diff == {"new_findings": [], "resolved_findings": [], "count_deltas": []}
